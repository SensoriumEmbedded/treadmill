"""``report.html``: one run, readable without the suite.

Rendered from ``run.json`` and the test folders next to it: a header that
says what ran against what, the outcome of every test, and for each test
that failed or errored the tail of the board's output, the golden diff and
the frame strip its folder holds. Links are relative, so the run folder can
be zipped and read elsewhere.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

TAIL_LINES = 40

_STYLE = """
body { font: 14px/1.4 -apple-system, Segoe UI, sans-serif; margin: 2em; color: #222; }
h1 { font-size: 1.4em; } h2 { font-size: 1.1em; margin-top: 2em; }
table { border-collapse: collapse; } td, th { padding: 2px 10px; text-align: left; vertical-align: top; }
th { background: #eee; } tr:nth-child(even) td { background: #f7f7f7; }
.passed { color: #176b17; } .failed, .error { color: #a01818; font-weight: bold; } .skipped { color: #777; }
pre { background: #f4f4f4; padding: 8px; overflow-x: auto; font-size: 12px; }
img { max-width: 100%; border: 1px solid #ccc; }
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _rows(pairs: list[tuple[str, object]]) -> str:
    return "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in pairs if v not in (None, "", [], {}))


def _tail(path: Path, lines: int = TAIL_LINES) -> str:
    text = path.read_bytes().decode("latin-1", "replace").replace("\r", "")
    return "\n".join(text.splitlines()[-lines:])


def _header(meta: dict) -> str:
    st, bd, fw = meta.get("station") or {}, meta.get("board") or {}, meta.get("firmware") or {}
    video, cal = meta.get("video") or {}, meta.get("calibration") or {}
    pairs = [
        ("Station", f"{st.get('id', '?')}: {st.get('model', '')} {st.get('region', '')} on rig {st.get('rig', '')}"),
        ("Board", f"{bd.get('version', '?')} built {bd.get('built', '?')}, {bd.get('image', '?')} image, "
                  f"{st.get('tr_kind', '')}; sees {bd.get('machine_line', '?')}"),
        ("Firmware", f"{fw.get('spec', 'none')}" + (f" -> {fw['version']} ({fw.get('stamp')})" if fw.get("version") else "")),
        ("Commit", f"{fw.get('describe') or fw.get('commit') or ''}" + (" dirty" if fw.get("dirty") else "")),
        ("Defines", ", ".join(fw.get("defines") or [])),
        ("Flashed", {True: "yes", False: "no (the board already ran it)"}.get(fw.get("flashed"), "")),
        ("Capabilities", ", ".join(st.get("caps") or [])),
        ("Video", (f"{video.get('kind', '')} {video.get('mode', '')}".strip() if video.get("available")
                   else f"none ({video.get('reason', '')})" if video else "")),
        ("Calibration", f"{cal.get('method')} residual {cal.get('residual')}" if cal else ""),
        ("Suite", f"{meta.get('suite', '')} {meta.get('suite_commit') or ''} on {meta.get('host', '')}, "
                  f"Python {meta.get('python', '')}"),
        ("Started", f"{meta.get('started', '')} to {meta.get('finished', '')}"),
        ("Exit", meta.get("exit_status")),
        ("Aborted", meta.get("aborted")),
        ("Command", " ".join(meta.get("argv") or [])),
    ]
    return f"<table>{_rows(pairs)}</table>"


def _results(tests: list[dict]) -> str:
    rows = []
    for t in tests:
        name = _e(t["nodeid"])
        link = f'<a href="#{_e(t["folder"])}">{name}</a>' if t.get("folder") else name
        message = (t.get("message") or "").strip().splitlines()
        rows.append(f"<tr><td class=\"{_e(t['outcome'])}\">{_e(t['outcome'])}</td><td>{link}</td>"
                    f"<td>{t.get('duration', 0):.1f} s</td><td>{_e(message[-1] if message else '')}</td></tr>")
    return ("<table><tr><th>Outcome</th><th>Test</th><th>Time</th><th>Last line</th></tr>"
            + "".join(rows) + "</table>")


def _details(run: Path, t: dict) -> str:
    folder = run / t["folder"]
    parts = [f"<h2 id=\"{_e(t['folder'])}\">{_e(t['nodeid'])}: <span class=\"{_e(t['outcome'])}\">"
             f"{_e(t['outcome'])}</span></h2>"]
    if t.get("message"):
        parts.append(f"<pre>{_e(t['message'])}</pre>")
    for diff in sorted(folder.glob("*.diff.txt")):
        parts.append(f"<p>{_e(diff.name)} (screen read | golden)</p><pre>{_e(diff.read_text(encoding='utf-8'))}</pre>")
    strip = folder / "frames.png"
    if strip.is_file():
        parts.append(f'<p>The last frames before the failure</p><img src="{_e(t["folder"])}/frames.png">')
    serial = folder / "serial.log"
    if serial.is_file() and serial.stat().st_size:
        parts.append(f"<p>The board's output, last {TAIL_LINES} lines "
                     f'(<a href="{_e(t["folder"])}/serial.log">serial.log</a>)</p><pre>{_e(_tail(serial))}</pre>')
    xcheck = folder / "xcheck.json"
    if xcheck.is_file():
        worst = max((x.get("disagreement", 0) for x in json.loads(xcheck.read_text(encoding="utf-8"))), default=0)
        parts.append(f"<p>Video against DMA: worst disagreement {worst:.1%} "
                     f'(<a href="{_e(t["folder"])}/xcheck.json">xcheck.json</a>)</p>')
    return "\n".join(parts)


def render_report(run: Path) -> Path:
    """Write ``report.html`` next to the run's ``run.json`` and return it."""
    run = Path(run)
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    tests = meta.get("tests") or []
    summary = meta.get("summary") or {}
    counts = ", ".join(f"{n} {k}" for k, n in sorted(summary.items())) or "no tests ran"
    body = [f"<h1>TReadmill run {_e(meta.get('stamp', run.name))}</h1>", _header(meta),
            f"<h2>Results: {_e(counts)}</h2>", _results(tests)]
    body += [_details(run, t) for t in tests if t.get("folder") and t["outcome"] in ("failed", "error")]
    page = (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>TReadmill run {_e(meta.get('stamp'))}"
            f"</title><style>{_STYLE}</style></head><body>" + "\n".join(body) + "</body></html>\n")
    out = run / "report.html"
    out.write_text(page, encoding="utf-8")
    return out
