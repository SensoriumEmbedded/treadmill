"""The margin map: the stored margin records of each knob on one chart.

It opens with an overview per machine: a row per knob, image and probe set
(the latest record of each), the clean window drawn as its offset from the
knob's default so every default sits on one line, each end red where a
failure was found (the last clean value, then the first failing one) or
green where the sweep stopped clean at its limit. ``Cyc_KernProp`` counts
Teensy cycles and is drawn at ``NS_PER_TEENSY_CYCLE``. Under it, the same
windows of the main image (not those under USB load) on a time line from
the Phi2 rise the interrupt fires on, a section per firmware routine with
the knobs it waits on (a knob two routines share is in both): each knob
from the point it counts from (the rise, the fall drawn at half the cycle,
the re-latch at nS_DMASetup's default after the fall, the interrupt for
nS_MaxAdj, about rise + nS_RWnReady for the Skoe sense), its default as a
tick on the bar, rows in the order their defaults come. The per-knob charts follow in a
collapsed section.

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

``export_svgs()`` writes each knob's chart as a static SVG file of its own,
for a public doc (``treadmill margins svg``): an ``xmlns``, its colors as
attributes over a white ground, its heading and key inside, no tooltips,
and rows labeled by machine model and standard alone, with the board, the
probes, the knobs set and the build (named with ``--build``) added only
where two rows would otherwise read the same.
"""

from __future__ import annotations

import html
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping

from treadmill.knobs import CYCLE_NS, KNOBS, NS_PER_TEENSY_CYCLE

WIDTH, LABEL_W, PAD = 980, 330, 20
ROW_H, STRIP_H = 22, 7
PLOT_X0, PLOT_X1 = LABEL_W + PAD, WIDTH - PAD

_STYLE = """
:root { --fg: #222; --muted: #666; --bg: #fff; --grid: #ddd; --bar: #2e8b57; --drift: #e08a1e; --fail: #b22222;
        --mark: #3366cc; --window: #bccadb; --good: #0ca30c; --critical: #d03b3b; --band: #ebeff4; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e6e6e6; --muted: #9a9a9a; --bg: #161616; --grid: #3a3a3a; --bar: #3fae6d; --drift: #f0a040;
          --fail: #e05555; --mark: #6f9cf0; --window: #3d4a5c; --band: #252c35; }
}
body { font: 14px/1.4 -apple-system, Segoe UI, sans-serif; margin: 2em; color: var(--fg); background: var(--bg); }
h1 { font-size: 1.4em; } h2 { font-size: 1.15em; margin-top: 2em; }
svg text { fill: var(--fg); font-size: 12px; } svg .muted { fill: var(--muted); font-size: 11px; }
svg .num { font: 11px Consolas, ui-monospace, monospace; fill: var(--muted); } svg .head { font-weight: 600; }
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


def _set(record: Mapping) -> str:
    """The other knobs a sweep set, as ``nS_RWnReady 60``; empty when none."""
    return ", ".join(f"{name} {value}" for name, value in (record.get("set") or {}).items())


def label(record: Mapping) -> str:
    """Machine model, registry id and standard, board, probes, the knobs set
    and build."""
    m = record.get("machine") or {}
    model = "C128" if m.get("c128") else (m.get("model") or "?")
    probes = "+".join(record.get("probes") or [])
    fixed = f", {_set(record)}" if record.get("set") else ""
    return (f"{model} {m.get('id') or '?'} {m.get('standard') or '?'}, {record.get('board') or '?'}, {probes}"
            f"{fixed}, {short_build(record)}")


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


def _cell_color(failures: int, samples: int, hung: bool, clean: str = "var(--bar)") -> str:
    if hung:
        return "#000"
    if not failures:
        return clean
    share = min(1.0, failures / samples) if samples else 1.0
    lightness = 70 - int(40 * min(1.0, share * 10))      # a few bad reads already show
    return f"hsl(0, 70%, {lightness}%)"


# The per-knob chart's colors: the page's CSS variables, or, in a static SVG file that must draw the
# same on its own (GitHub shows one as an image, on a light or a dark page), the light theme's values.
_LIVE = {"fg": "var(--fg)", "muted": "var(--muted)", "grid": "var(--grid)", "bar": "var(--bar)",
         "drift": "var(--drift)", "fail": "var(--fail)", "mark": "var(--mark)"}
_STATIC = {"fg": "#222", "muted": "#666", "grid": "#ddd", "bar": "#2e8b57", "drift": "#e08a1e", "fail": "#b22222",
           "mark": "#3366cc", "bg": "#fff"}
_FONT = "-apple-system, Segoe UI, Helvetica, Arial, sans-serif"
HEAD_H = 26                       # a static file's own heading, above the chart


def public_labels(records: list[Mapping], builds: Mapping[str, str] | None = None) -> list[str]:
    """Row labels for public text: machine model and video standard, never a
    station or a registry id. What else tells two rows apart is added only
    where the chart needs it: a number for each of several machines of one
    model and standard, the board, the probes, the knobs set, and the build
    (``builds`` names a commit by its first digits, as ``88f5c805=main``)."""
    builds = builds or {}

    def build(r: Mapping) -> str:
        commit = (r.get("firmware") or {}).get("commit") or ""
        named = next((text for prefix, text in builds.items() if prefix and commit.startswith(prefix)), None)
        return named or short_build(r)

    def board(r: Mapping) -> str:
        return {"tr-plus": "TR+", "tr": "TR"}.get(r.get("board"), r.get("board") or "?")

    def base(r: Mapping) -> str:
        m = r.get("machine") or {}
        return f"{'C128' if m.get('c128') else (m.get('model') or '?')} {m.get('standard') or '?'}"

    machines: dict[str, list] = defaultdict(list)
    for r in records:
        mid = (r.get("machine") or {}).get("id")
        if mid not in machines[base(r)]:
            machines[base(r)].append(mid)
    parts = {"board": board, "probes": lambda r: "+".join(r.get("probes") or []), "set": _set, "build": build}
    shown = {k for k, f in parts.items() if len({f(r) for r in records}) > 1 or (k == "build" and builds)}
    labels = []
    for r in records:
        ids = machines[base(r)]
        text = base(r) + (f" ({ids.index((r.get('machine') or {}).get('id')) + 1})" if len(ids) > 1 else "")
        labels.append(", ".join([text] + [f(r) for k, f in parts.items() if k in shown and f(r)]))
    return labels


def _knob_chart(name: str, records: list[Mapping], marks: list[int], *, static: bool = False,
                builds: Mapping[str, str] | None = None) -> str:
    """One knob's chart, a row per record. ``static``: a file of its own
    (``xmlns``, its colors as attributes over a white ground, its heading
    inside, no tooltips, rows by ``public_labels``), else a part of the map's
    page."""
    lines = [r for r in records if r.get("mode") != "grid"]
    if not lines:
        return ""
    c = _STATIC if static else _LIVE
    names = public_labels(lines, builds) if static else [label(r) for r in lines]

    def tip(text: str) -> str:
        return "" if static else f"<title>{_e(text)}</title>"

    def muted(anchor: str = "") -> str:
        end = f' text-anchor="{anchor}"' if anchor else ""
        return (f'fill="{c["muted"]}" font-size="11"' if static else 'class="muted"') + end

    values = [v for r in lines for v in _values(r)] + list(marks)
    lo, hi = min(values), max(values)
    span = max(hi - lo, 1)
    lo, hi = lo - span * 0.03, hi + span * 0.03

    def x(v: float) -> float:
        return PLOT_X0 + (v - lo) / (hi - lo) * (PLOT_X1 - PLOT_X0)

    rows_svg, y = [], 30
    for r, row_name in zip(lines, names):
        passes = sorted({p["pass"] for p in r.get("points") or []}) or [1]
        probes = r.get("probes") or []
        strip_lines = len(passes) * max(1, len(probes))
        plain = f' fill="{c["fg"]}" font-size="12"' if static else ""
        rows_svg.append(f'<text x="0" y="{y + 15}"{plain}>{_e(row_name)}</text>')
        low, high = _window(r)
        drifting = any((e or {}).get("drifting") for e in (r.get("edges") or {}).values())
        if low is not None and high is not None and low <= high:
            color = c["drift"] if drifting else c["bar"]
            title = f"clean {low} to {high}" + (" (drifting between passes)" if drifting else "")
            rows_svg.append(f'<rect x="{x(low):.1f}" y="{y + 4}" width="{max(2.0, x(high) - x(low)):.1f}" '
                            f'height="{ROW_H - 8}" fill="{color}">{tip(title)}</rect>')
        for e in (r.get("edges") or {}).values():
            if e.get("first_fail") is not None:
                fail_tip = tip(f"fails from {e['first_fail']} ({'+'.join(e.get('probes') or [])})")
                rows_svg.append(f'<line x1="{x(e["first_fail"]):.1f}" x2="{x(e["first_fail"]):.1f}" y1="{y + 2}" '
                                f'y2="{y + ROW_H - 2}" stroke="{c["fail"]}" stroke-width="2">{fail_tip}</line>')
        if r.get("default") is not None:
            d = x(r["default"])
            default_tip = tip(f"default {r['default']}")
            rows_svg.append(f'<line x1="{d:.1f}" x2="{d:.1f}" y1="{y}" y2="{y + ROW_H}" stroke="{c["fg"]}" '
                            f'stroke-width="2">{default_tip}</line>')
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
                    cell_tip = tip(f"{probe}, pass {n}, {p['value']}: {f}/{s} failed"
                                   + (f" ({'; '.join(p.get('notes') or [])})" if p.get("notes") else ""))
                    rows_svg.append(f'<rect x="{x(p["value"]) - cell_w / 2:.1f}" y="{y + k * STRIP_H}" '
                                    f'width="{cell_w:.1f}" height="{STRIP_H - 1}" '
                                    f'fill="{_cell_color(f, s, p.get("hung", False), c["bar"])}">{cell_tip}</rect>')
                k += 1
        rows_svg.append(f'<text {muted()} x="0" y="{y + STRIP_H * strip_lines - 1}">'
                        f'{_e(", ".join(f"pass {n}" for n in passes))}: {_e(", ".join(probes))}</text>')
        y += STRIP_H * strip_lines + 10
    height = y + 10
    axis = []
    tick = next((t for t in (5, 10, 20, 25, 50, 100, 200, 500) if (hi - lo) / t <= 12), 1000)
    for v in range(int(lo // tick + 1) * tick, int(hi) + 1, tick):
        axis.append(f'<line x1="{x(v):.1f}" x2="{x(v):.1f}" y1="20" y2="{height - 10}" stroke="{c["grid"]}"/>'
                    f'<text {muted("middle")} x="{x(v):.1f}" y="14">{v:.0f}</text>')
    for m in marks:
        axis.append(f'<line x1="{x(m):.1f}" x2="{x(m):.1f}" y1="20" y2="{height - 10}" stroke="{c["mark"]}" '
                    f'stroke-dasharray="4 3" stroke-width="2">{tip(f"under review: {m}")}</line>')
    unit = (lines[0].get("knob") or {}).get("unit", "ns")
    body = "".join(axis) + "".join(rows_svg)
    if not static:
        return (f"<h2>{_e(name)} ({_e(unit)})</h2>"
                f'<svg width="{WIDTH}" height="{height}" role="img" aria-label="{_e(name)} clean windows">'
                + body + "</svg>")
    full = height + HEAD_H
    key = ("bar: clean window; tick across a bar: its build's default; red tick: first failing value"
           + (f"; dashed: {', '.join(str(m) for m in marks)}" if marks else ""))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{full}" viewBox="0 0 {WIDTH} {full}" '
            f'font-family="{_FONT}" role="img" aria-label="{_e(name)} clean windows">'
            f'<rect width="{WIDTH}" height="{full}" fill="{c["bg"]}"/>'
            f'<text x="0" y="17" fill="{c["fg"]}" font-size="14" font-weight="600">{_e(name)} ({_e(unit)})</text>'
            f'<text x="{WIDTH}" y="17" fill="{c["muted"]}" font-size="11" text-anchor="end">{_e(key)}</text>'
            f'<g transform="translate(0,{HEAD_H})">{body}</g></svg>\n')


def export_svgs(records: Iterable[Mapping], folder: Path, marks: Mapping[str, list[int]] | None = None, *,
                builds: Mapping[str, str] | None = None, prefix: str = "margin-") -> list[Path]:
    """A static SVG file per knob, ``<prefix><knob>.svg`` in ``folder``, each
    drawing the same on its own, for a public doc: rows labeled by machine
    model and standard (``public_labels``). A later export writes over them."""
    marks = marks or {}
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, rs in by_knob(records).items():
        svg = _knob_chart(name, rs, marks.get(name, []), static=True, builds=builds)
        if not svg:
            continue
        path = folder / f"{prefix}{name}.svg"
        path.write_text(svg, encoding="utf-8", newline="\n")
        written.append(path)
    return written


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


# The overview's sections, in the order a bus cycle runs: (knob group, image or None for any, heading).
_SECTIONS = (("cpu", "main", "CPU cycle, main image"), ("cpu", "minimal", "CPU cycle, MinimalBoot"),
             ("vic", None, "VIC cycle"), ("dma", None, "DMA"), ("kernal", None, "Kernal Replace HIRAM sense"))
# Within a section, the knob table's order with nS_MaxAdj (timed at the interrupt's entry) last.
_ORDER = {k.name: (k.phase == "entry", i) for i, k in enumerate(KNOBS.values())}
_BY_NAME = {k.name: k for k in KNOBS.values() if k.swept}
# The cycle chart's sections: the firmware routines that time a bus cycle, each with the knobs it waits on.
# A knob two routines share is drawn in both, so each section reads as the whole chain.
_ROUTINES = (
    ("CPU cycle", "isrPHI2, on Phi2 rise",
     ("nS_MaxAdj", "nS_RWnReady", "nS_PLAprop", "nS_DataSetup", "nS_DataHold")),
    ("Kernal Replace, Skoe sense", "KernalCheck, after the address read; the byte held to nS_DataHold",
     ("nS_RWnReady", "Cyc_KernProp", "nS_DataHold")),
    ("VIC cycle", "EmulateVicCycles, after Phi2 fall, Ultimax images",
     ("nS_VICStart", "nS_VICDHold")),
    ("DMA", "the start's assert, then DMAByte; data from the re-latch",
     ("nS_DMAAssert", "nS_DMABAWait", "nS_DMASetup", "nS_DMADataHold", "nS_DMADataSetup")),
    ("Kernal Replace, DMA sense", "KDS_Begin and KDS_SenseCycle, after a write to $01",
     ("nS_DMAAssert", "nS_DMABAWait", "nS_DMASetup", "nS_KSenseSample", "nS_DMADataSetup")),
)
OV_ROW, OV_HEAD, OV_LABEL = 24, 30, 250


def _image(record: Mapping) -> str:
    return (record.get("firmware") or {}).get("image") or "main"


def _machine(record: Mapping) -> str:
    m = record.get("machine") or {}
    model = "C128" if m.get("c128") else (m.get("model") or "?")
    return f"{model} {m.get('id') or '?'} {m.get('standard') or '?'}, {record.get('board') or '?'}"


def _latest(records: Iterable[Mapping]) -> dict[str, list[Mapping]]:
    """Per machine, the newest record of each image, knob, probe set and
    knobs set (records come oldest first)."""
    newest: dict[tuple, Mapping] = {}
    for r in records:
        if r.get("mode") == "grid":
            continue
        key = (_machine(r), _image(r), (r.get("knob") or {}).get("name"), "+".join(r.get("probes") or []), _set(r))
        newest[key] = r
    out: dict[str, list[Mapping]] = defaultdict(list)
    for (machine, *_), r in newest.items():
        out[machine].append(r)
    return dict(out)


def _scale(r: Mapping) -> float:
    return NS_PER_TEENSY_CYCLE if (r.get("knob") or {}).get("unit") == "cycles" else 1.0


def _usb(r: Mapping) -> bool:
    return any("usbload" in p for p in r.get("probes") or [])


def _ends(r: Mapping) -> list[float]:
    """Every clean end and first failing value a record found."""
    return [v for e in (r.get("edges") or {}).values() if e
            for v in (e.get("clean_to"), e.get("first_fail")) if v is not None]


def _span(values: list[float], step: int) -> tuple[float, float]:
    lo, hi = min(values), max(values)
    pad = max(hi - lo, step) * 0.04
    return (lo - pad) // step * step, -((-(hi + pad)) // step) * step


def _row(r: Mapping, y: float, x, place, name: str, origin: str = "", tick: bool = False, tag: str = "") -> str:
    """One record's row: the clean window from ``place(value)`` to
    ``place(value)`` on the axis ``x``, each swept end red where a failure was
    found (the last clean value, then the first failing one) or green at the
    sweep's limit; ``tick`` marks the default on the bar."""
    mid = y + OV_ROW / 2
    unit = " cycles" if _scale(r) != 1.0 else ""
    edges = {way: e for way, e in (r.get("edges") or {}).items() if e}     # the ways swept
    tip = "; ".join(
        [_e(label(r)), f"default {r['default']}{unit}" + (f", {_e(origin)}" if origin else "")]
        + [f"{way}: " + (f"not finished, clean to {e.get('clean_to')}" if e.get("open")
                         else f"clean to {e.get('clean_to')}, fails at {e['first_fail']}" if e.get("first_fail")
                         is not None else f"clean to the sweep's limit {e.get('clean_to')}")
           for way, e in edges.items()])
    out = [f'<g><title>{tip}</title><rect x="0" y="{y + 1}" width="{WIDTH}" height="{OV_ROW - 2}" fill="transparent"/>',
           f'<text x="12" y="{mid + 4}">{_e(name)}' + (f'<tspan class="muted" dx="6">{_e(tag)}</tspan>' if tag
                                                      else "") + "</text>",
           f'<text class="num" x="{OV_LABEL - 8}" y="{mid + 4}" text-anchor="end">{r["default"]}{_e(unit)}</text>']
    ends = {way: (edges.get(way) or {}) for way in ("down", "up")}
    if any(way in edges and e.get("clean_to") is None for way, e in ends.items()):
        out.append(f'<text x="{x(place(r["default"])) + 8:.1f}" y="{mid + 4}" style="fill:var(--critical)">'
                   "fails at the default</text></g>")
        return "".join(out)
    low = place(ends["down"]["clean_to"] if "down" in edges else r["default"])
    high = place(ends["up"]["clean_to"] if "up" in edges else r["default"])
    out.append(f'<rect x="{x(low):.1f}" y="{mid - 5}" width="{max(2.0, x(high) - x(low)):.1f}" height="10" rx="2" '
               f'fill="var(--window)"/>')
    if tick:
        d = x(place(r["default"]))
        out.append(f'<line x1="{d:.1f}" x2="{d:.1f}" y1="{mid - 8}" y2="{mid + 8}" stroke="var(--fg)" '
                   f'stroke-width="2"/>')
    for way, at in (("down", low), ("up", high)):
        if way not in edges:
            continue
        e = ends[way]
        tx, anchor = (x(at) - 6, "end") if way == "down" else (x(at) + 6, "start")
        if e.get("open"):                # the walk stopped first: no end to draw, only how far it got
            out.append(f'<text class="num muted" x="{tx:.1f}" y="{mid + 4}" text-anchor="{anchor}">'
                       + (f'? {e["clean_to"]}' if way == "down" else f'{e["clean_to"]} ?') + "</text>")
            continue
        failed = e.get("first_fail") is not None
        cap = x(at) if way == "down" else x(at) - 4
        color = "var(--critical)" if failed else "var(--good)"
        out.append(f'<rect class="{"fail" if failed else "limit"}" x="{cap:.1f}" y="{mid - 7}" width="4" '
                   f'height="14" rx="1" fill="{color}"/>')
        clean = f'<tspan>{e["clean_to"]}</tspan>'
        fail = (f'<tspan style="fill:var(--critical)">'
                + (f'{e["first_fail"]} \u2715 ' if way == "down" else f' \u2715 {e["first_fail"]}')
                + "</tspan>") if failed else ""
        out.append(f'<text class="num" x="{tx:.1f}" y="{mid + 4}" text-anchor="{anchor}">'
                   + (fail + clean if way == "down" else clean + fail) + "</text>")
    out.append("</g>")
    return "".join(out)


def _axis(lo: float, hi: float, step: int, x, height: float, text) -> list[str]:
    out = []
    for t in range(int(lo), int(hi) + 1, step):
        out.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="22" y2="{height - 22}" stroke="var(--grid)"/>'
                   f'<text class="num" x="{x(t):.1f}" y="14" text-anchor="middle">{text(t)}</text>'
                   f'<text class="num" x="{x(t):.1f}" y="{height - 8}" text-anchor="middle">{text(t)}</text>')
    return out


def _overview(machine: str, records: list[Mapping]) -> str:
    sections = []
    for group, image, heading in _SECTIONS:
        rows = [r for r in records if (r.get("knob") or {}).get("group") == group and r.get("default") is not None
                and (image is None or _image(r) == image)]
        rows.sort(key=lambda r: (_ORDER.get(r["knob"]["name"], (True, 99)), _usb(r), len(r.get("probes") or [])))
        if rows:
            sections.append((heading, rows))
    if not sections:
        return ""
    offsets = [0.0] + [(v - r["default"]) * _scale(r) for _, rows in sections for r in rows for v in _ends(r)]
    lo, hi = _span(offsets, 50)
    x0, x1 = OV_LABEL + 70, WIDTH - 90

    def x(ns: float) -> float:
        return x0 + (ns - lo) / (hi - lo) * (x1 - x0)

    out, y = [], 30
    for heading, rows in sections:
        out.append(f'<text class="head" x="0" y="{y + OV_HEAD - 10}">{_e(heading)}</text>')
        y += OV_HEAD
        for r in rows:
            out.append(_row(r, y, x, lambda v, r=r: (v - r["default"]) * _scale(r),
                            r["knob"]["name"] + (" \u00b7 USB load" if _usb(r) else "")
                            + (f" \u00b7 {_set(r)}" if r.get("set") else "")))
            y += OV_ROW
    height = y + 30
    axis = _axis(lo, hi, 50, x, height, lambda t: "default" if t == 0 else f"{t:+d}".replace("-", "\u2212"))
    axis.append('<text class="num" x="0" y="14">ns from the default</text>')
    zero = (f'<line x1="{x(0):.1f}" x2="{x(0):.1f}" y1="22" y2="{height - 22}" stroke="var(--fg)" '
            f'stroke-width="1.5" stroke-dasharray="4 3"/>')
    return (f"<h2>Overview: {_e(machine)}</h2>"
            f'<svg width="{WIDTH}" height="{height}" role="img" aria-label="{_e(machine)} clean windows">'
            + "".join(axis) + "".join(out) + zero + "</svg>")


def _cycle_chart(machine: str, records: list[Mapping]) -> str:
    """The main image's windows placed in the Phi2 cycle from the rise the
    interrupt fires on, a section per routine (``_ROUTINES``), each knob from
    the point it counts from, rows in the order their defaults come. A DMA
    cycle starts at the fall (drawn at half the cycle) and moves its data in
    the next high half, so the axis runs on into the next cycle. Records
    under USB load or with another knob set are left out."""
    rows = [r for r in records if _image(r) == "main" and not _usb(r) and not r.get("set")
            and r.get("default") is not None and (r.get("knob") or {}).get("name") in _BY_NAME]
    if not rows:
        return ""
    m = rows[0].get("machine") or {}
    standard = "NTSC C128" if m.get("c128") and m.get("standard") == "NTSC" else (m.get("standard") or "NTSC")
    cycle = CYCLE_NS.get(standard, CYCLE_NS["NTSC"])
    fall = cycle / 2
    defaults = {k.name: k.default(standard) for k in KNOBS.values()}
    defaults.update({r["knob"]["name"]: r["default"] for r in rows})
    relatch = defaults["nS_DMASetup"]
    origins = {   # phase -> (where the count starts, what it is called)
        "rise": (0.0, "from Phi2 rise"),
        "fall": (fall, "from Phi2 fall"),
        "relatch": (fall + relatch, f"from the re-latch (nS_DMASetup {relatch} after the fall)"),
        "entry": (0.0, "from this interrupt: the next one later than this is late"),
        "cycles": (float(defaults["nS_RWnReady"]), "Teensy cycles after the A14 drive, about rise + nS_RWnReady "
                   "(code time not counted)"),
    }

    def place_for(r: Mapping):
        start = origins[_BY_NAME[r["knob"]["name"]].phase][0]
        return lambda v: start + v * _scale(r)

    by_name = {r["knob"]["name"]: r for r in rows}
    sections = [(title, sub, sorted((by_name[n] for n in names if n in by_name),
                                    key=lambda r: place_for(r)(r["default"])))
                for title, sub, names in _ROUTINES]
    sections = [(title, sub, rs) for title, sub, rs in sections if rs]
    values = [0.0, float(cycle)] + [place_for(r)(v) for r in rows for v in _ends(r) + [r["default"]]]
    lo, hi = min(values) // 100 * 100, _span(values, 100)[1]
    x0, x1 = OV_LABEL + 70, WIDTH - 90

    def x(ns: float) -> float:
        return x0 + (ns - lo) / (hi - lo) * (x1 - x0)

    out, y = [], 40
    for title, sub, rs in sections:
        out.append(f'<text class="head" x="0" y="{y + OV_HEAD - 10}">{_e(title)}'
                   f'<tspan class="muted" dx="8">{_e(sub)}</tspan></text>')
        y += OV_HEAD
        for r in rs:
            phase = _BY_NAME[r["knob"]["name"]].phase
            tag = {"fall": "fall +", "rise": "rise +", "relatch": "re-latch +", "entry": "interrupt +",
                   "cycles": "\u2248 rise +"}[phase]
            out.append(_row(r, y, x, place_for(r), r["knob"]["name"], origins[phase][1], tick=True, tag=tag))
            y += OV_ROW
    height = y + 30
    bands = [f'<rect x="{x(max(lo, a)):.1f}" y="22" width="{x(min(hi, b)) - x(max(lo, a)):.1f}" '
             f'height="{height - 44}" fill="var(--band)"/>'
             for a, b in ((0.0, fall), (float(cycle), cycle + fall)) if max(lo, a) < min(hi, b)]    # Phi2 high
    lines = []
    for t, name in ((0.0, "Phi2 rise"), (fall, "Phi2 fall"), (float(cycle), "next rise"), (cycle + fall, "next fall"),
                    (fall + relatch, "re-latch")):
        if not lo <= t <= hi:
            continue
        faint = name == "re-latch"         # its name goes left of the line: the next rise comes just after it
        lines.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="22" y2="{height - 22}" stroke="var(--fg)" '
                     f'stroke-width="{1 if faint else 1.5}" stroke-dasharray="{"2 3" if faint else "4 3"}"/>'
                     f'<text class="muted" x="{x(t) + (-4 if faint else 4):.1f}" y="36" '
                     f'text-anchor="{"end" if faint else "start"}">{name}</text>')
    axis = _axis(lo, hi, 100, x, height, lambda t: f"{t}")
    axis.append('<text class="num" x="0" y="14">ns from Phi2 rise</text>')
    return (f"<h2>In the cycle: {_e(machine)}, {cycle} ns</h2>"
            f'<svg width="{WIDTH}" height="{height}" role="img" aria-label="{_e(machine)} windows in one Phi2 cycle">'
            + "".join(bands) + "".join(axis) + "".join(lines) + "".join(out) + "</svg>")


def by_knob(records: Iterable[Mapping]) -> dict[str, list[Mapping]]:
    out: dict[str, list[Mapping]] = defaultdict(list)
    for r in records:
        out[(r.get("knob") or {}).get("name") or "?"].append(r)
    return dict(sorted(out.items()))


def render(records: Iterable[Mapping], out: Path, marks: Mapping[str, list[int]] | None = None,
           title: str = "TReadmill margin map") -> Path:
    """Write the map to ``out`` (an HTML file) and return it."""
    marks = marks or {}
    records = list(records)
    groups = by_knob(records)
    body = [f"<h1>{_e(title)}</h1>",
            '<p class="note">Overview: each knob\'s clean window as its offset from the default (the dashed line); '
            "a red end is the last clean value with the first failing one after the \u2715, a green end the sweep's "
            "limit, reached clean. The latest record of each knob, image and probe set; Cyc_KernProp is in Teensy "
            f"cycles, drawn at {NS_PER_TEENSY_CYCLE:.2f} ns each. In the cycle: the same windows of the main "
            "image, without USB load, on a time line from the Phi2 rise the interrupt fires on, a section per "
            "firmware routine with the knobs it waits on, each from the point it counts from; shaded: Phi2 high, "
            "the fall drawn at half the cycle; the tick on a bar is its default.</p>"]
    for machine, latest in _latest(records).items():
        body.append(_overview(machine, latest))
        body.append(_cycle_chart(machine, latest))
    if not groups:
        body.append("<p>No margin records.</p>")
    body.append('<details><summary>Every record, a chart per knob</summary><p class="note">Bars: the clean window. '
                "Red ticks: the first failing value each side. The tick across a bar: the default. Dashed: a value "
                "under review. Cells under a row: each value probed, per pass and probe (green clean, red failed, "
                "black a collapse); hover for counts.</p>")
    for name, rs in groups.items():
        body.append(_knob_chart(name, rs, marks.get(name, [])))
        for g in (r for r in rs if r.get("mode") == "grid"):
            body.append(f"<h2>{_e(name)} grid</h2>" + _grid_chart(g))
    body.append("</details>")
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
