"""A station whose machine has a keyboard board: Board opens it beside the TR
and folds its fitted outputs into the capabilities, a board that is not
there leaves the station without them, the run's record and artifacts carry
it, and ``exec``/``shell --no-link`` open it with no TR at all."""

import io
import json
import textwrap
import time
from pathlib import Path

import pytest

from selftest.fake_board import start_fake_board
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from treadmill.board import Board
from treadmill.cli import EXIT_OK, EXIT_USAGE, main
from treadmill.link import Link
from treadmill.report import describe_station, write_board_artifacts
from treadmill.session import keyboard_line, open_station
from treadmill.station import load_station
from treadmill.station.kbd import KbdNotFound

QUIET = ("-p", "no:anyio", "-p", "no:cacheprovider")


@pytest.fixture
def kb_lab(lab: Path) -> Path:  # noqa: F811
    (lab / "data" / "stations" / "kb.toml").write_text(
        'id = "kb"\nrig = "rigA"\n[machine]\nid = "bench"\nmodel = "C64"\nhas_kawari = true\n'
        '[tr]\nuid = "19277260"\n[keyboard]\nchip = "14470230"\nkawari_open = "NTSC"\n', encoding="utf-8")
    return lab


def _fake_link():
    _, stream = start_fake_board()
    return lambda: Link(stream)


def test_board_opens_the_keyboard_board_and_folds_in_what_is_fitted(kb_lab: Path):
    with open_station("kb", video=False) as s:
        b = s.board
        assert b.kbd is not None and b.kbd_problem is None and s.kbd is b.kbd and b.kbd.chip == "14470230"
        assert b.station.has("dma", "keys_hw", "keys_kernal", "nmi", "ext_reset", "tod_select", "region_switch")
        assert not b.station.has("tr_button") and not b.station.has("tr_alt")
        b.probe()                                      # a re-probe keeps them
        assert b.station.has("keys_hw", "region_switch")
        kbd = b.kbd
    assert kbd.closed


def test_a_keyboard_board_that_refuses_its_tod_rate_leaves_the_station_open_without_it(kb_lab: Path):
    """A board that answers '?' but refuses T costs the keyboard tests, as one
    that does not answer at all does, not the whole station."""
    from selftest.fake_kbd import FakeKeyboard
    from treadmill.station.kbd import KbdError, KeyboardBoard

    station, made = load_station("kb"), []

    def refusing():
        kbd = KeyboardBoard.attach(FakeKeyboard(chip=station.keyboard.chip).connect(), port="fake-keyboard")

        def tod(rate=None):
            raise KbdError("keyboard board refused 'T 60': ERR T busy")
        kbd.tod = tod
        made.append(kbd)
        return kbd

    b = Board.open(station, link_factory=_fake_link(), kbd_factory=refusing)
    try:
        assert b.kbd is None and b.kbd_problem == "its TOD line was not set: keyboard board refused 'T 60': ERR T busy"
        assert b.station.has("dma") and not b.station.has("keys_hw") and not b.station.has("tod_select")
        assert made[0].closed
    finally:
        b.close()


def test_caps_mask_takes_keyboard_caps_away(kb_lab: Path):
    with open_station("kb", video=False, caps_mask={"keys_hw", "ext_reset"}) as s:
        assert s.board.kbd is not None
        assert not s.board.station.has("keys_hw") and not s.board.station.has("ext_reset")
        assert s.board.station.has("nmi", "dma")


def test_a_keyboard_board_that_is_not_there_leaves_the_station_without_it(kb_lab: Path):
    def absent():
        raise KbdNotFound("no keyboard board with chip id 14470230 on USB; keyboard boards present: none")
    board = Board.open(load_station("kb"), link_factory=_fake_link(), kbd_factory=absent)
    try:
        assert board.kbd is None and "no keyboard board" in board.kbd_problem
        assert board.station.has("dma") and not board.station.has("keys_hw")
        assert describe_station(board)["keyboard"] == {"chip": "14470230", "open": False,
                                                       "problem": board.kbd_problem}
        assert "capabilities are off" in keyboard_line(board.kbd, board.kbd_problem)
    finally:
        board.close()


def test_a_fake_tr_opens_no_real_keyboard_board(kb_lab: Path):
    board = Board.open(load_station("kb"), link_factory=_fake_link())
    try:
        assert board.kbd is None and board.kbd_problem.startswith("not opened")
    finally:
        board.close()


def test_artifacts_carry_the_keyboard_board(kb_lab: Path, tmp_path: Path):
    with open_station("kb", video=False) as s:
        b = s.board
        t0, mark = time.monotonic(), len(b.link.stream.tap.chunks)
        assert s.kbd.time_us() > 0
        s.kbd.write_raw(b"dv")
        write_board_artifacts(tmp_path, b, t0, mark)
        log = (tmp_path / "kbd.log").read_text(encoding="utf-8")
        assert "> TIME" in log and "< OK TIME" in log and "> 'dv'" in log and "> ?" not in log
        entry = describe_station(b)["keyboard"]
        assert entry["open"] and entry["fitted"] == ["kawari", "reset", "tod"] and entry["proto"] == "1"


def test_exec_no_link_opens_the_keyboard_board_and_not_the_tr(kb_lab: Path, tmp_path: Path, capsys):
    script = tmp_path / "kb.py"
    script.write_text(textwrap.dedent("""
        result = {"none": [n for n in ("board", "link", "menu", "dbg", "keys", "firmware") if globals()[n] is None],
                  "proto": kbd.status()["proto"], "caps": sorted(station.caps), "screen": type(screen).__name__,
                  "keys": type(keys).__name__}
        keys.press(b"RUN")
    """), encoding="utf-8")
    code = main(["exec", "--station", "kb", "--no-link", "--reports", str(kb_lab / "reports"), "--json", str(script)])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    payload = json.loads(out)
    result = payload["result"]
    assert result["none"] == ["board", "link", "menu", "dbg", "firmware"] and result["proto"] == "1"
    assert result["keys"] == "HwKeys"
    assert {"keys_hw", "ext_reset", "region_switch"} <= set(result["caps"])
    assert not {"serial", "video", "screen", "dma"} & set(result["caps"]) and result["screen"] == "ScreenChannel"
    assert "[kbd] keyboard board chip 14470230" in err
    folder = Path(payload["folder"])
    log = (folder / "kbd.log").read_text(encoding="utf-8")
    assert "> ?" in log and "> S run" in log and not (folder / "serial.log").exists()
    meta = json.loads((Path(payload["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    assert meta["firmware"] == {"spec": "none", "no_link": True} and meta["board"] is None
    assert meta["station"]["keyboard"]["open"] and meta["summary"] == {"passed": 1}


def test_no_link_takes_no_firmware(kb_lab: Path, tmp_path: Path, capsys):
    script = tmp_path / "nothing.py"
    script.write_text("", encoding="utf-8")
    code = main(["exec", "--station", "kb", "--no-link", "--fw-defines", "Dbg_SerDMA",
                 "--reports", str(kb_lab / "reports"), "--json", str(script)])
    assert code == EXIT_USAGE
    assert "--no-link opens no TeensyROM" in json.loads(capsys.readouterr().err)["error"]
    assert not (kb_lab / "reports").exists(), "a usage error leaves no run folder"


def test_shell_no_link(kb_lab: Path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("print('proto', kbd.status()['proto'])\nprint('board', board)\n"))
    code = main(["shell", "--station", "kb", "--no-link", "--reports", str(kb_lab / "reports")])
    out, err = capsys.readouterr()
    assert code == EXIT_OK and "proto 1" in out and "board None" in out
    assert "kb, no TR link; names:" in err and "kbd" in err


def test_run_records_the_keyboard_board(kb_lab: Path, capsys):
    code = main(["run", "--station", "kb", "--reports", str(kb_lab / "reports"), "--json", "--",
                 "-k", "test_identity", *QUIET])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    station = json.loads(out)["station"]
    assert station["keyboard"]["open"] and station["keyboard"]["port"] == "fake-keyboard"
    assert "keys_hw" in station["caps"]
