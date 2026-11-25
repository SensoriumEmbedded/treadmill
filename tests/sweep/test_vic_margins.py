"""Test 21: the margins of the VIC-cycle knobs, each way. Serving an Ultimax
cartridge, the TeensyROM answers the VIC's reads of ROMH as well as the
CPU's: in Phi2's low half it samples the address at ``nS_VICStart`` after
Phi2 falls and drives the byte until ``nS_VICDHold``. Too early a sample
reads the address before the VIC has put it out; a release too early loses
the byte, too late drives into the CPU's half (Ultimax cartridges crash on
NTSC with a higher ``nS_VICDHold``). The probe is vicrom, an Ultimax
cartridge whose charset the VIC fetches from ROMH, judged by the picture
against a reference taken at the defaults, so it needs video and not DMA:
it runs on a plain TR too. Each knob walks down and up to the knob table's
limits, each edge is bisected, the defaults come back, and the edges go in
``margins.json``; one short of the station's floor fails the test, one that
shrank against an earlier build is a warning."""

import pytest

from treadmill.knobs import KNOBS
from treadmill.probes.vicrom import VicRomProbe
from treadmill.sweeps import SearchSpec, Sweep

pytestmark = [pytest.mark.needs("video"), pytest.mark.needs_fw("Dbg_SerTimChg"), pytest.mark.timing_sensitive,
              pytest.mark.duration(4)]

# The two knobs take the same path; nS_VICDHold differs only in the knob.
CASES = [pytest.param("v", id=KNOBS["v"].name), pytest.param("i", id=KNOBS["i"].name, marks=pytest.mark.alike)]


@pytest.mark.parametrize("letter", CASES)
def test_vic_margin(letter, at_menu, dbg, video, calibration, programs, request, pace, margin_check):
    board = at_menu
    knob = KNOBS[letter]
    request.getfixturevalue("sd_card")
    probe = VicRomProbe(board, video, calibration, programs("vicrom"), dwell=pace.video_dwell, log=print)
    spec = SearchSpec(both=True, step=pace.slow_step, fine=pace.slow_fine, repeat=pace.slow_repeat)
    margin = Sweep(dbg, knob, probe, spec, log=print).run()
    print(margin.describe())
    assert not margin.points[0].failed, (f"the default {knob.name} {margin.default} fails: "
                                         f"{'; '.join(margin.points[0].notes)}")
    margin_check(margin, board)
