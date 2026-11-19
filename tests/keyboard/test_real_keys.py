"""The menu moved by real keys: the keyboard board presses them on the C64's
matrix (``HwKeys``), whatever the TR is and even where DMA keys would do. A
directory and back, then the SID info page by F6 and back by SPACE, each step
confirmed on the screen. The board's own coverage of every key is its
feature-test script; this is the path treadmill's menu tests take on a plain
TR."""

import pytest

from treadmill.c64.keys import F6, SPACE, HwKeys
from treadmill.c64.menu import UP_DIR, MenuDriver

pytestmark = pytest.mark.needs("keys_hw")


def test_menu_moves_on_real_keys(at_menu, kbd, screen):
    menu = MenuDriver(screen, HwKeys(kbd), board=at_menu)
    root = menu.select_source("teensy")
    inside = menu.open("/Games")
    assert inside.entries[0].name == UP_DIR and "Joust!" in inside.names, inside.names
    assert menu.up().entries == root.entries
    menu.press(F6)
    screen.wait_text("SID Info Page")
    menu.press(SPACE)
    assert menu.wait(lambda listing: listing.is_root, what="the listing after the SID info page").is_root
