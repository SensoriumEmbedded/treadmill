"""reuloop: the C64's own REU transfers, stash and fetch, counted on the C64.

``reuclient.a`` loops a page out to the REU and back and compares it, so
every round is a DMA write and a DMA read of 256 bytes, started by the C64
(the path PR #99's first-byte loss was on). Its counters are read by DMA,
with the defaults back in place while a DMA knob is off. It needs Special
IO set to an REU: without one the fetch brings nothing back and every
round counts as bad, which ``setup`` reports as that rather than as an edge.

A sample whose rounds a second fall under ``STALL_SHARE`` of the setup's
rate at the defaults stopped partway and fails as a collapse. One whose
rounds were every one bad, or came at over ``RACING`` times that rate, is a
loop the last value broke rather than a verdict on this one: on a borrowed
NTSC C128 (2026-11-22) ``nS_DMASetup`` 490 left the loop counting every
round bad at twice its rate, and every value after it failed, the default
included (BACKLOG 49). Such a sample is taken again after a relaunch at the
defaults, and the second one stands.
"""

from __future__ import annotations

import time
from pathlib import Path

from treadmill.board import Board
from treadmill.sweeps import Sample, Setting, SweepError, at_defaults, relaunch_reset, stalled, with_stall

READY, READY_VALUE = 0xC104, 0x5A
COUNTERS = 0xC110                 # rounds (16-bit), then bad rounds (16-bit)
START_S = 15.0
CHECK_S = 1.0
RACING = 1.5                      # rounds a second past this many times the defaults' rate: a broken loop


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
        self.rate: float | None = None    # rounds a second at the defaults
        self._stalled = False

    def counters(self) -> tuple[int, int, float]:
        """(good rounds, bad rounds, when they were read)."""
        b = self.board.link.read_memory(COUNTERS, 4)
        return b[0] | b[1] << 8, b[2] | b[3] << 8, time.monotonic()

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
        r0, e0, t0 = self.counters()
        time.sleep(CHECK_S)
        r1, e1, t1 = self.counters()
        good, bad = (r1 - r0) & 0xFFFF, (e1 - e0) & 0xFFFF
        if good + bad == 0:
            raise SweepError(f"{self.name}: the C64 program is not looping")
        if bad:
            raise SweepError(f"{self.name}: {bad} of {good + bad} {self.unit} bad at the defaults: "
                             f"{self.bad_at_defaults}")
        self._stalled = False
        self.rate = (good + bad) / max(t1 - t0, 1e-6)
        self.log(f"[sweep] {self.name} at the defaults: {good} {self.unit} in {CHECK_S:.0f} s")

    def workload(self) -> None:
        """What runs at the value under test while the C64 counts."""
        time.sleep(self.dwell)

    def _relaunch(self, setting: Setting | None) -> None:
        with at_defaults(setting, always=True):
            relaunch_reset(self.board, self.name, self.log)
            self.setup()

    def _take(self, setting: Setting | None) -> tuple[Sample, str | None]:
        """One sample, and why its loop looks broken, if it does."""
        with at_defaults(setting):
            before = self.counters()
        self.workload()
        with at_defaults(setting):
            after = self.counters()
        return self.delta(before, after), self.broken(before, after)

    def sample(self, label: str, setting: Setting | None = None) -> Sample:
        if self._stalled:
            self.log(f"[sweep] {self.name} stopped at the last value; resetting and relaunching for {label}")
            self._relaunch(setting)
        got, broken = self._take(setting)
        if broken:
            self.log(f"[sweep] {self.name} at {label}: {broken}; relaunching at the defaults and sampling again")
            self._relaunch(setting)
            got, _ = self._take(setting)
        return got

    def broken(self, before: tuple, after: tuple) -> str | None:
        """Why the loop looks broken rather than failing at this value: every
        round bad, or rounds far faster than at the defaults."""
        good, bad = (after[0] - before[0]) & 0xFFFF, (after[1] - before[1]) & 0xFFFF
        seconds = after[-1] - before[-1]
        if good + bad and not good:
            return f"every one of {bad} {self.unit} bad"
        if self.rate and seconds > 0 and (good + bad) / seconds > RACING * self.rate:
            return (f"{(good + bad) / seconds:.0f} {self.unit} a second, {(good + bad) / seconds / self.rate:.1f} "
                    f"times the defaults' {self.rate:.0f}")
        return None

    def delta(self, before: tuple, after: tuple) -> Sample:
        """The rounds between two readings, each modulo 16 bits."""
        good, bad = (after[0] - before[0]) & 0xFFFF, (after[1] - before[1]) & 0xFFFF
        if good + bad == 0:
            self._stalled = True
            return Sample(1, 1, f"{self.name} stopped: the C64 hung", hung=True)
        why = stalled(self.name, good + bad, after[-1] - before[-1], self.rate, self.unit)
        if why:
            self._stalled = True
        return with_stall(Sample(bad, good + bad, f"{bad} bad of {good + bad} {self.unit}" if bad else None), why)

    def teardown(self) -> None:
        self.board.reset()
        self.board.wait_for_menu()
