"""Read a TeensyROM firmware hex: which cartridge it is for, its version
string and its build stamp, so the board can be checked against the file
written to it. Ported from the bench library's ``hexfile.py``.

One hex holds two images at fixed flash addresses. The build pins
``SOURCE_DATE_EPOCH`` (the commit time for a clean tree, the build's own
time for a dirty one), so every image in one build carries one
``__DATE__``/``__TIME__`` pair, and that pair is the banner's second line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# The FLASH regions the two images are linked against (tools/BootLinkerFiles).
MINIMAL_BASE = 0x60000000
MAIN_BASE, MAIN_BYTES = 0x60060000, 7552 * 1024
MAIN_LIMIT = MAIN_BASE + MAIN_BYTES

_DATA, _END_OF_FILE, _EXTENDED_ADDRESS = 0, 1, 4
_ENTRY_POINT = (3, 5)
_RECORD_OVERHEAD = 5
_ERASED = 0xFF

# __DATE__ and __TIME__ as GCC emits them: NUL-terminated literals that do not
# continue a longer printable run.
_DATE = re.compile(rb"(?<![!-~])([A-Z][a-z]{2} [ 0-3][0-9] [0-9]{4})\x00")
_TIME = re.compile(rb"(?<![!-~])([0-2][0-9]:[0-5][0-9]:[0-5][0-9])\x00")
# strVersionNumber as Common_Defs.h defines it, tag and dirty marker included.
_VERSION = re.compile(rb"(TeensyROM\+? v[A-Za-z0-9.\-]+\*?)\x00")


class HexError(ValueError):
    pass


@dataclass(frozen=True)
class HexInfo:
    path: Path
    kind: str | None            # tr-plus | tr | None
    version: str | None         # "TeensyROM+ v0.9-dma.tim*"
    stamp: str | None           # "Oct 30 2026, 10:00:00", as the banner prints it
    date: str | None
    time: str | None

    @property
    def tag(self) -> str | None:
        if not self.version:
            return None
        m = re.search(r" v[\d.]+(?:-([A-Za-z0-9.\-]+))?", self.version)
        return m.group(1) if m else None

    @property
    def dirty(self) -> bool:
        return bool(self.version) and self.version.endswith("*")


def decode(text: str) -> list[tuple[int, bytes]]:
    """Intel HEX as (address, data) blocks in file order, honouring extended
    linear address records."""
    blocks: list[tuple[int, bytes]] = []
    page = 0
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":") or len(line) % 2 != 1:
            raise HexError(f"line {number}: not a HEX record")
        record = bytes.fromhex(line[1:])
        if (len(record) < _RECORD_OVERHEAD or len(record) != record[0] + _RECORD_OVERHEAD
                or sum(record) & 0xFF):
            raise HexError(f"line {number}: bad HEX length or checksum")
        count, address, kind = record[0], record[1] << 8 | record[2], record[3]
        if kind == _DATA:
            blocks.append((page + address, record[4:4 + count]))
        elif kind == _EXTENDED_ADDRESS:
            page = (record[4] << 8 | record[5]) << 16
        elif kind != _END_OF_FILE and kind not in _ENTRY_POINT:
            raise HexError(f"line {number}: HEX record type {kind} is not implemented")
    if not blocks:
        raise HexError("no data records")
    return blocks


def region(blocks: list[tuple[int, bytes]], base: int, limit: int) -> bytes:
    """The bytes those blocks place between two addresses, gaps left erased."""
    inside = []
    for a, d in blocks:
        if a < limit and a + len(d) > base:
            start, stop = max(a, base), min(a + len(d), limit)
            inside.append((start, d[start - a:stop - a]))
    if not inside:
        return b""
    low = min(a for a, _ in inside)
    high = max(a + len(d) for a, d in inside)
    block = bytearray([_ERASED]) * (high - low)
    for a, d in inside:
        block[a - low:a - low + len(d)] = d
    return bytes(block)


def inspect_hex(path: str | Path) -> HexInfo:
    path = Path(path)
    main = region(decode(path.read_text(encoding="ascii")), MAIN_BASE, MAIN_LIMIT)
    dates = {m.group(1).decode() for m in _DATE.finditer(main)}
    times = {m.group(1).decode() for m in _TIME.finditer(main)}
    versions = {m.group(1).decode("latin-1") for m in _VERSION.finditer(main)}
    date = dates.pop() if len(dates) == 1 else None
    time_ = times.pop() if len(times) == 1 else None
    version = versions.pop() if len(versions) == 1 else None
    kind = None
    if version:
        kind = "tr-plus" if version.startswith("TeensyROM+") else "tr"
    stamp = f"{date}, {time_}" if date and time_ else None
    return HexInfo(path=path, kind=kind, version=version, stamp=stamp, date=date, time=time_)


def encode_hex(address: int, data: bytes) -> str:
    """Intel HEX text for one contiguous block (self-tests build fixtures with it)."""
    lines = []
    page = -1
    for offset in range(0, len(data), 16):
        a = address + offset
        if (a >> 16) != page:
            page = a >> 16
            rec = bytes([2, 0, 0, _EXTENDED_ADDRESS, page >> 8, page & 0xFF])
            lines.append(":" + (rec + bytes([(-sum(rec)) & 0xFF])).hex().upper())
        chunk = data[offset:offset + 16]
        rec = bytes([len(chunk), (a >> 8) & 0xFF, a & 0xFF, _DATA]) + chunk
        lines.append(":" + (rec + bytes([(-sum(rec)) & 0xFF])).hex().upper())
    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"
