"""Test 3: files to the card and back over USB serial, at full speed, from a
slow sender, and after a transfer that stalled."""

import random

import pytest

from treadmill.fixtures.sdcard import TRANSFER_DIR
from treadmill.link import Refused

SIZE_512K = 512 * 1024
SLOW_SIZE = 8 * 1024
SLOW_PACE = (64, 0.1)                # 64 bytes every 100 ms: under 2 bytes a millisecond, well inside the half-second byte timeout
STALL_SIZE, STALL_AT = 64 * 1024, 16 * 1024


def _bytes(seed: int, size: int) -> bytes:
    return random.Random(seed).randbytes(size)


def _names(link, directory: str) -> dict[str, int]:
    try:
        return {e["name"]: e.get("size") for e in link.list_directory(directory) if e.get("type") == "file"}
    except Refused:
        return {}


@pytest.mark.duration(1)
def test_post_get_delete_round_trip(link, sd_card):
    data = _bytes(0x5EED, SIZE_512K)
    path = f"{TRANSFER_DIR}/rt512k.bin"
    link.push_file(data, path)
    assert _names(link, TRANSFER_DIR).get("rt512k.bin") == SIZE_512K
    assert link.get_file(path) == data
    link.delete_file(path)
    assert "rt512k.bin" not in _names(link, TRANSFER_DIR)
    with pytest.raises(Refused):
        link.get_file(path)


def test_post_refuses_an_existing_file(link, sd_card):
    path = f"{TRANSFER_DIR}/twice.bin"
    link.push_file(b"first", path)
    try:
        with pytest.raises(Refused) as e:
            link.post_file(b"second", path)
        assert "exists" in e.value.text.lower(), e.value.text
        assert link.get_file(path) == b"first"
    finally:
        link.delete_file(path)


@pytest.mark.duration(1)
def test_slow_sender(link, sd_card):
    data = _bytes(0x5107, SLOW_SIZE)
    path = f"{TRANSFER_DIR}/slow.bin"
    try:
        link.delete_file(path)
    except Refused:
        pass
    link.post_file(data, path, pace=SLOW_PACE)
    try:
        assert link.get_file(path) == data
    finally:
        link.delete_file(path)


def test_stall_then_retry(link, sd_card):
    """A host that stops sending mid-file: the board gives the transfer up,
    leaves no partial file, and takes the whole file on the next try."""
    data = _bytes(0x57A1, STALL_SIZE)
    path = f"{TRANSFER_DIR}/stalled.bin"
    try:
        link.delete_file(path)
    except Refused:
        pass
    with pytest.raises(Refused) as e:
        link.post_file(data, path, truncate=STALL_AT)
    assert "stalled.bin" not in _names(link, TRANSFER_DIR), f"a partial file was left behind after: {e.value.text}"
    link.post_file(data, path)
    try:
        assert link.get_file(path) == data
    finally:
        link.delete_file(path)
