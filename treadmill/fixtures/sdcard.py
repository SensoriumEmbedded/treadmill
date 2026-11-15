"""The SD card fixtures: a directory per set under ``/treadmill``, put there
over the remote protocol before the tests that read it.

The type set is one small, generated file per item-type mark the menu can
show for a file on a card, plus a subdirectory, so the listing of its
directory exercises every mark at once. Nothing in it is an asset: every
byte is made here, the same way every time, so the expected listing is
known without touching the card.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from treadmill.c64.menu import MARK_DIR, UP_DIR, Entry, mark_for, render_listing, shown_name, shown_path
from treadmill.errors import TreadmillError
from treadmill.link.trlink import Link, Refused

FIXTURE_ROOT = "/treadmill"
TYPES_DIR = f"{FIXTURE_ROOT}/types"
TRANSFER_DIR = f"{FIXTURE_ROOT}/transfer"
NAMES_DIR = f"{FIXTURE_ROOT}/names"
LONG_HEX = "TeensyROM+_0.9.0.69-DMA-noisr_full.hex"


class FixtureError(TreadmillError):
    pass


@dataclass(frozen=True)
class FixtureFile:
    name: str           # relative to the set's directory; "sub/inner.prg" lives in its subdirectory
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def mark(self) -> str:
        return mark_for(self.name)

    @property
    def folder(self) -> str:
        return self.name.rsplit("/", 1)[0] if "/" in self.name else ""

    @property
    def basename(self) -> str:
        return self.name.rsplit("/", 1)[-1]

    def manifest(self) -> dict:
        return {"name": self.name, "size": self.size, "sha256": self.sha256, "mark": self.mark}


# -- the generated files ------------------------------------------------------

_BASIC_SYS2061 = b"\x0b\x08\x0a\x00\x9e\x32\x30\x36\x31\x00\x00\x00"   # 10 SYS2061


def _prg() -> bytes:
    return b"\x01\x08" + _BASIC_SYS2061 + b"\x60"                      # RTS at 2061


def _crt() -> bytes:
    """An 8 KB cartridge whose ROM parks the CPU: the CRT header and one CHIP
    packet, as the CRT format defines them."""
    rom = bytearray(0x2000)
    rom[0:9] = b"\x09\x80\x09\x80" + b"\xc3\xc2\xcd\x38\x30"           # cold/warm start, CBM80
    rom[9:12] = b"\x4c\x09\x80"                                          # jmp $8009
    header = b"C64 CARTRIDGE   " + (0x40).to_bytes(4, "big") + b"\x01\x00" + b"\x00\x00" + b"\x00\x01" \
        + bytes(6) + b"TREADMILL TYPE SET".ljust(32, b"\x00")
    chip = b"CHIP" + (0x10 + len(rom)).to_bytes(4, "big") + b"\x00\x00" + b"\x00\x00" \
        + (0x8000).to_bytes(2, "big") + len(rom).to_bytes(2, "big")
    return header + chip + bytes(rom)


def _hex() -> bytes:
    payload = bytes(range(16))
    record = bytes([16, 0, 0, 0]) + payload
    total = (-sum(record)) & 0xFF
    line = ":" + record.hex().upper() + f"{total:02X}"
    return (line + "\r\n:00000001FF\r\n").encode("ascii")


def _p00() -> bytes:
    return b"C64File\x00" + b"TREADMILL".ljust(16, b"\x00") + b"\x00\x00" + _prg()


def _sid() -> bytes:
    """A PSID v2 file: the header the SID player parses, and a tune that
    returns at once from init and play."""
    header = b"PSID" + (2).to_bytes(2, "big") + (0x7C).to_bytes(2, "big") + bytes(2) \
        + (0x1000).to_bytes(2, "big") + (0x1003).to_bytes(2, "big") + (1).to_bytes(2, "big") \
        + (1).to_bytes(2, "big") + bytes(4) \
        + b"TReadmill type set".ljust(32, b"\x00") + b"TReadmill".ljust(32, b"\x00") \
        + b"2026".ljust(32, b"\x00") + bytes(2) + bytes(4)
    assert len(header) == 0x7C
    return header + b"\x00\x10" + b"\x60\xea\xea\x60"


def _kla() -> bytes:
    bitmap = bytes((i * 7) & 0xFF for i in range(8000))
    return b"\x00\x60" + bitmap + bytes([0x10] * 1000) + bytes([0x01] * 1000) + b"\x00"


def _art() -> bytes:
    bitmap = bytes((i * 3) & 0xFF for i in range(8000))
    return b"\x00\x20" + bitmap + bytes([0x10] * 1000) + bytes(7)


def _txt() -> bytes:
    return b"TReadmill type set: a text file.\r\nOne line more.\r\n"


def _seq() -> bytes:
    return "TREADMILL TYPE SET: A PETSCII FILE.\rONE LINE MORE.\r".encode("ascii")


def _unknown() -> bytes:
    return b"no known type\n"


def _d64() -> bytes:
    """An empty 35-track disk image: a BAM sector with the disk name, the
    rest zero. 174848 bytes, the one big file of the set."""
    image = bytearray(174848)
    bam = 17 * 21 * 256                                                 # track 18, sector 0
    image[bam:bam + 4] = b"\x12\x01\x41\x00"
    image[bam + 0x90:bam + 0xA0] = b"TREADMILL".ljust(16, b"\xa0")
    image[bam + 0xA2:bam + 0xA4] = b"TR"
    image[bam + 0xA5:bam + 0xA7] = b"2A"
    return bytes(image)


def type_set() -> list[FixtureFile]:
    """One file per mark the menu shows for a file on a card, a file of a
    type it does not know, and a subdirectory with one file in it."""
    return [
        FixtureFile("prg.prg", _prg()),
        FixtureFile("crt.crt", _crt()),
        FixtureFile("hex.hex", _hex()),
        FixtureFile("p00.p00", _p00()),
        FixtureFile("sid.sid", _sid()),
        FixtureFile("kla.kla", _kla()),
        FixtureFile("art.art", _art()),
        FixtureFile("txt.txt", _txt()),
        FixtureFile("seq.seq", _seq()),
        FixtureFile("d64.d64", _d64()),
        FixtureFile("unknown.xyz", _unknown()),
        FixtureFile("sub/inner.prg", _prg()),
    ]


def name_set() -> list[FixtureFile]:
    """Names around the listing's 34 columns: an extension whose dot falls
    just past the cut and a name still too long without its extension, both
    with dots inside the cut; a cut through the extension, which only shows
    with extensions on; a short dotted name."""
    return [
        FixtureFile(LONG_HEX, _hex()),
        FixtureFile("A.Very.Long.Program.Name.With.Many.Dots.prg", _prg()),
        FixtureFile("Partial.Extension.Cut.At.Column.prg", _prg()),
        FixtureFile("v1.2.prg", _prg()),
    ]


# -- what the menu shows for a set ----------------------------------------------

def _menu_order(name: str) -> bytes:
    """``strcasecmp`` order, which is how ``LoadDirectory`` sorts a card's listing."""
    return name.lower().encode("latin-1", "replace")


def menu_entries(files: list[FixtureFile], at_root: bool = False, show_extensions: bool = False) -> list[Entry]:
    """The entries the menu lists for the set's own directory: the up-dir
    entry, its subdirectories as ``/name`` and its files, sorted together by
    their whole names, each file shown as ``shown_name`` has it."""
    named = [] if at_root else [(UP_DIR, Entry(UP_DIR, MARK_DIR))]
    named += [("/" + d, Entry("/" + d, MARK_DIR)) for d in sorted({f.folder for f in files if f.folder})]
    named += [(f.basename, Entry(shown_name(f.basename, show_extensions), f.mark)) for f in files if not f.folder]
    return [entry for _, entry in sorted(named, key=lambda pair: _menu_order(pair[0]))]


def expected_listing(files: list[FixtureFile], source: str = "sd", show_extensions: bool = False,
                     directory: str = TYPES_DIR) -> list[str]:
    """The 25 rows the menu shows for the set's directory, with the version
    banner and the clock left open."""
    return render_listing(source, shown_path(directory), menu_entries(files, show_extensions=show_extensions))


# -- putting a set on the card ----------------------------------------------------

def _listing(link: Link, directory: str, drive: str) -> dict[str, int] | None:
    """Files directly in ``directory`` with their sizes, or None when the
    directory is not there."""
    try:
        entries = link.list_directory(directory, drive)
    except Refused:
        return None
    return {e["name"]: int(e.get("size", -1)) for e in entries if e.get("type") == "file"}


def provision(link: Link, files: list[FixtureFile], directory: str = TYPES_DIR, drive: str = "sd",
              log=None) -> list[dict]:
    """Make ``directory`` hold exactly ``files``: post the ones missing or of
    another size, delete files that are not in the set (a subdirectory not in
    the set stays: there is no remote command to remove one). Returns the
    set's manifest once the card's listing matches it."""
    log = log or (lambda m: None)
    by_folder: dict[str, list[FixtureFile]] = {}
    for f in files:
        by_folder.setdefault(f.folder, []).append(f)
    posted = deleted = 0
    for folder, members in by_folder.items():
        where = f"{directory}/{folder}" if folder else directory
        present = _listing(link, where, drive) or {}
        wanted = {m.basename: m for m in members}
        for name in sorted(set(present) - set(wanted)):
            link.delete_file(f"{where}/{name}", drive)
            deleted += 1
        for m in members:
            if present.get(m.basename) != m.size:
                link.push_file(m.data, f"{where}/{m.basename}", drive)
                posted += 1
    for folder, members in by_folder.items():
        where = f"{directory}/{folder}" if folder else directory
        present = _listing(link, where, drive) or {}
        wrong = [m.name for m in members if present.get(m.basename) != m.size]
        if wrong:
            raise FixtureError(f"{where} on {drive} does not hold the set after provisioning: {', '.join(wrong)}")
    log(f"[fixtures] {directory} on {drive}: {len(files)} file(s), {posted} posted, {deleted} stray(s) deleted")
    return [f.manifest() for f in files]
