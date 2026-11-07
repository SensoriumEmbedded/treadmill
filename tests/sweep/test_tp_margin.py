"""Test 20, first cut: the read margin of nS_PLAprop. The knob walks up from
its default until the C64's reads of TeensyROM registers go wrong, the edge
is bisected, the defaults come back, and the margin is recorded; a station
that names a floor for the knob fails the test when the edge comes early.
The probe is idread (counters by DMA) on a TR+ with a card, else the menu
compared with its golden after each reset."""

import json

import pytest

from treadmill.sweeps import KNOBS, IdReadProbe, MenuProbe, SearchSpec, Sweep, current_value

pytestmark = [pytest.mark.needs_fw("Dbg_SerTimChg"), pytest.mark.timing_sensitive, pytest.mark.duration(8)]

RANGE_ABOVE_DEFAULT = 120
STEP, FINE = 10, 5


def test_tp_margin(at_menu, dbg, screen, programs, goldens, artifacts, station, request):
    board = at_menu
    knob = KNOBS["p"]
    if board.station.has("dma"):
        request.getfixturevalue("sd_card")
        probe = IdReadProbe(board, programs("idread"), dwell=1.0, log=print)
    else:
        probe = MenuProbe(board, screen, goldens.load("menu/root"), artifacts)
    default = current_value(dbg, knob)
    margin = Sweep(dbg, knob, probe, SearchSpec(stop=default + RANGE_ABOVE_DEFAULT, step=STEP, fine=FINE),
                   log=print).run()
    (artifacts / "margin.json").write_text(json.dumps(margin.to_dict(), indent=2), encoding="utf-8")
    print(margin.describe())
    assert margin.restored
    assert not margin.points[0].failed, f"the default {knob.name} {default} fails the {probe.name} probe"
    floor = station.margins.get(knob.name)
    if floor is not None:
        clean_to = margin.last_clean if margin.first_fail is not None else margin.spec.stop
        assert clean_to is not None and clean_to >= floor, (
            f"{knob.name} reads fail from {margin.first_fail}; the station's floor is {floor}")
