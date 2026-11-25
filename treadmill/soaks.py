"""Soaks: a probe run for minutes at the values the build ships with, every count kept.

A sweep's sample lasts seconds. An acceptance soak runs each probe for ten
minutes or more at the board's own timing values and must find nothing
wrong. The C64-side counters the probes read count rounds or passes in 16
bits: the REU client's wrap about every 460 s at 142 rounds a second, so a
soak that read them only at its start and end would lose whole wraps. A
soak reads them every ``interval`` seconds instead and adds up the
differences, each taken modulo its counter's width, which unwraps them as
long as an interval is shorter than a wrap.

A probe whose counters can be read that way (``counters``, ``workload`` and
``delta``: idread, the REU loop, cpuwatch) is read back to back, each
reading the start of the next interval, so no round goes uncounted. Any
other probe is sampled one interval at a time. A hang is counted, the probe
is relaunched, and the soak goes on to its end.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from treadmill.sweeps import Probe, Sample, SweepError


@dataclass
class Interval:
    at: float                     # seconds into the soak when it ended
    failures: int
    samples: int
    hung: bool = False
    note: str | None = None

    def to_dict(self) -> dict:
        return {"at": round(self.at, 1), "failures": self.failures, "samples": self.samples, "hung": self.hung,
                "note": self.note}


@dataclass
class Soak:
    probe: str
    seconds: float                # asked for
    interval: float
    intervals: list[Interval] = field(default_factory=list)
    info: dict = field(default_factory=dict)

    @property
    def failures(self) -> int:
        return sum(i.failures for i in self.intervals if not i.hung)

    @property
    def samples(self) -> int:
        return sum(i.samples for i in self.intervals if not i.hung)

    @property
    def hangs(self) -> int:
        return sum(i.hung for i in self.intervals)

    @property
    def clean(self) -> bool:
        return not self.failures and not self.hangs and bool(self.samples)

    def to_dict(self) -> dict:
        return {"probe": self.probe, "seconds": self.seconds, "interval": self.interval,
                "ran": round(self.intervals[-1].at, 1) if self.intervals else 0.0, "failures": self.failures,
                "samples": self.samples, "hangs": self.hangs, "clean": self.clean, "probe_info": self.info,
                "intervals": [i.to_dict() for i in self.intervals]}

    def describe(self) -> str:
        ran = self.intervals[-1].at if self.intervals else 0.0
        head = (f"{self.probe}: {ran:.0f} s in {len(self.intervals)} interval(s) of {self.interval:g} s: "
                f"{self.failures} wrong of {self.samples}" + (f", {self.hangs} hang(s)" if self.hangs else ""))
        lines = [head]
        for i in self.intervals:
            if i.failures or i.hung:
                lines.append(f"  at {i.at:7.1f} s: {'HUNG' if i.hung else f'{i.failures} / {i.samples}'}"
                             + (f"  ({i.note})" if i.note else ""))
        return "\n".join(lines)


def _gapless(probe) -> bool:
    return all(hasattr(probe, name) for name in ("counters", "workload", "delta"))


def _counters(probe) -> tuple | str:
    """The probe's counters, or why they did not read: counters read off the
    screen can fail to read whole, which the soak counts as a hang."""
    try:
        return probe.counters()
    except SweepError as e:
        return f"the counters do not read: {e}"


def soak(probe: Probe, seconds: float, interval: float = 10.0, *, log=print,
         on_interval: Callable[[Interval], None] | None = None) -> Soak:
    """``probe`` for ``seconds``, an interval at a time, at the board's own
    values; set up first and torn down after, whatever happens."""
    if interval <= 0 or seconds <= 0:
        raise ValueError("a soak needs a positive length and interval")
    probe.dwell = interval
    result = Soak(probe.name, seconds, interval)
    try:                                       # a setup that launched and then failed is torn down too
        probe.setup()
        t0 = time.monotonic()
        gapless = _gapless(probe)
        before = _counters(probe) if gapless else None
        while time.monotonic() - t0 < seconds:
            if gapless:
                if not isinstance(before, str):
                    probe.workload()
                now = before if isinstance(before, str) else _counters(probe)
                if isinstance(now, str):
                    got = Sample(1, 1, now, hung=True)
                else:
                    got = Sample(*probe.delta(before, now))
                    before = now
            else:
                got = Sample(*probe.sample(f"soak {len(result.intervals) + 1}"))
            step = Interval(time.monotonic() - t0, got.failures, got.samples, got.hung, got.note)
            result.intervals.append(step)
            log(f"[soak] {probe.name} {step.at:7.1f} s: "
                + ("HUNG" if step.hung else f"{step.failures} / {step.samples}") + (f" ({step.note})" if step.note else ""))
            if on_interval:
                on_interval(step)
            if step.hung and gapless:
                log(f"[soak] {probe.name} hung; relaunching")
                probe.teardown()
                probe.setup()
                before = _counters(probe)
        if hasattr(probe, "info"):
            result.info = probe.info()
    finally:
        probe.teardown()
    return result
