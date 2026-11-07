"""The suite's C64 programs assemble (when ACME is reachable)."""

import os
from pathlib import Path

import pytest

from treadmill.programs.build import PROGRAMS, AssembleError, assemble, find_acme


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
