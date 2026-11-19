"""The video standard: calibrations and glyph banks kept per standard with
today's names as the fallback, the TOD rate set when a station opens with
its TR, the Kawari switched by the keyboard board's V and checked on the
banner, and ``run --region``."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from selftest.fake_board import start_fake_board
from selftest.fake_kbd import FakeKeyboard
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from selftest.test_station_keyboard import QUIET, kb_lab  # noqa: F401  (a station with a keyboard board)
from treadmill.board import Board
from treadmill.cli import EXIT_OK, main
from treadmill.link import Link
from treadmill.link.trlink import NotAllowed
from treadmill.session import (
    bank_path, find_bank, find_calibration, open_linkless, open_station, station_calibration_path,
)
from treadmill.station import load_station
from treadmill.station.kawari import set_standard
from treadmill.station.kbd import KeyboardBoard


def _sent(kbd: KeyboardBoard, prefix: str) -> list[str]:
    return [line.strip() for _, line in kbd.sent if line.startswith(prefix)]


def test_a_station_s_files_are_kept_per_standard_with_today_s_names_as_the_fallback(lab):  # noqa: F811
    st = load_station("s1")
    ntsc = replace(st, standard="NTSC")
    calib, glyphs = lab / "data" / "calib", lab / "data" / "glyphs"
    calib.mkdir()
    glyphs.mkdir()
    assert station_calibration_path(st, lab / "data").name == "svideo-generic__bench.json"
    assert station_calibration_path(ntsc, lab / "data").name == "svideo-generic__bench__NTSC.json"
    assert bank_path(ntsc, lab / "data").name == "svideo-generic__bench__NTSC.npz"
    assert find_calibration(ntsc, lab / "data") == calib / "svideo-generic__bench__NTSC.json"    # where one goes
    assert find_bank(ntsc, lab / "data") == glyphs / "svideo-generic__bench__NTSC.npz"
    for path in (calib / "svideo-generic__bench.json", glyphs / "svideo-generic.npz"):
        path.write_text("{}")
    assert find_calibration(ntsc, lab / "data") == calib / "svideo-generic__bench.json"
    assert find_bank(ntsc, lab / "data") == glyphs / "svideo-generic.npz"
    (glyphs / "svideo-generic__bench.npz").write_text("")
    assert find_bank(ntsc, lab / "data") == glyphs / "svideo-generic__bench.npz"
    for path in (calib / "svideo-generic__bench__NTSC.json", glyphs / "svideo-generic__bench__NTSC.npz"):
        path.write_text("{}")
    assert find_calibration(ntsc, lab / "data").name == "svideo-generic__bench__NTSC.json"
    assert find_bank(ntsc, lab / "data").name == "svideo-generic__bench__NTSC.npz"
    assert find_calibration(replace(st, standard="PAL"), lab / "data").name == "svideo-generic__bench.json"


def test_a_station_opened_with_its_tr_knows_its_standard_and_sets_its_tod(kb_lab):  # noqa: F811
    with open_station("kb", video=False) as s:
        assert s.board.station.standard == "NTSC"
        assert _sent(s.kbd, "T ") == ["T 60"]


def test_a_station_opened_without_its_tr_leaves_the_tod_alone(kb_lab):  # noqa: F811
    with open_linkless("kb", video=False) as ls:
        assert ls.station.standard is None and _sent(ls.kbd, "T ") == []


def test_set_standard_switches_by_j1_and_the_tod_follows(kb_lab):  # noqa: F811
    with open_station("kb", video=False) as s:
        b = s.board
        assert set_standard(b, "pal") is True
        assert b.station.standard == "PAL" and b.banner.video == "PAL"
        assert _sent(s.kbd, "V ") == ["V closed"] and _sent(s.kbd, "T ")[-1] == "T 50"
        assert set_standard(b, "PAL") is False and _sent(s.kbd, "V ") == ["V closed"]
        assert set_standard(b, "NTSC") is True and _sent(s.kbd, "V ") == ["V closed", "V open"]


def test_a_kawari_whose_saved_model_changed_is_flipped_once_and_named(kb_lab):  # noqa: F811
    """The station file says J1 open gives NTSC; this Kawari's saved model
    is now PAL, so open gives PAL."""
    fake, stream = start_fake_board()
    station = load_station("kb")
    kb = FakeKeyboard(chip=station.keyboard.chip, on_reset=fake.external_reset,
                      on_video=lambda j1: fake.set_video("PAL" if j1 == "open" else "NTSC"))
    board = Board.open(station, link_factory=lambda: Link(stream),
                       kbd_factory=lambda: KeyboardBoard.attach(kb.connect()))
    said = []
    try:
        assert set_standard(board, "PAL", log=said.append) is True
        assert board.station.standard == "PAL" and kb.switches == ["closed", "open"]
        assert any('set kawari_open = "PAL"' in line for line in said), said
        assert board.station.keyboard.kawari_open == "PAL"
        assert set_standard(board, "NTSC", log=said.append) is True and kb.switches[-1] == "closed"
    finally:
        board.close()


def test_set_standard_needs_region_switch_and_a_plain_tr_resets_after_v(kb_lab):  # noqa: F811
    with open_station("kb", video=False, caps_mask={"region_switch"}) as s:
        with pytest.raises(NotAllowed, match="lacks region_switch, so it cannot switch to PAL"):
            set_standard(s.board, "PAL")
        assert _sent(s.kbd, "V ") == []
        assert set_standard(s.board, "NTSC") is False          # already there: nothing to switch
    with open_station("kb", video=False, caps_mask={"reset_sense"}) as s:
        resets = []
        reset = s.board.reset
        s.board.reset = lambda **kw: (resets.append(1), reset(**kw))
        assert set_standard(s.board, "PAL") and resets == [1]


def test_run_region_both_is_two_runs_one_per_standard(kb_lab: Path, capsys):  # noqa: F811
    code = main(["run", "--station", "kb", "--region", "both", "--reports", str(kb_lab / "reports"), "--json",
                 "--", "-k", "test_identity", *QUIET])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    runs = json.loads(out)["runs"]
    assert [r["station"]["standard"] for r in runs] == ["PAL", "NTSC"]
    assert [Path(r["run_dir"]).name[-4:] for r in runs] == ["-pal", "ntsc"]
    assert all(r["summary"] == {"passed": 1} for r in runs), runs


def test_the_keyboard_hardware_tests_pass_against_the_fakes(kb_lab: Path, capsys):  # noqa: F811
    """tests/keyboard on the fake TR+ and keyboard board: real keys move the
    menu, an external reset brings it back from the menu and from TR BASIC,
    and the Kawari goes to PAL and back to NTSC."""
    code = main(["run", "--station", "kb", "--tests", "tests/keyboard", "--reports", str(kb_lab / "reports"),
                 "--json", "--", *QUIET])
    out, err = capsys.readouterr()
    run = json.loads(out)
    assert code == EXIT_OK and run["summary"] == {"passed": 4}, run["tests"]


def test_run_region_stops_a_station_that_cannot_switch(lab: Path, capsys):  # noqa: F811
    code = main(["run", "--station", "s1", "--region", "PAL", "--reports", str(lab / "reports"), "--json",
                 "--", "-k", "test_identity", *QUIET])
    out, err = capsys.readouterr()
    assert code == 2, err
    assert "lacks region_switch, so it cannot switch to PAL" in json.loads(out)["aborted"]
