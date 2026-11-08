"""Test 5: the item-type marks the menu shows, on the Teensy Mem listings the
goldens know and on a card directory with one file of every type."""

import pytest

from treadmill.c64.menu import MARK_DIR
from treadmill.fixtures.sdcard import FIXTURE_ROOT, TYPES_DIR, expected_listing, type_set


def test_root_listing(menu, screen, goldens):
    """The Teensy Mem root: every entry a directory, and the rows the golden has."""
    listing = menu.ensure_root()
    assert listing.entries and all(e.mark == MARK_DIR for e in listing.entries), listing.entries
    goldens.check(screen.wait_settled(), "menu/root")


@pytest.mark.needs("keys_kernal")
@pytest.mark.parametrize("directory, golden", [("/Games", "menu/games"), ("/Pictures", "menu/pictures")])
def test_teensy_mem_directory(menu, screen, goldens, directory, golden):
    menu.select_source("teensy")
    menu.open(directory)
    try:
        goldens.check(screen.wait_settled(), golden)
    finally:
        menu.up()


@pytest.mark.needs("keys_kernal")
def test_card_type_set(menu, screen, goldens, sd_fixtures, station):
    """One file per mark in one directory of the card: the listing shows each
    file with the mark its extension earns, in the menu's sort order."""
    menu.select_source("sd")
    menu.open(FIXTURE_ROOT)                                   # "/treadmill", as the listing names a directory
    listing = menu.open("/" + TYPES_DIR.rsplit("/", 1)[-1])   # "/types"
    assert len(listing.entries) == len(sd_fixtures) + 1, listing.names      # files, one subdirectory, no stray
    show_extensions = bool(station.settings_baseline.get("show_extensions", False))
    goldens.check_rows(screen.wait_settled().rows(), expected_listing(type_set(), show_extensions=show_extensions),
                       "menu/sd-types")
