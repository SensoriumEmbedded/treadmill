"""Margin records: what a sweep found, kept as JSON, and what the stored ones say.

Every sweep writes ``margins.json`` (schema 1) in its folder: the station,
the machine and its standard, the board, the firmware (commit, dirty,
defines, tag, image), the knob, the mode, the probes, the held sums, the
default, each edge with the probe that found it, and every point of every
pass. ``treadmill margins`` reads them back from a reports folder as a
table, CSV, a map, or the floors a station's runs support (``suggest``).

A test holds each edge to the station's floor for that direction (below it
the test fails) and compares it with the newest earlier record for the same
station, knob, edge and probes on another build: a shrink past
``shrink_limit`` is a warning, not a failure, since machines drift (PR #99's
C128s moved 10x between passes).
"""

from __future__ import annotations

import ast
import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

SCHEMA = 1
NAME = "margins.json"
WAYS = ("down", "up")
SHRINK_MIN = 10                   # ns: below this an edge moving is the bisection's grain and the machine's noise


@dataclass(frozen=True)
class Floor:
    """How far each edge must be found clean, as knob values: the up edge
    clean at least up to ``up``, the down edge at least down to ``down``."""
    up: int | None = None
    down: int | None = None

    def get(self, way: str) -> int | None:
        return self.up if way == "up" else self.down


def build(body: Mapping, context: Mapping) -> dict:
    """A schema-1 record: the sweep's own ``Margin.to_dict()`` with the
    station, machine, board and firmware it ran on (``context_for``)."""
    return {"schema": SCHEMA, "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **context, **body}


def context_for(board, firmware: Mapping | None = None, *, image: str = "main") -> dict:
    """Where a sweep ran, from a ``Board`` and the run's firmware record."""
    st = board.station
    banner = board.banner
    fw = dict(firmware or {})
    return {
        "station": st.id,
        "machine": {"id": st.machine.id, "model": st.machine.model, "standard": banner.video if banner else None,
                    "c128": bool(banner and banner.machine == "C128")},
        "board": st.tr.kind,
        "firmware": {"version": banner.version if banner else None, "tag": banner.tag if banner else None,
                     "dirty": bool(fw.get("dirty")) or bool(banner and banner.dirty),
                     "commit": fw.get("commit"), "describe": fw.get("describe"),
                     "defines": list(fw.get("defines") or []), "image": image},
    }


def write(folder: Path, record: Mapping) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / NAME
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def load(root: Path) -> list[dict]:
    """Every schema-1 record under ``root``, oldest first; each carries the
    path it came from as ``_path``."""
    found = []
    for path in Path(root).rglob(NAME):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("schema") == SCHEMA:
            record["_path"] = str(path)
            found.append(record)
    return sorted(found, key=lambda r: r.get("when") or "")


def build_id(record: Mapping) -> str:
    """The build a record ran: its commit, else the banner's version (tag
    and dirty mark included)."""
    fw = record.get("firmware") or {}
    commit = fw.get("commit")
    if commit:
        return commit + ("*" if fw.get("dirty") else "")
    return fw.get("version") or "?"


def edge(record: Mapping, way: str) -> dict | None:
    return (record.get("edges") or {}).get(way)


def _same(a: Mapping, b: Mapping, way: str) -> bool:
    return (a.get("station") == b.get("station")
            and (a.get("knob") or {}).get("name") == (b.get("knob") or {}).get("name")
            and sorted(a.get("probes") or []) == sorted(b.get("probes") or [])
            and (a.get("firmware") or {}).get("image") == (b.get("firmware") or {}).get("image")
            and edge(a, way) is not None and edge(b, way) is not None)


def shrink_limit(record: Mapping) -> int:
    return max(2 * int((record.get("spec") or {}).get("fine") or 1), SHRINK_MIN)


def floor_problems(record: Mapping, floor: Floor | None) -> list[str]:
    """The edges that stop short of the station's floor: each one fails the test."""
    if floor is None:
        return []
    name = (record.get("knob") or {}).get("name")
    problems = []
    for way in WAYS:
        want, e = floor.get(way), edge(record, way)
        if want is None or e is None:
            continue
        got = e.get("clean_to")
        if got is None or (got < want if way == "up" else got > want):
            problems.append(f"{name} {way}: clean only to {got} (fails from {e.get('first_fail')}); "
                            f"the station's floor is {want}")
    return problems


def shrinks(record: Mapping, earlier: Iterable[Mapping]) -> list[str]:
    """Each edge that sits more than ``shrink_limit`` inside the newest
    earlier record of the same station, knob, edge and probes on another build."""
    name = (record.get("knob") or {}).get("name")
    mine = build_id(record)
    warnings = []
    for way in WAYS:
        before = [r for r in earlier if _same(r, record, way) and build_id(r) != mine
                  and (r.get("when") or "") <= (record.get("when") or "")]
        if not before:
            continue
        prev = before[-1]
        now, was = edge(record, way).get("clean_to"), edge(prev, way).get("clean_to")
        if now is None or was is None:
            continue
        lost = (was - now) if way == "up" else (now - was)
        if lost > shrink_limit(record):
            warnings.append(f"{name} {way}: clean to {now}, {lost} {(record.get('knob') or {}).get('unit', 'ns')} "
                            f"inside {was} on {build_id(prev)} ({prev.get('when')})")
    return warnings


def under_load(record: Mapping) -> bool:
    """A sweep made under USB load (a probe named ``...-usbload``)."""
    return any(p.endswith("-usbload") for p in record.get("probes") or [])


def suggest(records: Iterable[Mapping], station: str, *, last: int = 5) -> dict[str, Floor]:
    """Floors a station's last ``last`` records of each knob support: one
    coarse step inside the worst edge each way, never past the default. A
    station's floors are for its knobs without load, so sweeps made under
    USB load are left out."""
    by_knob: dict[str, list[Mapping]] = {}
    for r in records:
        if r.get("station") == station and not under_load(r):
            by_knob.setdefault((r.get("knob") or {}).get("name"), []).append(r)
    floors: dict[str, Floor] = {}
    for name, rs in sorted(by_knob.items()):
        rs = rs[-last:]
        values: dict[str, int | None] = {}
        for way in WAYS:
            ends = [(e["clean_to"], r) for r in rs if (e := edge(r, way)) and e.get("clean_to") is not None]
            if not ends:
                values[way] = None
                continue
            worst, r = (min if way == "up" else max)(ends, key=lambda t: t[0])
            step = int((r.get("spec") or {}).get("step") or 10)
            default = int(r.get("default"))
            values[way] = max(worst - step, default) if way == "up" else min(worst + step, default)
        if values["up"] is not None or values["down"] is not None:
            floors[name] = Floor(up=values["up"], down=values["down"])
    return floors


def floors_toml(floors: Mapping[str, Floor]) -> str:
    """The ``[margins]`` table for a station file."""
    lines = ["[margins]"]
    for name, f in floors.items():
        parts = [f"{way} = {f.get(way)}" for way in ("up", "down") if f.get(way) is not None]
        lines.append(f"{name} = {{ {', '.join(parts)} }}")
    return "\n".join(lines) + "\n"


COLUMNS = ("when", "station", "machine", "standard", "board", "build", "image", "knob", "probes", "mode",
           "passes", "default", "down_clean", "down_fail", "up_clean", "up_fail", "drifting", "path")


def rows(records: Iterable[Mapping]) -> list[dict]:
    out = []
    for r in records:
        m = r.get("machine") or {}
        down, up = edge(r, "down") or {}, edge(r, "up") or {}
        drifting = [way for way in WAYS if (edge(r, way) or {}).get("drifting")]
        out.append({
            "when": r.get("when"), "station": r.get("station"), "machine": m.get("id"),
            "standard": ("NTSC C128" if m.get("c128") and m.get("standard") == "NTSC" else m.get("standard")),
            "board": r.get("board"), "build": build_id(r), "image": (r.get("firmware") or {}).get("image"),
            "knob": (r.get("knob") or {}).get("name"), "probes": "+".join(r.get("probes") or []),
            "mode": r.get("mode"), "passes": r.get("passes"), "default": r.get("default"),
            "down_clean": down.get("clean_to"), "down_fail": down.get("first_fail"),
            "up_clean": up.get("clean_to"), "up_fail": up.get("first_fail"),
            "drifting": ",".join(drifting), "path": r.get("_path"),
        })
    return out


def table(records: Iterable[Mapping]) -> str:
    """One line a record: the clean window and where each side fails."""
    rs = rows(records)
    if not rs:
        return "no margin records"
    cols = [c for c in COLUMNS if c not in ("path", "when")]
    cells = [[("" if row[c] is None else str(row[c])) for c in cols] for row in rs]
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(cols)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(cols, widths))]
    lines += ["  ".join(v.ljust(w) for v, w in zip(r, widths)) for r in cells]
    return "\n".join(lines)


def to_csv(records: Iterable[Mapping]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(COLUMNS), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows(records))
    return buf.getvalue()


# -- the PR #99 script's runs ------------------------------------------------------

PR99_RESULT = "dmasetup-sweep.txt"
PR99_PROBES = {"z": "zpattern", "reu": "reuloop"}


def _pr99_context(meta: Mapping) -> dict:
    st, board, fw = meta.get("station") or {}, meta.get("board") or {}, meta.get("firmware") or {}
    line = (board.get("machine_line") or "").split()
    return {
        "station": st.get("id"),
        "machine": {"id": st.get("machine"), "model": st.get("model"), "standard": line[1] if len(line) > 1 else None,
                    "c128": bool(line) and line[0] == "C128"},
        "board": st.get("tr_kind"),
        "firmware": {"version": board.get("version"), "tag": board.get("tag"),
                     "dirty": bool(fw.get("dirty")) or bool(board.get("dirty")), "commit": fw.get("commit"),
                     "describe": fw.get("describe"), "defines": list(fw.get("defines") or []),
                     "image": board.get("image") or "main"},
    }


def import_pr99(run: Path) -> list[dict]:
    """Schema-1 records from a run of ``scripts/2026-11-12-dmasetup-sweep.py``
    (its ``dmasetup-sweep.txt`` and ``run.json``): one per probe it ran, a
    scan of nS_DMASetup with the sums held. The script measured screen
    noise beside every point; it is judged here as the noise probe judges
    it (``treadmill/probes/noise.py``), against the point nearest the
    default, since the script's own baseline ran with no DMA at all."""
    from treadmill.knobs import KNOBS
    from treadmill.probes.noise import FACTOR, FLOOR
    from treadmill.sweeps import Point, edge_of

    run = Path(run)
    result = next(run.glob(f"exec-*/{PR99_RESULT}"), None)
    if result is None:
        raise ValueError(f"{run}: no {PR99_RESULT} under an exec folder; not a PR #99 sweep run")
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    lines = result.read_text(encoding="utf-8").splitlines()
    head = lines[0]
    defaults = ast.literal_eval(head.split("defaults ", 1)[1].split(" sums", 1)[0])
    sums = ast.literal_eval(head.split(" sums ", 1)[1].split(" baseline", 1)[0])
    rows: dict[str, list[dict]] = {}
    for line in lines[1:]:
        kind, _, rest = line.partition(" ")
        if kind in PR99_PROBES:
            rows.setdefault(kind, []).append(ast.literal_eval(rest.strip()))
    knob, context = KNOBS["e"], _pr99_context(meta)
    default = defaults["e"]
    records = []
    for kind, found in rows.items():
        probe = PR99_PROBES[kind]
        noisy = [r for r in found if r.get("noise")]
        base = min(noisy, key=lambda r: abs(r["e"] - default))["noise"]["max"] if noisy else None
        limit = max(int(FACTOR * base), base + FLOOR) if base is not None else None
        points = []
        for r in found:
            if kind == "z":
                bad = r.get("bad", 0) + r.get("prefill_bad", 0) if "error" not in r else 1
                total = 2 * r.get("bytes", 0) or 1
            else:
                bad, total = r.get("bad_pages", 0), r.get("rounds", 0) + r.get("bad_pages", 0) or 1
            by_probe = {probe: [bad, total]}
            failed_by = [probe] if bad else []
            notes = [r["error"]] if "error" in r else []
            if limit is not None and r.get("noise"):
                over = int(r["noise"]["max"] > limit)
                by_probe["noise"] = [over, 1]
                if over:
                    failed_by.append("noise")
                    notes.append(f"noise {r['noise']['max']} pixels over the limit {limit}")
            failures = sum(v[0] for v in by_probe.values())
            samples = sum(v[1] for v in by_probe.values())
            held = {k: r[k] for k in ("w", "y") if k in r}
            points.append(Point(r["e"], failures, samples, 0.0, notes, by_probe, failed_by, bool(r.get("error")),
                                1, held))
        values = [p.value for p in points]
        step = abs(values[1] - values[0]) if len(values) > 1 else 10
        low, high = min(values), max(values)
        edges = {way: edge_of(points, default, way, stop, step).to_dict(default)
                 for way, stop in (("down", low), ("up", high)) if stop != default}
        probes = [probe] + (["noise"] if limit is not None else [])
        records.append({
            "schema": SCHEMA, "when": meta.get("started"), **context,
            "knob": {"name": knob.name, "letter": knob.letter, "unit": knob.unit, "group": knob.group},
            "mode": "scan", "probes": probes, "standard": context["machine"]["standard"], "default": default,
            "start": default, "defaults": defaults, "held": {"release": sums[0], "latch": sums[1]},
            "limits": None, "spec": {"stop": None, "step": step, "fine": step, "repeat": 1, "scan": [low, high],
                                     "grid": None},
            "passes": 1, "restored": True, "seconds": None, "edges": edges,
            "points": [p.to_dict() for p in points],
            "probe_info": {"noise": {"baseline": base, "limit": limit, "factor": FACTOR, "floor": FLOOR}}
            if limit is not None else {},
            "imported_from": str(result),
        })
    return records
