"""What every margin test shares: the record, the floor and the shrink check."""

from __future__ import annotations

import pytest

from treadmill import margins
from treadmill.sweeps import Margin


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
