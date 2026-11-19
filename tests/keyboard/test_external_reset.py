"""A reset the TR did not make: the keyboard board pulls the C64's /RESET,
and a TR+ senses it, says 'Button detected', and brings the menu back. From
the menu, and from TR BASIC, whose handler ignores every host command: the
case the recovery ladder's ext-reset rung exists for, which before the
keyboard board took a USB reboot."""

import time

import pytest

from treadmill.c64.menu import LEGEND_ROW1

pytestmark = pytest.mark.needs("ext_reset", "reset_sense")

TR_BASIC_ITEM = "/TeensyROM Specific/BASIC TR Command IO access only"
READY_S = 10.0              # the item's launch, to BASIC's READY under the TR BASIC handler


def _back_at_the_menu(board, screen, since: float) -> None:
    board.wait_for_menu()
    assert "Button detected" in board.link.stream.tap.since(since).decode("latin-1"), \
        "the TR+ did not sense the reset"
    assert board.events.current_handler() == "TeensyROM"
    screen.wait_text(LEGEND_ROW1.strip(), row=23)


def test_external_reset_from_the_menu(at_menu, screen):
    t0 = time.monotonic()
    at_menu.ext_reset()
    _back_at_the_menu(at_menu, screen, t0)


def test_external_reset_brings_tr_basic_back(at_menu, screen):
    board = at_menu
    board.link.launch(TR_BASIC_ITEM, "teensy")
    board.events.feed(board.link.stream.read_idle(READY_S, 1.0, READY_S))
    assert board.events.current_handler() == "TR_BASIC"
    t0 = time.monotonic()
    board.ext_reset()
    _back_at_the_menu(board, screen, t0)
