"""The keyboard board's reset on the C64's /RESET line: ``Board.ext_reset()``
needs the capability, a fake TR+ senses the pulse, and the recovery ladder
takes it before a USB reboot for a TR+ that ignores the port."""

import pytest

from selftest.fake_board import start_fake_board
from selftest.fake_kbd import FakeKeyboard
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from selftest.test_station_keyboard import kb_lab  # noqa: F401  (a station whose machine has a keyboard board)
from treadmill.board import Board
from treadmill.link import Link
from treadmill.link.trlink import NotAllowed
from treadmill.session import open_station
from treadmill.station import load_station
from treadmill.station.kbd import KeyboardBoard


def _r_lines(kbd: KeyboardBoard) -> list[str]:
    return [line for _, line in kbd.sent if line.startswith("R")]


def test_ext_reset_pulses_reset_and_the_tr_plus_senses_it(kb_lab):  # noqa: F811
    with open_station("kb", video=False) as s:
        s.board.ext_reset()
        assert _r_lines(s.kbd) == ["R\n"]
        s.board.wait_for_menu(5.0)
        assert "Button detected" in b"".join(c for _, c in s.board.link.stream.tap.chunks).decode("latin-1")


def test_ext_reset_is_not_sent_without_the_capability(kb_lab):  # noqa: F811
    with open_station("kb", video=False, caps_mask={"ext_reset"}) as s:
        with pytest.raises(NotAllowed, match="lacks ext_reset"):
            s.board.ext_reset()
        assert _r_lines(s.kbd) == []


def _deaf_board(caps_mask=frozenset()):
    """A TR+ under TR BASIC: it answers nothing until it senses a reset."""
    fake, stream = start_fake_board()

    def sensed():
        fake.silent = False
        fake.external_reset()

    station = load_station("kb")
    board = Board.open(station, link_factory=lambda: Link(stream), caps_mask=caps_mask,
                       kbd_factory=lambda: KeyboardBoard.attach(
                           FakeKeyboard(chip=station.keyboard.chip, on_reset=sensed).connect()))
    board.events.feed(b"Loading IO handler: TR_BASIC\r\n")
    fake.silent = True
    return board


def test_a_deaf_tr_plus_comes_back_through_the_external_reset(kb_lab):  # noqa: F811
    """Without the keyboard board this took the USB reboot; a reset over the
    port would only wait out the boot timeout."""
    board = _deaf_board()
    try:
        resets, reboots = [], []
        board._rung_reset = lambda: resets.append("reset")
        board.reboot_hook = lambda: reboots.append("reboot")
        board.ensure_menu()
        assert board.recoveries == ["ext-reset"] and resets == [] and reboots == []
        assert _r_lines(board.kbd) == ["R\n"]
    finally:
        board.close()


@pytest.mark.parametrize("case", ["no reset sense", "keyboard board gone"])
def test_the_ladder_goes_past_an_external_reset_that_cannot_help(kb_lab, case):  # noqa: F811
    """A plain TR stays in its handler through a reset it didn't make, so the
    rung needs reset sensing; a keyboard board that stopped answering is a
    failed rung, not the end of the ladder."""
    board = _deaf_board(caps_mask={"reset_sense"} if case == "no reset sense" else frozenset())
    try:
        if case == "keyboard board gone":
            board.kbd.close()
        _, second = start_fake_board()
        board.reboot_hook = lambda: None
        board.reconnect_hook = lambda: Link(second)
        board.ensure_menu()
        assert board.recoveries == ["usb-reboot"]
        assert _r_lines(board.kbd) == []
    finally:
        board.close()
