"""vicrom: the VIC's own reads from the cartridge, judged by the picture.

``vicrom.a`` is an 8K Ultimax cartridge whose charset the VIC fetches from
ROMH, so every glyph on screen comes through the TeensyROM's VIC cycles
(``EmulateVicCycles``, timed by ``nS_VICStart`` and ``nS_VICDHold``). At the
defaults the probe takes a reference picture of the text area, the median
of several frames, and measures how far plain frames stray from it (the
capture's own noise); a sample is the worst frame over ``dwell`` seconds,
counted as the pixels that differ from the reference by more than ``LEVEL``,
and fails past ``FACTOR`` times that baseline and ``FLOOR`` above it, as the
noise probe judges. The C64 counts frames at $0F00 and steps the border once
a frame: the probe reads the count by DMA on a TR+, else watches the border,
and a C64 that stopped is a hang.
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from treadmill.board import Board
from treadmill.observe.calibrate import Calibration
from treadmill.observe.capture import FrameSource
from treadmill.probes.noise import FACTOR, FLOOR, LEVEL
from treadmill.sweeps import Sample, Setting, SweepError

PASSES, READY, READY_VALUE = 0x0F00, 0x0F04, 0x5A
START_S = 15.0
REFERENCE_FRAMES = 15
FRAME_WAIT_S = 2.0
BORDER_PX = 6                     # how far outside the text area the border strip is read


class VicRomProbe:
    name = "vicrom"

    def __init__(self, board: Board, video: FrameSource | None, calibration: Calibration | None, program: Path,
                 *, dwell: float = 2.0, drive: str = "sd", remote: str = "/treadmill/vicrom.crt", log=print):
        self.board = board
        self.video = video
        self.calibration = calibration
        self.program = Path(program)
        self.dwell = dwell
        self.drive = drive
        self.remote = remote
        self.log = log
        self.reference: np.ndarray | None = None
        self.baseline: int | None = None
        self._stalled = False

    # -- the picture ----------------------------------------------------------------

    def _text(self, frame) -> np.ndarray:
        """The text area less half a cell all round: the border steps every
        frame, and its blur into the edge cells would count as wrong glyphs."""
        x, y, w, h = self.calibration.text_area
        dx, dy = self.calibration.cell_w / 2, self.calibration.cell_h / 2
        crop = frame.image[int(y + dy):int(y + h - dy), int(x + dx):int(x + w - dx)]
        return cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    def _border(self, frame) -> float:
        x, y, _, _ = self.calibration.text_area
        strip = frame.image[max(0, int(y) - BORDER_PX):int(y), int(x):int(x) + 200]
        return float(strip.mean()) if strip.size else 0.0

    def _frames(self, seconds: float) -> list:
        frames, last, end = [], None, time.monotonic() + seconds
        while time.monotonic() < end:
            last = self.video.next_frame(after=last, timeout=FRAME_WAIT_S)
            frames.append(last)
        return frames

    def _worst(self, frames) -> int:
        return max((int(np.count_nonzero(cv2.absdiff(self._text(f), self.reference) > LEVEL)) for f in frames),
                   default=0)

    def limit(self) -> int:
        base = self.baseline or 0
        return max(int(FACTOR * base), base + FLOOR)

    # -- the C64 ----------------------------------------------------------------------

    def _passes(self) -> int:
        b = self.board.link.read_memory(PASSES, 2)
        return b[0] | b[1] << 8

    def _launch(self) -> None:
        self.board.ensure_menu()
        link = self.board.link
        link.push_file(self.program.read_bytes(), self.remote, self.drive)
        if self.board.dma_allowed():
            link.write_memory(READY, b"\0")
        link.launch(self.remote, self.drive)
        deadline = time.monotonic() + START_S
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if not self.board.dma_allowed() or link.read_memory(READY, 1)[0] == READY_VALUE:
                break
        else:
            raise SweepError("vicrom never reported ready")
        time.sleep(0.5)                  # a frame or two drawn with the glyphs before the reference

    def setup(self) -> None:
        if self.video is None or self.calibration is None:
            raise SweepError("the vicrom probe reads the picture: it needs the station's video and its calibration")
        self._launch()
        frames = [self._text(f) for f in self._frames(REFERENCE_FRAMES / 30)][:REFERENCE_FRAMES]
        if not frames:
            raise SweepError("vicrom: no frames to take a reference from")
        self.reference = np.median(np.stack(frames), axis=0).astype(np.uint8)
        self.baseline = self._worst(self._frames(self.dwell))
        self._stalled = False
        self.log(f"[sweep] vicrom reference at the defaults; baseline {self.baseline} pixels (limit {self.limit()})")

    def sample(self, label: str, setting: Setting | None = None) -> Sample:
        if self._stalled:
            self.log(f"[sweep] vicrom stopped at the last value; relaunching for {label}")
            self.board.reset()
            self.board.wait_for_menu()
            self._launch()
        dma = self.board.dma_allowed()
        before = self._passes() if dma else None
        frames = self._frames(self.dwell)
        if dma:
            alive = self._passes() != before
        else:
            alive = len({round(self._border(f)) for f in frames}) > 1
        if not alive:
            self._stalled = True
            return Sample(1, 1, "vicrom stopped: the C64 hung", hung=True)
        worst = self._worst(frames)
        if worst > self.limit():
            return Sample(1, 1, f"glyphs {worst} pixels off the reference, over the limit {self.limit()}")
        return Sample(0, 1)

    def teardown(self) -> None:
        self.board.reset()
        self.board.wait_for_menu()

    def info(self) -> dict:
        return {"baseline": self.baseline, "limit": self.limit(), "level": LEVEL, "factor": FACTOR, "floor": FLOOR}
