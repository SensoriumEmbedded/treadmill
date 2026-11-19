"""Test 18's first half: the keyboard board's V switches a Kawari C64 to the
other standard and back. The TR's banner follows each switch, the picture
reads with the calibration and glyph bank kept for the new standard, and the
TOD line runs at the new standard's rate when the station's rate is
"standard". The TOD matrix itself comes next."""

import pytest

from treadmill.c64.menu import LEGEND_ROW1
from treadmill.station.kawari import OTHER, set_standard

pytestmark = pytest.mark.needs("region_switch")

TOD_HZ = {"PAL": "50", "NTSC": "60"}


def _check(board, screen_now, standard: str) -> None:
    assert board.station.standard == standard
    assert board.link.banner().video == standard
    screen_now().wait_text(LEGEND_ROW1.strip(), row=23)
    if board.station.keyboard.tod == "standard" and board.station.has("tod_select"):
        assert board.kbd.tod() == TOD_HZ[standard]


def test_region_switch_and_back(at_menu, screen_now):
    board = at_menu
    start = board.station.standard
    if start not in OTHER:
        pytest.skip(f"the banner reports no standard: {board.banner.raw if board.banner else None}")
    try:
        assert set_standard(board, OTHER[start])
        _check(board, screen_now, OTHER[start])
    finally:
        set_standard(board, start)
    _check(board, screen_now, start)
