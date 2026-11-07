"""Knob x probe x search: a bus timing margin as a number.

What was done by hand before (build with ``Dbg_SerTimChg``, raise
``nS_PLAprop`` over serial until the C64's reads of TeensyROM-served bytes
fail) is an engine here. A *knob* is one of the firmware's ``t`` timing
variables; a *probe* takes samples at the current value and says how many
failed; a *search* walks the knob from its default outward in coarse steps,
then bisects the first failing interval down to ``fine``. The defaults are
restored on exit whatever happened, and the result is a margin: the last
clean value, the first failing one, and every point probed. The TeensyROM
cannot see its own read errors, so the probes are the C64's: a program
counting wrong register reads (``idread``, read back by DMA on a TR+), or
the menu itself compared with its golden after a reset (``menu``, by video
on any TR).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from treadmill.board import Board
from treadmill.errors import EXIT_USAGE, TreadmillError
from treadmill.link import DebugCommands, LinkError, TransportError
from treadmill.link.dbgcmds import TIMING_KNOBS, TIMING_NAMES
from treadmill.observe.channels import ScreenChannel, ScreenError
from treadmill.observe.golden import Golden, compare, save_golden

IDREAD_RESULTS = 0xC000           # idread.a's counters: passes (16-bit), wrong reads (24-bit)
IDREAD_READS_PER_PASS = 512       # two registers, 256 rounds
IDREAD_START_S = 6.0
MENU_BOOT_S = 20.0                # a knob that breaks the menu program never boots: do not wait the station's minute


class SweepError(TreadmillError):
    pass


@dataclass(frozen=True)
class Knob:
    letter: str                   # the letter after 't'
    name: str                     # the firmware variable the listing prints
    digits: int
    define: str = "Dbg_SerTimChg"
    unit: str = "ns"


KNOBS: dict[str, Knob] = {
    letter: Knob(letter, TIMING_NAMES[letter], TIMING_KNOBS[letter], unit="cycles" if letter == "k" else "ns")
    for letter in TIMING_KNOBS
}


def knob_named(text: str) -> Knob:
    """``p``, ``tp`` or ``nS_PLAprop``."""
    key = text.strip()
    if key in KNOBS:
        return KNOBS[key]
    if len(key) == 2 and key[0] == "t" and key[1] in KNOBS:
        return KNOBS[key[1]]
    for knob in KNOBS.values():
        if knob.name.lower() == key.lower():
            return knob
    raise SweepError(f"no timing knob {text!r}; one of " + ", ".join(f"{k.letter} ({k.name})" for k in KNOBS.values()))


@dataclass
class Point:
    value: int
    failures: int
    samples: int
    seconds: float
    notes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.failures > 0

    @property
    def rate(self) -> float:
        return self.failures / self.samples if self.samples else 1.0


class Probe(Protocol):
    name: str

    def setup(self) -> None: ...

    def sample(self, label: str) -> tuple[int, int, str | None]:
        """(failures, samples, note) at the knob's current value."""
        ...

    def teardown(self) -> None: ...


class IdReadProbe:
    """idread.a on the C64 reads the presence registers and counts wrong
    answers; a sample is the counts added over ``dwell`` seconds, read by DMA."""

    name = "idread"

    def __init__(self, board: Board, program: Path, *, dwell: float = 1.0, drive: str = "sd",
                 remote: str = "/treadmill/idread.prg", log=print):
        self.board = board
        self.program = Path(program)
        self.dwell = dwell
        self.drive = drive
        self.remote = remote
        self.log = log
        self._stalled = False

    def setup(self) -> None:
        if not self.board.dma_allowed():
            raise SweepError("the idread probe reads its counters by DMA: it needs a TR+ running its main image")
        self.board.ensure_menu()
        self.board.link.push_file(self.program.read_bytes(), self.remote, self.drive)
        self.board.link.launch(self.remote, self.drive)
        deadline = time.monotonic() + IDREAD_START_S
        first = self.counters()[0]
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if self.counters()[0] != first:
                self._stalled = False
                return
        raise SweepError("idread is not running on the C64: its pass counter does not move")

    def counters(self) -> tuple[int, int]:
        b = self.board.link.read_memory(IDREAD_RESULTS, 5)
        return b[0] | b[1] << 8, b[2] | b[3] << 8 | b[4] << 16

    def sample(self, label: str) -> tuple[int, int, str | None]:
        if self._stalled:
            self.log(f"[sweep] idread stopped at the last value; relaunching for {label}")
            self.setup()
        before = self.counters()
        time.sleep(self.dwell)
        after = self.counters()
        passes = (after[0] - before[0]) & 0xFFFF
        wrong = (after[1] - before[1]) & 0xFFFFFF
        if passes == 0:
            self._stalled = True
            return 1, 1, "idread stopped: the C64 hung"
        return wrong, passes * IDREAD_READS_PER_PASS, None

    def teardown(self) -> None:
        self.board.ensure_menu()


class MenuProbe:
    """Reset, let the menu boot, and compare its listing with the root
    golden: the item-type marks are what the NTSC misread broke, and the
    read works by video on a plain TR. A menu that does not come back is a
    failure too."""

    name = "menu"

    def __init__(self, board: Board, screen: ScreenChannel, golden: Golden, artifacts: Path | None = None,
                 *, boot_timeout: float = MENU_BOOT_S, settle_timeout: float = 10.0):
        self.board = board
        self.screen = screen
        self.golden = golden
        self.artifacts = artifacts
        self.boot_timeout = boot_timeout
        self.settle_timeout = settle_timeout

    def setup(self) -> None:
        if not self.screen.has_video and not self.board.dma_allowed():
            raise SweepError("the menu probe cannot read the screen: the station has no video channel "
                             "(capture, calibration and glyph bank) and no DMA")

    def sample(self, label: str) -> tuple[int, int, str | None]:
        self.board.reset()
        try:
            self.board.wait_for_menu(timeout=self.boot_timeout)
        except (LinkError, TransportError) as e:
            return 1, 1, f"the menu did not come back: {e}"
        try:
            read = self.screen.wait_settled(timeout=self.settle_timeout)
        except ScreenError as e:
            return 1, 1, f"the screen did not settle: {e}"
        mismatches = compare(read.rows(), self.golden)
        if mismatches and self.artifacts is not None:
            save_golden(read.rows(), self.artifacts / f"{label}.screen.txt")
        if mismatches:
            return 1, 1, f"{len(mismatches)} cell(s) differ from the golden"
        return 0, 1, None

    def teardown(self) -> None:
        self.board.ensure_menu()


@dataclass(frozen=True)
class SearchSpec:
    stop: int
    start: int | None = None      # None: the knob's current value
    step: int = 10
    fine: int = 1                 # bisect until the clean and failing values are this close
    repeat: int = 1               # samples per point

    def __post_init__(self) -> None:
        # The walk moves by step and the bisection ends at fine: at zero neither loop would end,
        # and a point with no samples would pass as clean.
        for name in ("step", "fine", "repeat"):
            if getattr(self, name) < 1:
                raise usage_error(f"{name} must be at least 1, not {getattr(self, name)}")


def search(probe_at: Callable[[int], Point], start: int, spec: SearchSpec) -> tuple[list[Point], int | None, int | None]:
    """Coarse steps from ``start`` toward ``spec.stop`` until a point fails,
    then bisection between the last clean and the first failing value until
    they are ``spec.fine`` apart. Returns the points, the last clean value
    and the first failing one (None when the range holds no edge, or when
    the start itself fails)."""
    direction = 1 if spec.stop >= start else -1
    points: list[Point] = []
    last_clean: int | None = None
    first_fail: int | None = None
    value = start
    while (value <= spec.stop) if direction > 0 else (value >= spec.stop):
        point = probe_at(value)
        points.append(point)
        if point.failed:
            first_fail = value
            break
        last_clean = value
        value += direction * spec.step
    if first_fail is None or last_clean is None:
        return points, last_clean, first_fail
    while abs(first_fail - last_clean) > spec.fine:
        mid = (first_fail + last_clean) // 2
        point = probe_at(mid)
        points.append(point)
        if point.failed:
            first_fail = mid
        else:
            last_clean = mid
    return points, last_clean, first_fail


@dataclass
class Margin:
    knob: Knob
    probe: str
    default: int
    start: int
    spec: SearchSpec
    points: list[Point]
    last_clean: int | None
    first_fail: int | None
    restored: bool
    seconds: float

    @property
    def direction(self) -> int:
        return 1 if self.spec.stop >= self.start else -1

    @property
    def margin(self) -> int | None:
        """How far the knob moves from its default before the first clean
        value is left behind; None when the range held no edge."""
        if self.last_clean is None or self.first_fail is None:
            return None
        return abs(self.last_clean - self.default)

    def to_dict(self) -> dict:
        return {
            "knob": self.knob.name, "letter": self.knob.letter, "unit": self.knob.unit, "probe": self.probe,
            "default": self.default, "start": self.start, "stop": self.spec.stop, "step": self.spec.step,
            "fine": self.spec.fine, "repeat": self.spec.repeat, "direction": self.direction,
            "last_clean": self.last_clean, "first_fail": self.first_fail, "margin": self.margin,
            "restored": self.restored, "seconds": round(self.seconds, 1),
            "points": [{"value": p.value, "failures": p.failures, "samples": p.samples, "rate": round(p.rate, 5),
                        "seconds": round(p.seconds, 2), "notes": p.notes} for p in self.points],
        }

    def describe(self) -> str:
        head = (f"{self.knob.name} ({self.probe} probe): from {self.start} toward {self.spec.stop} by "
                f"{self.spec.step}, fine {self.spec.fine}, {self.spec.repeat} sample(s) a point")
        lines = [head]
        for p in sorted(self.points, key=lambda p: p.value):
            mark = "FAIL " if p.failed else "clean"
            note = f"  ({'; '.join(p.notes)})" if p.notes else ""
            lines.append(f"  {p.value:5d}  {mark}  {p.failures:6d} / {p.samples:<7d}{note}")
        if self.first_fail is None:
            lines.append(f"{self.knob.name}: clean through {self.last_clean}; no edge within the range")
        elif self.last_clean is None:
            lines.append(f"{self.knob.name}: fails at {self.first_fail}, the first value probed")
        else:
            lines.append(f"{self.knob.name}: clean up to {self.last_clean}, fails from {self.first_fail} "
                         f"(default {self.default}: {self.margin} {self.knob.unit} of margin)")
        if not self.restored:
            lines.append("DEFAULTS NOT RESTORED: the board still runs the last value")
        return "\n".join(lines)


def value_in(listing: str, knob: Knob) -> int:
    """The knob's value in a ``t`` listing."""
    m = re.search(rf"{re.escape(knob.name)}\s+(\d+)", listing)
    if not m:
        raise SweepError(f"the timing listing has no {knob.name}; does the build carry {knob.define}?\n{listing}")
    return int(m.group(1))


def current_value(dbg: DebugCommands, knob: Knob) -> int:
    """The knob's value now, from the bare ``t`` listing."""
    return value_in(dbg.listing(), knob)


class Sweep:
    """One knob, one probe, one search; the defaults restored when it ends."""

    def __init__(self, dbg: DebugCommands, knob: Knob, probe: Probe, spec: SearchSpec, *, log=print,
                 on_point: Callable[[Point], None] | None = None):
        self.dbg = dbg
        self.knob = knob
        self.probe = probe
        self.spec = spec
        self.log = log
        self.on_point = on_point

    def current(self) -> int:
        return current_value(self.dbg, self.knob)

    def set_value(self, value: int) -> None:
        shown = self.dbg.set_knob(self.knob.letter, value)
        if value_in(shown, self.knob) != value:
            raise SweepError(f"t{self.knob.letter}{value} not taken:\n{shown}")

    def probe_at(self, value: int) -> Point:
        t0 = time.monotonic()
        self.set_value(value)
        failures = samples = 0
        notes: list[str] = []
        for _ in range(self.spec.repeat):
            f, s, note = self.probe.sample(f"{self.knob.letter}{value:0{self.knob.digits}d}")
            failures += f
            samples += s
            if note and note not in notes:
                notes.append(note)
        point = Point(value, failures, samples, time.monotonic() - t0, notes)
        self.log(f"[sweep] {self.knob.name} {value}: {'FAIL' if point.failed else 'clean'} "
                 f"{failures}/{samples}" + (f" ({'; '.join(notes)})" if notes else ""))
        if self.on_point:
            self.on_point(point)
        return point

    def run(self) -> Margin:
        t0 = time.monotonic()
        default = self.current()
        start = default if self.spec.start is None else self.spec.start
        points: list[Point] = []
        last_clean = first_fail = None
        restored = False
        self.probe.setup()
        try:
            points, last_clean, first_fail = search(self.probe_at, start, self.spec)
        finally:
            try:
                restored = "Defaults set" in self.dbg.restore_defaults()
            finally:
                self.probe.teardown()
        margin = Margin(self.knob, self.probe.name, default, start, self.spec, points, last_clean, first_fail,
                        restored, time.monotonic() - t0)
        if not restored:
            raise SweepError("td not confirmed: the board is still running the last value\n" + margin.describe())
        return margin


def usage_error(message: str) -> SweepError:
    err = SweepError(message)
    err.code = EXIT_USAGE
    return err
