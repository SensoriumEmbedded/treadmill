"""menu: reset, boot, and compare the menu with its golden."""

from __future__ import annotations

from pathlib import Path

from treadmill.board import Board
from treadmill.link import LinkError, TransportError
from treadmill.observe.channels import ScreenChannel, ScreenError
from treadmill.observe.golden import Golden, compare, save_golden
from treadmill.sweeps import Sample, Setting, SweepError

MENU_BOOT_S = 20.0                # a knob that breaks the menu program never boots: do not wait the station's minute


class MenuProbe:
    """Reset, let the menu boot, and compare its listing with the root
    golden: the item-type marks are what the NTSC misread broke, and the
    read works by video on a plain TR. A menu that does not come back is a
    failure too. It resets the C64, so it is not for a knob that times DMA
    (a C128 can hang on DMA right after a reset). The menu acts on what it
    reads over IO1, so reads past an edge can leave the TeensyROM's own
    state bad until the Teensy restarts (BACKLOG 53): a sweep checks the
    default before each point that follows a failure (``leaves_state``)."""

    name = "menu"
    resets = True
    leaves_state = True

    def __init__(self, board: Board, screen: ScreenChannel, golden: Golden, artifacts: Path | None = None,
                 *, boot_timeout: float = MENU_BOOT_S, settle_timeout: float = 10.0):
        self.board = board
        self.screen = screen
        self.golden = golden
        self.artifacts = artifacts
        self.boot_timeout = boot_timeout
        self.settle_timeout = settle_timeout

    def setup(self) -> None:
        if not self.screen.has_video and not self.board.dma_allowed():
            raise SweepError("the menu probe cannot read the screen: the station has no video channel "
                             "(capture, calibration and glyph bank) and no DMA")

    def sample(self, label: str, setting: Setting | None = None) -> Sample:
        self.board.reset()
        try:
            self.board.wait_for_menu(timeout=self.boot_timeout)
        except (LinkError, TransportError) as e:
            return Sample(1, 1, f"the menu did not come back: {e}", hung=True)
        try:
            read = self.screen.wait_settled(timeout=self.settle_timeout)
        except ScreenError as e:
            return Sample(1, 1, f"the screen did not settle: {e}")
        mismatches = compare(read.rows(), self.golden)
        if mismatches and self.artifacts is not None:
            save_golden(read.rows(), self.artifacts / f"{label}.screen.txt")
        if mismatches:
            return Sample(1, 1, f"{len(mismatches)} cell(s) differ from the golden")
        return Sample(0, 1)

    def teardown(self) -> None:
        self.board.ensure_menu()
