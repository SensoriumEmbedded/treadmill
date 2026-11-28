"""krread: Kernal Replace's HIRAM sense, counted on the C64.

A Kernal Replace launch serves a KERNAL image of its own in place of the
machine's whenever HIRAM is set, and the TeensyROM cannot see the CPU port:
after every write to $00/$01 it senses HIRAM again, by driving A14 low and
watching ROMH on the next KERNAL read (``Cyc_KernProp``, "Skoe", a C64's
method) or by stealing the cycle after the write and reading ROMH as bus
master (``nS_KSenseSample``, the DMA method a C128 needs). A wrong sense either
leaves the machine's own KERNAL showing where the replacement belongs, or
drives the replacement over RAM.

The probe KERNAL is the machine's own, read by DMA, with its longest run of
one filler byte inverted (``probe_kernal``), so the C64 can tell the two
apart: ``krread.a`` toggles HIRAM and reads the run each way, counting reads
without the inverted bytes with HIRAM set and reads without its own RAM
pattern with HIRAM clear. Its counters are read off the top row by video,
never by DMA: a host DMA access while a program runs under Kernal Replace
leaves the DMA sense on its last verdict (``KDS_Begin`` defers to another DMA
user) and corrupts the program's own RAM under either sense (both seen on the
Kawari C64 002b, BACKLOG 40), so a sweep that read them by DMA measured its
own reads. The first read after
each write to $01 is counted apart (``first_reads``): under Skoe it is the
sense's own cycle, while the rest show whether the verdict was right, which is
what a point is judged on. The image is chosen as the Kernal file and Special
IO set to Kernal Replace by the caller (``Settings.kernal_file``,
``Settings.special_io``), and ``KSenseMode`` set (``ksense_mode``), since
``td`` leaves it alone. A sample whose passes a second fall under
``STALL_SHARE`` of those counted at the defaults in the setup stopped
partway, and fails as a collapse.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from treadmill.board import Board
from treadmill.link import DebugCommands
from treadmill.observe.channels import CONFIDENCE_FLOOR, ScreenChannel
from treadmill.probes.idread import SHOW_DIGITS
from treadmill.sweeps import Sample, Setting, SweepError, at_defaults, relaunch_reset, stalled, with_stall

KERNAL, KERNAL_SIZE = 0xE000, 0x2000
READY, READY_VALUE = 0xC104, 0x5A
COUNTERS = 0xC110                 # passes; the rest wrong with HIRAM set, with it clear; the first reads (16-bit)
SHOW_FIELDS = tuple((5 * i, 4) for i in range(5))   # the same five on the top row in hex: (cell, width)
SHOW_READS = 8                    # frames to find two in a row that agree on the miss counts
MIN_RUN, MAX_RUN = 16, 64         # the marker run: long enough to count, short enough to read every pass
RAM_PATTERN = 0x0F
START_S = 15.0
CHECK_S = 1.0
SKOE, DMA, AUTO = 0, 2, 9         # KSenseMode


def probe_kernal(kernal: bytes) -> tuple[bytes, dict[str, int]]:
    """The image to serve and the defines krread.a needs: the longest run of
    one byte value in the machine's KERNAL (its filler: $E4B7-$E4D2 is 28 of
    $AA in the stock one), at most ``MAX_RUN`` long, inverted."""
    if len(kernal) != KERNAL_SIZE:
        raise SweepError(f"a KERNAL is {KERNAL_SIZE} bytes, not {len(kernal)}")
    best_start, best_len, i = 0, 0, 0
    while i < len(kernal) - 6:                       # not into the vectors
        j = i
        while j < len(kernal) - 6 and kernal[j] == kernal[i]:
            j += 1
        if j - i > best_len:
            best_start, best_len = i, j - i
        i = j
    if best_len < MIN_RUN:
        raise SweepError(f"the KERNAL has no run of one byte {MIN_RUN} long to mark (the longest is {best_len})")
    length, fill = min(best_len, MAX_RUN), kernal[best_start]
    pattern = RAM_PATTERN if RAM_PATTERN not in (fill, fill ^ 0xFF) else 0x3C
    image = bytearray(kernal)
    for k in range(best_start, best_start + length):
        image[k] ^= 0xFF
    return bytes(image), {"MARK": KERNAL + best_start, "LEN": length, "FILL": fill, "PAT": pattern}


@contextmanager
def ksense_mode(dbg: DebugCommands, mode: int) -> Iterator[int]:
    """``KSenseMode`` (``tx#``) for the block, the mode before it put back
    after: ``td`` does not restore it."""
    from treadmill.knobs import parse_listing
    was = parse_listing(dbg.listing()).get("x")
    if was is None:
        raise SweepError("the timing listing has no KSenseMode: Kernal Replace's knobs need a TR+ build")
    dbg.set_knob("x", mode)
    try:
        yield was
    finally:
        dbg.set_knob("x", was)


class KrReadProbe:
    name = "krread"

    def __init__(self, board: Board, screen: ScreenChannel, program: Path, length: int, *, dwell: float = 2.0,
                 drive: str = "sd", remote: str = "/treadmill/krread.prg", log=print):
        self.board = board
        self.screen = screen
        self.program = Path(program)
        self.length = length
        self.dwell = dwell
        self.drive = drive
        self.remote = remote
        self.log = log
        self.rate: float | None = None    # passes a second at the defaults
        self._stalled = False
        # per value under test ("k=35"): first reads missed with HIRAM set, with it clear, and passes
        self.first_reads: dict[str, list[int]] = {}

    def _shown(self) -> tuple[int, ...] | None:
        """The five counters off one frame's top row; None when a digit does
        not read with confidence (a frame caught mid-redraw, or no counters)."""
        read = self.screen.read_video()
        values = []
        for start, width in SHOW_FIELDS:
            cells = range(start, start + width)
            if any(int(read.codes[i]) not in SHOW_DIGITS or read.confidence[i] < CONFIDENCE_FLOOR for i in cells):
                return None
            values.append(int("".join("%x" % SHOW_DIGITS[int(read.codes[i])] for i in cells), 16))
        return tuple(values)

    def counters(self) -> tuple[int, ...]:
        """Passes and the four miss counts, from the second of two frames in
        a row that agree on the miss counts: they hold still while the sense
        is right, and one misread digit would otherwise count as wrong reads
        the C64 never made. Miss counts that keep moving (reads going wrong as
        it runs) give the last frame that read whole."""
        last = None
        for _ in range(SHOW_READS):
            values = self._shown()
            if values is not None:
                if last is not None and values[1:] == last[1:]:
                    return values
                last = values
        if last is None:
            raise SweepError("krread's counters on the top row do not read whole")
        return last

    def setup(self) -> None:
        if not self.screen.has_video:
            raise SweepError("the krread probe reads its counters off the screen: the station needs a capture, "
                             "its calibration and a glyph bank")
        self.board.ensure_menu()
        link = self.board.link
        link.push_file(self.program.read_bytes(), self.remote, self.drive)
        link.launch(self.remote, self.drive)
        # Two moves of the pass count, as idread's: a top row that merely parses is not a loop.
        deadline = time.monotonic() + START_S
        seen, moves = None, 0
        while time.monotonic() < deadline:
            try:
                passes = self.counters()[0]
            except SweepError:
                passes = None
            if passes is not None and seen is not None and passes != seen:
                moves += 1
                if moves >= 2:
                    self._stalled = False
                    self.rate = self._rate()
                    return
            seen = passes if passes is not None else seen
            time.sleep(CHECK_S / 4)
        shown = [r.strip() for r in self.screen.read_video().rows() if r.strip()][:4]
        raise SweepError(f"krread is not counting on the C64's top row; the screen shows {shown}")

    def _rate(self) -> float | None:
        """Passes a second over CHECK_S, each count taken when its read came back."""
        try:
            before, t0 = self.counters(), time.monotonic()
            time.sleep(CHECK_S)
            after, t1 = self.counters(), time.monotonic()
        except SweepError:
            return None
        return ((after[0] - before[0]) & 0xFFFF) / max(t1 - t0, 1e-6)

    def sample(self, label: str, setting: Setting | None = None) -> Sample:
        if self._stalled:
            self.log(f"[sweep] krread stopped at the last value; resetting and relaunching for {label}")
            with at_defaults(setting, always=True):
                relaunch_reset(self.board, "krread", self.log)
                self.setup()
        try:
            before, t0 = self.counters(), time.monotonic()
            time.sleep(self.dwell)
            after, t1 = self.counters(), time.monotonic()
        except SweepError as e:
            self._stalled = True
            return Sample(1, 1, f"krread stopped: {e}", hung=True)
        passes, rom, ram, rom_first, ram_first = ((a - b) & 0xFFFF for a, b in zip(after, before))
        if not passes:
            self._stalled = True
            return Sample(1, 1, "krread stopped: the C64 hung", hung=True)
        key = ",".join(f"{k}={v}" for k, v in sorted(setting.values.items())) if setting else label
        seen = self.first_reads.setdefault(key, [0, 0, 0])
        for i, n in enumerate((rom_first, ram_first, passes)):
            seen[i] += n
        reads = passes * (self.length - 1)
        notes = []
        if rom or ram:
            notes.append(f"HIRAM set: {rom}/{reads} without the replacement; clear: {ram}/{reads} with it")
        if rom_first or ram_first:
            notes.append(f"the first read after the write: {rom_first}/{passes} without the replacement, "
                         f"{ram_first}/{passes} with it over RAM")
        why = stalled("krread", passes, t1 - t0, self.rate, "passes")
        if why:
            self._stalled = True
        return with_stall(Sample(rom + ram, 2 * reads, "; ".join(notes) or None), why)

    def info(self) -> dict:
        return {"first_reads": self.first_reads}

    def teardown(self) -> None:
        self.board.reset()
        self.board.wait_for_menu()
