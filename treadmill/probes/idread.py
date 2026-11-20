"""idread: the C64's own count of wrong reads from TeensyROM-served bytes."""

from __future__ import annotations

import time
from pathlib import Path

from treadmill.board import Board
from treadmill.sweeps import Sample, Setting, SweepError, at_defaults

IDREAD_RESULTS = 0xC000           # idread.a's counters: passes (16-bit), wrong ROM reads, wrong IO2 reads (24-bit)
IDREAD_ROM_READS_PER_PASS = 512   # two ROM pages, 256 rounds
IDREAD_IO_READS_PER_PASS = 256    # the IO2 page, 256 rounds
IDREAD_START_S = 6.0


class IdReadProbe:
    """idread.a, a one-bank EasyFlash cartridge, reads its own ROM and the
    EasyFlash handler's IO2 page on the C64 and counts wrong answers per
    path; a sample is the counts added over ``dwell`` seconds, read by DMA,
    with both paths' shares in its note."""

    name = "idread"

    def __init__(self, board: Board, program: Path, *, dwell: float = 1.0, drive: str = "sd",
                 remote: str = "/treadmill/idread.crt", log=print):
        self.board = board
        self.program = Path(program)
        self.dwell = dwell
        self.drive = drive
        self.remote = remote
        self.log = log
        self._stalled = False

    def setup(self) -> None:
        if not self.board.dma_allowed():
            raise SweepError("the idread probe reads its counters by DMA: it needs a TR+ running its main image")
        self.board.ensure_menu()
        self.board.link.push_file(self.program.read_bytes(), self.remote, self.drive)
        self.board.link.launch(self.remote, self.drive)
        # The first change can be the start-up zeroing the counters over what RAM held: a
        # cartridge that dies right after that passed this check on the bench. Two changes
        # mean the loop runs.
        deadline = time.monotonic() + IDREAD_START_S
        seen, moves = self.counters()[0], 0
        while time.monotonic() < deadline:
            time.sleep(0.5)
            passes = self.counters()[0]
            if passes != seen:
                seen, moves = passes, moves + 1
                if moves >= 2:
                    self._stalled = False
                    return
        raise SweepError("idread is not running on the C64: its pass counter does not move")

    def counters(self) -> tuple[int, int, int]:
        """(passes, wrong ROM reads, wrong IO2 reads)."""
        b = self.board.link.read_memory(IDREAD_RESULTS, 8)
        return b[0] | b[1] << 8, b[2] | b[3] << 8 | b[4] << 16, b[5] | b[6] << 8 | b[7] << 16

    def sample(self, label: str, setting: Setting | None = None) -> Sample:
        if self._stalled:
            self.log(f"[sweep] idread stopped at the last value; relaunching for {label}")
            with at_defaults(setting):
                self.setup()
        with at_defaults(setting):
            before = self.counters()
        time.sleep(self.dwell)
        with at_defaults(setting):
            after = self.counters()
        passes = (after[0] - before[0]) & 0xFFFF
        rom = (after[1] - before[1]) & 0xFFFFFF
        io = (after[2] - before[2]) & 0xFFFFFF
        if passes == 0:
            self._stalled = True
            return Sample(1, 1, "idread stopped: the C64 hung", hung=True)
        rom_reads, io_reads = passes * IDREAD_ROM_READS_PER_PASS, passes * IDREAD_IO_READS_PER_PASS
        return Sample(rom + io, rom_reads + io_reads, f"rom {rom}/{rom_reads}, io2 {io}/{io_reads}")

    def teardown(self) -> None:
        self.board.ensure_menu()
