"""Assemble the suite's own C64 programs with ACME at test time.

The sources live next to this file; the assembler is the firmware
checkout's cached one (``tools/.cache/acme-*/**/acme``) or ``TREADMILL_ACME``.
Outputs land in the run's work root, never in the repository.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from treadmill.errors import TreadmillError
from treadmill.firmware.checkout import DEFAULT_WORK_ROOT

SOURCES = Path(__file__).parent
PROGRAMS = {
    "allcodes_ug0": ("allcodes.a", {"CHARSET": 0, "PAGE": 0}),
    "allcodes_ug1": ("allcodes.a", {"CHARSET": 0, "PAGE": 1}),
    "allcodes_lu0": ("allcodes.a", {"CHARSET": 1, "PAGE": 0}),
    "allcodes_lu1": ("allcodes.a", {"CHARSET": 1, "PAGE": 1}),
    "hello": ("hello.a", {}),
    "idread": ("idread.a", {}),
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


def assemble(name: str, out_dir: Path | None = None, primary: Path | None = None,
             acme: Path | None = None, extra: dict[str, int] | None = None) -> Path:
    """``name`` from PROGRAMS, assembled to ``<out_dir>/<name>[-<extra>].prg``;
    ``extra`` adds or overrides assembler defines (``{"COLOR": 14}``)."""
    if name not in PROGRAMS:
        raise AssembleError(f"no program {name!r}; one of {', '.join(PROGRAMS)}")
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
    out = out_dir / f"{name}.prg"
    cmd = [str(acme), "-f", "cbm", "-o", str(out)]
    cmd += [f"-D{k}={v}" for k, v in defines.items()]
    cmd.append(source)
    result = subprocess.run(cmd, cwd=str(SOURCES), capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0 or not out.is_file():
        raise AssembleError(f"acme failed for {name}:\n{result.stdout}{result.stderr}")
    data = out.read_bytes()
    if len(data) < 3 or int.from_bytes(data[:2], "little") != PRG_LOAD:
        raise AssembleError(f"{out} does not load at ${PRG_LOAD:04X}")
    return out
