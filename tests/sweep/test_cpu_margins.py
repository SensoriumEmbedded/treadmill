"""Test 20: the margins of the CPU-cycle knobs, each way. On every bus cycle
it serves, the TeensyROM counts from the Phi2-rising interrupt: the address
and R/W at ``nS_RWnReady``, the selects at ``nS_PLAprop`` (then it drives the
byte), a C64 write latched at ``nS_DataSetup``, the drive released at
``nS_DataHold``; ``nS_MaxAdj`` decides when an interrupt is late enough to
pull the count back. Each knob walks down and up from its default to the
knob table's limits, each edge is bisected, the defaults come back, and the
edges go in ``margins.json``: one short of the station's floor fails the
test, one that shrank against an earlier build is a warning.

The probe is idread (a cartridge counting its ROM and IO2 reads apart, by
DMA) on a TR+ with a card, else the menu compared with its golden after
each reset. Where the station names a floor, the menu probe also runs at
it, so ``nS_PLAprop``'s floor covers the IO1 item-type read the NTSC
misread broke (fixed by ``UpdateItemTypeReg``). The machine is the point:
a Reloaded MKII, a Mega65 and a Kawari each have their own edges, PAL and
NTSC apart.

USB load (version requests as fast as the board answers, while idread
counts) makes the board's interrupts arrive late, which ``nS_MaxAdj``'s
upper edge needs and which PR #103's MinimalBoot errors came with. So
``nS_MaxAdj`` is swept twice, without load and under it, and each other
knob's default is checked once more under load after its sweep. The load
runs beside idread, so on a plain TR those parts skip. The loaded sweep is
recorded (its probe is idread-usbload, so its shrink check compares like
with like) but not held to the station's floor, which is for the knob
without load."""

import pytest

from treadmill.knobs import KNOBS
from treadmill.probes import IdReadProbe, MenuProbe
from treadmill.sweeps import SearchSpec, Sweep

pytestmark = [pytest.mark.needs_fw("Dbg_SerTimChg"), pytest.mark.timing_sensitive, pytest.mark.duration(4)]

FINE = 5
STEPS = {"r": 10, "p": 10, "s": 10, "h": 10, "m": 20}   # nS_MaxAdj's range is about 350 ns wide
# nS_PLAprop and nS_MaxAdj under load take every path of this test between them (the menu at a floor,
# a loaded default, the readback at the defaults with load); the rest differ only in the knob.
ALIKE = {("r", False), ("s", False), ("h", False), ("m", False)}
CASES = [pytest.param(letter, load, id=KNOBS[letter].name + ("-usbload" if load else ""),
                      marks=[pytest.mark.alike] if (letter, load) in ALIKE else [])
         for letter, load in [(letter, False) for letter in STEPS] + [("m", True)]]


@pytest.mark.parametrize("letter, load", CASES)
def test_cpu_margin(letter, load, at_menu, dbg, screen, programs, goldens, artifacts, station, request, pace,
                    margin_check):
    board = at_menu
    knob = KNOBS[letter]
    dma = board.station.has("dma")
    if load and not dma:
        pytest.skip("USB load runs beside idread, which needs a TR+")
    menu = MenuProbe(board, screen, goldens.load("menu/root"), artifacts)
    if dma:
        request.getfixturevalue("sd_card")
        probe = IdReadProbe(board, programs("idread"), dwell=pace.dwell, usb_load=load, log=print)
    else:
        probe = menu
    spec = SearchSpec(both=True, step=STEPS[letter], fine=FINE, repeat=pace.repeat)
    margin = Sweep(dbg, knob, probe, spec, log=print).run()
    print(margin.describe())
    assert not margin.points[0].failed, f"the default {knob.name} {margin.default} fails the {probe.name} probe"
    floor = station.margins.get(knob.name)
    if floor is not None and probe is not menu and not load:
        values = [v for v in (floor.down, floor.up) if v is not None]
        bad = [p for p in Sweep(dbg, knob, menu, SearchSpec(), log=print).spot(values) if p.failed]
        assert not bad, "the menu probe fails at the floor: " + "; ".join(f"{p.value}: {p.notes}" for p in bad)
    if dma and letter != "m":
        loaded = IdReadProbe(board, programs("idread"), dwell=pace.dwell, usb_load=True, log=print)
        point, = Sweep(dbg, knob, loaded, SearchSpec(repeat=pace.repeat), log=print).spot([margin.default])
        assert not point.failed, f"the default {knob.name} {margin.default} fails under USB load: {point.notes}"
    margin_check(margin, board, floor=not load)
