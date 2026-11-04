"""The link against the fake board: framing, refusals, guards and the
unprompted text between commands."""

import pytest

from treadmill.c64.screen import KEYBUF, KEYCOUNT, SCREEN_BYTES
from treadmill.link import Link, NotAllowed, Refused
from treadmill.link.protocol import DRIVE_SD
from selftest.fake_board import BANNER_TR, start_fake_board


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
    assert board.memory[KEYBUF] == 0x85 and board.memory[KEYCOUNT] == 1
    board.memory[KEYCOUNT] = 0
    link.keys(b"run\r")
    assert bytes(board.memory[KEYBUF:KEYBUF + 4]) == b"run\r" and board.memory[KEYCOUNT] == 4


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
