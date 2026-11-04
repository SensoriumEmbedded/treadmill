"""The machine registry: the validation tracker's ``systems.csv``.

The tracker (a folder beside the TeensyROM checkout, not in git) is the one
list of the lab's machines, so the suite reads it instead of keeping a second
copy. Its columns: id, nickname, model, pca_number, region, pca_rev, status,
baseline_diag, notes. Station files name a machine by that id; anything the
tracker does not know (kernal, boot timeout) comes from the station file.
"""

from __future__ import annotations

import csv
from pathlib import Path

from treadmill.station.model import Machine


def normalize_model(text: str) -> str:
    """The tracker's free-text model column, folded to the few models the
    suite distinguishes."""
    t = text.strip().upper()
    if "128" in t:
        return "C128"
    if "U64" in t or "ULTIMATE" in t:
        return "C64U"
    if "SX" in t:
        return "SX-64"
    return "C64"


def normalize_region(text: str) -> str:
    t = text.strip().upper()
    if t.startswith("PAL"):
        return "PAL"
    if "SWITCH" in t or "BOTH" in t:
        return "switchable"
    return "NTSC"


def read_registry(path: Path) -> dict[str, Machine]:
    """Every machine in the CSV, keyed by its id. Rows without an id are skipped."""
    machines: dict[str, Machine] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            mid = (row.get("id") or "").strip()
            if not mid:
                continue
            notes = (row.get("notes") or "").strip()
            machines[mid] = Machine(
                id=mid,
                model=normalize_model(row.get("model") or ""),
                region=normalize_region(row.get("region") or ""),
                nickname=(row.get("nickname") or "").strip(),
                pca=(row.get("pca_number") or "").strip(),
                notes=notes,
                timing_tolerant=normalize_model(row.get("model") or "") == "C64U",
                has_kawari="kawari" in notes.lower(),
            )
    return machines
