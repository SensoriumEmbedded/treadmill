"""idread: the C64's own count of wrong reads from TeensyROM-served bytes,
under the main image (read back by DMA) or under MinimalBoot (read off the
screen, since MinimalBoot refuses DMA)."""

from __future__ import annotations

import time
from pathlib import Path

from treadmill.board import Board
from treadmill.link import LinkError, TransportError
from treadmill.observe.channels import CONFIDENCE_FLOOR, ScreenChannel
from treadmill.programs.build import CHIP_SIZE
from treadmill.probes.usbload import usb_load
from treadmill.sweeps import Sample, Setting, SweepError, at_defaults

IDREAD_RESULTS = 0xC000           # idread.a's counters: passes (16-bit), wrong ROM reads, wrong IO2 reads (24-bit)
IDREAD_ROM_READS_PER_PASS = 512   # two ROM pages, 256 rounds
IDREAD_IO_READS_PER_PASS = 256    # the IO2 page, 256 rounds
IDREAD_START_S = 6.0
MINIMAL_BANK = 64                 # a chip this high makes the main image hand the cartridge to MinimalBoot
DROP_S = 20.0                     # the launch, to the main image restarting as MinimalBoot
MINIMAL_S = 20.0                  # MinimalBoot answering once the port is back
# idread.a built with SHOW: its counters on the top row in hex (cells, width), digits as screen codes.
SHOW_FIELDS = ((0, 4), (5, 6), (12, 6))
SHOW_DIGITS = {**{0x30 + i: i for i in range(10)}, **{0x01 + i: 10 + i for i in range(6)}}
SHOW_READS = 5


class IdReadProbe:
    """idread.a, a one-bank EasyFlash cartridge, reads its own ROM and the
    EasyFlash handler's IO2 page on the C64 and counts wrong answers per
    path; a sample is the counts added over ``dwell`` seconds, read by DMA,
    with both paths' shares in its note."""

    name = "idread"

    def __init__(self, board: Board, program: Path, *, dwell: float = 1.0, drive: str = "sd",
                 remote: str = "/treadmill/idread.crt", usb_load: bool = False, log=print):
        self.board = board
        self.program = Path(program)
        self.dwell = dwell
        self.drive = drive
        self.usb_load = usb_load
        if usb_load:
            self.name = f"{self.name}-usbload"
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
        if self.usb_load:
            usb_load(self.board.link, self.dwell)
        else:
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


def with_minimal_chip(crt: bytes) -> bytes:
    """A cartridge image with an erased chip in bank ``MINIMAL_BANK`` added,
    which the cartridge never selects: enough for the main image to hand the
    whole image to MinimalBoot (FileParsers.ino)."""
    return (crt + b"CHIP" + (0x10 + CHIP_SIZE).to_bytes(4, "big") + bytes(2) + MINIMAL_BANK.to_bytes(2, "big")
            + (0x8000).to_bytes(2, "big") + CHIP_SIZE.to_bytes(2, "big") + b"\xff" * CHIP_SIZE)


class MinimalIdReadProbe(IdReadProbe):
    """idread under MinimalBoot, the image large EasyFlash cartridges run
    in. ``program`` is idread built with SHOW; the probe adds a bank-64 chip,
    launches it, follows the board's USB as it restarts in MinimalBoot, and
    reads the counters off the screen by video. It changes the image the
    knobs belong to, so the sweep sets it up before it reads the defaults
    (``changes_image``), and the teardown resets back to the main image."""

    changes_image = True

    def __init__(self, board: Board, screen: ScreenChannel, program: Path, *, dwell: float = 2.0,
                 drive: str = "sd", usb_load: bool = False, log=print):
        super().__init__(board, program, dwell=dwell, drive=drive, remote="/treadmill/idread-mb.crt",
                         usb_load=usb_load, log=log)
        self.screen = screen
        self.name = "idread-minimal" + ("-usbload" if usb_load else "")

    def counters(self) -> tuple[int, int, int]:
        for _ in range(SHOW_READS):
            read = self.screen.read_video()
            values = []
            for start, width in SHOW_FIELDS:
                cells = range(start, start + width)
                if any(int(read.codes[i]) not in SHOW_DIGITS or read.confidence[i] < CONFIDENCE_FLOOR
                       for i in cells):
                    break
                values.append(int("".join("%x" % SHOW_DIGITS[int(read.codes[i])] for i in cells), 16))
            else:
                return values[0], values[1], values[2]
            time.sleep(0.2)
        raise SweepError("idread's counters on the top row do not read whole")

    def setup(self) -> None:
        if not self.screen.has_video:
            raise SweepError("the MinimalBoot idread probe reads its counters by video: the station needs a "
                             "capture, its calibration and a glyph bank")
        self.board.ensure_menu()
        link = self.board.link
        link.push_file(with_minimal_chip(self.program.read_bytes()), self.remote, self.drive)
        link.launch(self.remote, self.drive)
        self.board.reconnect_after_drop(DROP_S)
        deadline = time.monotonic() + MINIMAL_S
        while self.board.link.fw_check() != "minimal":
            if time.monotonic() > deadline:
                raise SweepError(f"MinimalBoot did not answer within {MINIMAL_S:.0f} s of the hand-off")
            time.sleep(0.5)
        deadline = time.monotonic() + IDREAD_START_S
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
                    return
            seen = passes if passes is not None else seen
            time.sleep(0.3)
        raise SweepError("idread is not counting on the C64's top row under MinimalBoot")

    def teardown(self) -> None:
        try:
            self.board.link.reset()      # out of MinimalBoot: the board restarts in its main image
        except (LinkError, TransportError):
            pass                         # the reply can be cut off by the port dropping
        self.board.reconnect_after_drop(DROP_S)
        self.board.wait_for_menu()
