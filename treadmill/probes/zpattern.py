"""zpattern: the firmware's own DMA pattern test (``z###``, ``Dbg_SerDMA``).

At $C000 the board writes a pre-fill page by DMA, reads it back, writes a
pattern over it and reads that back, ``pages`` times for each of five
patterns: $FF over $FF (the control: nothing has to change), $FF over
$00, $00 over $FF, $00/$FF alternating over $00 and $55/$AA alternating
over $FF, so every bit swings both ways and the bus swings every cycle. It
is the host's DMA alone, write and read, with the C64 running whatever it
runs; it overwrites $C000-$C0FF, so the C64 is reset to the menu after the
sweep (with the defaults back, by then).
"""

from __future__ import annotations

import re

from treadmill.board import Board
from treadmill.link import DebugCommands
from treadmill.sweeps import Sample, Setting, SweepError

PATTERNS = 5
ROW = re.compile(r"^(\$\w\w(?:/\$\w\w alt)?) over \$(\w\w) @ \$\w+: (\d+) bad of (\d+) bytes"
                 r"(?:, pre-fill (\d+) bad)?", re.M)
STALL = re.compile(r"^\s*no (.+?) on pass (\d+)", re.M)
START_S, PAGE_S = 30.0, 0.2       # the bench's longest: 300 pages took about a minute on a C128


class ZPatternProbe:
    """One ``z###`` per sample: failures are the bad bytes of all five
    patterns and of their pre-fills, out of every byte written and read."""

    name = "zpattern"

    def __init__(self, board: Board, dbg: DebugCommands, *, pages: int = 100, log=print):
        if not 1 <= pages <= 999:
            raise SweepError(f"zpattern pages {pages}: 1 to 999 (z### takes three digits)")
        self.board = board
        self.dbg = dbg
        self.pages = pages
        self.log = log

    def setup(self) -> None:
        if not self.dbg.available("z"):
            raise SweepError("the zpattern probe needs a build with Dbg_SerDMA (z###)")
        if not self.board.dma_allowed():
            raise SweepError("the zpattern probe is the board's DMA: it needs a TR+ running its main image")

    def sample(self, label: str, setting: Setting | None = None) -> Sample:
        def done(text: str) -> bool:
            return len(ROW.findall(text)) >= PATTERNS or bool(STALL.search(text))

        text = self.dbg.run(f"z{self.pages:03d}", done, START_S + self.pages * PAGE_S)
        rows = ROW.findall(text)
        stall = STALL.search(text)
        if stall or len(rows) < PATTERNS:
            why = f"no {stall.group(1)} on pass {stall.group(2)}" if stall else "the pattern test did not finish"
            return Sample(1, 1, f"{why} ({len(rows)} of {PATTERNS} patterns)", hung=True)
        bad = sum(int(r[2]) for r in rows)
        prefill = sum(int(r[4] or 0) for r in rows)
        total = sum(int(r[3]) for r in rows)
        note = None
        if bad or prefill:
            worst = ", ".join(f"{r[0]} over ${r[1]} {r[2]}" for r in rows if int(r[2]))
            note = f"{bad}/{total} bad, pre-fill {prefill}" + (f" ({worst})" if worst else "")
        return Sample(bad + prefill, 2 * total, note)

    def teardown(self) -> None:
        self.board.reset()
        self.board.wait_for_menu()
