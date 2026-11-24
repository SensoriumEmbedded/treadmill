"""2026-11-24: does the C64 keep running through many remote pauses?

    treadmill exec --station <id> scripts/2026-11-24-pause-loop.py --json [-- --cycles 600 --check-every 50]

Firmware: any build; the release build of the change under test is the point.
Station: any TR with video (a plain TR has no DMA, so the screen is read off
the picture).

A remote pause (C64PauseOn) starts the firmware's asynchronous DMA: the
Phi2 interrupt asserts /DMA on the first read cycle it judges safe, using the
VIC's BA. A wrong judgment asserts it on a write cycle, the write is lost,
and the C64's program can crash. On a plain TR, pause is the only
asynchronous DMA start. This pauses and resumes the TR menu ``--cycles``
times, each pause and each run held a random 0 to ``--hold`` seconds beyond
the link's own spacing. Every ``--check-every`` cycles (and before the first
and after the last) it checks by video that a pause freezes the menu's clock
and that a resume lets it tick, and at the end that the menu's other rows
are as they were at the start. ``result`` holds the counts, each check, and
the cells that differ at the end. The board is resumed on the way out.
"""

import argparse
import json
import random
import time

parser = argparse.ArgumentParser()
parser.add_argument("--cycles", type=int, default=600)
parser.add_argument("--check-every", type=int, default=50)
parser.add_argument("--hold", type=float, default=0.2, help="most extra seconds a pause or a run is held")
parser.add_argument("--settle", type=float, default=1.6, help="seconds between the two reads of a check")
parser.add_argument("--seed", type=int, default=None)
opts = parser.parse_args()

CLOCK_ROW, CLOCK_COLS = 1, slice(29, 39)   # tests/golden/menu/root.screen.txt: the clock on row 1
STATIC_ROWS = [0] + list(range(2, 25))
UNSURE = "?"

rng = random.Random(opts.seed)


def clock() -> str:
    """The clock's text, read again while any of its cells reads unsure."""
    for _ in range(4):
        text = screen.read().row(CLOCK_ROW)[CLOCK_COLS]
        if UNSURE not in text:
            return text
    return text


def static_rows() -> list[str]:
    rows = screen.read().rows()
    return [rows[r] for r in STATIC_ROWS]


def check(at: int) -> dict:
    """A verified pause (the clock holds) then a verified run (it moves)."""
    link.pause(True)
    time.sleep(0.2)
    a = clock()
    time.sleep(opts.settle)
    b = clock()
    link.pause(False)
    time.sleep(0.2)
    c = clock()
    time.sleep(opts.settle)
    d = clock()
    entry = {"cycle": at, "paused": [a, b], "running": [c, d],
             "frozen": a == b and UNSURE not in a, "ticking": c != d and UNSURE not in c + d}
    log(f"check at {at}: paused {a!r} {b!r}, running {c!r} {d!r}"
        f"{'' if entry['frozen'] and entry['ticking'] else '  <-- FAIL'}")
    return entry


before = static_rows()
first = clock()
time.sleep(opts.settle)
if clock() == first:
    raise SystemExit(f"the menu's clock reads {first!r} twice {opts.settle} s apart: it does not tick, so a pause "
                     "cannot be told from a stopped C64")

result = {"cycles": 0, "checks": [], "failed_checks": 0}
started = time.monotonic()
try:
    result["checks"].append(check(0))
    for n in range(1, opts.cycles + 1):
        link.pause(True)
        time.sleep(rng.uniform(0, opts.hold))
        link.pause(False)
        time.sleep(rng.uniform(0, opts.hold))
        result["cycles"] = n
        if n % opts.check_every == 0 or n == opts.cycles:
            result["checks"].append(check(n))
finally:
    link.pause(False)

after = static_rows()
diff = [{"row": r, "col": c, "before": x, "after": y}
        for r, (old, new) in zip(STATIC_ROWS, zip(before, after))
        for c, (x, y) in enumerate(zip(old, new)) if x != y and UNSURE not in (x, y)]
result["failed_checks"] = sum(not (e["frozen"] and e["ticking"]) for e in result["checks"])
result["menu_cells_changed"] = diff
result["seconds"] = round(time.monotonic() - started, 1)
(artifacts / "pause-loop.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
(artifacts / "before.screen.txt").write_text("\n".join(before) + "\n", encoding="utf-8")
(artifacts / "after.screen.txt").write_text("\n".join(after) + "\n", encoding="utf-8")
log(f"{result['cycles']} pause cycles in {result['seconds']} s, {len(result['checks'])} checks, "
    f"{result['failed_checks']} failed, {len(diff)} menu cells changed")
