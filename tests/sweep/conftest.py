"""What every margin test shares: the pace of its samples, and the record,
the floor and the shrink check."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from treadmill import margins
from treadmill.session import fake_requested
from treadmill.sweeps import Margin


@dataclass(frozen=True)
class Pace:
    dwell: float                  # idread: seconds of counting a sample
    repeat: int                   # samples a point
    z_pages: int                  # z###: 256-byte pages of each pattern a sample
    loop_dwell: float             # reuloop: seconds of the C64's REU loop a sample
    video_dwell: float            # vicrom and idread under MinimalBoot: seconds of video a sample
    kr_dwell: float               # krread: seconds of HIRAM toggling a sample
    slow_step: int                # the DMA and VIC knobs, whose points take seconds: coarse step, bisection grain
    slow_fine: int
    fine: int = 5                 # idread's CPU-cycle sweeps: the bisection grain
    slow_repeat: int = 1          # the slow knobs' samples a point


@pytest.fixture
def pace(request) -> Pace:
    """A board: a second of idread counting and three samples a point, since
    an edge wanders a few ns between runs (one wrong read in 24,064 at 260,
    none the next); z100, about 20 s; ten seconds of the REU loop, about
    1,400 rounds, as PR #99's sweeps ran; two seconds of video for vicrom
    and for idread under MinimalBoot, and of HIRAM toggling for krread;
    the DMA and VIC knobs by 10 ns, bisected to 5. The fake: a fraction of
    each, one sample, and twice the steps, since every DMA point costs about
    ten commands; its counts are a rate and its edges hold still, so the
    result is the same in far less time.

    ``--margin-fine NS`` bisects every edge to NS ns instead, with three
    samples a point on every knob: 5 ns is enough for a build whose waits
    move in steps of about 12 ns, but a wait that converts to cycles once
    lands within about 4 ns, and 2 resolves it."""
    pace = Pace(0.1, 1, 4, 0.2, 0.2, 0.3, 20, 10) if fake_requested() else Pace(1.0, 3, 100, 10.0, 2.0, 2.0, 10, 5)
    fine = request.config.getoption("--margin-fine")
    if fine is None:
        return pace
    if fine < 1:
        raise pytest.UsageError(f"--margin-fine {fine}: give 1 ns or more")
    return replace(pace, fine=fine, slow_fine=fine, repeat=max(pace.repeat, 3), slow_repeat=3)


@pytest.fixture
def restart(board):
    """The Teensy restarted through the USB reboot, for a sweep whose walk
    left the TeensyROM's own state bad (``Sweep(restart=...)``, backlog 53);
    None when the run does not know the hex the board carries."""
    return board.restart_teensy if board.reboot_hook else None


@pytest.fixture
def margin_check(station, firmware, run_dir, artifacts, warn):
    """Writes a sweep's ``margins.json`` and holds it to the station: an edge
    short of the station's floor for that direction fails the test, and one
    more than ``margins.shrink_limit`` inside the newest earlier record of
    the same station, knob, edge and probes on another build is a warning.
    A default that still fails after the walk, with the Teensy restarted
    or no restart at hand, fails the test: what runs next would fail too.
    ``floor=False`` for a sweep the knob's floor is not for (one under USB
    load). Returns the record."""

    def check(margin: Margin, board, *, image: str = "main", name: str | None = None, floor: bool = True) -> dict:
        record = margins.build(margin.to_dict(), margins.context_for(board, firmware.record, image=image))
        folder = artifacts / name if name else artifacts
        path = margins.write(folder, record)
        earlier = [r for r in margins.load(run_dir.path.parent) if r.get("_path") != str(path)]
        for text in margins.shrinks(record, earlier):
            warn(text)
        assert margin.restored, "td not confirmed: the board still runs the last value"
        assert (margin.after or {}).get("clean", True), (f"{margin.knob.name} fails at its default after the walk: "
                                                         + "; ".join(margin.after.get("notes") or []))
        problems = margins.floor_problems(record, station.margins.get(margin.knob.name) if floor else None)
        assert not problems, "\n".join(problems) + "\n" + margin.describe()
        return record

    return check
