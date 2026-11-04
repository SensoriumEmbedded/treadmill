"""Framing, byte order and the drift check against the firmware source."""

import os
import re
from pathlib import Path

import pytest

from treadmill.link import protocol as P

DEFINITIONS = [
    re.compile(r"^\s*#define\s+(\w+)\s+(0x[0-9A-Fa-f]+)\b", re.M),
    re.compile(r"^\s*const\s+uint\d+_t\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;", re.M),
    re.compile(r"^\s*(\w+)\s*=\s*(\d+)\s*,", re.M),
]


def test_byte_orders():
    assert P.to_board(0x6444) == b"\x64\x44"
    assert P.to_board(0x12345678, 4) == b"\x12\x34\x56\x78"
    assert P.from_board(b"\xcc\x64") == P.ACK
    assert P.board_reply(P.FAIL) == b"\x7f\x9b"
    with pytest.raises(P.ProtocolError):
        P.to_board(0x10000)


def test_path_field_rules():
    assert P.path_field("/a.prg") == b"/a.prg\0"
    with pytest.raises(P.ProtocolError):
        P.path_field("a\0b")
    with pytest.raises(P.ProtocolError):
        P.path_field("Ā")
    with pytest.raises(P.ProtocolError):
        P.path_field("x" * (P.MAX_PATH_BYTES + 1))
    assert P.path_field("x" * P.MAX_PATH_BYTES).endswith(b"\0")


def test_checksum_is_16_bit():
    assert P.checksum(b"\xff" * 300) == (255 * 300) & 0xFFFF


def test_every_wire_value_matches_the_firmware_source():
    """Pins every token to Source/Teensy of the configured checkout, so a moved
    token fails here rather than on a board."""
    root = os.environ.get("TREADMILL_FW_REPO")
    if not root:
        pytest.skip("TREADMILL_FW_REPO not set")
    source = Path(root) / "Source" / "Teensy"
    found: dict[str, set[int]] = {}
    for path in source.rglob("*"):
        if path.suffix not in {".h", ".c", ".cpp", ".ino"}:
            continue
        text = path.read_text(encoding="latin-1")
        for pattern in DEFINITIONS:
            for name, value in pattern.findall(text):
                found.setdefault(name, set()).add(int(value, 0))
    drifted = [f"{name}: ours 0x{ours:x}, firmware {sorted(found.get(name, set()))}"
               for name, ours in P.FIRMWARE_NAMES.items()
               if name not in found or any(v != ours for v in found[name])]
    assert drifted == []
