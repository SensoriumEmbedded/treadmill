"""Knob x probes x search: a bus timing margin as a number, each way.

What was done by hand before (build with ``Dbg_SerTimChg``, move a knob over
serial until the C64's reads of TeensyROM-served bytes fail) is an engine
here. A *knob* is one of the firmware's ``t`` timing variables
(``treadmill.knobs``); a *probe* takes samples at the current value and says
how many failed (``treadmill.probes``); a *search* walks the knob from its
default in coarse steps until a point fails, then bisects that interval
down to ``fine``.

Modes: one edge (toward ``stop``); both edges (the default once, then down
to the knob's lower limit and up to its upper one, each edge bisected); and
a scan (every step of a fixed range, no bisection). Passes repeat the
search or the scan, alternating which edge or which end goes first, and a
value counts as clean only if it was clean in every pass; an edge that
moves between passes marks the record drifting. ``nS_DMASetup`` moves with
the release and the latch held (``nS_DMADataHold`` and ``nS_DMADataSetup``
move the other way, from the sums the board's defaults make). Other knobs
may be set to a value of their own for the whole sweep (``fixed``), and the
limits, which come from the knob table and its couplings, follow them; a
spec past them is refused.

The TeensyROM cannot see its own read errors, so the probes are the C64's,
or the screen's. While a knob that times DMA is off its default, a probe
reads its counters by DMA only with the defaults back in place (the
``Setting`` it is handed brackets the read), nothing resets the C64, and
the defaults are restored on exit whatever happened.
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Callable, Iterator, NamedTuple, Protocol, Sequence

from treadmill import knobs
from treadmill.errors import EXIT_USAGE, TreadmillError
from treadmill.knobs import HELD_SUMS, KNOBS, Knob, parse_listing, standard_of  # noqa: F401  (KNOBS: re-exported)
from treadmill.link import DebugCommands

_DEFAULTS_FOR = re.compile(r"Defaults set \((PAL|NTSC) (C64|C128)\)")


class SweepError(TreadmillError):
    pass


def usage_error(message: str) -> SweepError:
    err = SweepError(message)
    err.code = EXIT_USAGE
    return err


def knob_named(text: str) -> Knob:
    """``p``, ``tp`` or ``nS_PLAprop``; a mode such as ``KSenseMode`` is set, not swept."""
    try:
        knob = knobs.knob_named(text)
    except KeyError as e:
        raise SweepError(e.args[0]) from None
    if not knob.swept:
        raise SweepError(f"{knob.name} is a mode, set with {knob.command}#, not swept")
    return knob


class Sample(NamedTuple):
    failures: int
    samples: int
    note: str | None = None
    hung: bool = False            # the workload stopped: a collapse, not a count


class Probe(Protocol):
    name: str

    def setup(self) -> None: ...

    def sample(self, label: str, setting: "Setting | None" = None) -> Sample | tuple:
        """(failures, samples, note) at the knob's current value."""
        ...

    def teardown(self) -> None: ...


class Setting:
    """The values under test at one point, handed to each probe so that a
    read by DMA can step back to the defaults while a DMA-timing knob is off."""

    def __init__(self, sweep: "Sweep", values: dict[str, int], bracket: bool):
        self.sweep = sweep
        self.values = values
        self.bracket = bracket

    @contextmanager
    def at_defaults(self, always: bool = False) -> Iterator[None]:
        """The defaults back for the block, then the values again: for a
        read by DMA while a DMA-timing knob is off (``bracket``), or
        ``always``, for a relaunch through a reset and the menu, which a
        CPU knob past its edge can keep from booting."""
        if not (self.bracket or always):
            yield
            return
        self.sweep.restore()
        try:
            yield
        finally:
            self.sweep.apply(self.values)


@contextmanager
def at_defaults(setting: Setting | None, always: bool = False) -> Iterator[None]:
    """``setting.at_defaults()``, or nothing when a probe runs on its own."""
    if setting is None:
        yield
    else:
        with setting.at_defaults(always):
            yield


@dataclass
class Point:
    value: int
    failures: int
    samples: int
    seconds: float
    notes: list[str] = field(default_factory=list)
    by_probe: dict[str, list[int]] = field(default_factory=dict)    # probe -> [failures, samples]
    failed_by: list[str] = field(default_factory=list)
    hung: bool = False
    pass_no: int = 1
    held: dict[str, int] = field(default_factory=dict)              # the knobs moved with this one

    @property
    def failed(self) -> bool:
        return self.failures > 0

    @property
    def rate(self) -> float:
        return self.failures / self.samples if self.samples else 1.0

    def to_dict(self) -> dict:
        return {"pass": self.pass_no, "value": self.value, "failures": self.failures, "samples": self.samples,
                "rate": round(self.rate, 5), "seconds": round(self.seconds, 2), "by_probe": self.by_probe,
                "failed_by": self.failed_by, "hung": self.hung, "held": self.held, "notes": self.notes}


@dataclass(frozen=True)
class SearchSpec:
    stop: int | None = None       # one edge: walk toward this value (None: up to the knob's limit)
    start: int | None = None      # None: the knob's default
    step: int = 10
    fine: int = 1                 # bisect until the clean and failing values are this close
    repeat: int = 1               # samples a point, each probe
    both: bool = False            # both edges, each to the knob's limit
    passes: int = 1
    scan: tuple[int, int] | None = None   # every step from LO to HI, no bisection
    grid: tuple[int, int] | None = None   # with scan, for nS_DMASetup: nS_DMADataHold's range, a column a step

    def __post_init__(self) -> None:
        # The walk moves by step and the bisection ends at fine: at zero neither loop would end,
        # and a point with no samples would pass as clean.
        for name in ("step", "fine", "repeat", "passes"):
            if getattr(self, name) < 1:
                raise usage_error(f"{name} must be at least 1, not {getattr(self, name)}")
        for name in ("scan", "grid"):
            span = getattr(self, name)
            if span is not None and span[0] > span[1]:
                raise usage_error(f"{name} {span[0]}:{span[1]}: the low end comes first")
        if self.grid is not None and self.scan is None:
            raise usage_error("a grid's rows are a scan: give --scan LO:HI with --grid LO:HI")

    @property
    def mode(self) -> str:
        if self.grid is not None:
            return "grid"
        return "scan" if self.scan is not None else "both" if self.both else "one"


def steps(low: int, high: int, step: int) -> list[int]:
    """Every ``step`` from ``low``, and ``high`` itself."""
    values = list(range(low, high + 1, step))
    return values if values[-1] == high else values + [high]


def search(probe_at: Callable[[int], Point], start: int, spec: SearchSpec, *,
           probe_start: bool = True) -> tuple[list[Point], int | None, int | None]:
    """Coarse steps from ``start`` toward ``spec.stop`` until a point fails,
    then bisection between the last clean and the first failing value until
    they are ``spec.fine`` apart. Returns the points, the last clean value
    and the first failing one (None when the range holds no edge, or when
    the start itself fails). ``probe_start=False``: the start was probed
    already and was clean."""
    direction = 1 if spec.stop >= start else -1
    points: list[Point] = []
    last_clean: int | None = None
    first_fail: int | None = None
    value = start
    if not probe_start:
        last_clean = start
        if start == spec.stop:
            return points, last_clean, None
        value = start + direction * spec.step
        if (value - spec.stop) * direction > 0:
            value = spec.stop
    while True:
        point = probe_at(value)
        points.append(point)
        if point.failed:
            first_fail = value
            break
        last_clean = value
        if value == spec.stop:
            break
        value += direction * spec.step
        if (value - spec.stop) * direction > 0:           # the last step lands on the stop, not past it
            value = spec.stop
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
class Edge:
    way: str                      # down | up
    stop: int                     # the limit the walk could reach
    clean_to: int | None          # the furthest value clean in every pass with no failure nearer the origin
    first_fail: int | None        # the nearest value that failed in any pass; None: clean through the stop
    probes: list[str] = field(default_factory=list)    # the probes that failed there
    drifting: bool = False
    per_pass: list[dict] = field(default_factory=list)

    def margin(self, origin: int) -> int | None:
        return None if self.clean_to is None else abs(self.clean_to - origin)

    def to_dict(self, default: int) -> dict:
        return {"way": self.way, "stop": self.stop, "clean_to": self.clean_to, "first_fail": self.first_fail,
                "margin": self.margin(default), "probes": self.probes, "drifting": self.drifting,
                "per_pass": self.per_pass}


def _edge_values(points: Sequence[Point], origin: int, sign: int) -> tuple[int | None, int | None]:
    side = [p for p in points if (p.value - origin) * sign >= 0]
    failed = {p.value for p in side if p.failed}
    first = min(failed, key=lambda v: (v - origin) * sign) if failed else None
    clean = {p.value for p in side if p.value not in failed and (first is None or (p.value - first) * sign < 0)}
    return (max(clean, key=lambda v: (v - origin) * sign) if clean else None), first


def edge_of(points: Sequence[Point], origin: int, way: str, stop: int, fine: int) -> Edge:
    """One side's edge from every point probed on it, all passes together:
    a value that failed in any pass is failed."""
    sign = 1 if way == "up" else -1
    clean_to, first = _edge_values(points, origin, sign)
    probes = sorted({n for p in points if p.value == first for n in p.failed_by}) if first is not None else []
    per_pass = []
    for n in sorted({p.pass_no for p in points}):
        c, f = _edge_values([p for p in points if p.pass_no == n], origin, sign)
        per_pass.append({"pass": n, "clean_to": c, "first_fail": f})
    fails = [x["first_fail"] for x in per_pass if x["first_fail"] is not None]
    some_found_it = 0 < len(fails) < len(per_pass)
    drifting = len(per_pass) > 1 and (some_found_it or (bool(fails) and max(fails) - min(fails) > fine))
    return Edge(way, stop, clean_to, first, probes, drifting, per_pass)


@dataclass
class Margin:
    knob: Knob
    probes: list[str]
    default: int
    start: int
    spec: SearchSpec
    points: list[Point]
    edges: dict[str, Edge]
    restored: bool
    seconds: float
    standard: str | None = None
    defaults: dict[str, int] = field(default_factory=dict)
    held: dict[str, int] | None = None                 # the sums held, by name (release, latch)
    limits: tuple[int, int] | None = None
    probe_info: dict[str, dict] = field(default_factory=dict)   # what a probe keeps besides counts (baselines)
    fixed: dict[str, int] = field(default_factory=dict)         # other knobs set for the whole sweep, by letter
    after: dict | None = None     # a walk that failed: the default checked after it (clean, restarted, notes)

    @property
    def probe(self) -> str:
        return "+".join(self.probes)

    @property
    def mode(self) -> str:
        return self.spec.mode

    # One edge's numbers, as a single-direction sweep reports them.
    def _only(self) -> Edge | None:
        return next(iter(self.edges.values())) if len(self.edges) == 1 else None

    @property
    def direction(self) -> int:
        edge = self._only()
        return -1 if edge is not None and edge.way == "down" else 1

    @property
    def last_clean(self) -> int | None:
        edge = self._only()
        return edge.clean_to if edge else None

    @property
    def first_fail(self) -> int | None:
        edge = self._only()
        return edge.first_fail if edge else None

    @property
    def margin(self) -> int | None:
        """How far the knob moves from its default before the first clean
        value is left behind; None when the range held no edge."""
        edge = self._only()
        if edge is None or edge.clean_to is None or edge.first_fail is None:
            return None
        return abs(edge.clean_to - self.default)

    def to_dict(self) -> dict:
        s = self.spec
        return {
            "knob": {"name": self.knob.name, "letter": self.knob.letter, "unit": self.knob.unit,
                     "group": self.knob.group},
            "mode": self.mode, "probes": self.probes, "standard": self.standard, "default": self.default,
            "start": self.start, "defaults": self.defaults, "held": self.held,
            "set": {KNOBS[l].name: v for l, v in self.fixed.items()},
            "limits": list(self.limits) if self.limits else None,
            "spec": {"stop": s.stop, "step": s.step, "fine": s.fine, "repeat": s.repeat,
                     "scan": list(s.scan) if s.scan else None, "grid": list(s.grid) if s.grid else None},
            "passes": s.passes, "restored": self.restored, "seconds": round(self.seconds, 1),
            "edges": {way: e.to_dict(self.default) for way, e in self.edges.items()},
            "probe_info": self.probe_info, "after_walk": self.after,
            "points": [p.to_dict() for p in self.points],
        }

    def describe(self) -> str:
        s = self.spec
        how = {"one": f"from {self.start} toward {s.stop} by {s.step}, fine {s.fine}",
               "both": f"both ways from {self.start} by {s.step}, fine {s.fine}"
                       + (f", limits {self.limits[0]}..{self.limits[1]}" if self.limits else ""),
               "scan": f"every {s.step} from {s.scan[0] if s.scan else ''} to {s.scan[1] if s.scan else ''}",
               "grid": f"rows {s.scan} by nS_DMADataHold {s.grid}, every {s.step}"}[self.mode]
        held = f", {' and '.join(f'{k} {v}' for k, v in self.held.items())} held" if self.held else ""
        fixed = (", " + " and ".join(f"{KNOBS[l].name} set to {v}" for l, v in self.fixed.items())
                 if self.fixed else "")
        lines = [f"{self.knob.name} ({self.probe}): {how}, {s.repeat} sample(s) a point, "
                 f"{s.passes} pass(es){held}{fixed}"]
        if self.mode == "grid":
            return "\n".join(lines + self.grid_map())
        many = s.passes > 1
        for p in sorted(self.points, key=lambda p: (p.value, p.pass_no)):
            mark = "FAIL " if p.failed else "clean"
            note = f"  ({'; '.join(p.notes)})" if p.notes else ""
            tag = f"p{p.pass_no} " if many else ""
            lines.append(f"  {tag}{p.value:5d}  {mark}  {p.failures:6d} / {p.samples:<7d}{note}")
        for way, e in self.edges.items():
            name = self.knob.name
            if e.first_fail is None:
                lines.append(f"{name}: clean {way} through {e.clean_to}; no edge within the range")
            elif e.clean_to is None:
                lines.append(f"{name}: fails at {e.first_fail}, the first value probed")
            else:
                lines.append(f"{name}: clean {way} to {e.clean_to}, fails from {e.first_fail} by {self.probe_list(e)} "
                             f"(default {self.default}: {e.margin(self.default)} {self.knob.unit} of margin)")
            if e.drifting:
                lines.append(f"  drifting between passes: " + ", ".join(
                    f"pass {x['pass']} clean to {x['clean_to']}, fails from {x['first_fail']}" for x in e.per_pass))
        if self.after and self.after.get("restarted"):
            lines.append("after the walk the default failed (" + "; ".join(self.after.get("notes") or []) + "): "
                         + ("the Teensy was restarted and the default ran clean" if self.after["clean"]
                            else "it failed again after a Teensy restart"))
        elif self.after and not self.after["clean"]:
            lines.append("after the walk the default failed, and no restart was at hand: the TeensyROM's state "
                         "may be bad for what runs next")
        if not self.restored:
            lines.append("DEFAULTS NOT RESTORED: the board still runs the last value")
        return "\n".join(lines)

    def grid_map(self) -> list[str]:
        """A grid as text: a row a nS_DMASetup value, a column a
        nS_DMADataHold value; '.' clean in every pass, 'x' failed in one,
        '!' the workload collapsed there, blank not probed."""
        cells: dict[tuple[int, int], str] = {}
        for p in self.points:
            key = (p.value, p.held.get("w"))
            mark = "!" if p.hung else "x" if p.failed else "."
            if cells.get(key) in ("!", "x") and mark == ".":
                continue
            cells[key] = "!" if "!" in (mark, cells.get(key)) else mark
        rows = sorted({e for e, _ in cells})
        cols = sorted({w for _, w in cells})
        lines = ["  nS_DMASetup \\ nS_DMADataHold " + " ".join(f"{w:3d}" for w in cols)]
        for e in rows:
            lines.append(f"  {e:5d}" + " " * 25 + " ".join(f"  {cells.get((e, w), ' ')}" for w in cols))
        return lines

    @staticmethod
    def probe_list(edge: Edge) -> str:
        return "+".join(edge.probes) or "?"


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
    """One knob, its probes, one search, scan or pair of edges; the defaults
    restored when it ends."""

    def __init__(self, dbg: DebugCommands, knob: Knob, probes: Probe | Sequence[Probe], spec: SearchSpec, *,
                 standard: str | None = None, hold: bool | None = None, fixed: dict[str, int] | None = None,
                 restart: Callable[[], None] | None = None, log=print,
                 on_point: Callable[[Point], None] | None = None):
        self.dbg = dbg
        self.knob = knob
        self.probes = list(probes) if isinstance(probes, (list, tuple)) else [probes]
        self.spec = spec
        self.standard = standard
        self.hold = (knob.letter == "e") if hold is None else hold
        self.log = log
        self.on_point = on_point
        self.restart = restart            # the Teensy restarted and back at its menu, for _after_walk
        self.defaults: dict[str, int] = {}
        self.sums: dict[str, int] | None = None
        self.low = self.high = 0
        self._now: dict[str, int] = {}
        if self.hold and knob.letter != "e":
            raise usage_error(f"only nS_DMASetup moves with sums held, not {knob.name}")
        if spec.grid is not None and knob.letter != "e":
            raise usage_error(f"a grid is nS_DMASetup by nS_DMADataHold, not {knob.name}")
        self.fixed = dict(fixed or {})
        moved = {knob.letter} | ({HELD_SUMS[n][0][1] for n in HELD_SUMS} if self.hold or spec.grid else set())
        for letter, value in self.fixed.items():
            other = KNOBS[letter]
            if letter in moved:
                raise usage_error(f"{other.name} moves with the sweep of {knob.name}; it cannot also be set")
            if not other.swept or not other.fits(value):
                raise usage_error(f"{other.name} cannot be set to {value}")
        for k in [knob] + [KNOBS[l] for l in self.fixed]:
            if k.times_dma:
                resets = [p.name for p in self.probes if getattr(p, "resets", False)]
                if resets:
                    raise usage_error(f"{k.name} times DMA: the {', '.join(resets)} probe resets the C64 at the "
                                      f"value under test, and a C128 can hang on DMA right after a reset")

    def current(self) -> int:
        return current_value(self.dbg, self.knob)

    def ordered(self) -> list[Probe]:
        """The probes in the order they are set up: the companions last,
        since a companion's baseline runs the others' workload."""
        return ([p for p in self.probes if not getattr(p, "companion", False)]
                + [p for p in self.probes if getattr(p, "companion", False)])

    # -- the board's knobs -------------------------------------------------------------

    def restore(self) -> dict[str, int]:
        """``td``, confirmed: the knobs ``td`` restores back at the defaults."""
        reply = self.dbg.restore_defaults()
        listed = parse_listing(reply)
        if "Defaults set" not in reply:
            raise SweepError(f"td not confirmed:\n{reply}")
        if self.defaults:
            wrong = {l: listed.get(l) for l, v in self.defaults.items()
                     if KNOBS[l].restored_by_td and listed.get(l) != v}
            if wrong:
                raise SweepError(f"td did not restore {wrong}; the defaults were {self.defaults}")
        self._now = dict(listed)
        return listed

    def prepare(self) -> None:
        """The defaults (``td``), the standard they are for, the held sums and
        the knob's limits."""
        reply = self.dbg.restore_defaults()
        if "Defaults set" not in reply:
            raise SweepError(f"td not confirmed:\n{reply}")
        self.defaults = parse_listing(reply)
        self._now = dict(self.defaults)
        for knob in [self.knob] + [KNOBS[l] for l in self.fixed]:
            if knob.letter not in self.defaults:
                raise SweepError(f"the timing listing has no {knob.name}; does the build carry "
                                 f"{knob.define}{', and is it a TR+' if knob.tr_plus_only else ''}?\n{reply}")
        if self.standard is None:
            m = _DEFAULTS_FOR.search(reply)
            if not m:
                raise usage_error("td did not name the standard (MinimalBoot does not): give the sweep its standard")
            self.standard = standard_of(m.group(2), m.group(1))
        held = ["latch"] if self.spec.grid is not None else list(HELD_SUMS) if self.hold else []
        if held:
            # A grid moves nS_DMADataHold itself, so only the latch stays where the defaults put it.
            self.sums = {name: sum(self.defaults[l] for l in HELD_SUMS[name][0]) for name in held}
        at_default = self.setting_for(self.defaults[self.knob.letter])
        for letter, value in self.fixed.items():
            at = {**self.defaults, **{o: x for o, x in at_default.items() if o != letter}}
            low, high = knobs.limits(KNOBS[letter], self.standard, at)
            if not low <= value <= high:
                raise usage_error(f"{KNOBS[letter].name} {value} is past its limits {low}..{high} for "
                                  f"{self.standard} with {self.knob.name} at its default")
        self.low, self.high = self._range()

    def setting_for(self, value: int) -> dict[str, int]:
        setting = {**self.fixed, self.knob.letter: value}
        for name, total in (self.sums or {}).items():
            setting[HELD_SUMS[name][0][1]] = total - value
        return setting

    def allowed(self, value: int) -> bool:
        return self.allowed_setting(self.setting_for(value))

    def allowed_setting(self, setting: dict[str, int]) -> bool:
        for letter, v in setting.items():
            # Each knob's limits follow the others' values; its own counts at its default.
            at = {**self.defaults, **{other: x for other, x in setting.items() if other != letter}}
            low, high = knobs.limits(KNOBS[letter], self.standard, at)
            if not (KNOBS[letter].fits(v) and low <= v <= high):
                return False
        return True

    def _range(self) -> tuple[int, int]:
        """The run of allowed values around the default."""
        default = self.defaults[self.knob.letter]
        if not self.allowed(default):
            raise usage_error(f"the board's default {self.knob.name} {default} is outside the knob table's limits "
                              f"for {self.standard}: check treadmill.knobs against this build")
        low = high = default
        while self.allowed(low - 1):
            low -= 1
        while self.allowed(high + 1):
            high += 1
        return low, high

    def check(self, value: int, what: str) -> None:
        if not self.low <= value <= self.high:
            raise usage_error(f"{what} {self.knob.name} {value} is past the sweep limits {self.low}..{self.high} "
                              f"for {self.standard}" + (" with the sums held" if self.sums else ""))

    def apply(self, setting: dict[str, int]) -> None:
        """Set each knob of ``setting``, the ones going down first so a held
        sum never passes its value on the way, and check the listing."""
        order = sorted(setting, key=lambda l: setting[l] - self._now.get(l, setting[l]))
        reply = ""
        for letter in order:
            reply = self.dbg.set_knob(letter, setting[letter])
        got = parse_listing(reply)
        wrong = {l: got.get(l) for l, v in setting.items() if got.get(l) != v}
        if wrong:
            raise SweepError(f"{setting} not taken: the board lists {wrong}\n{reply}")
        self._now.update(got)

    # -- points ------------------------------------------------------------------------

    def probe_at(self, value: int, pass_no: int = 1, setting: dict[str, int] | None = None) -> Point:
        t0 = time.monotonic()
        setting = setting or self.setting_for(value)
        self.apply(setting)
        handle = Setting(self, setting, bracket=any(KNOBS[l].readback_at_defaults for l in setting))
        label = self.knob.format(value)[1:]
        main = [p for p in self.probes if not getattr(p, "companion", False)]
        companions = [p for p in self.probes if getattr(p, "companion", False)]
        failures = samples = 0
        notes: list[str] = []
        by_probe: dict[str, list[int]] = {}
        failed_by: list[str] = []
        hung = False

        def add(name: str, got) -> None:
            nonlocal failures, samples, hung
            s = Sample(*got)
            failures += s.failures
            samples += s.samples
            counts = by_probe.setdefault(name, [0, 0])
            counts[0] += s.failures
            counts[1] += s.samples
            if s.failures and name not in failed_by:
                failed_by.append(name)
            hung = hung or s.hung
            note = s.note if len(self.probes) == 1 or not s.note else f"{name}: {s.note}"
            if note and note not in notes:
                notes.append(note)

        for _ in range(self.spec.repeat):
            for c in companions:
                c.begin(label)
            try:
                for probe in main:
                    add(probe.name, probe.sample(label, handle))
            finally:
                for c in companions:
                    add(c.name, c.end())
        if not handle.bracket:
            # A firmware that re-applies its defaults (a standard change, a relaunch) would leave
            # the samples taken at the wrong value: count the point as failed rather than clean.
            listed = parse_listing(self.dbg.listing())
            moved = {l: listed.get(l) for l, v in setting.items() if listed.get(l) != v}
            if moved:
                failures, samples = failures + 1, samples + 1
                notes.append(f"the board moved {moved} during the samples")
                self.apply(setting)
        held = {l: v for l, v in setting.items() if l != self.knob.letter}
        point = Point(value, failures, samples, time.monotonic() - t0, notes, by_probe, failed_by, hung, pass_no,
                      held)
        self.log(f"[sweep] {self.knob.name} {value}" + (f" (pass {pass_no})" if self.spec.passes > 1 else "")
                 + f": {'FAIL' if point.failed else 'clean'} {failures}/{samples}"
                 + (f" ({'; '.join(notes)})" if notes else ""))
        if self.on_point:
            self.on_point(point)
        return point

    # -- the modes ---------------------------------------------------------------------

    def _both(self, start: int, pass_no: int) -> list[Point]:
        points = [self.probe_at(start, pass_no)]
        if points[0].failed:
            self.log(f"[sweep] {self.knob.name} fails at {start} itself: no edges to look for")
            return points
        ways = ("down", "up") if pass_no % 2 else ("up", "down")
        for way in ways:
            stop = self.low if way == "down" else self.high
            if stop == start:
                continue
            found, _, _ = search(lambda v: self.probe_at(v, pass_no), start, replace(self.spec, stop=stop),
                                 probe_start=False)
            points += found
        return points

    def _grid(self, pass_no: int) -> list[Point]:
        """A row a nS_DMASetup value, a column a nS_DMADataHold value, the
        latch held; rows run in turn up and down (and the other way round on
        an even pass), and a row stops after two collapses in a row past its
        clean cells. Cells past the limits are left out."""
        latch = self.sums["latch"]
        cols = steps(*self.spec.grid, self.spec.step)
        points: list[Point] = []
        for i, e in enumerate(steps(*self.spec.scan, self.spec.step)):
            order = cols if (i + pass_no) % 2 else cols[::-1]
            collapses, seen_clean = 0, False
            for w in order:
                setting = {**self.fixed, "e": e, "w": w, "y": latch - e}
                if not self.allowed_setting(setting):
                    continue
                point = self.probe_at(e, pass_no, setting)
                points.append(point)
                seen_clean = seen_clean or not point.failed
                collapses = collapses + 1 if point.hung else 0
                if collapses >= 2 and seen_clean:
                    self.log(f"[sweep] two collapses in a row: row {e} of pass {pass_no} stops at {w}")
                    break
        return points

    def _scan(self, pass_no: int) -> list[Point]:
        low, high = self.spec.scan
        values = steps(low, high, self.spec.step)
        if pass_no % 2 == 0:
            values.reverse()
        points: list[Point] = []
        collapses, seen_clean = 0, False
        for value in values:
            point = self.probe_at(value, pass_no)
            points.append(point)
            seen_clean = seen_clean or not point.failed
            collapses = collapses + 1 if point.hung else 0
            if collapses >= 2 and seen_clean:
                self.log(f"[sweep] two collapses in a row past the clean values: pass {pass_no} stops at {value}")
                break
        return points

    def _start(self, probe: Probe, started: list[Probe]) -> None:
        """``probe`` set up and added to ``started``. A setup can fail with
        its program already launched, so a failed one is torn down at once:
        the caller then puts its settings back through the menu."""
        try:
            probe.setup()
        except BaseException:
            try:
                probe.teardown()
            except Exception as e:  # noqa: BLE001 - the setup's failure is the one to report
                self.log(f"[sweep] {probe.name}: teardown after its failed setup: {e}")
            raise
        started.append(probe)

    def _early(self, checks: Callable[[], None]) -> list[Probe]:
        """The probes that change the image the knobs belong to (a hand-off
        to MinimalBoot) set up first, then the defaults read and ``checks``
        run on them; if any of that fails, those probes are torn down again,
        so nothing is left in the other image."""
        started: list[Probe] = []
        try:
            for probe in self.ordered():
                if getattr(probe, "changes_image", False):
                    self._start(probe, started)
            self.prepare()
            checks()
        except BaseException:
            for probe in reversed(started):
                probe.teardown()
            raise
        return started

    def spot(self, values: Sequence[int]) -> list[Point]:
        """Each value once (``spec.repeat`` samples a probe), within the
        limits, and the defaults back after: a floor checked directly."""
        def checks() -> None:
            for value in values:
                self.check(value, "a spot value")

        started = self._early(checks)
        points: list[Point] = []
        try:
            for probe in self.ordered():
                if probe not in started:
                    self._start(probe, started)
            points = [self.probe_at(value) for value in values]
        finally:
            try:
                self.restore()
            finally:
                for probe in reversed(started):
                    probe.teardown()
        return points

    def _checks(self) -> None:
        """The spec against the limits the defaults give."""
        spec = self.spec
        default = self.defaults[self.knob.letter]
        self.check(default if spec.start is None else spec.start, "the start")
        if spec.mode == "one":
            if spec.stop is None:
                self.spec = spec = replace(spec, stop=self.high)
            self.check(spec.stop, "the stop")
        elif spec.mode == "grid":
            self.check(spec.scan[0], "the grid's first row")
            self.check(spec.scan[1], "the grid's last row")
            latch = self.sums["latch"]
            if not any(self.allowed_setting({**self.fixed, "e": e, "w": w, "y": latch - e})
                       for e in steps(*spec.scan, spec.step) for w in steps(*spec.grid, spec.step)):
                raise usage_error(f"no cell of the grid {spec.scan} by nS_DMADataHold {spec.grid} is inside the "
                                  f"limits")
        elif spec.mode == "scan":
            self.check(spec.scan[0], "the scan's low end")
            self.check(spec.scan[1], "the scan's high end")
            if not spec.scan[0] <= default <= spec.scan[1]:
                raise usage_error(f"the scan {spec.scan[0]}:{spec.scan[1]} must take in the default {default}: "
                                  f"the edges are measured from it")

    def _after_walk(self, started: list[Probe]) -> dict:
        """After a walk that failed somewhere: the defaults back, one point at
        the default. A failure there is the TeensyROM's own state left bad,
        not the knob (an IO1 read past its edge garbled the menu until the
        Teensy restarted, backlog 53), and the next sweep would fail at its
        default too: restart the Teensy, set the probes up again, and check
        once more."""
        default = self.defaults[self.knob.letter]
        at_defaults = {letter: self.defaults[letter] for letter in self.setting_for(default)}
        self.restore()
        first = self.probe_at(default, 0, at_defaults)
        if not first.failed:
            return {"clean": True, "restarted": False, "notes": []}
        if self.restart is None:
            self.log(f"[sweep] {self.knob.name} fails at its default after the walk, and no restart is at hand")
            return {"clean": False, "restarted": False, "notes": first.notes}
        self.log(f"[sweep] {self.knob.name} fails at its default after the walk; restarting the Teensy")
        try:
            while started:
                started.pop().teardown()
            self.restart()
            for probe in self.ordered():
                self._start(probe, started)
            self.restore()
        except TreadmillError as e:          # the walk's record stands; what the restart left is said in it
            self.log(f"[sweep] the restart after the walk failed: {e}")
            return {"clean": False, "restarted": False, "notes": first.notes + [f"the restart failed: {e}"]}
        again = self.probe_at(default, 0, at_defaults)
        return {"clean": not again.failed, "restarted": True, "notes": first.notes + again.notes}

    def run(self) -> Margin:
        t0 = time.monotonic()
        started = self._early(self._checks)
        spec = self.spec
        default = self.defaults[self.knob.letter]
        start = default if spec.start is None else spec.start
        points: list[Point] = []
        after = None
        restored = False
        try:
            for probe in self.ordered():
                if probe not in started:
                    self._start(probe, started)
            if spec.mode == "one":
                points, _, _ = search(lambda v: self.probe_at(v, 1), start, spec)
            elif spec.mode == "both":
                for n in range(1, spec.passes + 1):
                    points += self._both(start, n)
            elif spec.mode == "grid":
                for n in range(1, spec.passes + 1):
                    points += self._grid(n)
            else:
                for n in range(1, spec.passes + 1):
                    points += self._scan(n)
            if any(p.failed for p in points):
                after = self._after_walk(started)
        finally:
            try:
                self.restore()
                restored = True
            except SweepError as e:
                self.log(f"[sweep] {e}")
            finally:
                for probe in reversed(started):
                    probe.teardown()
        if spec.mode == "one":
            way = "up" if spec.stop >= start else "down"
            edges = {way: edge_of(points, start, way, spec.stop, spec.fine)}
        elif spec.mode == "grid":
            edges = {}                   # a grid is read as a map, not along one line
        else:
            origin = start if spec.mode == "both" else default
            low, high = (self.low, self.high) if spec.mode == "both" else spec.scan
            edges = {way: edge_of(points, origin, way, stop, spec.fine)
                     for way, stop in (("down", low), ("up", high)) if stop != origin}
        info = {p.name: p.info() for p in self.probes if hasattr(p, "info")}
        margin = Margin(self.knob, [p.name for p in self.probes], default, start, spec, points, edges, restored,
                        time.monotonic() - t0, self.standard, dict(self.defaults), self.sums, (self.low, self.high),
                        info, dict(self.fixed), after)
        if not restored:
            raise SweepError("td not confirmed: the board is still running the last value\n" + margin.describe())
        return margin
