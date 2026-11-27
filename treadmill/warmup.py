"""The warm-up before a margin run: a C64C was once marginal only after it
had warmed up, so a sweep starts on a machine that has been on ``WARMUP_MIN``
minutes. Nothing on the bench says how long a machine has been on, so the
caller states it (``--powered-since``, ``--powered-for``) or that the machine
is warm (``--assume-warm``). Without that the station's mains relay is asked:
mains off (latched or not) are switched on and the whole warm-up is waited;
otherwise the run is refused, so the assumption is always stated. What it
rested on rides in the environment to the steps a compare starts, and every
run.json and margin record carries it (``warm_up``).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

# A C64C was marginal only once it had warmed up, so a margin run starts on a machine on this long.
WARMUP_MIN = 30
WAIT_LOG_S = 5 * 60                           # how often the wait says how much is left
ENV = "TREADMILL_WARMUP"                      # a compare hands its steps what the warm-up rested on
STATED, RELAY, ASSUMED, NOT_NEEDED = "stated", "switched on by the relay", "assumed warm", "not needed"


class WarmupUnknown(Exception):
    """Nobody said when the machine was switched on, and the relay cannot."""


@dataclass
class Warmup:
    powered_since: str | None         # when the machine was switched on (local time), when known
    how: str                          # STATED, RELAY, ASSUMED or NOT_NEEDED
    note: str                         # what it rests on, in words
    wait_s: float                     # how long to wait before the first step

    def describe(self) -> str:
        since = f"powered since {self.powered_since}, " if self.powered_since else ""
        left = f"waiting {self.wait_s / 60:.0f} min" if self.wait_s > 0 else "no wait"
        return f"warm-up: {since}{self.note}; {left}"

    def record(self) -> dict:
        """What run.json and the margin records carry."""
        return {"powered_since": self.powered_since, "how": self.how}


def parse_since(text: str, now: datetime) -> datetime:
    """``HH:MM`` today, or a full ISO date and time; never in the future."""
    try:
        when = (datetime.combine(now.date(), datetime.strptime(text, "%H:%M").time()) if len(text) <= 5
                else datetime.fromisoformat(text))
    except ValueError:
        raise ValueError(f"--powered-since {text!r}: give HH:MM (today) or YYYY-MM-DDTHH:MM") from None
    if when > now:
        raise ValueError(f"--powered-since {text}: that is later than now ({now:%H:%M})")
    return when


def _since(when: datetime, now: datetime, minutes: int, how: str, note: str) -> Warmup:
    on_s = (now - when).total_seconds()
    return Warmup(when.isoformat(timespec="minutes"), how, f"{note}; on {on_s / 60:.0f} of {minutes} min",
                  max(0.0, minutes * 60 - on_s))


def warmup_for(*, now: datetime, minutes: int, since: datetime | None = None, assume_warm: bool = False,
               relay_off: bool | None = None, machine: str = "the machine") -> Warmup:
    """What to wait before the first step, and why. ``relay_off``: True when
    the station's mains relay has the machine off (it is switched on and the
    whole warm-up waited), None when there is no relay to ask. Raises
    WarmupUnknown when nothing says how long the machine has been on."""
    if minutes <= 0:
        return Warmup(None, NOT_NEEDED, "not needed (--warmup 0)", 0.0)
    if assume_warm:
        return Warmup(None, ASSUMED, "assumed warm (--assume-warm)", 0.0)
    if since is not None:
        return _since(since, now, minutes, STATED, "given")
    if relay_off:
        return Warmup(now.isoformat(timespec="minutes"), RELAY, "the mains go on through the relay now",
                      minutes * 60.0)
    raise WarmupUnknown(f"when was {machine} switched on? A margin run starts once it has been on {minutes} min: "
                        f"give --powered-since HH:MM or --powered-for MIN, or --assume-warm if it is warm")


def from_env(now: datetime, minutes: int) -> Warmup | None:
    """The warm-up a compare settled for the step it started, the wait
    worked out again (none, once the compare has waited)."""
    try:
        said = json.loads(os.environ.get(ENV) or "null")
    except ValueError:
        return None
    if not isinstance(said, dict) or said.get("how") not in (STATED, RELAY, ASSUMED, NOT_NEEDED):
        return None
    if said["how"] in (STATED, RELAY) and said.get("powered_since"):
        w = _since(datetime.fromisoformat(said["powered_since"]), now, minutes, said["how"], "from the compare")
        return w
    return Warmup(said.get("powered_since"), said["how"], f"{said['how']} (from the compare)", 0.0)


def hand_on(w: Warmup) -> None:
    """The steps this process starts, and its own records, carry the warm-up."""
    os.environ[ENV] = json.dumps(w.record())


def current() -> dict | None:
    """What this run's warm-up rested on, for run.json and the margin
    records; None when no warm-up was settled (a run that sweeps nothing)."""
    try:
        said = json.loads(os.environ.get(ENV) or "null")
    except ValueError:
        return None
    return said if isinstance(said, dict) else None


def relay_is_off(station: str) -> bool | None:
    """Whether the station's mains relay has the machine off, latched off
    included: True or False, None when there is no relay to ask. Only the
    relay's port is opened, under the rig's lock; ``power status`` would open
    the whole station, and refuses one latched off. A rig another command
    holds raises RigBusy, which names it."""
    from treadmill.errors import TreadmillError
    from treadmill.station import load_station
    from treadmill.station.lock import RigBusy, RigLock
    from treadmill.station.power import SerialRelayPower
    try:
        st = load_station(station)
        spec = st.rig.power
        if spec is None or spec.backend != "serial-relay":
            return None
        with RigLock(st.rig.id, wait=5.0):
            state = SerialRelayPower(spec).check()
    except RigBusy:
        raise
    except (TreadmillError, OSError):
        return None
    return {"off": True, "on": False}.get(state)


def switch_on(station: str) -> None:
    """The station's mains on through its relay, a latch cleared, under the rig's lock."""
    from treadmill.station import load_station
    from treadmill.station.lock import RigLock
    from treadmill.station.power import SerialRelayPower
    st = load_station(station)
    with RigLock(st.rig.id, wait=5.0):
        SerialRelayPower(st.rig.power).on()


def wait_out(w: Warmup, log=print, sleep: Callable[[float], None] | None = None) -> None:
    """Wait the warm-up, saying how much is left every few minutes."""
    pause = sleep or time.sleep
    left = w.wait_s
    while left > 0:
        chunk = min(WAIT_LOG_S, left)
        pause(chunk)
        left -= chunk
        if left > 0:
            log(f"[warm-up] {datetime.now():%H:%M} {left / 60:.0f} min left")
