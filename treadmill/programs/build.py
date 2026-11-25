"""Assemble the suite's own C64 programs with ACME at test time: PRGs,
cartridge images wrapped as .crt files, and raw payloads a test writes into
memory by DMA.

The sources live next to this file; the assembler is the firmware
checkout's cached one (``tools/.cache/acme-*/**/acme``) or ``TREADMILL_ACME``.
Outputs land in the run's work root, never in the repository.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

from treadmill.errors import TreadmillError
from treadmill.firmware.checkout import DEFAULT_WORK_ROOT

SOURCES = Path(__file__).parent
PROGRAMS = {
    "allcodes_ug0": ("allcodes.a", {"CHARSET": 0, "PAGE": 0}),
    "allcodes_ug1": ("allcodes.a", {"CHARSET": 0, "PAGE": 1}),
    "allcodes_lu0": ("allcodes.a", {"CHARSET": 1, "PAGE": 0}),
    "allcodes_lu1": ("allcodes.a", {"CHARSET": 1, "PAGE": 1}),
    "hello": ("hello.a", {}),
    "reuclient": ("reuclient.a", {}),
    "reuclient_sprites": ("reuclient.a", {"SPRITES": 1}),
    "reureadback": ("reureadback.a", {}),
    "reureadback_sprites": ("reureadback.a", {"SPRITES": 1}),
    "reureadback_hammer": ("reureadback.a", {"HAMMER": 1}),
    "reureadback_hammer_sprites": ("reureadback.a", {"HAMMER": 1, "SPRITES": 1}),
    "cpuwatch": ("cpuwatch.a", {}),
    # The stock KERNAL's filler run; the probe gives the machine's own (treadmill.probes.krread).
    "krread": ("krread.a", {"MARK": 0xE4B7, "LEN": 28, "FILL": 0xAA, "PAT": 0x0F}),
}
# Code with no load address, written into a running machine by DMA: the source
# sets its own origin, and ``origin`` is where the test writes it.
PAYLOADS = {
    "tputecho": ("tputecho.a", {}, 0xC000),
    "asidlog": ("asidlog.a", {}, 0xC000),
}


class Cartridge(NamedTuple):
    """8 KB chips assembled back to back, wrapped as a .crt."""
    source: str
    defines: dict[str, int]
    hardware: int                   # the CRT hardware type
    name: str                       # the CRT name, 32 characters at most
    loads: tuple[int, ...]          # each chip's load address, in the order assembled
    lines: tuple[int, int]          # (EXROM, GAME) as the header gives them
    banks: tuple[int, ...] = ()     # each chip's bank; all 0 when empty


# An EasyFlash image's header names Ultimax mode and its ROMH chips load at $A000.
CRT_EASYFLASH = 32
CRT_NORMAL = 0
CARTRIDGES = {
    # Bank 1 has a ROML only: the loop alternates the bank through $DE00 in 8K mode.
    "idread": Cartridge("idread.a", {}, CRT_EASYFLASH, "TREADMILL IDREAD", (0x8000, 0xA000, 0x8000), (1, 0),
                        (0, 0, 1)),
    # An 8K Ultimax image (EXROM high, GAME low): the TeensyROM serves it with its VIC cycles on.
    "vicrom": Cartridge("vicrom.a", {}, CRT_NORMAL, "TREADMILL VICROM", (0xE000,), (1, 0)),
}
# The sparse grid allcodes.a draws: cell i of page p shows code p * 228 + i.
SPARSE_ROWS, SPARSE_COLS, SPARSE_CELLS = 12, 19, 228


def sparse_cell(i: int) -> int:
    """Screen cell index (0..999) of grid cell ``i``."""
    return (1 + 2 * (i // SPARSE_COLS)) * 40 + 1 + 2 * (i % SPARSE_COLS)


def sparse_pages() -> list[tuple[list[int], list[int]]]:
    """For each page, (screen cell indices, codes shown there)."""
    pages = []
    for page in range(2):
        first = page * SPARSE_CELLS
        count = SPARSE_CELLS if page == 0 else 256 - SPARSE_CELLS
        pages.append(([sparse_cell(i) for i in range(count)], [first + i for i in range(count)]))
    return pages
ACME_ENV = "TREADMILL_ACME"
PRG_LOAD = 0x0801
CHIP_SIZE = 0x2000
CRT_SIGNATURE = b"C64 CARTRIDGE   "
CRT_HEADER = 0x40


class AssembleError(TreadmillError):
    pass


def find_acme(primary: Path | None = None) -> Path | None:
    env = os.environ.get(ACME_ENV)
    if env:
        return Path(env)
    on_path = shutil.which("acme")
    if on_path:
        return Path(on_path)
    if primary is not None:
        hits = sorted(Path(primary).glob("tools/.cache/acme-*/**/acme.exe")) or \
            sorted(p for p in Path(primary).glob("tools/.cache/acme-*/**/acme") if p.is_file())
        if hits:
            return hits[-1]
    return None


def crt_image(rom: bytes, name: str, hardware: int, loads: tuple[int, ...], lines: tuple[int, int],
              banks: tuple[int, ...] = ()) -> bytes:
    """A .crt file: the 64-byte header (version 1.0, ``lines`` as EXROM and
    GAME) and one CHIP packet per 8 KB of ``rom``, loading at ``loads`` in
    order, in ``banks`` (all bank 0 when empty). The format's multi-byte
    fields are big-endian."""
    if len(rom) != CHIP_SIZE * len(loads):
        raise AssembleError(f"{len(loads)} chip(s) are {CHIP_SIZE * len(loads)} bytes, not {len(rom)}")
    banks = banks or (0,) * len(loads)
    if len(banks) != len(loads):
        raise AssembleError(f"{len(loads)} chip(s) but {len(banks)} bank(s)")
    header = (CRT_SIGNATURE + CRT_HEADER.to_bytes(4, "big") + (0x0100).to_bytes(2, "big")
              + hardware.to_bytes(2, "big") + bytes(lines) + bytes(6)
              + name.encode("ascii")[:32].ljust(32, bytes(1)))
    chips = b"".join(b"CHIP" + (0x10 + CHIP_SIZE).to_bytes(4, "big") + bytes(2) + bank.to_bytes(2, "big")
                     + load.to_bytes(2, "big") + CHIP_SIZE.to_bytes(2, "big") + rom[i * CHIP_SIZE:(i + 1) * CHIP_SIZE]
                     for i, (load, bank) in enumerate(zip(loads, banks)))
    return header + chips


def assemble(name: str, out_dir: Path | None = None, primary: Path | None = None,
             acme: Path | None = None, extra: dict[str, int] | None = None) -> Path:
    """``name`` from PROGRAMS, assembled to ``<out_dir>/<name>[-<extra>].prg``,
    from CARTRIDGES, to ``<name>.crt``, or from PAYLOADS, to a raw ``<name>.bin``
    assembled at its origin (the ``ORIGIN`` define); ``extra`` adds or
    overrides assembler defines (``{"COLOR": 14}``)."""
    cartridge = CARTRIDGES.get(name)
    payload = PAYLOADS.get(name)
    if cartridge is None and payload is None and name not in PROGRAMS:
        raise AssembleError(f"no program {name!r}; one of {', '.join([*PROGRAMS, *CARTRIDGES, *PAYLOADS])}")
    if cartridge:
        source, defines = cartridge.source, cartridge.defines
    elif payload:
        source, defines = payload[0], {**payload[1], "ORIGIN": payload[2]}
    else:
        source, defines = PROGRAMS[name]
    if extra:
        defines = {**defines, **extra}
        name += "".join(f"-{k.lower()}{v}" for k, v in sorted(extra.items()))
    acme = acme or find_acme(primary)
    if acme is None:
        raise AssembleError(f"ACME not found: set {ACME_ENV}, put acme on PATH, or point at a firmware "
                            "checkout with tools/.cache")
    out_dir = Path(out_dir or DEFAULT_WORK_ROOT / "programs")
    out_dir.mkdir(parents=True, exist_ok=True)
    plain = cartridge is not None or payload is not None
    out = out_dir / (f"{name}.bin" if plain else f"{name}.prg")
    cmd = [str(acme), "-f", "plain" if plain else "cbm", "-o", str(out)]
    cmd += [f"-D{k}={v}" for k, v in defines.items()]
    cmd.append(source)
    result = subprocess.run(cmd, cwd=str(SOURCES), capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0 or not out.is_file():
        raise AssembleError(f"acme failed for {name}:\n{result.stdout}{result.stderr}")
    data = out.read_bytes()
    if cartridge:
        crt = out.with_suffix(".crt")
        crt.write_bytes(crt_image(data, cartridge.name, cartridge.hardware, cartridge.loads, cartridge.lines,
                                  cartridge.banks))
        return crt
    if payload:
        if not data:
            raise AssembleError(f"{out} is empty")
        return out
    if len(data) < 3 or int.from_bytes(data[:2], "little") != PRG_LOAD:
        raise AssembleError(f"{out} does not load at ${PRG_LOAD:04X}")
    return out
