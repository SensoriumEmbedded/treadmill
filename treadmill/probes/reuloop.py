"""reuloop: the C64's own REU transfers, stash and fetch, counted on the C64.

``reuclient.a`` loops a page out to the REU and back and compares it, so
every round is a DMA write and a DMA read of 256 bytes, started by the C64
(the path PR #99's first-byte loss was on). Its counters are read by DMA,
with the defaults back in place while a DMA knob is off. It needs Special
IO set to an REU: without one the fetch brings nothing back and every
round counts as bad, which ``setup`` reports as that rather than as an edge.
"""

from __future__ import annotations

import time
from pathlib import Path

from treadmill.board import Board
from treadmill.sweeps import Sample, Setting, SweepError, at_defaults, relaunch_reset

READY, READY_VALUE = 0xC104, 0x5A
COUNTERS = 0xC110                 # rounds (16-bit), then bad rounds (16-bit)
START_S = 15.0
CHECK_S = 1.0


class ReuLoopProbe:
    """A sample is the good and bad rounds over ``dwell`` seconds; a client
    that stops counting has crashed and is relaunched at the defaults."""

    unit = "rounds"
    bad_at_defaults = "is Special IO set to an REU?"

    def __init__(self, board: Board, program: Path, *, dwell: float = 10.0, sprites: bool = False,
                 drive: str = "sd", remote: str | None = None, log=print):
        self.board = board
        self.program = Path(program)
        self.dwell = dwell
        self.drive = drive
        self.name = "reuloop-sprites" if sprites else "reuloop"
        self.remote = remote or f"/treadmill/{self.name}.prg"
        self.log = log
        self._stalled = False

    def counters(self) -> tuple[int, int]:
        b = self.board.link.read_memory(COUNTERS, 4)
        return b[0] | b[1] << 8, b[2] | b[3] << 8

    def setup(self) -> None:
        if not self.board.dma_allowed():
            raise SweepError("the reuloop probe reads its counters by DMA: it needs a TR+ running its main image")
        self.board.ensure_menu()
        link = self.board.link
        link.push_file(self.program.read_bytes(), self.remote, self.drive)
        link.write_memory(READY, b"\0")
        link.launch(self.remote, self.drive)
        deadline = time.monotonic() + START_S
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if link.read_memory(READY, 1)[0] == READY_VALUE:
                break
        else:
            raise SweepError(f"{self.name}: the C64 program never reported ready")
        r0, e0 = self.counters()
        time.sleep(CHECK_S)
        r1, e1 = self.counters()
        good, bad = (r1 - r0) & 0xFFFF, (e1 - e0) & 0xFFFF
        if good + bad == 0:
            raise SweepError(f"{self.name}: the C64 program is not looping")
        if bad:
            raise SweepError(f"{self.name}: {bad} of {good + bad} {self.unit} bad at the defaults: "
                             f"{self.bad_at_defaults}")
        self._stalled = False
        self.log(f"[sweep] {self.name} at the defaults: {good} {self.unit} in {CHECK_S:.0f} s")

    def workload(self) -> None:
        """What runs at the value under test while the C64 counts."""
        time.sleep(self.dwell)

    def sample(self, label: str, setting: Setting | None = None) -> Sample:
        if self._stalled:
            self.log(f"[sweep] {self.name} stopped at the last value; resetting and relaunching for {label}")
            with at_defaults(setting, always=True):
                relaunch_reset(self.board, self.name, self.log)
                self.setup()
        with at_defaults(setting):
            before = self.counters()
        self.workload()
        with at_defaults(setting):
            after = self.counters()
        return self.delta(before, after)

    def delta(self, before: tuple[int, int], after: tuple[int, int]) -> Sample:
        """The rounds between two readings, each modulo 16 bits."""
        (r0, e0), (r1, e1) = before, after
        good, bad = (r1 - r0) & 0xFFFF, (e1 - e0) & 0xFFFF
        if good + bad == 0:
            self._stalled = True
            return Sample(1, 1, f"{self.name} stopped: the C64 hung", hung=True)
        return Sample(bad, good + bad, f"{bad} bad of {good + bad} {self.unit}" if bad else None)

    def teardown(self) -> None:
        self.board.reset()
        self.board.wait_for_menu()
