"""``Board`` against the fake board: a reconnect probes again and keeps the
run's capabilities, and an open that fails holds nothing."""

import os

import pytest

from selftest.fake_board import start_fake_board
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from treadmill.board import Board
from treadmill.link import Link, TransportError
from treadmill.station import load_station
from treadmill.station.lock import HELD_ENV, RigLock


def test_reconnect_probes_again_and_keeps_the_caps(lab):  # noqa: F811
    _, first = start_fake_board()
    _, second = start_fake_board()
    board = Board.open(load_station("s1"), link_factory=lambda: Link(first),
                       fw_defines=("Dbg_SerDMA",), caps_mask={"screen_dma"})
    try:
        assert board.link.dma_allowed
        assert board.station.has("dma", "fw:Dbg_SerDMA") and not board.station.has("screen_dma")
        board.link.ping()
        before = list(board.link.stream.tap.chunks)
        assert before
        board.reboot_hook = lambda: None
        board.reconnect_hook = lambda: Link(second)
        first.close()                                   # the USB went away under the link
        board.ensure_menu()
        assert board.recoveries == ["usb-reboot"] and board.link.stream is second
        assert board.link.dma_allowed and not board.probe_pending
        assert board.station.has("dma", "fw:Dbg_SerDMA") and not board.station.has("screen_dma")
        assert board.link.stream.tap.chunks[:len(before)] == before
    finally:
        board.close()


def test_open_holds_nothing_when_the_probe_fails(lab):  # noqa: F811
    _, stream = start_fake_board()
    stream.close()                                      # a port that opens, with nothing answering through it
    with pytest.raises(TransportError):
        Board.open(load_station("s1"), link_factory=lambda: Link(stream))
    assert "rigA" not in os.environ.get(HELD_ENV, "")
    lock = RigLock("rigA").acquire()                    # free: neither busy nor a nested hold
    assert not lock.nested
    lock.release()
