"""cpuwatch: the C64's own writes while the board starts and stops DMA.

``cpuwatch.a`` writes a page and checks it, pass after pass, and counts the
passes with a wrong byte; a sample runs the firmware's ``z###`` beside it,
so /DMA goes up and down all through the loop. Asserted too late
(``nS_DMAAssert``), /DMA lands in a cycle the CPU is already using, and the
write the CPU was making goes wrong; the pattern test's own counts are the
zpattern probe's business. Counters are read by DMA at the defaults, as
reuloop's are.
"""

from __future__ import annotations

from pathlib import Path

from treadmill.board import Board
from treadmill.link import DebugCommands
from treadmill.probes.reuloop import ReuLoopProbe
from treadmill.probes.zpattern import PAGE_S, PATTERNS, ROW, STALL, START_S
from treadmill.sweeps import SweepError


class CpuWatchProbe(ReuLoopProbe):
    unit = "passes"
    bad_at_defaults = "the C64's own writes go wrong with no DMA off its defaults"

    def __init__(self, board: Board, dbg: DebugCommands, program: Path, *, pages: int = 100, drive: str = "sd",
                 log=print):
        super().__init__(board, program, drive=drive, remote="/treadmill/cpuwatch.prg", log=log)
        self.name = "cpuwatch"
        self.dbg = dbg
        self.pages = pages

    def setup(self) -> None:
        if not self.dbg.available("z"):
            raise SweepError("the cpuwatch probe starts DMA with z###: it needs a build with Dbg_SerDMA")
        super().setup()

    def workload(self) -> None:
        self.dbg.run(f"z{self.pages:03d}", lambda t: len(ROW.findall(t)) >= PATTERNS or bool(STALL.search(t)),
                     START_S + self.pages * PAGE_S)
