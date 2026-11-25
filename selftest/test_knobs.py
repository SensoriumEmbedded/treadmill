"""The timing knob table (treadmill.knobs): its letters, names and digits
against the firmware's 't' handlers, its limits and held sums, and the fake
board's model of 't' on a TR+, a plain TR and MinimalBoot."""

import os
import re
from pathlib import Path

import pytest

from treadmill.knobs import CYCLE_NS, HELD_SUMS, KNOBS, STANDARDS, limits, parse_listing, standard_of
from treadmill.link import Link, NotAllowed
from treadmill.link.dbgcmds import DebugCommands
from treadmill.sweeps import SweepError, knob_named
from selftest.fake_board import start_fake_board

MINIMAL_LETTERS = {"m", "r", "p", "s", "h", "v"}
TR_PLUS_LETTERS = {"x", "n"}
# SetVideoStdTiming (main) and SetVideoStdDMATiming (MinimalBoot) set these for the standard, so
# neither 'td' block assigns them itself.
SET_BY_VIDEO_STD = {"nS_MaxAdj", "nS_DMASetup", "nS_DMADataSetup", "nS_DMADataHold"}


def _defaults(standard: str) -> dict[str, int]:
    return {k.letter: k.default(standard) for k in KNOBS.values()}


def test_every_swept_default_lies_inside_its_limits():
    for standard in STANDARDS:
        at = _defaults(standard)
        for knob in KNOBS.values():
            if not knob.swept:
                continue
            low, high = limits(knob, standard, at)
            assert low <= at[knob.letter] <= high, (knob.name, standard, low, high)
            assert knob.fits(low) and knob.fits(high), (knob.name, standard, low, high)


def test_the_held_sums_are_pr99s():
    """nS_DMASetup + nS_DMADataHold (release) and + nS_DMADataSetup (latch),
    as PR #99 measured and held them."""
    sums = {name: tuple(sum(KNOBS[l].default(s) for l in letters) for s in STANDARDS)
            for name, (letters, _) in HELD_SUMS.items()}
    assert sums == {"release": (884, 854, 854), "latch": (844, 834, 834)}
    for standard in STANDARDS:
        for name, (letters, inside) in HELD_SUMS.items():
            assert sum(KNOBS[l].default(standard) for l in letters) <= CYCLE_NS[standard] - inside


def test_couplings_follow_the_other_knob():
    at = _defaults("NTSC")
    assert limits(KNOBS["p"], "NTSC", at) == (143, 270)
    assert limits(KNOBS["r"], "NTSC", {**at, "p": 120})[1] == 120
    assert limits(KNOBS["n"], "NTSC", at) == (50, 404 - 20)
    assert limits(KNOBS["m"], "PAL", _defaults("PAL"))[0] == 1015 - 120
    assert limits(KNOBS["w"], "NTSC", at)[1] == 978 - 60 - 430


def test_which_knobs_time_dma_and_which_read_back_at_the_defaults():
    assert {k.letter for k in KNOBS.values() if k.times_dma} == {"a", "b", "e", "w", "y", "n"}
    assert {k.letter for k in KNOBS.values() if k.readback_at_defaults} == {"m", "a", "b", "e", "w", "y", "n"}


def test_standard_names_and_knob_names():
    assert [standard_of("C64", "PAL"), standard_of("C64", "NTSC"), standard_of("C128", "NTSC"),
            standard_of("C128", "PAL"), standard_of(None, None)] == ["PAL", "NTSC", "NTSC C128", "PAL", "PAL"]
    assert knob_named("tn").name == "nS_KSenseSample" and knob_named("cyc_kernprop").letter == "k"
    with pytest.raises(SweepError) as e:
        knob_named("KSenseMode")
    assert "a mode" in str(e.value)


def test_parse_listing_reads_every_knob_including_the_mode():
    text = ("\nVal Set to: 210\n\n-----\nCurrent:    Variable  Val (Command)\n\t nS_PLAprop   210 (tp###)\n"
            "\t KSenseMode    2, in effect 0 (tx#: 0 Skoe, 2 DMA, 9 auto)\n\t nS_KSenseSample 250 (tn###)\n")
    assert parse_listing(text) == {"p": 210, "x": 2, "n": 250}


@pytest.fixture
def fake():
    made = []

    def start(**overrides):
        board, stream = start_fake_board(debug_defines={"Dbg_SerTimChg"}, **overrides)
        link = Link(stream)
        made.append(link)
        return board, DebugCommands(link, {"Dbg_SerTimChg"})

    yield start
    for link in made:
        link.close()


def test_a_tr_plus_lists_and_takes_the_kernal_replace_knobs(fake):
    board, dbg = fake()
    assert set(parse_listing(dbg.listing())) == set(KNOBS)
    assert "Val Set to: 0" in dbg.set_knob("x", 0) and "Val Set to: 300" in dbg.set_knob("n", 300)
    after = parse_listing(dbg.restore_defaults())
    assert after["x"] == 0, "td leaves KSenseMode, as the firmware's does"
    assert after["n"] == 250 and after == {**_defaults("NTSC"), "x": 0}


def test_a_plain_tr_is_never_sent_the_kernal_replace_letters(fake):
    board, dbg = fake(dma=False)
    with pytest.raises(NotAllowed) as e:
        dbg.set_knob("x", 9)
    assert "does not show it" in str(e.value)
    assert board.letters == ["t"], "only the listing went out, not tx9's digit as a command of its own"
    assert "Val Set to: 210" in dbg.set_knob("p", 210)


def test_td_restores_the_standard_the_machine_runs(fake):
    board, dbg = fake()
    board.set_video("PAL")
    assert "Defaults set (PAL C64)" in dbg.restore_defaults()
    assert board.knobs == _defaults("PAL")
    board.banner = board.banner.replace("C64  PAL Vid  50 Hz", "C128 NTSC Vid  60 Hz")
    assert parse_listing(dbg.restore_defaults()) == _defaults("NTSC C128")


def test_minimalboot_takes_its_six_and_restores_them(fake):
    board, dbg = fake(image="minimal")
    assert set(parse_listing(dbg.listing())) == MINIMAL_LETTERS
    dbg.set_knob("p", 200)
    assert "No changes" in dbg.send("te400"), "MinimalBoot's 't' has no nS_DMASetup"
    reply = dbg.restore_defaults()
    assert "Defaults set\n" in reply and parse_listing(reply) == {l: KNOBS[l].default("NTSC") for l in MINIMAL_LETTERS}


# -- against the firmware's source --------------------------------------------------------------

_CASE = re.compile(r"case '(\w)':[^\n]*\n\s*GetDigits\((\d+),\s*&(\w+)\)")
_PRINTF = re.compile(r'printf\("((?:[^"\\]|\\.)*)"((?:\s*,\s*\w+)*)\s*\)')
_ASSIGN = re.compile(r"\b(\w+)\s*=\s*Def_\w+")


@pytest.fixture
def firmware():
    root = os.environ.get("TREADMILL_FW_REPO")
    if not root:
        pytest.skip("TREADMILL_FW_REPO not set")
    return Path(root) / "Source" / "Teensy"


def _t_block(path: Path) -> str:
    text = path.read_text(encoding="latin-1")
    start = text.index("case 't':")
    end = text.index("List current vals", start)
    return text[start:end + 60]


def _listing_from_source(block: str) -> str:
    """The listing the block's printf lines would print, every knob at its NTSC default."""
    values = {k.name: k.default("NTSC") for k in KNOBS.values()}
    out = []
    for fmt, args in _PRINTF.findall(block):
        if not fmt.startswith("\\t"):
            continue
        fmt = fmt.replace("\\t", "\t").replace("\\n", "\n")
        fmt = re.sub(r"%(0?\d*)l?[dui]", r"%\1d", fmt)
        names = [a.strip() for a in args.split(",") if a.strip()]
        out.append(fmt % tuple(values.get(n, 0) for n in names))
    return "".join(out)


def test_main_t_matches_the_table(firmware):
    block = _t_block(firmware / "SerUSBIO.ino")
    found = {letter: (int(digits), name) for letter, digits, name in _CASE.findall(block)}
    assert found == {k.letter: (k.digits, k.name) for k in KNOBS.values()}
    kernal = block[block.index("#ifdef Fab04_KernalReplace"):]
    kernal = kernal[:kernal.index("#endif")]
    assert {letter for letter, _, _ in _CASE.findall(kernal)} == TR_PLUS_LETTERS
    assert {k.letter for k in KNOBS.values() if k.tr_plus_only} == TR_PLUS_LETTERS
    assert parse_listing(_listing_from_source(block)) == _defaults("NTSC")
    td = block[block.index("case 'd':"):]
    td = td[:td.index("break;")]
    restored = set(_ASSIGN.findall(td)) | SET_BY_VIDEO_STD
    assert restored == {k.name for k in KNOBS.values() if k.restored_by_td}


def test_minimalboot_t_matches_the_table(firmware):
    block = _t_block(firmware / "MinimalBoot" / "Min_SerUSBIO.ino")
    found = {letter: (int(digits), name) for letter, digits, name in _CASE.findall(block)}
    assert found == {k.letter: (k.digits, k.name) for k in KNOBS.values() if "minimal" in k.images}
    assert set(found) == MINIMAL_LETTERS
    assert set(parse_listing(_listing_from_source(block))) == MINIMAL_LETTERS
    td = block[block.index("case 'd':"):]
    td = td[:td.index("break;")]
    assert "SetVideoStdDMATiming" in td
    assert {KNOBS[l].name for l in MINIMAL_LETTERS} <= set(_ASSIGN.findall(td)) | SET_BY_VIDEO_STD
