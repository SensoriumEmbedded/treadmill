"""``Board`` against the fake board: a reconnect probes again and keeps the
run's capabilities, and an open that fails holds nothing."""

import os

import pytest

from selftest.fake_board import start_fake_board
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from treadmill.board import Board, Wedged
from treadmill.link import Link, NotFound, TransportError
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
        board.link.drain_when_pending = True
        before = list(board.link.stream.tap.chunks)
        assert before
        reboots = []
        board.reboot_hook = lambda: reboots.append("reboot")
        board.reconnect_hook = lambda: Link(second)
        first.close()                                   # the USB went away under the link
        board.ensure_menu()
        assert board.recoveries == ["reconnect"] and board.link.stream is second
        assert reboots == [], "a board back on USB by itself needs no reboot (backlog 42)"
        assert board.link.dma_allowed and not board.probe_pending and board.link.drain_when_pending
        assert board.station.has("dma", "fw:Dbg_SerDMA") and not board.station.has("screen_dma")
        assert board.link.stream.tap.chunks[:len(before)] == before
    finally:
        board.close()


def test_a_handler_that_ignores_the_port_goes_straight_to_the_reboot(lab):  # noqa: F811
    """Under TR BASIC the firmware drops every host command, so a reset could
    only wait out the boot timeout: the ladder starts at the USB reboot."""
    _, first = start_fake_board()
    _, second = start_fake_board()
    board = Board.open(load_station("s1"), link_factory=lambda: Link(first))
    try:
        board.events.feed(b"Loading IO handler: TR_BASIC\r\n")
        resets = []
        board._rung_reset = lambda: resets.append("reset")
        board.reboot_hook = lambda: None
        board.reconnect_hook = lambda: Link(second)
        first.close()
        board.ensure_menu()
        assert resets == [] and board.recoveries == ["usb-reboot"]
    finally:
        board.close()


def test_without_a_reboot_the_ladder_still_tries_the_reset(lab):  # noqa: F811
    _, stream = start_fake_board()
    board = Board.open(load_station("s1"), link_factory=lambda: Link(stream))
    try:
        board.events.feed(b"Loading IO handler: TR_BASIC\r\n")
        resets = []
        board._rung_reset = lambda: resets.append("reset")
        board.wait_for_menu = lambda timeout=None: None
        stream.close()
        board.ensure_menu()
        assert resets == ["reset"] and board.recoveries == ["reset"]
    finally:
        board.close()


def test_a_reconnect_that_finds_no_board_marks_the_station_wedged(lab):  # noqa: F811
    """On a port, the reconnect hook looks the board up by chip id, and a
    board that never came back is NotFound: a rung that failed, not a crash."""
    _, stream = start_fake_board()
    board = Board.open(load_station("s1"), link_factory=lambda: Link(stream))
    try:
        board.reboot_hook = lambda: None

        def gone() -> Link:
            raise NotFound("no TeensyROM with chip id 1 on USB")

        board.reconnect_hook = gone
        stream.close()
        with pytest.raises(Wedged, match="usb-reboot: no TeensyROM"):
            board.ensure_menu()
        assert board.wedged_reason and board.recoveries == []
        with pytest.raises(Wedged):
            board.ensure_menu()                         # the rest of the run skips without retrying
    finally:
        board.close()


def test_open_on_a_port_attaches_the_ladder_s_upper_rungs(lab, monkeypatch):  # noqa: F811
    """On a real port the board can always be found again by chip id; the
    USB reboot needs the run to know the hex and the checkout that flashed it."""
    from pathlib import Path

    import treadmill.board as board_module
    from treadmill.firmware.hexinfo import HexInfo
    from treadmill.firmware.prepare import FirmwareRun
    from treadmill.firmware.spec import parse_spec
    from treadmill.link.discover import Found

    streams = iter([start_fake_board()[1], start_fake_board()[1]])
    monkeypatch.setattr(board_module, "find_port", lambda uid, hint=None, timeout=0.0: Found("COM0", uid, "banner"))
    monkeypatch.setattr(board_module, "open_serial", lambda port: next(streams))
    station = load_station("s1")
    board = Board.open(station)
    try:
        assert board.reconnect_hook is not None and board.reboot_hook is None
    finally:
        board.close()
    run = FirmwareRun(parse_spec("hex=x.hex"), hex=HexInfo(Path("x.hex"), "tr-plus", "TeensyROM+ v0.9",
                                                             "Oct 30 2026, 10:00:00", "Oct 30 2026", "10:00:00"),
                      primary=Path("."))
    board = Board.open(station, firmware=run)
    try:
        assert board.reconnect_hook is not None and board.reboot_hook is not None
    finally:
        board.close()


def test_the_reboot_rung_writes_a_board_waiting_alone_in_the_bootloader_unnamed(monkeypatch):
    """The 134-baud request sends the board into HalfKay, where it cannot say
    its chip id, so the flasher refuses it by --uid (backlog 42). When it is
    the one board waiting there, by the serial HalfKay shows (the chip id
    over ten, in hex), it is written without --uid; otherwise as before."""
    from pathlib import Path

    import treadmill.firmware.flash as flash_mod

    calls = []                                          # no port: no 134-baud request and no wait after it
    monkeypatch.setattr(flash_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(flash_mod, "flash", lambda info, uid, primary, **kw: calls.append(kw)
                        or flash_mod.Flashed(info, "banner", 0.0, False))
    assert flash_mod.bootloader_serial("123456780") == "BC614E"
    monkeypatch.setattr(flash_mod, "halfkay_serials", lambda: ["BC614E"])
    assert flash_mod.relaunch("hex", "123456780", Path("."), None, log=lambda m: None) == "banner"
    assert calls[-1]["named"] is False and calls[-1]["force"]
    for others in (["BC614E", "1234"], ["1234"], []):
        monkeypatch.setattr(flash_mod, "halfkay_serials", lambda others=others: others)
        assert not flash_mod.waiting_alone("123456780", timeout=0)
    monkeypatch.setattr(flash_mod, "waiting_alone", lambda uid: False)
    flash_mod.relaunch("hex", "123456780", Path("."), None, log=lambda m: None)
    assert "named" not in calls[-1], "still running, or beside another waiting board: by --uid, as before"


def test_open_holds_nothing_when_the_probe_fails(lab):  # noqa: F811
    _, stream = start_fake_board()
    stream.close()                                      # a port that opens, with nothing answering through it
    with pytest.raises(TransportError):
        Board.open(load_station("s1"), link_factory=lambda: Link(stream))
    assert "rigA" not in os.environ.get(HELD_ENV, "")
    lock = RigLock("rigA").acquire()                    # free: neither busy nor a nested hold
    assert not lock.nested
    lock.release()
