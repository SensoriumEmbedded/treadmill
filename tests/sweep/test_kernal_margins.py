"""Test 22: the margins of Kernal Replace's HIRAM sense, each way. Serving a
KERNAL of its own, the TeensyROM must know after every write to $00/$01
whether the CPU has the KERNAL banked in, and it cannot see the CPU port: on
a C64 it drives A14 low on the next KERNAL read and samples ROMH after
``Cyc_KernProp`` Teensy cycles (Skoe); on a C128, whose transceiver hides the
address the TeensyROM drives, it steals the cycle after the write and samples
ROMH at ``nS_KSenseSample`` from the re-latch (the DMA method), which works on
a C64 too. The probe KERNAL is the machine's own with its filler inverted, so
krread.a can count both ways a sense goes wrong: the machine's own KERNAL
where the replacement belongs, and the replacement over RAM. It shows its
counts on the top row and the probe reads them by video: a host DMA access
while krread runs under Kernal Replace would skip a DMA sense and corrupt
krread's own RAM, so the test reads the C64's memory only at the menu.

The test chooses the probe KERNAL as the Kernal Replace file, sets Special IO
to Kernal Replace and KSenseMode to the method under test, and puts all three
back after; td leaves KSenseMode, so ksense_mode restores it itself. Each
knob walks down and up to the knob table's limits (nS_KSenseSample stays 20
below nS_DMADataSetup), the defaults come back, and the edges go through
margin_check. The edges come from the sense's verdict, the reads after the
first; the first read after each write to $01, the Skoe sense's own cycle,
is held to none wrong at the default on its own, after the margin is kept."""

import pytest

from treadmill.c64.settings import KERNAL_REPLACE, Settings
from treadmill.knobs import KNOBS
from treadmill.probes.krread import DMA, KERNAL, KERNAL_SIZE, SKOE, KrReadProbe, ksense_mode, probe_kernal
from treadmill.sweeps import SearchSpec, Sweep

pytestmark = [pytest.mark.needs("dma"), pytest.mark.needs("video"), pytest.mark.needs_fw("Dbg_SerTimChg"),
              pytest.mark.destructive,
              pytest.mark.timing_sensitive, pytest.mark.duration(5)]

PROBE_KERNAL = "/treadmill/krprobe.bin"
STEPS = {"k": 4}                  # Teensy cycles, 1.23 ns each at 816 MHz


@pytest.mark.parametrize("letter", ["k", "n"], ids=[KNOBS["k"].name, KNOBS["n"].name])
def test_kernal_sense_margin(letter, at_menu, dbg, screen, keys, menu, programs, request, pace, margin_check):
    board = at_menu
    knob = KNOBS[letter]
    if letter == "k" and board.is_c128:
        pytest.skip("the Skoe sense cannot work on a C128: its transceiver hides the address the TeensyROM drives")
    request.getfixturevalue("sd_card")
    image, defines = probe_kernal(board.link.read_memory(KERNAL, KERNAL_SIZE))
    board.link.push_file(image, PROBE_KERNAL, "sd")
    probe = KrReadProbe(board, screen, programs("krread", **defines), defines["LEN"], dwell=pace.kr_dwell, log=print)
    step = STEPS.get(letter, pace.slow_step)
    spec = SearchSpec(both=True, step=step, fine=1 if letter == "k" else pace.slow_fine)
    settings = Settings(screen, keys, log=print)
    with settings.kernal_file(menu, "SD:" + PROBE_KERNAL), settings.special_io(KERNAL_REPLACE), \
            ksense_mode(dbg, SKOE if letter == "k" else DMA):
        margin = Sweep(dbg, knob, probe, spec, log=print).run()
    print(margin.describe())
    assert not margin.points[0].failed, (f"the default {knob.name} {margin.default} fails: "
                                         f"{'; '.join(margin.points[0].notes)}")
    margin_check(margin, board)
    rom_first, ram_first, passes = probe.first_reads[f"{letter}={margin.default}"]
    assert not (rom_first or ram_first), (
        f"at the default {knob.name} {margin.default}, the first KERNAL read after a write to $01 came wrong: "
        f"{rom_first} of {passes} without the replacement, {ram_first} with it over RAM")
