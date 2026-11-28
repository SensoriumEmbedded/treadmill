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
without load.

``nS_PLAprop``'s floor is ``nS_RWnReady``, which at its default already lands
where the selects are read, so its walk down stops 7 ns below its default.
It is swept down once more with ``nS_RWnReady`` set to 0 for the sweep, to
the PLA's own edge or wherever the C64 fails first. That record carries
``set``, so it is held to no floor.

idread's IO2 page is the EasyFlash handler's, about the shortest read the
firmware has, so on a TR+ ``nS_PLAprop`` is also swept on the slower ones
(``treadmill.probes.regread``): the REU's registers (Special IO set to the
REU and put back), the same beside the host's DMA write jobs, and the
TeensyROM menu's own IO1 handler."""

import contextlib

import pytest

from treadmill.c64.settings import REU, Settings
from treadmill.knobs import KNOBS
from treadmill.probes import IdReadProbe, MenuProbe
from treadmill.probes.regread import MenuReadProbe, RegReadProbe
from treadmill.sweeps import SearchSpec, Sweep

pytestmark = [pytest.mark.needs_fw("Dbg_SerTimChg"), pytest.mark.timing_sensitive, pytest.mark.duration(4)]

STEPS = {"r": 10, "p": 10, "s": 10, "h": 10, "m": 20}   # nS_MaxAdj's range is about 350 ns wide
# nS_PLAprop and nS_MaxAdj under load take every path of this test between them (the menu at a floor,
# a loaded default, the readback at the defaults with load); the rest differ only in the knob.
ALIKE = {("r", False), ("s", False), ("h", False), ("m", False)}
CASES = [pytest.param(letter, load, id=KNOBS[letter].name + ("-usbload" if load else ""),
                      marks=[pytest.mark.alike] if (letter, load) in ALIKE else [])
         for letter, load in [(letter, False) for letter in STEPS] + [("m", True)]]
# regread-jobs differs from regread only in the host's jobs beside it.
HANDLERS = [pytest.param("regread", id="nS_PLAprop-regread"),
            pytest.param("regread-jobs", id="nS_PLAprop-regread-jobs", marks=pytest.mark.alike),
            pytest.param("regread-menu", id="nS_PLAprop-regread-menu")]


@pytest.mark.parametrize("letter, load", CASES)
def test_cpu_margin(letter, load, at_menu, dbg, screen, programs, goldens, artifacts, station, request, pace,
                    margin_check, restart):
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
    spec = SearchSpec(both=True, step=STEPS[letter], fine=pace.fine, repeat=pace.repeat)
    margin = Sweep(dbg, knob, probe, spec, restart=restart, log=print).run()
    print(margin.describe())
    assert not margin.points[0].failed, f"the default {knob.name} {margin.default} fails the {probe.name} probe"
    floor = station.margins.get(knob.name)
    if floor is not None and probe is not menu and not load and not margin.stopped:
        values = [v for v in (floor.down, floor.up) if v is not None]
        bad = [p for p in Sweep(dbg, knob, menu, SearchSpec(), log=print).spot(values) if p.failed]
        assert not bad, "the menu probe fails at the floor: " + "; ".join(f"{p.value}: {p.notes}" for p in bad)
    if dma and letter != "m" and not margin.stopped:     # a stopped walk's record goes to margin_check first
        loaded = IdReadProbe(board, programs("idread"), dwell=pace.dwell, usb_load=True, log=print)
        point, = Sweep(dbg, knob, loaded, SearchSpec(repeat=pace.repeat), log=print).spot([margin.default])
        assert not point.failed, f"the default {knob.name} {margin.default} fails under USB load: {point.notes}"
    margin_check(margin, board, floor=not load)


@pytest.mark.needs("dma")
@pytest.mark.parametrize("probe_name", HANDLERS)
def test_handler_margin(probe_name, at_menu, dbg, screen, keys, programs, pace, margin_check, restart):
    board = at_menu
    knob = KNOBS["p"]
    if probe_name == "regread-menu":
        probe = MenuReadProbe(board, programs("menuread"), dwell=pace.dwell, log=print)
        special = contextlib.nullcontext()
    else:
        probe = RegReadProbe(board, programs("reureadback_hammer"), dwell=pace.dwell,
                             jobs=probe_name == "regread-jobs", log=print)
        special = Settings(screen, keys, log=print).special_io(REU)
    spec = SearchSpec(both=True, step=STEPS["p"], fine=pace.fine, repeat=pace.repeat)
    with special:
        margin = Sweep(dbg, knob, probe, spec, restart=restart, log=print).run()
    print(margin.describe())
    assert not margin.points[0].failed, (f"the default {knob.name} {margin.default} fails the {probe.name} probe: "
                                         + "; ".join(margin.points[0].notes))
    margin_check(margin, board)


def test_plaprop_down_with_rwnready_at_0(at_menu, dbg, screen, programs, goldens, artifacts, request, pace,
                                         margin_check, restart):
    board = at_menu
    knob = KNOBS["p"]
    if board.station.has("dma"):
        request.getfixturevalue("sd_card")
        probe = IdReadProbe(board, programs("idread"), dwell=pace.dwell, log=print)
    else:
        probe = MenuProbe(board, screen, goldens.load("menu/root"), artifacts)
    spec = SearchSpec(stop=0, step=STEPS["p"], fine=pace.fine, repeat=pace.repeat)
    margin = Sweep(dbg, knob, probe, spec, fixed={"r": 0}, restart=restart, log=print).run()
    print(margin.describe())
    assert not margin.points[0].failed, (f"the default {knob.name} {margin.default} fails the {probe.name} probe "
                                         f"with nS_RWnReady at 0: " + "; ".join(margin.points[0].notes))
    margin_check(margin, board, floor=False)
