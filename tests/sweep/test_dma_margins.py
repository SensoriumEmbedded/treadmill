"""Test 26: the margins of the DMA knobs, each way. When the TeensyROM takes
the bus it counts from the Phi2-falling latch: /DMA asserted at
``nS_DMAAssert``, BA sampled at ``nS_DMABAWait`` to steal the cycle or skip
it, R/W and the address driven at ``nS_DMASetup`` before Phi2 rises, then
from that re-latch a write's data released at ``nS_DMADataHold`` and a
read's latched at ``nS_DMADataSetup``. ``nS_DMASetup`` moves with the
release and the latch held where the defaults put them, as PR #99 swept
it. Each knob walks down and up to the knob table's limits, each edge is
bisected, the defaults come back, and the edges go in ``margins.json``: one
short of the station's floor fails the test, one that shrank against an
earlier build is a warning.

The probes: the firmware's own DMA pattern test (``z###``), the C64's REU
loop (with all eight sprites on for ``nS_DMABAWait``, so the DMA waits for
BA) or, for ``nS_DMAAssert``, the C64's own writes beside ``z###`` (the
C64 runs one program at a time), and screen noise beside the pattern test
where the station has video, which finds a drive in the VIC's half of the
cycle before any count does. Special IO is set to the REU for the test and
put back. A C128 gets three passes: factory NTSC C128s drifted 10x and more
between passes at one value in PR #99."""

import pytest

from treadmill.c64.settings import REU, Settings
from treadmill.knobs import KNOBS
from treadmill.probes.cpuwatch import CpuWatchProbe
from treadmill.probes.noise import NoiseProbe
from treadmill.probes.reuloop import ReuLoopProbe
from treadmill.probes.zpattern import ZPatternProbe
from treadmill.sweeps import SearchSpec, Sweep

pytestmark = [pytest.mark.needs("dma"), pytest.mark.needs_fw("Dbg_SerTimChg", "Dbg_SerDMA"),
              pytest.mark.destructive, pytest.mark.timing_sensitive, pytest.mark.duration(12)]

C128_PASSES = 3
DMA_KNOBS = ["a", "b", "e", "w", "y"]


@pytest.mark.parametrize("letter", DMA_KNOBS, ids=[KNOBS[letter].name for letter in DMA_KNOBS])
def test_dma_margin(letter, at_menu, dbg, screen, keys, video, calibration, programs, pace, margin_check):
    board = at_menu
    knob = KNOBS[letter]
    pattern = ZPatternProbe(board, dbg, pages=pace.z_pages, log=print)
    if letter == "a":                    # the C64 runs one program at a time: its own writes here
        loop = CpuWatchProbe(board, dbg, programs("cpuwatch"), pages=pace.z_pages, log=print)
    else:
        sprites = letter == "b"
        loop = ReuLoopProbe(board, programs("reuclient_sprites" if sprites else "reuclient"),
                            dwell=pace.loop_dwell, sprites=sprites, log=print)
    probes = [pattern, loop]
    if video is not None and calibration is not None:
        probes.append(NoiseProbe(video, calibration, pattern, log=print))
    passes = C128_PASSES if board.is_c128 else 1
    spec = SearchSpec(both=True, step=pace.dma_step, fine=pace.dma_fine, passes=passes)
    with Settings(screen, keys, log=print).special_io(REU):
        margin = Sweep(dbg, knob, probes, spec, log=print).run()
    print(margin.describe())
    assert not margin.points[0].failed, (f"the default {knob.name} {margin.default} fails: "
                                         f"{'; '.join(margin.points[0].notes)}")
    margin_check(margin, board)
