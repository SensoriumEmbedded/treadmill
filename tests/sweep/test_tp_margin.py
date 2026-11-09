"""Test 20, first cut: the read margin of nS_PLAprop, both ways. Raised, the
knob walks until the C64's reads of TeensyROM-served bytes come too late;
lowered, until the select lines are sampled before they assert, which it can
do only down to nS_RWnReady (below it the wait returns at once). Each edge is
bisected, the defaults come back, and the margins are recorded; a station
that names a floor for the knob fails the test when the upper edge comes
early. The probe is idread (a cartridge counting its ROM and IO2 reads apart,
by DMA) on a TR+ with a card, else the menu compared with its golden after
each reset."""

import json

import pytest

from treadmill.sweeps import KNOBS, IdReadProbe, MenuProbe, SearchSpec, Sweep, current_value

pytestmark = [pytest.mark.needs_fw("Dbg_SerTimChg"), pytest.mark.timing_sensitive, pytest.mark.duration(3)]

RANGE_ABOVE_DEFAULT = 120
STEP, FINE = 10, 5
STEP_DOWN = 5             # the room below the default is the gap to nS_RWnReady, 15 ns at the defaults
REPEAT = 3                # the edge wanders a few ns between runs: one wrong read in 24,064 at 260, none the next


def test_tp_margin(at_menu, dbg, screen, programs, goldens, artifacts, station, request):
    board = at_menu
    knob = KNOBS["p"]
    if board.station.has("dma"):
        request.getfixturevalue("sd_card")
        probe = IdReadProbe(board, programs("idread"), dwell=1.0, log=print)
    else:
        probe = MenuProbe(board, screen, goldens.load("menu/root"), artifacts)
    default = current_value(dbg, knob)
    lowest = current_value(dbg, KNOBS["r"])
    up = Sweep(dbg, knob, probe, SearchSpec(stop=default + RANGE_ABOVE_DEFAULT, step=STEP, fine=FINE, repeat=REPEAT),
               log=print).run()
    down = None
    if lowest < default:
        down = Sweep(dbg, knob, probe, SearchSpec(stop=lowest, step=STEP_DOWN, fine=FINE, repeat=REPEAT),
                     log=print).run()
    record = {"up": up.to_dict(), "down": down.to_dict() if down else None, "lowest": lowest}
    (artifacts / "margin.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(up.describe())
    if down:
        print(down.describe())
    assert up.restored and (down is None or down.restored)
    assert not up.points[0].failed, f"the default {knob.name} {default} fails the {probe.name} probe"
    floor = station.margins.get(knob.name)
    if floor is not None:
        clean_to = up.last_clean if up.first_fail is not None else up.spec.stop
        assert clean_to is not None and clean_to >= floor, (
            f"{knob.name} reads fail from {up.first_fail}; the station's floor is {floor}")
