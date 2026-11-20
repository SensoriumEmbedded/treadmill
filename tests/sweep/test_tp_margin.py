"""Test 20, first cut: the read margin of nS_PLAprop, both ways. Raised, the
knob walks until the C64's reads of TeensyROM-served bytes come too late;
lowered, until the select lines are sampled before they assert, which it can
do only down to nS_RWnReady (below it the wait returns at once). Each edge is
bisected, the defaults come back, and each edge goes in a margins.json; an
edge short of the station's floor for it fails the test, and one that shrank
against an earlier build is a warning. The probe is idread (a cartridge counting its ROM and IO2 reads apart,
by DMA) on a TR+ with a card, else the menu compared with its golden after
each reset."""

import pytest

from treadmill.probes import IdReadProbe, MenuProbe
from treadmill.sweeps import KNOBS, SearchSpec, Sweep, current_value

pytestmark = [pytest.mark.needs_fw("Dbg_SerTimChg"), pytest.mark.timing_sensitive, pytest.mark.duration(3)]

RANGE_ABOVE_DEFAULT = 120
STEP, FINE = 10, 5
STEP_DOWN = 5             # the room below the default is the gap to nS_RWnReady, 15 ns at the defaults
REPEAT = 3                # the edge wanders a few ns between runs: one wrong read in 24,064 at 260, none the next


def test_tp_margin(at_menu, dbg, screen, programs, goldens, artifacts, request, margin_check):
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
    print(up.describe())
    assert not up.points[0].failed, f"the default {knob.name} {default} fails the {probe.name} probe"
    margin_check(up, board, name="up")
    if lowest < default:
        down = Sweep(dbg, knob, probe, SearchSpec(stop=lowest, step=STEP_DOWN, fine=FINE, repeat=REPEAT),
                     log=print).run()
        print(down.describe())
        margin_check(down, board, name="down")
