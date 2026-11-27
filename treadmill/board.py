"""``Board``: a station's TeensyROM, open for a whole run.

It owns the rig lock, the serial link, the event log, the recovery ladder,
and the keyboard board when the station's machine has one, and it is what
the pytest fixtures and the driver's ``exec`` and ``shell`` hand out. The
ladder, run before every test, climbs only as far as
it must: when the board has left USB, first finding it again, since it may
be back by itself (the host slept, or the board restarted on its own); a
reset; then, on a TR+ whose machine has a keyboard board with
its reset fitted, a reset on the C64's /RESET line, which the TR+ senses
from any handler; then the 134-baud USB reboot with a loader relaunch of the
firmware already on the board, when the run knows which hex that is (with no
loader on the host, a board left waiting in the bootloader is written again
without its chip id when it is the only one waiting there); then a
mains cycle when the rig has power control; else the station is marked wedged
and the rest of the run skips with that reason. A mains cycle takes the
keyboard board down with the machine and undoes a Kawari's standard switch,
so the rung puts both back once the menu is up.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from treadmill.errors import TreadmillError
from treadmill.firmware.flash import REAPPEAR_S, FlashError, relaunch
from treadmill.link import Link, LinkError, NotFound, Refused, TransportError, find_port, open_serial
from treadmill.link.trlink import NotAllowed
from treadmill.link.banner import Banner
from treadmill.link.state import MENU_READY, State, derive_state
from treadmill.link.unsolicited import EventLog
from treadmill.station.kawari import KawariError, set_standard
from treadmill.station.kbd import KbdError, KeyboardBoard
from treadmill.station.lock import RigLock
from treadmill.station.model import Station
from treadmill.station.power import Power, PowerUnavailable, SerialRelayPower, make_power

if TYPE_CHECKING:
    from treadmill.firmware.prepare import FirmwareRun

BOOT_POLL_S = 0.5
C128_RESET_HOLD_S = 0.25        # no DMA within ~80 ms of a C128 reset release (Constraints.md); margin on top
# Handlers under which the firmware ignores every host command (ServiceSerial
# returns at once for TR BASIC, whose program owns the serial port): a reset
# sent there only waits out the boot timeout.
SERIAL_DEAF_HANDLERS = frozenset({"TR_BASIC"})
# The keyboard board powers from the machine; its port is back within about a second of power-on.
KBD_REAPPEAR_S = 5.0
STAYED_ON = ("the board stayed on USB through the power cycle: the supply's switch is on ON, the supply powers "
             "another machine, or off_seconds is shorter than the supply holds up")


def port_gone(e: TransportError) -> bool:
    """An open that failed because the port no longer exists, as opposed to
    one another program holds."""
    text = str(e)
    return "FileNotFoundError" in text or "No such file" in text


class Wedged(TreadmillError):
    """The board stopped answering and nothing the station has could bring it back."""


def open_keyboard(station: Station, factory: Callable[[], KeyboardBoard] | None = None, *,
                  discover: bool = True, timeout: float = 0.0) -> tuple[KeyboardBoard | None, str | None]:
    """The station's keyboard board, or None and why not. A board the station
    names that is not on USB, or will not answer, leaves the station without
    its capabilities instead of failing the open: the tests that need it skip
    with the reason. ``factory`` replaces discovery (a fake board); without
    one, ``discover=False`` opens nothing. ``timeout``: how long to look for
    a board that is still coming up with its machine."""
    if station.keyboard is None:
        return None, None
    if factory is None and not discover:
        return None, "not opened: the TR is a fake and no fake keyboard board was given"
    try:
        return (factory() if factory else KeyboardBoard.open(station.keyboard.chip, timeout=timeout)), None
    except KbdError as e:
        return None, str(e)


def power_hint(power: Power, station: Station) -> str:
    """What the mains relay says about a TeensyROM that is not on USB, for
    the not-found error: the machine's switch and plug when the relay has
    power on, or the relay itself when it doesn't answer."""
    if not isinstance(power, SerialRelayPower):
        return ""
    ch = power.channel
    try:
        state = power.check()
    except PowerUnavailable as e:
        return (f". The mains relay doesn't answer ({e}), and with the supply's switch on AUTO the supply has no "
                "power without it")
    if state == "on":
        return (f". The mains relay has channel {ch} on: check that the machine's power switch is on and that the "
                "bench supply is plugged into this machine")
    return f". The mains relay has channel {ch} {state}: a power cycle may be under way"


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
    # The ladder's upper rungs: reboot the Teensy without reflashing, then find the board again.
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
    kbd: KeyboardBoard | None = None
    kbd_problem: str | None = None       # why the station's keyboard board isn't open
    # How to open the keyboard board again after a power cycle: the factory Board.open was given, or discovery.
    kbd_factory: Callable[[], KeyboardBoard] | None = None
    kbd_discover: bool = True
    latch_cleared: bool = False          # the open turned on a mains relay channel latched off
    power_problem: str | None = None     # why the station's power control is off, when its rig has one
    power_state: str | None = None       # the relay channel's state when the station opened
    power_timings: dict[str, float | None] = field(default_factory=dict)   # the last power cycle's
    region: str | None = None            # the run's --region, which a power cycle undoes on a Kawari
    _power_on_at: float = 0.0

    # -- opening ----------------------------------------------------------

    @classmethod
    def open(cls, station: Station, *, lock_wait: float = 5.0, attended: bool = False,
             link_factory: Callable[[], Link] | None = None, fw_defines: tuple[str, ...] = (),
             caps_mask: set[str] | frozenset[str] = frozenset(),
             firmware: "FirmwareRun | None" = None,
             kbd_factory: Callable[[], KeyboardBoard] | None = None,
             power_factory: Callable[[], Power] | None = None, power_on_latched: bool = True) -> "Board":
        """Lock the rig, find the board by chip id, open it and probe it, with
        the recovery ladder's upper rungs attached: a reconnect by chip id,
        and, when ``firmware`` knows the hex on the board and the checkout
        whose flasher put it there, the USB reboot that relaunches it. Then
        the power control's check and the keyboard board the station names,
        whose fitted outputs join the capabilities. ``link_factory`` replaces
        discovery and the serial port (the self-tests hand in a link to a
        fake board) and gets no rung past a reset; with it, only
        ``kbd_factory`` opens a keyboard board. ``power_factory`` replaces
        the rig's power backend (a fake mains relay). A mains relay channel
        latched off (``power off --latch``) is turned on first, and the board
        is waited for while it boots; with ``power_on_latched`` False the open
        is refused instead (PoweredDown). Nothing stays held when
        this fails: the lock and the ports are released before the error
        leaves."""
        power = power_factory() if power_factory is not None else make_power(station.rig.power, attended)
        lock = RigLock(station.rig.id, wait=lock_wait).acquire()
        events = EventLog()
        try:
            latch_cleared = isinstance(power, SerialRelayPower) and power.on_if_latched(station.id, power_on_latched)
            if link_factory is not None:
                link = link_factory()
                link._on_unsolicited = events.feed
            else:
                try:
                    found = find_port(station.tr.uid, station.tr.port_hint,
                                      timeout=REAPPEAR_S if latch_cleared else 0.0)
                except NotFound as e:
                    raise NotFound(f"{e}{power_hint(power, station)}") from None
                link = Link(open_serial(found.port), on_unsolicited=events.feed)
        except Exception:
            power.close()
            lock.release()
            raise
        board = cls(station=station, link=link, events=events, lock=lock, power=power,
                    fw_defines=tuple(fw_defines), caps_mask=frozenset(caps_mask),
                    kbd_factory=kbd_factory, kbd_discover=link_factory is None)
        board.latch_cleared = latch_cleared
        if link_factory is None:
            board.attach_recovery(firmware)
        try:
            board.check_power()
            board.kbd, board.kbd_problem = open_keyboard(station, kbd_factory, discover=link_factory is None)
            if board.kbd is not None:
                board.station = board.station.with_keyboard(board.kbd.fitted).masked(board.caps_mask)
            board.probe()
            try:
                board.apply_tod()
            except KbdError as e:
                board.drop_keyboard(f"its TOD line was not set: {e}")
        except BaseException:
            board.close()
            raise
        return board

    def drop_keyboard(self, why: str) -> None:
        """Go on without the keyboard board, as when it does not answer at
        open: its capabilities off, and the tests that need them skipping
        with ``why``."""
        kbd, self.kbd, self.kbd_problem = self.kbd, None, why
        self.station = self.station.with_keyboard(None).masked(self.caps_mask)
        if kbd is not None:
            kbd.close()

    def check_power(self) -> None:
        """The rig's power control answers before the run needs it: a mains
        relay that is missing, or isn't what the rig names, takes the
        ``power`` capability away with the reason, as a keyboard board that
        doesn't answer takes its own. Nothing is switched."""
        if self.station.rig.power is None:
            return
        try:
            self.power_state = self.power.check()
        except PowerUnavailable as e:
            self.power_problem = str(e)
            self.mask({"power"})

    def attach_recovery(self, firmware: "FirmwareRun | None") -> None:
        """The ladder's rungs above a reset, for a board on a real port."""
        uid, hint = self.station.tr.uid, self.station.tr.port_hint

        def reconnect() -> Link:
            deadline = time.monotonic() + REAPPEAR_S
            while True:
                found = find_port(uid, hint, timeout=max(0.0, deadline - time.monotonic()))
                try:
                    return Link(open_serial(found.port))
                except TransportError as e:
                    # Windows can still list the port for a moment after a reset out of MinimalBoot took
                    # it away ("could not open port ...: FileNotFoundError", the 250425 C64 with a Kawari,
                    # 2026-11-26): look again. A port another program holds is refused, never retried.
                    if not port_gone(e) or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.5)
        self.reconnect_hook = reconnect
        if firmware is not None and firmware.hex is not None and firmware.primary is not None:
            hex_info, primary = firmware.hex, firmware.primary
            self.reboot_hook = lambda: relaunch(hex_info, uid, primary, hint)

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
            self.station = self.station.with_probe(kind, fw_defines=self.fw_defines,
                                                   standard=probe.banner.video).masked(self.caps_mask)

    def apply_tod(self) -> None:
        """The station's TOD rate on the keyboard board's TOD line, when it
        drives one: ``standard`` is 50 Hz on PAL and 60 Hz on NTSC, by the
        banner, and is left alone while the banner has not said which. Only
        a station opened with its TR does this; ``--no-link`` leaves the
        line as it found it."""
        if self.kbd is None or not self.station.has("tod_select"):
            return
        rate = self.station.keyboard.tod
        if rate == "standard":
            if self.station.standard not in ("PAL", "NTSC"):
                return
            rate = 50 if self.station.standard == "PAL" else 60
        self.kbd.tod(rate)

    def mask(self, caps: set[str] | frozenset[str]) -> None:
        """Take capabilities away for the rest of the run (--caps-mask, a
        capture that did not open); a later probe keeps them away."""
        self.caps_mask = self.caps_mask | frozenset(caps)
        self.station = self.station.masked(self.caps_mask)

    def close(self) -> None:
        try:
            self.link.close()
        finally:
            try:
                if self.kbd is not None:
                    self.kbd.close()
            finally:
                try:
                    self.power.close()
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

    def reset(self, *, through_noise: bool = False) -> None:
        """Reset the C64 and start the hold-off a C128 needs before any DMA.
        ``through_noise``: see ``Link.reset``."""
        self.link.reset(through_noise=through_noise)
        if self.is_c128:
            self.dma_hold_until = time.monotonic() + C128_RESET_HOLD_S

    def ext_reset(self) -> None:
        """Reset the C64 on its /RESET line from the keyboard board, with the
        TR out of it: a TR+ senses the reset and goes back to its menu from
        any handler, TR BASIC's included. The C128's hold-off starts as for
        ``reset``, once the pulse has ended."""
        if self.kbd is None or not self.station.has("ext_reset"):
            why = f" (keyboard board: {self.kbd_problem})" if self.kbd_problem else ""
            raise NotAllowed(f"external reset not sent: station {self.station.id} lacks ext_reset{why}")
        self.kbd.reset()
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
                    self.reconnect()
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
        rungs: list[tuple[str, Callable[[], None]]] = []
        deaf = self.events.current_handler() in SERIAL_DEAF_HANDLERS
        if self.reconnect_hook and self.link.stream.ended and not deaf:
            # The board left USB and may be back by itself (the host slept, or the board restarted on its own):
            # found again, it needs no rung that resets or reboots it, and the reboot can strand it (backlog 42).
            rungs.append(("reconnect", self._rung_reconnect))
        # Without reset sensing, a reset the TR doesn't make leaves it in its handler: no way back to the menu.
        ext = self.kbd is not None and self.station.has("ext_reset", "reset_sense")
        if not ((self.reboot_hook or ext) and deaf):
            rungs.append(("reset", self._rung_reset))
        if ext:
            rungs.append(("ext-reset", self.ext_reset))
        if self.reboot_hook:
            rungs.append(("usb-reboot", self._rung_reboot))
        if self.station.has("power"):
            rungs.append(("power-cycle", self._rung_power))
        failures = []
        for name, rung in rungs:
            try:
                rung()
                self.wait_for_menu()
                if name == "power-cycle":
                    self.after_power_cycle()
                self.recoveries.append(name)
                return
            except (LinkError, TransportError, PowerUnavailable, Refused, NotFound, KbdError, NotAllowed,
                    KawariError, FlashError) as e:      # a reboot that strands the board: the power rung is next
                failures.append(f"{name}: {e}")
        self.wedged_reason = "; ".join(failures) or "no recovery rung available"
        raise Wedged(self.wedged_reason)

    def _rung_reset(self) -> None:
        self.reset()

    def _rung_reconnect(self) -> None:
        self.reconnect()
        if self.link.stream.ended:
            raise LinkError("the board is not back on USB")

    def restart_teensy(self) -> None:
        """The Teensy restarted through the ladder's USB reboot (its own state
        gone, as a power cycle would leave it), back at the menu, and counted
        as a recovery: what a sweep needs when its walk left the TeensyROM's
        state bad (backlog 53). A reboot the flasher refuses, with the board
        left in the bootloader beside the station's other Teensys, falls to
        the mains on a station with ``power``, as the ladder does."""
        if self.reboot_hook is None:
            raise LinkError("no USB reboot: the run does not know the hex the board carries")
        try:
            self._rung_reboot()
            self.wait_for_menu()
            self.recoveries.append("usb-reboot")
        except (LinkError, TransportError, Refused, NotFound, NotAllowed, FlashError):
            if not self.station.has("power"):
                raise
            self._rung_power()
            self.wait_for_menu()
            self.after_power_cycle()
            self.recoveries.append("power-cycle")

    def _rung_reboot(self) -> None:
        assert self.reboot_hook is not None
        self.link.close()                    # the hook opens the port itself, at 134 baud
        self.reboot_hook()
        if self.reconnect_hook:
            self.reconnect()

    @property
    def power_off_seconds(self) -> float:
        return self.station.rig.power.off_seconds if self.station.rig.power else 5.0

    def _rung_power(self, off_seconds: float | None = None) -> None:
        """The mains off and on. The TeensyROM powers from the machine, so its
        USB must go with the mains: a board still on USB afterwards means the
        cut never reached it."""
        t_cut = time.monotonic()
        self.power.cycle(self.power_off_seconds if off_seconds is None else off_seconds)
        self._power_on_at = t_on = time.monotonic()
        stream = self.link.stream
        if not stream.ended:
            raise PowerUnavailable(STAYED_ON)
        gone = stream.ended_at
        self.power_timings = {"off_s": round(t_on - t_cut, 2),
                              "left_usb_s": round(gone - t_cut, 2) if gone is not None else None,
                              "usb_back_s": None, "menu_s": None}
        if self.reconnect_hook:
            self.reconnect()
            self.power_timings["usb_back_s"] = round(time.monotonic() - t_on, 2)

    def after_power_cycle(self) -> None:
        """Once the menu is back after a mains cycle, put back what the cycle
        undid: the keyboard board, which powers from the machine, with its
        TOD line, and the run's ``--region`` on a Kawari, whose ``V`` setting
        lasts only until the C64 powers off."""
        if self._power_on_at:
            self.power_timings["menu_s"] = round(time.monotonic() - self._power_on_at, 2)
        if self.station.keyboard is not None:
            if self.kbd is not None:
                self.kbd.close()
            self.kbd, self.kbd_problem = open_keyboard(self.station, self.kbd_factory, discover=self.kbd_discover,
                                                       timeout=KBD_REAPPEAR_S)
            self.station = self.station.with_keyboard(self.kbd.fitted if self.kbd else None).masked(self.caps_mask)
            try:
                self.apply_tod()
            except KbdError as e:
                self.drop_keyboard(f"its TOD line was not set after the power cycle: {e}")
        if self.region and self.station.standard != self.region:
            set_standard(self, self.region)

    def power_cycle(self, off_seconds: float | None = None) -> dict[str, float | None]:
        """One power cycle through the ladder's own rung, then the menu back
        and what the cycle undid put back: ``treadmill power cycle`` and
        ``run --power-check``. Not a recovery, so not counted as one. The
        timings, in seconds: ``off_s`` the cut as the relay timed it,
        ``left_usb_s`` from the cut to the board leaving USB, ``usb_back_s``
        and ``menu_s`` from power back to the board on USB and at the menu."""
        if not self.station.has("power"):
            why = f" ({self.power_problem})" if self.power_problem else ""
            raise PowerUnavailable(f"station {self.station.id} lacks power{why}")
        self._rung_power(off_seconds)
        self.wait_for_menu()
        self.after_power_cycle()
        return dict(self.power_timings)

    def reopen(self, away: float) -> None:
        """Close the port for ``away`` seconds, then open it again: the host
        not reading. Whether the board's USB writes then stall is the host
        driver's call; on Windows the driver keeps taking (and discarding)
        what the board sends while the port is closed."""
        assert self.reconnect_hook is not None
        self.link.close()
        time.sleep(away)
        self.reconnect()

    def reconnect_after_drop(self, timeout: float) -> None:
        """The port dropping because the caller made it (a launch the main
        image hands to the minimal one): wait up to ``timeout`` for it, then
        reconnect. Not a recovery, so not counted as one."""
        assert self.reconnect_hook is not None
        deadline = time.monotonic() + timeout
        while not self.link.stream.ended:
            if time.monotonic() > deadline:
                raise LinkError(f"the port did not drop within {timeout:g} s")
            time.sleep(0.1)
        self.reconnect()

    def reconnect(self) -> None:
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
        self.link.drain_when_pending = old.drain_when_pending
        if self.link.stream is not old.stream:      # the same stream back would copy its log into itself
            self.link.stream.tap.chunks[:0] = old.stream.tap.chunks
        self.probe_pending = True
