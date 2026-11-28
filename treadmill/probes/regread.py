"""regread: the C64 reads a handler's registers in a loop and counts the
wrong answers, for the handlers whose reads take longer than idread's.

idread judges the CPU-cycle knobs on the EasyFlash handler's IO2 page, whose
read is ``DataPortWriteWaitLog(EZFlashRAM[Address])``, about the shortest
path the firmware has; other handlers run more of their own code before
they drive the byte, so their reads go wrong at a lower ``nS_PLAprop``. On
the 250407 C64 in NTSC, TeensyROM PR #121's ``regsweep.py`` had the REU's
registers failing from ``nS_PLAprop`` 209 on main, where idread was clean
to 230 (BACKLOG 35).

``regread`` runs ``reureadback.a`` built with ``HAMMER``, Special IO at the
REU: the C64 sets $DF02-$DF08 once and reads them back, an IO2 read every 9
cycles; a read that comes back wrong is read again a few cycles later and
counted as late (right then) or wrong twice. The probe starts a CIA2 timer
NMI that stores to $D418 every 200 cycles, as ``reuhammer.py`` runs it.
``regread-jobs`` runs host DMA jobs beside it, each slice started on that
store (``treadmill.probes.hostjobs``), and checks the last job's bytes
landed: the load under which the REU registers read wrong at the defaults.

``regread-menu`` reads the TeensyROM menu's own IO1 handler: with the menu
waiting for a key, the probe writes ``menuread.a`` into the C64's RAM by DMA
and points the IRQ vector at it, so the C64 runs it in the menu's place
with the menu's handler still serving IO1: the presence registers through
the handler's plain read, and the version string through its serial-string
read, the path the menu's banner comes over. Its one write, the string's
select, could land in a register the firmware keeps in EEPROM if its
address were read too early, so it sweeps ``nS_PLAprop`` alone (``only``).

All three read their counters by DMA, so they need a TR+ running its main
image; the counters are read at the defaults while a DMA knob is off. A
sample whose reads (or passes) a second fall under ``STALL_SHARE`` of the
setup's at the defaults stopped partway and fails as a collapse. A wrong
read costs the loop time of its own, so a sample with wrong reads is slow
without having stopped: it counts as stopped only if the counters still
stand still a moment after it (``ALIVE_S``).

The REU loop's rounds and misses, and the menu's passes, are 16-bit
counters, read modulo 16 bits: a reading taken longer apart than the
fastest takes to wrap undercounts. With every REU read wrong the misses
wrap in a few seconds, and the menu's passes in about 25 s at the defaults.
The default dwell (1 s) stays inside both; a soak's 10 s reading can
undercount the REU's misses only once nearly every read is wrong.
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Callable

from treadmill.board import Board
from treadmill.probes.hostjobs import Jobs, host_jobs, landed, start_nmi
from treadmill.sweeps import Sample, Setting, SweepError, at_defaults, relaunch_reset, stalled, with_stall

READY, READY_VALUE = 0xC104, 0x5A
COUNTERS, COUNTER_BYTES = 0xC110, 0x37      # reureadback.a: rounds, bad pages, seed, then per register ...
MISSES, TWICE = 0x10, 0x28                   # ... late misses and misses wrong twice (low bytes; high 8 on)
HELD = 0xF7                                  # what each register read back once set
REGISTERS = ("$DF02", "$DF03", "$DF04", "$DF05", "$DF06", "$DF07", "$DF08")
READS_PER_ROUND = 256 * len(REGISTERS)       # a round is 256 passes over the seven
START_S = 15.0
CHECK_S = 1.0
ALIVE_S = 0.25                               # the wait before a slow sample's counters are read again


def still(again: Callable[[], tuple] | None, after: tuple, why: str | None, failures: int) -> str | None:
    """``why`` the sample stalled, for a sample with wrong reads only if
    the counters read ``again`` have not moved since ``after``: its wrong
    reads alone can slow it under the share."""
    if why is None or not failures:
        return why
    return why if again is not None and again()[:-1] == after[:-1] else None


def reread(probe, setting: Setting | None) -> Callable[[], tuple]:
    """``probe``'s counters read once more, ``ALIVE_S`` on, for ``still``."""
    def again() -> tuple:
        time.sleep(ALIVE_S)
        with at_defaults(setting):
            return probe.counters()
    return again


class RegReadProbe:
    """The REU's registers, read back by the C64; ``jobs``: host DMA jobs beside it."""

    unit = "reads"

    def __init__(self, board: Board, program: Path, *, dwell: float = 1.0, jobs: bool = False, drive: str = "sd",
                 log=print):
        self.board = board
        self.program = Path(program)
        self.dwell = dwell
        self.jobs = jobs
        self.drive = drive
        self.name = "regread-jobs" if jobs else "regread"
        self.remote = f"/treadmill/{self.name}.prg"
        self.log = log
        self.held: bytes | None = None
        self.rate: float | None = None    # register reads a second at the defaults
        self.rng = random.Random(1)
        self._jobs = Jobs()
        self._stalled = False

    def counters(self) -> tuple:
        """(rounds, each register's late misses, each one's misses wrong twice, when they were read)."""
        b = self.board.link.read_memory(COUNTERS, COUNTER_BYTES)
        word = lambda at: b[at] | b[at + 8] << 8                # noqa: E731
        return (b[0] | b[1] << 8, tuple(word(MISSES + k) for k in range(len(REGISTERS))),
                tuple(word(TWICE + k) for k in range(len(REGISTERS))), time.monotonic())

    def setup(self) -> None:
        if not self.board.dma_allowed():
            raise SweepError(f"the {self.name} probe reads its counters by DMA: it needs a TR+ running its main image")
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
        self.held = link.read_memory(HELD, len(REGISTERS))
        start_nmi(link)
        self.rate = None
        before = self.counters()
        self.workload(CHECK_S)
        after = self.counters()
        got = self.delta(before, after)
        # Open bus answers most of the reads wrong where no REU is; a few wrong at the defaults are the
        # sweep's to report, at its first point.
        if got.hung or got.failures * 2 > got.samples:
            raise SweepError(f"{self.name}: {got.note or 'the C64 program is not reading'} at the defaults: "
                             "is Special IO set to an REU?")
        self._stalled = False
        self.rate = got.samples / max(after[-1] - before[-1], 1e-6)
        self.log(f"[sweep] {self.name} at the defaults: {got.samples} register reads in {CHECK_S:.0f} s"
                 + (f", {got.failures} wrong" if got.failures else ""))

    def workload(self, seconds: float | None = None) -> None:
        """What runs while the C64 counts: ``dwell`` seconds, with the host's jobs beside it if asked."""
        seconds = self.dwell if seconds is None else seconds
        if self.jobs:
            self._jobs = host_jobs(self.board.link, seconds, self.rng)
        else:
            self._jobs = Jobs()
            time.sleep(seconds)

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
            jobs_landed = landed(self.board.link, self._jobs)
        got = self.delta(before, after, reread(self, setting))
        if got.hung or jobs_landed:
            return got
        return Sample(got.failures + 1, got.samples + 1, "; ".join(
            n for n in (got.note, "the last host job's bytes are not in the C64's memory") if n))

    def delta(self, before: tuple, after: tuple, again: Callable[[], tuple] | None = None) -> Sample:
        """The reads between two readings, each counter modulo 16 bits; a
        pass that stops at a wrong read starts over, so the rounds can stand
        still while the misses count. ``again``: the counters read once
        more, for a slow sample with wrong reads (``still``)."""
        rounds = (after[0] - before[0]) & 0xFFFF
        late = [(a - b) & 0xFFFF for a, b in zip(after[1], before[1])]
        twice = [(a - b) & 0xFFFF for a, b in zip(after[2], before[2])]
        wrong = sum(late) + sum(twice)
        if rounds == 0 and wrong == 0:
            self._stalled = True
            return Sample(1, 1, f"{self.name} stopped: the C64 hung", hung=True)
        reads = rounds * READS_PER_ROUND + wrong
        notes = []
        if wrong:
            worst = max(range(len(REGISTERS)), key=lambda k: late[k] + twice[k])
            notes.append(f"{wrong} of {reads} REU register reads wrong ({sum(late)} right on a second read), "
                         f"most at {REGISTERS[worst]}")
        if self._jobs.sent or self._jobs.refused:
            notes.append(f"{self._jobs.sent} host jobs")
        failures = wrong + self._jobs.refused
        if self._jobs.refused:
            notes.append(f"{self._jobs.refused} refused ({self._jobs.last_refusal})")
        why = still(again, after, stalled(self.name, reads, after[-1] - before[-1], self.rate, "reads"), failures)
        if why:
            self._stalled = True
        got = Sample(failures, reads + self._jobs.refused, "; ".join(notes) if wrong or self._jobs.refused else None)
        return with_stall(got, why)

    def teardown(self) -> None:
        self.board.reset()
        self.board.wait_for_menu()

    def info(self) -> dict:
        return {"held": {r: f"${v:02X}" for r, v in zip(REGISTERS, self.held or b"")}, "jobs": self.jobs}


MENU_ORIGIN = 0xC000                # menuread.a's PAYLOADS origin
MENU_ENTRY = MENU_ORIGIN + 0x50     # what $0314 points at
MENU_READY = MENU_ORIGIN + 0x09
IRQ_VECTOR = 0x0314
MENU_START_S = 5.0
MAX_COPY = 62                       # menuread.a's MAXLEN less one: a copy that long found no 0


class MenuReadProbe:
    """The TeensyROM menu's IO1 handler, read by ``menuread.a`` in the menu's place."""

    name = "regread-menu"
    only = frozenset({"p"})         # the knobs it may sweep, with nothing else set

    def __init__(self, board: Board, payload: Path, *, dwell: float = 1.0, log=print):
        self.board = board
        self.payload = Path(payload)
        self.dwell = dwell
        self.log = log
        self.length = 0
        self.rate: float | None = None    # passes a second at the defaults
        self._stalled = False

    def counters(self) -> tuple[int, int, int, float]:
        """(passes, wrong presence reads, wrong string bytes, when they were read)."""
        b = self.board.link.read_memory(MENU_ORIGIN, 8)
        return b[0] | b[1] << 8, b[2] | b[3] << 8 | b[4] << 16, b[5] | b[6] << 8 | b[7] << 16, time.monotonic()

    def setup(self) -> None:
        if not self.board.dma_allowed():
            raise SweepError("the regread-menu probe goes in by DMA: it needs a TR+ running its main image")
        self.board.ensure_menu()
        link = self.board.link
        link.write_memory(MENU_ORIGIN, self.payload.read_bytes())
        for _ in range(2):                  # once more if the menu had its interrupt off for a while
            link.write_memory(IRQ_VECTOR, MENU_ENTRY.to_bytes(2, "little"))
            deadline = time.monotonic() + MENU_START_S / 2
            while time.monotonic() < deadline and link.read_memory(MENU_READY, 1)[0] != READY_VALUE:
                time.sleep(0.25)
            if link.read_memory(MENU_READY, 1)[0] == READY_VALUE:
                break
        else:
            raise SweepError("regread-menu: the menu's interrupt never entered menuread.a")
        self.length = link.read_memory(MENU_ORIGIN + 8, 1)[0]
        if not 0 < self.length <= MAX_COPY:
            raise SweepError(f"regread-menu: the version string read back {self.length} bytes long: is the menu's "
                             "handler serving IO1?")
        self.rate = None
        before = self.counters()
        time.sleep(1.0)
        after = self.counters()
        got = self.delta(before, after)
        if got.hung or got.failures * 2 > got.samples:
            raise SweepError(f"regread-menu: {got.note} at the defaults: is the menu's handler serving IO1?")
        self._stalled = False
        self.rate = ((after[0] - before[0]) & 0xFFFF) / max(after[-1] - before[-1], 1e-6)
        self.log(f"[sweep] regread-menu at the defaults: a {self.length}-byte version string, {got.samples} IO1 "
                 "reads in 1 s" + (f", {got.failures} wrong" if got.failures else ""))

    def workload(self) -> None:
        time.sleep(self.dwell)

    def sample(self, label: str, setting: Setting | None = None) -> Sample:
        if self._stalled:
            self.log(f"[sweep] regread-menu stopped at the last value; resetting and going in again for {label}")
            with at_defaults(setting, always=True):
                relaunch_reset(self.board, self.name, self.log)
                self.setup()
        with at_defaults(setting):
            before = self.counters()
        self.workload()
        with at_defaults(setting):
            after = self.counters()
        return self.delta(before, after, reread(self, setting))

    def delta(self, before: tuple, after: tuple, again: Callable[[], tuple] | None = None) -> Sample:
        passes = (after[0] - before[0]) & 0xFFFF
        io, string = (after[1] - before[1]) & 0xFFFFFF, (after[2] - before[2]) & 0xFFFFFF
        if passes == 0:
            self._stalled = True
            return Sample(1, 1, "regread-menu stopped: the C64 hung", hung=True)
        io_reads, string_reads = 2 * passes, (self.length + 1) * passes
        why = still(again, after, stalled(self.name, passes, after[-1] - before[-1], self.rate, "passes"), io + string)
        if why:
            self._stalled = True
        return with_stall(Sample(io + string, io_reads + string_reads, f"registers {io}/{io_reads}, version string "
                                 f"{string}/{string_reads}" if io or string else None), why)

    def teardown(self) -> None:
        self.board.reset()
        self.board.wait_for_menu()

    def info(self) -> dict:
        return {"version_bytes": self.length}
