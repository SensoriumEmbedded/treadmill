"""Text goldens: 25 rows of 40 characters, diffable and station-independent.

A golden file's screen rows start with ``|`` (and may end with one); the
lines before the first row are a header the matcher ignores, by convention
starting with ``%``. ``?`` matches any character in that cell, a row that is
just ``|*`` matches any row, short rows are padded with spaces, and missing
rows at the end match anything. A file with no ``|`` rows is read as bare
rows, its last 25 lines. A read by DMA, by video or by the MCP server's
``read_screen`` has the same shape, so a live screen can be saved as a
golden unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from treadmill.c64.screen import SCREEN_COLUMNS, SCREEN_ROWS

ANY_CELL = "?"
ANY_ROW = "*"
ROW_PREFIX = "|"


@dataclass(frozen=True)
class Golden:
    rows: tuple[str | None, ...]       # None = any row
    header: tuple[str, ...]
    name: str = ""

    @classmethod
    def parse(cls, text: str, name: str = "") -> "Golden":
        lines = text.rstrip("\n").split("\n")
        if any(ln.startswith(ROW_PREFIX) for ln in lines):
            first = next(i for i, ln in enumerate(lines) if ln.startswith(ROW_PREFIX))
            header = tuple(lines[:first])
            body = [ln[1:-1] if len(ln) > 1 and ln.endswith(ROW_PREFIX) else ln[1:]
                    for ln in lines[first:] if ln.startswith(ROW_PREFIX)][:SCREEN_ROWS]
        else:
            body = lines[-SCREEN_ROWS:]
            header = tuple(lines[:-len(body)]) if len(lines) > len(body) else ()
        rows = tuple(None if ln.strip() == ANY_ROW else ln.rstrip("\r").ljust(SCREEN_COLUMNS)[:SCREEN_COLUMNS]
                     for ln in body)
        rows += (None,) * (SCREEN_ROWS - len(rows))
        return cls(rows, header, name)

    @classmethod
    def load(cls, path: Path | str) -> "Golden":
        path = Path(path)
        return cls.parse(path.read_text(encoding="utf-8"), path.name)

    def text(self) -> str:
        return "\n".join(f"{ROW_PREFIX}{ANY_ROW if r is None else r}{ROW_PREFIX}" for r in self.rows)


@dataclass(frozen=True)
class Mismatch:
    row: int
    col: int
    expected: str
    actual: str


def compare(rows: list[str], golden: Golden) -> list[Mismatch]:
    """Every cell where the screen differs from the golden."""
    out = []
    for r, want in enumerate(golden.rows):
        if want is None:
            continue
        have = (rows[r] if r < len(rows) else "").ljust(SCREEN_COLUMNS)[:SCREEN_COLUMNS]
        for c in range(SCREEN_COLUMNS):
            if want[c] != ANY_CELL and want[c] != have[c]:
                out.append(Mismatch(r, c, want[c], have[c]))
    return out


def diff_text(rows: list[str], golden: Golden) -> str:
    """The two screens side by side with a marker line under each row that differs."""
    lines = []
    bad: dict[int, set[int]] = {}
    for m in compare(rows, golden):
        bad.setdefault(m.row, set()).add(m.col)
    for r in range(SCREEN_ROWS):
        want = golden.rows[r]
        have = (rows[r] if r < len(rows) else "").ljust(SCREEN_COLUMNS)[:SCREEN_COLUMNS]
        lines.append(f"{r:2d} |{have}|  |{ANY_ROW if want is None else want}|")
        if r in bad:
            marks = "".join("^" if c in bad[r] else " " for c in range(SCREEN_COLUMNS))
            lines.append(f"   |{marks}|")
    return "\n".join(lines)


def save_golden(rows: list[str], path: Path | str, header: tuple[str, ...] = ()) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [f"{ROW_PREFIX}{r.ljust(SCREEN_COLUMNS)[:SCREEN_COLUMNS]}{ROW_PREFIX}" for r in rows[:SCREEN_ROWS]]
    path.write_text("\n".join([*header, *body]) + "\n", encoding="utf-8")
    return path
