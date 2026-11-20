"""The suite's C64 programs and cartridges assemble (when ACME is reachable)."""

import os
from pathlib import Path

import pytest

from treadmill import asid
from treadmill.programs.build import (
    CARTRIDGES, CRT_EASYFLASH, PAYLOADS, PROGRAMS, AssembleError, assemble, crt_image, find_acme,
)


def _acme() -> Path | None:
    primary = os.environ.get("TREADMILL_FW_REPO")
    return find_acme(Path(primary) if primary else None)


@pytest.mark.skipif(_acme() is None, reason="no ACME: set TREADMILL_ACME or TREADMILL_FW_REPO")
def test_programs_assemble(tmp_path: Path):
    for name in PROGRAMS:
        prg = assemble(name, tmp_path, acme=_acme())
        data = prg.read_bytes()
        assert data[:2] == b"\x01\x08" and 60 < len(data) < 700, name
    ug, lu = (tmp_path / "allcodes_ug0.prg").read_bytes(), (tmp_path / "allcodes_lu0.prg").read_bytes()
    assert ug != lu and len(ug) == len(lu)                     # only the $D018 value differs
    assert len((tmp_path / "allcodes_lu1.prg").read_bytes()) < len(lu)   # page 1 has a shorter table


@pytest.mark.skipif(_acme() is None, reason="no ACME: set TREADMILL_ACME or TREADMILL_FW_REPO")
def test_cartridges_assemble_to_crt_files(tmp_path: Path):
    for name in CARTRIDGES:
        loads = CARTRIDGES[name].loads
        crt = assemble(name, tmp_path, acme=_acme())
        data = crt.read_bytes()
        assert crt.suffix == ".crt" and len(data) == 0x40 + len(loads) * (0x10 + 0x2000), name
        if loads[0] == 0x8000:
            assert data[0x50 + 4:0x50 + 9] == b"\xc3\xc2\xcd80", name         # CBM80: the KERNAL starts it
        else:                                                                # Ultimax: the reset vector does
            assert data[-4:-2] == loads[0].to_bytes(2, "little"), name
    vicrom = (tmp_path / "vicrom.crt").read_bytes()
    assert vicrom[0x18:0x1A] == bytes([1, 0]) and vicrom[0x50 + 0x1000:0x50 + 0x1008] != bytes(8), \
        "EXROM high, GAME low (8K-Hi), and a charset at $F000, where the VIC sees ROMH"
    data = (tmp_path / "idread.crt").read_bytes()
    roml, romh = data[0x50:0x2050], data[0x2060:]
    assert roml[0x100:0x200] == bytes(range(256)) and roml[0x200:0x300] == bytes(i ^ 0xFF for i in range(256))
    assert romh[-4:-2] == bytes([0x00, 0xE0])                                       # Ultimax reset into ROMH
    assert roml[2:4] == bytes([0xBC, 0xFE])          # RESTORE returns through the KERNAL's NMI exit, counters kept


@pytest.mark.skipif(_acme() is None, reason="no ACME: set TREADMILL_ACME or TREADMILL_FW_REPO")
def test_payloads_assemble_raw_at_their_origin(tmp_path: Path):
    assert PAYLOADS["tputecho"][2] == 0xC000 and PAYLOADS["asidlog"][2] == asid.LOGGER
    tput = assemble("tputecho", tmp_path, acme=_acme()).read_bytes()
    assert tput[:2] == bytes([0xA2, 0x00]) and len(tput) < 0x0400          # ldx #0, no load address
    assert bytes([0xFE, 0xFD, 0xFC, 0xFB]) in tput
    log = assemble("asidlog", tmp_path, acme=_acme(), extra={"RESUME": 0x1A2B}).read_bytes()
    assert log[:3] == bytes(3)                                              # the pair count starts at 0
    assert log[3:9] == asid.PLAYER_IRQ_HEAD                                 # the entry makes the player's reads
    assert log[9:12] == bytes([0x8E, 0x00, asid.LOG >> 8])                  # stx LOG
    assert log[-3:] == bytes([0x4C, 0x2B, 0x1A])                            # jmp RESUME
    with pytest.raises(AssembleError, match="RESUME"):
        assemble("asidlog", tmp_path, acme=_acme())


def test_crt_image_lays_out_the_header_and_chips():
    rom = bytes(range(256)) * 64
    data = crt_image(rom, "NAME", CRT_EASYFLASH, (0x8000, 0xA000), (1, 0))
    assert data[:16] == b"C64 CARTRIDGE   " and data[0x10:0x14] == bytes([0, 0, 0, 0x40])
    assert data[0x14:0x16] == bytes([1, 0]) and data[0x16:0x18] == bytes([0, 0x20])   # version 1.0, EasyFlash
    assert (data[0x18], data[0x19]) == (1, 0)                                         # EXROM, GAME as given
    assert data[0x20:0x40] == b"NAME" + bytes(28)
    assert data[0x40:0x50] == b"CHIP" + bytes([0, 0, 0x20, 0x10, 0, 0, 0, 0, 0x80, 0, 0x20, 0])
    assert data[0x50:0x2050] == rom[:0x2000]
    assert data[0x2050:0x2060] == b"CHIP" + bytes([0, 0, 0x20, 0x10, 0, 0, 0, 0, 0xA0, 0, 0x20, 0])
    assert data[0x2060:] == rom[0x2000:]
    with pytest.raises(AssembleError):
        crt_image(rom[:100], "SHORT", CRT_EASYFLASH, (0x8000,), (0, 1))


def test_sparse_layout_covers_every_code_with_blank_neighbors():
    from treadmill.programs.build import sparse_pages
    seen = []
    for cells, codes in sparse_pages():
        seen += codes
        for cell in cells:
            r, c = divmod(cell, 40)
            assert r % 2 == 1 and c % 2 == 1 and 1 <= r <= 23 and 1 <= c <= 37
    assert sorted(seen) == list(range(256))


def test_unknown_program_is_refused(tmp_path: Path):
    with pytest.raises(AssembleError):
        assemble("nope", tmp_path, acme=Path("acme"))
