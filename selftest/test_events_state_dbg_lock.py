"""Unprompted-event classification, derived state, the debug-letter gate and
the rig lock, all without hardware."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from treadmill.link import Link, NotAllowed
from treadmill.link.dbgcmds import DebugCommands
from treadmill.link.protocol import BAD_SID, GOOD_SID, board_reply
from treadmill.link.state import MENU_BOOTING, MENU_READY, MINIMAL, RUNNING, SILENT, derive_state
from treadmill.link.unsolicited import EventLog, classify
from treadmill.station.lock import HELD_ENV, RigBusy, RigLock
from selftest.fake_board import start_fake_board


def test_classify_lines_and_sid_tokens():
    data = (b"Loading IO handler: Swiftlink\r\nRemote Launch:\nP:/x\nF:y\n" + board_reply(GOOD_SID)
            + b"PHI2 timed: 985248 Hz\nCrashReport:\n  A problem occurred\nUnk cmd: 0x6499\n"
            + board_reply(BAD_SID) + b"something else\n")
    events = classify(data)
    kinds = [e.kind for e in events]
    assert kinds.count("sid") == 2
    assert kinds[:2] == ["sid", "sid"]                   # tokens are pulled out first
    rest = [e for e in events if e.kind != "sid"]
    assert [e.kind for e in rest] == ["handler", "launch", "text", "text", "phi2",
                                       "crash_report", "text", "unknown_command", "text"]
    assert rest[0].name == "Swiftlink"
    assert {e.name for e in events if e.kind == "sid"} == {"good", "bad"}


def test_event_log_tracks_the_current_handler():
    log = EventLog()
    log.feed(b"Loading IO handler: TeensyROM\n")
    log.feed(b"Loading IO handler: REU\n")
    assert log.current_handler() == "REU"
    assert "handler" in log.kinds() and log.last("reset") is None


@pytest.fixture
def board_and_link():
    board, stream = start_fake_board()
    link = Link(stream)
    yield board, link
    link.close()


def test_states(board_and_link):
    board, link = board_and_link
    assert derive_state(link).kind == MENU_READY
    board.boot_polls = 1
    link.reset()
    assert derive_state(link).kind == MENU_BOOTING
    link.launch("/A.PRG")
    s = derive_state(link, last_handler="TeensyROM")
    assert s.kind == RUNNING and s.handler == "TeensyROM"
    board.image = "minimal"
    assert derive_state(link).kind == MINIMAL
    board.silent = True
    assert derive_state(link).kind == SILENT


def test_debug_letters_are_gated_on_defines(board_and_link):
    board, link = board_and_link
    dbg = DebugCommands(link, defines=set())
    with pytest.raises(NotAllowed):
        dbg.listing()
    with pytest.raises(NotAllowed):
        dbg.send("e")                                   # never without the station's say-so
    assert board.letters == []
    board.debug_defines = {"Dbg_SerTimChg"}
    dbg = DebugCommands(link, defines={"Dbg_SerTimChg"}, allow_eeprom_reset=True)
    assert "set to 210" in dbg.set_knob("p", 210)
    assert board.knobs["p"] == 210 and board.letters[-1] == "tp210"
    assert "restored" in dbg.restore_defaults() and board.knobs["p"] == 150
    with pytest.raises(NotAllowed):
        dbg.set_knob("m", 10000)
    with pytest.raises(NotAllowed):
        dbg.send("z999")                                # Dbg_SerDMA not in this build
    assert "EEPROM" in dbg.send("e")


def test_rig_lock_excludes_a_second_holder(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TREADMILL_LOCK_DIR", str(tmp_path))
    monkeypatch.delenv(HELD_ENV, raising=False)
    with RigLock("rigX", command="first run") as lock:
        assert not lock.nested and "first run" in lock.holder()
        # A second process cannot take it, and learns who holds it.
        code = textwrap.dedent("""
            import sys
            from treadmill.station.lock import RigLock, RigBusy
            try:
                RigLock("rigX").acquire(); print("acquired")
            except RigBusy as e:
                print("busy:", e.holder)
        """)
        env = {**os.environ, "TREADMILL_LOCK_DIR": str(tmp_path)}
        env.pop(HELD_ENV, None)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, env=env,
                             cwd=str(Path(__file__).resolve().parents[1]))
        assert out.stdout.startswith("busy:") and "first run" in out.stdout, out
        # A child of the holder nests instead of waiting.
        assert os.environ[HELD_ENV] == "rigX"
        inner = RigLock("rigX").acquire()
        assert inner.nested
        inner.release()
    assert HELD_ENV not in os.environ
    # Released: the next holder gets it.
    with RigLock("rigX", command="second run") as again:
        assert "second run" in again.holder()
