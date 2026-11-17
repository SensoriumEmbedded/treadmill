"""The link against the fake board: framing, refusals, guards and the
unprompted text between commands."""

import threading
import time

import pytest

from treadmill.c64.screen import (
    C128_RESET, KEYBUF, KEYBUF_128, KEYCOUNT, KEYCOUNT_128, RESET_VECTOR, SCREEN_BYTES,
)
from treadmill.link import Link, LinkError, NotAllowed, Refused, trlink
from treadmill.link.transport import ByteStream, TransportError, open_pipe
from treadmill.link.protocol import DRIVE_SD
from selftest.fake_board import BANNER, BANNER_TR, start_fake_board

C64_RESET = 0xFCE2


@pytest.fixture
def board_and_link():
    chatter = []
    board, stream = start_fake_board()
    link = Link(stream, on_unsolicited=chatter.append)
    yield board, link, chatter
    link.close()


def test_probe_identifies_a_tr_plus_main_image(board_and_link):
    board, link, _ = board_and_link
    probe = link.probe()
    assert probe.image == "main" and probe.banner.uid == "19277260" and probe.dma
    assert link.dma_allowed and not link.c128


def test_dma_is_refused_until_the_probe_allows_it(board_and_link):
    board, link, _ = board_and_link
    with pytest.raises(NotAllowed):
        link.read_memory(0x0400, 16)
    assert board.received == []                 # nothing was sent
    link.probe()
    board.memory[0x0400:0x0404] = bytes([0x14, 0x05, 0x18, 0x14])
    dump = link.read_screen()
    assert dump.address == 0x0400 and dump.rows[0].startswith("text")
    assert len(dump.codes) == SCREEN_BYTES


def test_plain_tr_never_gets_dma(board_and_link):
    board, link, _ = board_and_link
    board.banner = BANNER_TR
    board.dma = False
    probe = link.probe()
    assert probe.banner.kind == "tr" and not probe.dma and not link.dma_allowed
    with pytest.raises(NotAllowed):
        link.write_memory(0xC6, b"\x01")


def test_keys_go_into_the_kernal_buffer(board_and_link):
    board, link, _ = board_and_link
    link.probe()
    link.key(0x85)
    # The fake's menu takes the keys as the real menu's GETIN loop does: the buffer is empty again.
    assert board.memory[KEYBUF] == 0x85 and board.memory[KEYCOUNT] == 0
    link.keys(b"run\r")
    assert bytes(board.memory[KEYBUF:KEYBUF + 4]) == b"run\r" and board.memory[KEYCOUNT] == 0


def _c128_with_reset_vector(board, link, vector: int) -> None:
    board.banner = BANNER.replace("C64 ", "C128")
    board.memory[RESET_VECTOR:RESET_VECTOR + 2] = vector.to_bytes(2, "little")
    link.probe()
    assert link.c128


def test_a_c128_at_the_tr_menu_gets_keys_in_the_c64_buffer(board_and_link):
    board, link, _ = board_and_link
    _c128_with_reset_vector(board, link, C64_RESET)        # 64 mode, where the TR menu runs it
    link.key(0x85)
    assert board.memory[KEYBUF] == 0x85 and board.memory[KEYCOUNT] == 0
    assert board.memory[KEYBUF_128] != 0x85


def test_a_c128_in_128_mode_gets_keys_in_its_own_buffer(board_and_link):
    board, link, _ = board_and_link
    _c128_with_reset_vector(board, link, C128_RESET)
    board.memory[KEYCOUNT_128] = 0
    link.keys(b"run\r")
    assert bytes(board.memory[KEYBUF_128:KEYBUF_128 + 4]) == b"run\r" and board.memory[KEYCOUNT_128] == 4
    assert board.memory[KEYBUF] != ord("r")


def test_ping_ready_then_busy_after_a_launch(board_and_link):
    board, link, chatter = board_and_link
    assert link.ping() == "TeensyROM+ v0.9 ready!"
    link.launch("/GAME.PRG", "sd")
    assert board.launched == [(DRIVE_SD, "/GAME.PRG")]
    with pytest.raises(Refused) as e:
        link.ping()
    assert e.value.text == "Busy!"
    # The handler-swap line the board printed after the launch was captured,
    # not mistaken for a reply.
    assert any(b"Loading IO handler" in c for c in chatter)
    assert "Reset cmd received" in link.reset()
    assert link.ping().endswith("ready!")


def test_file_round_trip_with_the_stats_trailer(board_and_link):
    board, link, _ = board_and_link
    board.directories.append("/T")
    data = bytes(range(256)) * 4
    link.push_file(data, "/T/A.PRG")
    assert board.files["/T/A.PRG"] == data
    link.push_file(b"new", "/T/A.PRG")                 # replace: delete, then post
    assert board.files["/T/A.PRG"] == b"new"
    assert link.get_file("/T/A.PRG") == b"new"
    board.listing = [{"type": "file", "name": "A.PRG", "size": 3}]
    assert link.list_directory("/T") == board.listing
    with pytest.raises(Refused) as e:
        link.list_directory("/missing")
    assert "Directory not found" in e.value.text
    link.delete_file("/T/A.PRG")
    with pytest.raises(Refused):
        link.delete_file("/T/A.PRG")
    # After all that, the trailers were consumed: a plain command still frames.
    assert link.ping().endswith("ready!")


def test_minimal_image_answers_only_its_four(board_and_link):
    board, link, _ = board_and_link
    board.image = "minimal"
    assert link.fw_check() == "minimal"
    with pytest.raises(Refused) as e:
        link.list_directory("/")
    assert e.value.text == "Busy!"


def test_silent_board_gives_none(board_and_link):
    board, link, _ = board_and_link
    board.silent = True
    assert link.fw_check() is None


def test_reset_boot_polls(board_and_link):
    board, link, _ = board_and_link
    board.boot_polls = 2
    link.reset()
    assert link.banner().boot == "in progress"
    assert link.banner().boot == "in progress"
    assert link.banner().boot == "complete"


# -- drain_when_pending: a command waits only for stray bytes ---------------------------------

def _arrived(stream, count: int = 1, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while len(stream.tap.chunks) < count and time.monotonic() < deadline:
        time.sleep(0.005)


def test_settle_returns_at_once_on_a_quiet_stream_and_waits_out_stray_bytes():
    stream, far = open_pipe()
    try:
        t0 = time.monotonic()
        assert stream.settle(5.0, 10.0) == (b"", True)          # 0.5 s of quiet on the pipe, not waited
        assert time.monotonic() - t0 < 0.25
        far.write(b"Loading IO handler: TeensyROM\n")
        _arrived(stream)
        t0 = time.monotonic()
        assert stream.settle(1.0, 10.0) == (b"Loading IO handler: TeensyROM\n", True)
        assert time.monotonic() - t0 >= 0.1
    finally:
        stream.close()


def test_settle_gives_up_on_a_stream_that_never_goes_quiet():
    stream, far = open_pipe()
    stop = threading.Event()

    def flood():
        while not stop.is_set():
            far.write(b"?Stat: c3\n")
            time.sleep(0.005)
    thread = threading.Thread(target=flood, daemon=True)
    thread.start()
    try:
        _arrived(stream)
        t0 = time.monotonic()
        stale, quiet = stream.settle(1.0, 0.5)
        assert not quiet and b"?Stat: c3\n" in stale
        assert 0.5 <= time.monotonic() - t0 < 1.5
    finally:
        stop.set()
        thread.join()
        stream.close()


def test_with_drain_when_pending_stray_text_is_logged_and_a_flood_refuses_the_command(board_and_link, monkeypatch):
    board, link, chatter = board_and_link
    link.probe()
    link.drain_when_pending = True
    board.say("Loading IO handler: TeensyROM\n")
    _arrived(link.stream, len(link.stream.tap.chunks) + 1)
    assert len(link.read_memory(KEYBUF, 4)) == 4
    assert b"Loading IO handler: TeensyROM\n" in b"".join(chatter)
    monkeypatch.setattr(trlink, "FLOOD_S", 0.3)
    stop = threading.Event()

    def flood():
        while not stop.is_set():
            board.say("?Stat: c3\n")
            time.sleep(0.005)
    seen = len(link.stream.tap.chunks)
    thread = threading.Thread(target=flood, daemon=True)
    thread.start()
    try:
        _arrived(link.stream, seen + 1)       # a flood already running; one starting mid-reply is not this guard's
        with pytest.raises(LinkError, match=r"did not go quiet in 0\.3 s, so nothing was sent: '\?Stat: c3'"):
            link.read_memory(KEYBUF, 4)
    finally:
        stop.set()
        thread.join()


def test_a_reset_goes_through_a_board_printing_in_a_loop(board_and_link):
    """A TR+ printing '?Stat: c3' for each status its stuck C64 writes never
    goes quiet for a drain; the reset is sent through it, and its reply read
    once the loop stops with the C64."""
    board, link, _ = board_and_link
    link.probe()
    link.drain_when_pending = True
    seen = len(link.stream.tap.chunks)
    board.flood("?Stat: c3\n")
    _arrived(link.stream, seen + 1)
    assert "Reset cmd received" in link.reset(through_noise=True) and not board.flooding


def test_a_write_the_port_refuses_is_a_transport_error():
    """pyserial raises 'Write timeout' (an OSError) when the board stopped
    reading or has gone; the stream says so as a TransportError, which the
    link's callers handle as a board that cannot be reached."""
    class Refusing:
        def read(self, n):
            time.sleep(0.05)
            return b""

        def write(self, data):
            raise OSError("Write timeout")

        def close(self):
            pass
    stream = ByteStream(Refusing(), "refusing")
    try:
        with pytest.raises(TransportError, match="refusing: Write timeout"):
            stream.write(b"d")
    finally:
        stream.close()
