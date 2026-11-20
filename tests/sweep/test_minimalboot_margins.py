"""Test 23: the CPU-cycle margins under MinimalBoot, each way. The main image
hands an EasyFlash cartridge with a chip in bank 64 or above to MinimalBoot,
the small image that boots fast and serves large cartridges, and MinimalBoot
times its bus cycles with its own copies of the knobs: ``nS_RWnReady``,
``nS_PLAprop``, ``nS_DataSetup``, ``nS_DataHold`` and ``nS_MaxAdj``, set by
its own ``t`` (a build with ``Dbg_SerTimChg`` patches both images' headers).
MinimalBoot refuses DMA, so idread is built with SHOW and its counters are
read off the screen by video: any TR with a capture. The defaults are
MinimalBoot's own, read after the hand-off; its ``td`` names no standard, so
the sweep is given the one the main image reported. The edges go through
margin_check, recorded as the minimal image's.

USB load (version requests as fast as the board answers, while idread
counts) makes the board's interrupts arrive late, which ``nS_MaxAdj``'s
upper edge needs and which PR #103's MinimalBoot errors came with: here it
works on any TR, since the counters are read by video. ``nS_MaxAdj`` is swept
twice, without load and under it, and each other knob's default is checked
once more under load after its sweep. The loaded sweep is recorded but not
held to the station's floor, which is for the knob without load."""

import pytest

from treadmill.knobs import KNOBS, standard_of
from treadmill.probes.idread import MinimalIdReadProbe
from treadmill.sweeps import SearchSpec, Sweep

pytestmark = [pytest.mark.needs("video"), pytest.mark.needs_fw("Dbg_SerTimChg"), pytest.mark.timing_sensitive,
              pytest.mark.duration(7)]

MINIMAL_KNOBS = ["r", "p", "s", "h", "m"]
CASES = [(letter, False) for letter in MINIMAL_KNOBS] + [("m", True)]


@pytest.mark.parametrize("letter, load", CASES,
                         ids=[KNOBS[l].name + ("-usbload" if load else "") for l, load in CASES])
def test_minimalboot_margin(letter, load, at_menu, dbg, screen, programs, request, pace, margin_check):
    board = at_menu
    knob = KNOBS[letter]
    request.getfixturevalue("sd_card")
    standard = standard_of(board.banner.machine, board.banner.video)
    shown = programs("idread", SHOW=1)
    probe = MinimalIdReadProbe(board, screen, shown, dwell=pace.video_dwell, usb_load=load, log=print)
    step = pace.slow_step * (2 if letter == "m" else 1)      # nS_MaxAdj's range is about 350 ns wide
    margin = Sweep(dbg, knob, probe, SearchSpec(both=True, step=step, fine=pace.slow_fine), standard=standard,
                   log=print).run()
    print(margin.describe())
    assert not margin.points[0].failed, (f"the default {knob.name} {margin.default} fails under MinimalBoot: "
                                         f"{'; '.join(margin.points[0].notes)}")
    if letter != "m":
        loaded = MinimalIdReadProbe(board, screen, shown, dwell=pace.video_dwell, usb_load=True, log=print)
        point, = Sweep(dbg, knob, loaded, SearchSpec(), standard=standard, log=print).spot([margin.default])
        assert not point.failed, (f"the default {knob.name} {margin.default} fails under MinimalBoot with USB load: "
                                  f"{point.notes}")
    margin_check(margin, board, image="minimal", floor=not load)
