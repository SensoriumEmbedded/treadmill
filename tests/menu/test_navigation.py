"""Test 6: moving through the menu with keys: the three sources, into a
directory and back, the SID info page and back, every step confirmed on the
screen."""

import pytest

from treadmill.c64.keys import F6, SPACE
from treadmill.c64.menu import SOURCES, UP_DIR, parse_listing

pytestmark = pytest.mark.needs("keys_kernal")


def test_sources(menu, goldens):
    root = goldens.load("menu/root")
    expected = parse_listing([row if row is not None else "?" * 40 for row in root.rows])
    for drive, name in SOURCES.items():
        listing = menu.select_source(drive)
        assert listing.source == name and listing.is_root, listing
    listing = menu.select_source("teensy")
    assert listing.names == expected.names, listing.names


def test_into_a_directory_and_back(menu):
    root = menu.select_source("teensy")
    inside = menu.open("/Games")
    assert inside.entries[0].name == UP_DIR and "Joust!" in inside.names, inside.names
    assert any(e.handler for e in inside.entries), "no item with an IO handler shows its '+'"
    back = menu.up()
    assert back.entries == root.entries


def test_sid_info_page_and_back(menu, screen):
    menu.select_source("teensy")
    menu.press(F6)
    screen.wait_text("SID Info Page")
    menu.press(SPACE)
    assert menu.wait(lambda listing: listing.is_root, what="the listing after the SID info page").is_root
