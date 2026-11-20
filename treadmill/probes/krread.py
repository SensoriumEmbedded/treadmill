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
pattern with HIRAM clear. Its counters are read by DMA, at the defaults when
the knob under test times DMA. The image is chosen as the Kernal file and
Special IO set to Kernal Replace by the caller (``Settings.kernal_file``,
``Settings.special_io``), and ``KSenseMode`` set (``ksense_mode``), since
``td`` leaves it alone.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from treadmill.board import Board
from treadmill.link import DebugCommands
from treadmill.sweeps import Sample, Setting, SweepError, at_defaults

KERNAL, KERNAL_SIZE = 0xE000, 0x2000
READY, READY_VALUE = 0xC104, 0x5A
COUNTERS = 0xC110                 # passes, wrong with HIRAM set, wrong with it clear (16-bit each)
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

    def __init__(self, board: Board, program: Path, length: int, *, dwell: float = 2.0, drive: str = "sd",
                 remote: str = "/treadmill/krread.prg", log=print):
        self.board = board
        self.program = Path(program)
        self.length = length
        self.dwell = dwell
        self.drive = drive
        self.remote = remote
        self.log = log
        self._stalled = False

    def counters(self) -> tuple[int, int, int]:
        b = self.board.link.read_memory(COUNTERS, 6)
        return b[0] | b[1] << 8, b[2] | b[3] << 8, b[4] | b[5] << 8

    def setup(self) -> None:
        if not self.board.dma_allowed():
            raise SweepError("the krread probe reads its counters by DMA: it needs a TR+ running its main image")
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
            raise SweepError("krread never reported ready")
        p0, r0, m0 = self.counters()
        time.sleep(CHECK_S)
        p1, r1, m1 = self.counters()
        passes, rom, ram = (p1 - p0) & 0xFFFF, (r1 - r0) & 0xFFFF, (m1 - m0) & 0xFFFF
        if not passes:
            raise SweepError("krread is not looping")
        if rom or ram:
            raise SweepError(f"krread at the defaults: {rom} reads without the probe KERNAL, {ram} with it over "
                             f"RAM: is Special IO Kernal Replace, with the probe KERNAL as its file?")
        self._stalled = False

    def sample(self, label: str, setting: Setting | None = None) -> Sample:
        if self._stalled:
            self.log(f"[sweep] krread stopped at the last value; resetting and relaunching for {label}")
            with at_defaults(setting):
                self.board.reset()
                self.board.wait_for_menu()
                self.setup()
        with at_defaults(setting):
            p0, r0, m0 = self.counters()
        time.sleep(self.dwell)
        with at_defaults(setting):
            p1, r1, m1 = self.counters()
        passes, rom, ram = (p1 - p0) & 0xFFFF, (r1 - r0) & 0xFFFF, (m1 - m0) & 0xFFFF
        if not passes:
            self._stalled = True
            return Sample(1, 1, "krread stopped: the C64 hung", hung=True)
        reads = passes * self.length
        note = f"HIRAM set: {rom}/{reads} without the replacement; clear: {ram}/{reads} with it" if rom or ram else None
        return Sample(rom + ram, 2 * reads, note)

    def teardown(self) -> None:
        self.board.reset()
        self.board.wait_for_menu()
