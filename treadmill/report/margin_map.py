"""The margin map: the stored margin records of each knob on one chart.

One chart per knob, a row per record (machine, standard, board, probes and
build): the clean window as a bar from the down edge to the up edge, the
first failing value each side as a red tick, the default as a tick across the bar,
and each value under review (```--mark``) as a line through the chart. A
drifting edge is drawn orange. Under each row, a strip of cells shows every
value probed, a line per pass and probe: green clean, red failed (darker
the larger the share), black where the workload collapsed; a cell's
tooltip gives its counts and notes. A grid record is a heatmap of
nS_DMASetup by nS_DMADataHold, a value per cell.

``mermaid()`` gives the windows as a Mermaid gantt block per knob, on a
numeric axis, the form of the PR #99 charts, for pasting into a PR. Rows
are labelled by machine model, registry id and standard, never by station
or rig.
"""

from __future__ import annotations

import html
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping

WIDTH, LABEL_W, PAD = 980, 330, 20
ROW_H, STRIP_H = 22, 7
PLOT_X0, PLOT_X1 = LABEL_W + PAD, WIDTH - PAD

_STYLE = """
:root { --fg: #222; --muted: #666; --bg: #fff; --grid: #ddd; --bar: #2e8b57; --drift: #e08a1e; --fail: #b22222;
        --mark: #3366cc; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e6e6e6; --muted: #9a9a9a; --bg: #161616; --grid: #3a3a3a; --bar: #3fae6d; --drift: #f0a040;
          --fail: #e05555; --mark: #6f9cf0; }
}
body { font: 14px/1.4 -apple-system, Segoe UI, sans-serif; margin: 2em; color: var(--fg); background: var(--bg); }
h1 { font-size: 1.4em; } h2 { font-size: 1.15em; margin-top: 2em; }
svg text { fill: var(--fg); font-size: 12px; } svg .muted { fill: var(--muted); font-size: 11px; }
p.note { color: var(--muted); }
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def short_build(record: Mapping) -> str:
    """The commit's first eight digits, or the banner's version without the
    product name when no commit was recorded; '*' for a dirty build."""
    fw = record.get("firmware") or {}
    if fw.get("commit"):
        return fw["commit"][:8] + ("*" if fw.get("dirty") else "")
    version = (fw.get("version") or "?").split(" ", 1)[-1]
    return version if version.endswith("*") or not fw.get("dirty") else version + "*"


def label(record: Mapping) -> str:
    """Machine model, registry id and standard, board, probes and build."""
    m = record.get("machine") or {}
    model = "C128" if m.get("c128") else (m.get("model") or "?")
    probes = "+".join(record.get("probes") or [])
    return (f"{model} {m.get('id') or '?'} {m.get('standard') or '?'}, {record.get('board') or '?'}, {probes}, "
            f"{short_build(record)}")


def _values(record: Mapping) -> list[int]:
    found = [p["value"] for p in record.get("points") or []]
    for e in (record.get("edges") or {}).values():
        found += [v for v in (e.get("clean_to"), e.get("first_fail"), e.get("stop")) if v is not None]
    if record.get("default") is not None:
        found.append(record["default"])
    return found


def _window(record: Mapping) -> tuple[int | None, int | None]:
    """The clean window: from the down edge's clean end to the up edge's;
    a side that was not swept ends at the default."""
    edges = record.get("edges") or {}
    default = record.get("default")
    low = (edges.get("down") or {}).get("clean_to", default) if "down" in edges else default
    high = (edges.get("up") or {}).get("clean_to", default) if "up" in edges else default
    return low, high


def _cell_color(failures: int, samples: int, hung: bool) -> str:
    if hung:
        return "#000"
    if not failures:
        return "var(--bar)"
    share = min(1.0, failures / samples) if samples else 1.0
    lightness = 70 - int(40 * min(1.0, share * 10))      # a few bad reads already show
    return f"hsl(0, 70%, {lightness}%)"


def _knob_chart(name: str, records: list[Mapping], marks: list[int]) -> str:
    lines = [r for r in records if r.get("mode") != "grid"]
    if not lines:
        return ""
    values = [v for r in lines for v in _values(r)] + list(marks)
    lo, hi = min(values), max(values)
    span = max(hi - lo, 1)
    lo, hi = lo - span * 0.03, hi + span * 0.03

    def x(v: float) -> float:
        return PLOT_X0 + (v - lo) / (hi - lo) * (PLOT_X1 - PLOT_X0)

    rows_svg, y = [], 30
    for r in lines:
        passes = sorted({p["pass"] for p in r.get("points") or []}) or [1]
        probes = r.get("probes") or []
        strip_lines = len(passes) * max(1, len(probes))
        rows_svg.append(f'<text x="0" y="{y + 15}">{_e(label(r))}</text>')
        low, high = _window(r)
        drifting = any((e or {}).get("drifting") for e in (r.get("edges") or {}).values())
        if low is not None and high is not None and low <= high:
            color = "var(--drift)" if drifting else "var(--bar)"
            title = f"clean {low} to {high}" + (" (drifting between passes)" if drifting else "")
            rows_svg.append(f'<rect x="{x(low):.1f}" y="{y + 4}" width="{max(2.0, x(high) - x(low)):.1f}" '
                            f'height="{ROW_H - 8}" fill="{color}"><title>{_e(title)}</title></rect>')
        for e in (r.get("edges") or {}).values():
            if e.get("first_fail") is not None:
                rows_svg.append(f'<line x1="{x(e["first_fail"]):.1f}" x2="{x(e["first_fail"]):.1f}" y1="{y + 2}" '
                                f'y2="{y + ROW_H - 2}" stroke="var(--fail)" stroke-width="2">'
                                f'<title>fails from {e["first_fail"]} ({_e("+".join(e.get("probes") or []))})'
                                f'</title></line>')
        if r.get("default") is not None:
            d = x(r["default"])
            rows_svg.append(f'<line x1="{d:.1f}" x2="{d:.1f}" y1="{y}" y2="{y + ROW_H}" stroke="var(--fg)" '
                            f'stroke-width="2"><title>default {r["default"]}</title></line>')
        y += ROW_H
        step = (r.get("spec") or {}).get("step") or 10
        cell_w = max(2.0, (x(lo + step) - x(lo)) * 0.9)
        k = 0
        for n in passes:
            for probe in probes or ["?"]:
                for p in r.get("points") or []:
                    if p["pass"] != n:
                        continue
                    f, s = (p.get("by_probe") or {}).get(probe, [p["failures"], p["samples"]])
                    tip = (f"{probe}, pass {n}, {p['value']}: {f}/{s} failed"
                           + (f" ({'; '.join(p.get('notes') or [])})" if p.get("notes") else ""))
                    rows_svg.append(f'<rect x="{x(p["value"]) - cell_w / 2:.1f}" y="{y + k * STRIP_H}" '
                                    f'width="{cell_w:.1f}" height="{STRIP_H - 1}" '
                                    f'fill="{_cell_color(f, s, p.get("hung", False))}"><title>{_e(tip)}</title></rect>')
                k += 1
        rows_svg.append(f'<text class="muted" x="0" y="{y + STRIP_H * strip_lines - 1}">'
                        f'{_e(", ".join(f"pass {n}" for n in passes))}: {_e(", ".join(probes))}</text>')
        y += STRIP_H * strip_lines + 10
    height = y + 10
    axis = []
    tick = next((t for t in (5, 10, 20, 25, 50, 100, 200, 500) if (hi - lo) / t <= 12), 1000)
    for v in range(int(lo // tick + 1) * tick, int(hi) + 1, tick):
        axis.append(f'<line x1="{x(v):.1f}" x2="{x(v):.1f}" y1="20" y2="{height - 10}" stroke="var(--grid)"/>'
                    f'<text class="muted" x="{x(v):.1f}" y="14" text-anchor="middle">{v:.0f}</text>')
    for m in marks:
        axis.append(f'<line x1="{x(m):.1f}" x2="{x(m):.1f}" y1="20" y2="{height - 10}" stroke="var(--mark)" '
                    f'stroke-dasharray="4 3" stroke-width="2"><title>under review: {m}</title></line>')
    unit = (lines[0].get("knob") or {}).get("unit", "ns")
    return (f"<h2>{_e(name)} ({_e(unit)})</h2>"
            f'<svg width="{WIDTH}" height="{height}" role="img" aria-label="{_e(name)} clean windows">'
            + "".join(axis) + "".join(rows_svg) + "</svg>")


def _grid_chart(record: Mapping) -> str:
    cells: dict[tuple[int, int], list[int]] = {}
    hung: set[tuple[int, int]] = set()
    for p in record.get("points") or []:
        key = (p["value"], (p.get("held") or {}).get("w"))
        f, s = cells.get(key, [0, 0])
        cells[key] = [f + p["failures"], s + p["samples"]]
        if p.get("hung"):
            hung.add(key)
    rows = sorted({e for e, _ in cells})
    cols = sorted({w for _, w in cells if w is not None})
    size = 26
    out = [f'<text x="0" y="14">{_e(label(record))}</text>',
           f'<text class="muted" x="0" y="{40 + size * len(rows) + 16}">rows nS_DMASetup, columns nS_DMADataHold, '
           f'latch held at {(record.get("held") or {}).get("latch")}</text>']
    for j, w in enumerate(cols):
        out.append(f'<text class="muted" x="{90 + j * size + size / 2}" y="34" text-anchor="middle">{w}</text>')
    for i, e in enumerate(rows):
        out.append(f'<text class="muted" x="80" y="{40 + i * size + 17}" text-anchor="end">{e}</text>')
        for j, w in enumerate(cols):
            if (e, w) not in cells:
                continue
            f, s = cells[(e, w)]
            out.append(f'<rect x="{90 + j * size}" y="{40 + i * size}" width="{size - 2}" height="{size - 2}" '
                       f'fill="{_cell_color(f, s, (e, w) in hung)}"><title>{e} x {w}: {f}/{s} failed</title></rect>')
    height = 40 + size * len(rows) + 30
    return (f'<svg width="{max(WIDTH, 100 + size * len(cols))}" height="{height}" role="img" '
            f'aria-label="nS_DMASetup by nS_DMADataHold">' + "".join(out) + "</svg>")


def by_knob(records: Iterable[Mapping]) -> dict[str, list[Mapping]]:
    out: dict[str, list[Mapping]] = defaultdict(list)
    for r in records:
        out[(r.get("knob") or {}).get("name") or "?"].append(r)
    return dict(sorted(out.items()))


def render(records: Iterable[Mapping], out: Path, marks: Mapping[str, list[int]] | None = None,
           title: str = "TReadmill margin map") -> Path:
    """Write the map to ``out`` (an HTML file) and return it."""
    marks = marks or {}
    groups = by_knob(records)
    body = [f"<h1>{_e(title)}</h1>",
            '<p class="note">Bars: the clean window. Red ticks: the first failing value each side. The tick '
            "across a bar: the default. Dashed: a value under review. Cells under a row: each value probed, per "
            "pass and probe (green clean, red failed, black a collapse); hover for counts.</p>"]
    if not groups:
        body.append("<p>No margin records.</p>")
    for name, rs in groups.items():
        body.append(_knob_chart(name, rs, marks.get(name, [])))
        for g in (r for r in rs if r.get("mode") == "grid"):
            body.append(f"<h2>{_e(name)} grid</h2>" + _grid_chart(g))
    page = (f'<!doctype html><html><head><meta charset="utf-8"><title>{_e(title)}</title><style>{_STYLE}</style>'
            "</head><body>" + "\n".join(body) + "</body></html>\n")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


def _mermaid_text(text: str) -> str:
    return text.replace(":", " ").replace("#", "").replace(";", ",")


def mermaid(records: Iterable[Mapping], marks: Mapping[str, list[int]] | None = None) -> str:
    """A Mermaid gantt block per knob: a section per machine, a bar per
    record from its down edge to its up edge, milestones for the default and
    for each value under review."""
    marks = marks or {}
    blocks = []
    for name, rs in by_knob(records).items():
        lines = ["```mermaid", "gantt", f"    title {_mermaid_text(name)} clean windows", "    dateFormat X",
                 "    axisFormat %s"]
        defaults = sorted({r["default"] for r in rs if r.get("default") is not None})
        lines.append("    section reference")
        for d in defaults:
            lines.append(f"    default {d} :milestone, {d}, 0s")
        for m in marks.get(name, []):
            lines.append(f"    under review {m} :milestone, {m}, 0s")
        sections: dict[str, list[str]] = defaultdict(list)
        for r in rs:
            if r.get("mode") == "grid":
                continue
            low, high = _window(r)
            m = r.get("machine") or {}
            model = "C128" if m.get("c128") else (m.get("model") or "?")
            section = f"{model} {m.get('id') or '?'} {m.get('standard') or '?'}"
            task = _mermaid_text(f"{'+'.join(r.get('probes') or [])} {short_build(r)}")
            if low is None or high is None or low > high:
                sections[section].append(f"    {task} fails at the default :crit, {r.get('default')}, 0s")
            else:
                sections[section].append(f"    {task} {low}-{high} :{low}, {high}")
        for section, tasks in sections.items():
            lines.append(f"    section {_mermaid_text(section)}")
            lines += tasks
        lines.append("```")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")
