"""The TeensyROM control protocol's wire values (``docs/ControlComms.md``).

Commands and their integer fields go to the board most significant byte
first; the board answers least significant byte first. ``FIRMWARE_NAMES``
maps each value to the C identifier it came from, and the protocol-drift
self-test reads those out of ``Source/Teensy`` so a moved token fails a test
rather than a board.
"""

from __future__ import annotations

from treadmill.errors import TreadmillError


class ProtocolError(TreadmillError, ValueError):
    """A value that cannot go on the wire as asked."""


# Commands. Always available, whatever else the board is doing:
LAUNCH_FILE = 0x6444
RESET_C64 = 0x64EE
VERSION_INFO = 0x6476
FW_CHECK = 0x64E0
C64_PAUSE_ON = 0x6431
C64_PAUSE_OFF = 0x6430
READ_C64_MEM = 0x64FD      # TR+ main image only
WRITE_C64_MEM = 0x64FB     # TR+ main image only
WRITE_C64_SPANS = 0x64FC   # TR+ main image only
# Refused with FAIL + "Busy!" unless the board is sitting at its own menu:
PING = 0x6455
POST_FILE = 0x64BB
GET_FILE = 0x64B0
DELETE_FILE = 0x64CF
GET_DIR_NDJSON = 0x64DE
SET_RTC = 0x6474

# Replies.
ACK = 0x64CC
FAIL = 0x9B7F
FW_FULL = 0x64E2
FW_MINIMAL = 0x64E1
DIR_START = 0x5A5A
DIR_END = 0xA5A5

# Unprompted tokens the main image sends when it parses a SID header.
GOOD_SID = 0x9B81
BAD_SID = 0x9B80

IMAGES = {FW_FULL: "main", FW_MINIMAL: "minimal"}

# Drive numbers in file and directory commands.
DRIVES = {"usb": 0, "sd": 1, "teensy": 2}
DRIVE_USB, DRIVE_SD, DRIVE_TEENSY = 0, 1, 2

# The commands the main image only runs at the menu. After each, the firmware
# appends two 32-bit stats (Stats.Unlocked, then F_CPU_ACTUAL) that a reader
# must consume before the next reply.
MENU_ONLY = frozenset({POST_FILE, DELETE_FILE, GET_DIR_NDJSON, GET_FILE})
STATS_TRAILER_BYTES = 8

# The firmware's own name for each wire value.
FIRMWARE_NAMES = {
    "LaunchFileToken": LAUNCH_FILE,
    "ResetC64Token": RESET_C64,
    "VersionInfoToken": VERSION_INFO,
    "FWCheckToken": FW_CHECK,
    "C64PauseOnToken": C64_PAUSE_ON,
    "C64PauseOffToken": C64_PAUSE_OFF,
    "ReadC64MemToken": READ_C64_MEM,
    "WriteC64MemToken": WRITE_C64_MEM,
    "WriteC64SpansToken": WRITE_C64_SPANS,
    "PingToken": PING,
    "PostFileToken": POST_FILE,
    "GetFileToken": GET_FILE,
    "DeleteFileToken": DELETE_FILE,
    "GetDirNDJSONToken": GET_DIR_NDJSON,
    "SetRTCToken": SET_RTC,
    "AckToken": ACK,
    "FailToken": FAIL,
    "FWFullToken": FW_FULL,
    "FWMinimalToken": FW_MINIMAL,
    "StartDirectoryListToken": DIR_START,
    "EndDirectoryListToken": DIR_END,
    "GoodSIDToken": GOOD_SID,
    "BadSIDToken": BAD_SID,
    "rmtUSBDrive": DRIVE_USB,
    "rmtSD": DRIVE_SD,
    "rmtTeensy": DRIVE_TEENSY,
}

# GetPathParameter() in FileTransfer.ino reads into MaxNamePathLength bytes
# (Common_Defs.h). A longer path, or one with a NUL inside, leaves bytes the
# board then reads as commands of their own, so both are refused here.
MAX_PATH_BYTES = 256 + 100 + 2 - 1


def to_board(value: int, width: int = 2) -> bytes:
    """A token or an integer parameter, most significant byte first."""
    if value < 0 or value >= 1 << (8 * width):
        raise ProtocolError(f"{value} does not fit in {width} byte(s)")
    return value.to_bytes(width, "big")


def from_board(data: bytes) -> int:
    """The 16-bit value in a two-byte reply, least significant byte first."""
    return int.from_bytes(data[:2], "little")


def board_reply(value: int) -> bytes:
    """The two bytes the board sends for a 16-bit value: the inverse of
    ``from_board``, for finding a reply in a stream."""
    return value.to_bytes(2, "little")


def path_field(path: str) -> bytes:
    """A path as the board reads it: Latin-1, NUL-terminated, bounded."""
    try:
        raw = path.encode("latin-1")
    except UnicodeEncodeError as e:
        raise ProtocolError("a path holds Latin-1 characters only") from e
    if b"\0" in raw:
        raise ProtocolError("a path cannot contain NUL")
    if len(raw) > MAX_PATH_BYTES:
        raise ProtocolError(f"a path is at most {MAX_PATH_BYTES} bytes")
    return raw + b"\0"


def checksum(data: bytes) -> int:
    """The 16-bit sum the file transfer commands carry."""
    return sum(data) & 0xFFFF
