"""What every margin test shares: the pace of its samples, and the record,
the floor and the shrink check."""

from __future__ import annotations

from dataclasses import dataclass

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
    vic_dwell: float              # vicrom: seconds of frames a sample
    kr_dwell: float               # krread: seconds of HIRAM toggling a sample
    slow_step: int                # the DMA and VIC knobs, whose points take seconds: coarse step, bisection grain
    slow_fine: int


@pytest.fixture
def pace() -> Pace:
    """A board: a second of idread counting and three samples a point, since
    an edge wanders a few ns between runs (one wrong read in 24,064 at 260,
    none the next); z100, about 20 s; ten seconds of the REU loop, about
    1,400 rounds, as PR #99's sweeps ran; two seconds of frames for vicrom
    and of HIRAM toggling for krread;
    the DMA and VIC knobs by 10 ns, bisected to 5. The fake: a fraction of
    each, one sample, and twice the steps, since every DMA point costs about
    ten commands; its counts are a rate and its edges hold still, so the
    result is the same in far less time."""
    if fake_requested():
        return Pace(0.1, 1, 4, 0.2, 0.3, 0.3, 20, 10)
    return Pace(1.0, 3, 100, 10.0, 2.0, 2.0, 10, 5)


@pytest.fixture
def margin_check(station, firmware, run_dir, artifacts, warn):
    """Writes a sweep's ``margins.json`` and holds it to the station: an edge
    short of the station's floor for that direction fails the test, and one
    more than ``margins.shrink_limit`` inside the newest earlier record of
    the same station, knob, edge and probes on another build is a warning.
    Returns the record."""

    def check(margin: Margin, board, *, image: str = "main", name: str | None = None) -> dict:
        record = margins.build(margin.to_dict(), margins.context_for(board, firmware.record, image=image))
        folder = artifacts / name if name else artifacts
        path = margins.write(folder, record)
        earlier = [r for r in margins.load(run_dir.path.parent) if r.get("_path") != str(path)]
        for text in margins.shrinks(record, earlier):
            warn(text)
        assert margin.restored, "td not confirmed: the board still runs the last value"
        problems = margins.floor_problems(record, station.margins.get(margin.knob.name))
        assert not problems, "\n".join(problems) + "\n" + margin.describe()
        return record

    return check
