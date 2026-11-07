"""``Board``: a station's TeensyROM, open for a whole run.

It owns the rig lock, the serial link, the event log and the recovery
ladder, and it is what the pytest fixtures hand to tests. The ladder, run
before every test, climbs only as far as it must: a reset; then the 134-baud
USB reboot with a loader relaunch of the firmware already on the board, when
a flasher is attached (step 4 of the plan); then a mains cycle when the rig
has power control; else the station is marked wedged and the rest of the run
skips with that reason.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from treadmill.errors import TreadmillError
from treadmill.link import Link, LinkError, Refused, TransportError, find_port, open_serial
from treadmill.link.banner import Banner
from treadmill.link.state import MENU_READY, State, derive_state
from treadmill.link.unsolicited import EventLog
from treadmill.station.lock import RigLock
from treadmill.station.model import Station
from treadmill.station.power import Power, PowerUnavailable, make_power

BOOT_POLL_S = 0.5
C128_RESET_HOLD_S = 0.25        # no DMA within ~80 ms of a C128 reset release (Constraints.md); margin on top



class Wedged(TreadmillError):
    """The board stopped answering and nothing the station has could bring it back."""


@dataclass
class Board:
    station: Station
    link: Link
    events: EventLog
    lock: RigLock | None = None
    power: Power = field(default_factory=lambda: make_power(None))
    banner: Banner | None = None
    image: str | None = None
    wedged_reason: str | None = None
    # Step 4 attaches these: reboot the Teensy without reflashing, then reconnect.
    reboot_hook: Callable[[], None] | None = None
    reconnect_hook: Callable[[], Link] | None = None
    recoveries: list[str] = field(default_factory=list)
    # The DMA policy (plan 2.2): no DMA for a moment after a C128 reset, none while a test says the REU is live.
    dma_hold_until: float = 0.0
    reu_live: bool = False
    # What every probe folds into the station's capabilities: the firmware's defines and the run's --caps-mask.
    fw_defines: tuple[str, ...] = ()
    caps_mask: frozenset[str] = frozenset()
    probe_pending: bool = False          # a reconnect happened; whatever answers next is probed when the menu is up

    # -- opening ----------------------------------------------------------

    @classmethod
    def open(cls, station: Station, *, lock_wait: float = 5.0, attended: bool = False,
             link_factory: Callable[[], Link] | None = None, fw_defines: tuple[str, ...] = (),
             caps_mask: set[str] | frozenset[str] = frozenset()) -> "Board":
        """Lock the rig, find the board by chip id, open it and probe it.
        ``link_factory`` replaces discovery and the serial port (the self-tests
        hand in a link to a fake board). Nothing stays held when this fails:
        the lock and the port are released before the error leaves."""
        lock = RigLock(station.rig.id, wait=lock_wait).acquire()
        events = EventLog()
        try:
            if link_factory is not None:
                link = link_factory()
                link._on_unsolicited = events.feed
            else:
                found = find_port(station.tr.uid, station.tr.port_hint)
                link = Link(open_serial(found.port), on_unsolicited=events.feed)
        except Exception:
            lock.release()
            raise
        board = cls(station=station, link=link, events=events, lock=lock,
                    power=make_power(station.rig.power, attended),
                    fw_defines=tuple(fw_defines), caps_mask=frozenset(caps_mask))
        try:
            board.probe()
        except BaseException:
            board.close()
            raise
        return board

    def probe(self) -> None:
        """Which image and board answer, and the station's capabilities from
        that, with the firmware's defines and the run's mask folded in: a
        re-probe after a reconnect lands where the first probe did."""
        probe = self.link.probe()
        self.image = probe.image
        self.banner = probe.banner
        self.probe_pending = False
        kind = probe.banner.kind if probe.banner else None
        if kind:
            self.station = self.station.with_probe(kind, fw_defines=self.fw_defines).masked(self.caps_mask)

    def mask(self, caps: set[str] | frozenset[str]) -> None:
        """Take capabilities away for the rest of the run (--caps-mask, a
        capture that did not open); a later probe keeps them away."""
        self.caps_mask = self.caps_mask | frozenset(caps)
        self.station = self.station.masked(self.caps_mask)

    def close(self) -> None:
        try:
            self.link.close()
        finally:
            if self.lock:
                self.lock.release()

    # -- DMA policy ----------------------------------------------------------

    def dma_allowed(self) -> bool:
        """DMA memory access is allowed: the link probed a TR+ main image,
        the station still has the capability (a --caps-mask takes it away to
        prove the plain-TR tier), the C128's post-reset hold-off has passed,
        and no test declared the REU live."""
        return (self.link.dma_allowed and self.station.has("dma") and not self.reu_live
                and time.monotonic() >= self.dma_hold_until)

    def reset(self) -> None:
        """Reset the C64 and start the hold-off a C128 needs before any DMA."""
        self.link.reset()
        if self.is_c128:
            self.dma_hold_until = time.monotonic() + C128_RESET_HOLD_S

    @property
    def is_c128(self) -> bool:
        """The machine is a C128 by the station file or by the board's own
        machine line, whichever says so: the hold-off must not depend on a
        station file naming the right machine."""
        return self.station.machine.is_c128 or (self.banner is not None and self.banner.machine == "C128")

    # -- state and recovery ----------------------------------------------------

    def state(self) -> State:
        return derive_state(self.link, self.events.current_handler())

    def wait_for_menu(self, timeout: float | None = None) -> None:
        """Until the main image reports ``Boot: complete`` and answers a ping,
        reconnecting through the hook if the port drops (a reset from the
        minimal image restarts the board)."""
        timeout = self.station.machine.boot_timeout_s if timeout is None else timeout
        deadline = time.monotonic() + timeout
        last = "no answer"
        while time.monotonic() < deadline:
            try:
                st = self.state()
            except (LinkError, TransportError) as e:
                last = str(e)
                if self.link.stream.ended and self.reconnect_hook:
                    self._reconnect()
                time.sleep(BOOT_POLL_S)
                continue
            if st.kind == MENU_READY:
                if self.probe_pending:
                    self.probe()
                return
            last = st.kind
            time.sleep(BOOT_POLL_S)
        raise LinkError(f"the menu was not ready after {timeout:g} s (last: {last})")

    def ensure_menu(self) -> None:
        """The recovery ladder: cheapest rung first, up to the station's limit."""
        if self.wedged_reason:
            raise Wedged(self.wedged_reason)
        try:
            if self.state().kind == MENU_READY:
                return
        except (LinkError, TransportError):
            pass
        rungs: list[tuple[str, Callable[[], None]]] = [("reset", self._rung_reset)]
        if self.reboot_hook:
            rungs.append(("usb-reboot", self._rung_reboot))
        if self.station.has("power"):
            rungs.append(("power-cycle", self._rung_power))
        failures = []
        for name, rung in rungs:
            try:
                rung()
                self.wait_for_menu()
                self.recoveries.append(name)
                return
            except (LinkError, TransportError, PowerUnavailable, Refused) as e:
                failures.append(f"{name}: {e}")
        self.wedged_reason = "; ".join(failures) or "no recovery rung available"
        raise Wedged(self.wedged_reason)

    def _rung_reset(self) -> None:
        self.reset()

    def _rung_reboot(self) -> None:
        assert self.reboot_hook is not None
        self.link.close()                    # the hook opens the port itself, at 134 baud
        self.reboot_hook()
        if self.reconnect_hook:
            self._reconnect()

    def _rung_power(self) -> None:
        self.power.cycle(self.station.rig.power.off_seconds if self.station.rig.power else 5.0)
        if self.reconnect_hook:
            self._reconnect()

    def _reconnect(self) -> None:
        """A new link after the board's USB went away (a reboot, a power
        cycle, a reset out of the minimal image). The new link starts with
        DMA refused and the board is probed again once the menu is up, since
        what answers now may be another image or build. The old stream's
        tap moves over, so a serial log that spans the reconnect is one
        list and an offset taken before it stays valid."""
        assert self.reconnect_hook is not None
        old = self.link
        old.close()
        self.link = self.reconnect_hook()
        self.link._on_unsolicited = self.events.feed
        self.link.stream.tap.chunks[:0] = old.stream.tap.chunks
        self.probe_pending = True
