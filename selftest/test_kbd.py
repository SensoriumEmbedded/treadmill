"""The keyboard board's client against the fake board: opening, reply
matching, timed actions, aborts, events, the port closing, and finding the
board by its serial string without opening anything else."""

import time

import pytest

from selftest.fake_kbd import FakeKeyboard
from treadmill.link import discover
from treadmill.link.transport import TransportError
from treadmill.station import kbd
from treadmill.station.kbd import KbdError, KbdNotFound, KbdRefused, KeyboardBoard


@pytest.fixture
def fake():
    return FakeKeyboard(partial_s=0.1)


@pytest.fixture
def board(fake):
    kb = KeyboardBoard.attach(fake.connect())
    yield kb
    kb.close()


# -- opening -------------------------------------------------------------------------

def test_open_reads_identity_before_quieting_events(fake, board):
    assert fake.received == ["?", "E 0"]
    assert board.identity["proto"] == "1" and board.identity["e"] == "1"
    assert board.chip == "14470230"
    assert board.fitted == {"tod", "reset", "kawari"}
    assert board.status()["e"] == "0"


def test_open_refuses_another_protocol():
    with pytest.raises(KbdError, match="protocol 2"):
        KeyboardBoard.attach(FakeKeyboard(proto=2).connect())


def test_a_closed_port_releases_holds_and_turns_events_back_on(fake, board):
    board.hold("17+12")
    assert board.status()["held"] == "12+17"
    board.close()
    board.close()                                   # twice is fine
    deadline = time.monotonic() + 2
    while fake.holds and time.monotonic() < deadline:
        time.sleep(0.01)
    again = KeyboardBoard.attach(fake.connect())
    try:
        assert again.identity["e"] == "1" and again.identity["held"] == "-"
    finally:
        again.close()


def test_a_pending_command_fails_when_the_port_closes(board):
    pending = board.start("P 77 2000 0")
    board.stream.close()
    with pytest.raises(KbdError, match="closed"):
        pending.wait(2)


# -- replies -------------------------------------------------------------------------

def test_errors_come_back_as_replies_with_their_reason(board):
    assert board.command("ZZ").reason == "syntax"            # matched though the board echoes '*'
    assert board.command("K 88 d").reason == "range"
    assert board.command("K 77 x").reason == "syntax"
    assert board.command("B menu").reason == "notfitted"
    long = board.command("K " + "1" * 130)
    assert (long.cmd, long.reason) == ("*", "syntax")


def test_typed_calls_raise_on_err(board):
    with pytest.raises(KbdRefused) as e:
        board.type("a|b")
    assert e.value.reply.reason == "range" and e.value.reply.line == "S a|b"


def test_timed_action_replies_when_done_and_others_are_answered_meanwhile(board):
    pending = board.start("P 77 300 0")
    assert board.status()["proto"] == "1"                    # queries are always answered
    assert board.command("K 12 d").reason == "busy"
    assert not pending.done
    reply = pending.wait()
    assert reply.ok and int(reply["t1"]) - int(reply["t0"]) >= 250_000


def test_abort_returns_the_aborted_reply_before_ok_a(fake, board):
    pending = board.start("S hello world")
    time.sleep(0.05)
    replies = board.abort()
    assert [(r.cmd, r.ok, r.reason) for r in replies] == [("S", False, "aborted"), ("A", True, None)]
    assert pending.wait().reason == "aborted" and fake.typed == []


def test_v_holds_reset_to_the_end_when_aborted_after_its_pulse(fake, board):
    sent = time.monotonic()
    board.start("V closed 300")
    time.sleep(0.05)
    replies = board.abort()
    assert replies[0].reason == "aborted" and replies[0].t_host - sent >= 0.29
    assert fake.switches == ["closed"] and board.video_state()["j1"] == "closed"


def test_hold_timeout_arrives_as_an_event(board):
    board.hold("77", max_ms=100)
    t, line = board.wait_event(r"^EV K 77 timeout$", 2)
    assert line == "EV K 77 timeout" and (t, line) in board.events


def test_partial_line_is_dropped_after_the_quiet_time(fake, board):
    board.write_raw(b"dv")                         # a TeensyROM's VersionInfo token, no LF
    time.sleep(0.25)
    assert board.command("TIME").ok


def test_blank_line_gets_no_reply(board):
    board.write_raw(b"\n")
    assert board.read_lines(0.15) == []
    with pytest.raises(ValueError):
        board.start("")


def test_typed_commands(fake, board):
    assert board.tod(60) == "60" and board.tod() == "60"
    width = board.reset(100)
    assert int(width["t1"]) >= int(width["t0"]) and fake.resets == [100]
    assert board.type('load"$",8{return}')["n"] == "10"
    assert board.sequence(["d 77", "w 20", "n 20", "u 77"])["steps"] == "4"
    assert board.video("open", 10)["j1"] == "open"
    assert board.time_us() > 0


# -- the protocol's arithmetic --------------------------------------------------------

@pytest.mark.parametrize("line, cmd", [
    ("?", "?"), ("time", "TIME"), ("st 50 50 hi", "ST"), ("ZZ", "*"), ("K 77 d" + " " * 120, "*"),
    ("K 77\x01 d", "*"), ("   ", None),
])
def test_expected_cmd(line, cmd):
    assert kbd.expected_cmd(line) == cmd


@pytest.mark.parametrize("line, seconds", [
    ("R", 0.05), ("R 100", 0.0001), ("R hold 5000", 0.0), ("P 77 100 20", 0.12), ("V closed 2000", 2.001),
    ("S hi", 60.0), ("X d 77;w 50", 10.0), ("?", 0.0),
])
def test_action_seconds(line, seconds):
    assert kbd.action_seconds(line) == pytest.approx(seconds)


# -- finding the board -----------------------------------------------------------------

def _ports(monkeypatch, ports):
    monkeypatch.setattr(kbd, "list_teensyrom_ports", lambda: ports)


def test_keyboard_found_by_serial_string_in_any_case(monkeypatch):
    _ports(monkeypatch, [("COM5", "TeensyROM-Serial-19307720"), ("COM7", "C64USBKEYBOARD_14470230"),
                         ("COM8", "C64USBKeyboard_99")])
    assert kbd.find_keyboard("14470230").port == "COM7"
    assert [b.chip for b in kbd.find_keyboards()] == ["14470230", "99"]
    with pytest.raises(KbdError, match="2 keyboard boards"):
        kbd.find_keyboard()
    with pytest.raises(KbdNotFound, match="chip id 5"):
        kbd.find_keyboard("5")


def test_find_port_never_opens_a_named_device(monkeypatch):
    opened = []

    def refuse(port):
        opened.append(port)
        raise TransportError(f"{port}: busy")
    monkeypatch.setattr(discover, "list_teensyrom_ports",
                        lambda: [("COM7", "C64USBKEYBOARD_14470230"), ("COM5", "TeensyROM-Serial-1"),
                                 ("COM6", "12345678"), ("COM9", None)])
    monkeypatch.setattr(discover, "open_serial", refuse)
    with pytest.raises(discover.NotFound):
        discover.find_port("19307720")
    assert opened == ["COM6", "COM9"]
