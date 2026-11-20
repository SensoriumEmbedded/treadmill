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


@pytest.fixture
def pace() -> Pace:
    """A board: a second of counting, three samples a point, since an edge
    wanders a few ns between runs (one wrong read in 24,064 at 260, none the
    next). The fake: a tenth, one sample; its counts are a rate and its
    edges hold still, so the result is the same in a tenth of the time."""
    return Pace(0.1, 1) if fake_requested() else Pace(1.0, 3)


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
