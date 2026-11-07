"""One screen read, whichever channel produced it, and the policy that
picks the channel (section 2.2 of the plan).

Video is the default: it does not touch the machine. DMA is used when a
caller asks for an exact read and the board allows it (a TR+ main image,
outside the C128's post-reset hold-off, with no REU live). With both
available, a cross-check records how far video is from DMA: that number
is the acceptance gate for the video channel and a running calibration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from treadmill.c64.screen import (
    CHARSET_CUSTOM, REVERSE_VIDEO, SCREEN_BYTES, SCREEN_COLUMNS, SCREEN_ROWS, code_as_text,
)
from treadmill.link.trlink import Link, ScreenDump
from treadmill.observe.calibrate import Calibration, track
from treadmill.observe.capture import Frame, FrameSource
from treadmill.observe.glyphs import GlyphBank, screen_match, spill
from treadmill.observe.normalize import (
    cell_colors, color_distance, core, core_cells, distance_map, interior_blank,
)

SOURCE_VIDEO = "video"
SOURCE_DMA = "dma"
UNSURE = "?"                     # a cell below the confidence threshold reads as this
CONFIDENCE_FLOOR = 0.25
XCHECK_LIMIT = 0.01              # fraction of cells video may disagree with DMA on
DEFAULT_TIMEOUT = 15.0
POLL_S = 0.1
PASSES = 3                       # decode, predict the neighbors' spill, decode again, once more with better levels
RESIDUAL_ABS = 2.4               # x noise floor: what a blank cell's margin may keep after spill is subtracted
RESIDUAL_REL = 0.6               # x predicted spill: a cell's own ink is never a small fraction of the prediction


class ScreenError(Exception):
    pass


class ScreenTimeout(ScreenError):
    pass


@dataclass
class ScreenRead:
    codes: np.ndarray                   # (1000,) uint8, reverse bit included
    charset: str
    confidence: np.ndarray              # (1000,) float32
    source: str
    at: float
    frame: Frame | None = None
    colors: np.ndarray | None = None    # (1000, 3) BGR, video only
    address: int | None = None          # DMA only
    charset_scores: dict = field(default_factory=dict)
    calibration: Calibration | None = None   # video only: the (tracked) geometry used

    @property
    def blank(self) -> np.ndarray:
        return self.codes == 0x20

    def rows(self, floor: float = CONFIDENCE_FLOOR) -> list[str]:
        out = []
        for r in range(SCREEN_ROWS):
            cells = []
            for c in range(SCREEN_COLUMNS):
                i = r * SCREEN_COLUMNS + c
                cells.append(UNSURE if self.confidence[i] < floor else code_as_text(int(self.codes[i]), self.charset))
            out.append("".join(cells))
        return out

    def text(self, floor: float = CONFIDENCE_FLOOR) -> str:
        return "\n".join(self.rows(floor))

    def row(self, r: int, floor: float = CONFIDENCE_FLOOR) -> str:
        return self.rows(floor)[r]

    def reverse(self) -> np.ndarray:
        return (self.codes & REVERSE_VIDEO) != 0

    def mean_confidence(self) -> float:
        nonblank = self.confidence[~self.blank]
        return float(nonblank.mean()) if nonblank.size else 1.0

    def summary(self) -> dict:
        return {"source": self.source, "charset": self.charset, "confidence": round(self.mean_confidence(), 3),
                "unsure": int((self.confidence < CONFIDENCE_FLOOR).sum())}


def from_dump(dump: ScreenDump, at: float | None = None) -> ScreenRead:
    codes = np.frombuffer(bytes(dump.codes), dtype=np.uint8)
    if codes.size != SCREEN_BYTES:
        raise ScreenError(f"a DMA screen read returned {codes.size} bytes, not {SCREEN_BYTES}")
    return ScreenRead(codes.copy(), dump.charset, np.ones(SCREEN_BYTES, np.float32), SOURCE_DMA,
                      at if at is not None else time.monotonic(), address=dump.address)


def decode_cells(cells: np.ndarray, bank: GlyphBank, noise_floor: float, edge_level: float,
                 charset: str | None = None, passes: int = PASSES):
    """Match the cell cores, predict what the neighbors spilled into each
    from that reading, subtract it and match again. After subtraction a
    cell is blank when its interior is at the noise floor and what is left
    in its margins is a small fraction of the spill predicted there: the
    model is not exact, a cell's own ink is never that small."""
    m = screen_match(bank, cells, charset, noise_floor=noise_floor,
                     blank=interior_blank(cells, noise_floor))
    corrected = cells
    for _ in range(passes - 1):
        predicted = spill(bank, m.codes, m.amplitude, m.charset, edge_level)
        corrected = np.maximum(cells - predicted, 0.0)
        residual = corrected.reshape(corrected.shape[0], -1).max(axis=1)
        allowed = np.maximum(RESIDUAL_ABS * noise_floor,
                             RESIDUAL_REL * predicted.reshape(predicted.shape[0], -1).max(axis=1))
        blank = interior_blank(corrected, noise_floor) & (residual < allowed)
        m = screen_match(bank, corrected, charset, noise_floor=noise_floor, blank=blank)
    return m, corrected


def read_video(frame: Frame, calib: Calibration, bank: GlyphBank, charset: str | None = None,
               follow: bool = True) -> ScreenRead:
    """Decode one frame: normalize (following small drift of the picture
    unless ``follow`` is off), foreground map, cells, spill-corrected glyphs."""
    if not calib.fits(frame.image):
        raise ScreenError(f"frame is {frame.size[0]}x{frame.size[1]} but the calibration is for "
                          f"{calib.frame_w}x{calib.frame_h}")
    if follow:
        calib = track(frame.image, calib)
    norm = calib.normalize(frame.image)
    dmap = distance_map(norm, calib.background)
    cells = core_cells(dmap)
    want = None if charset in (None, CHARSET_CUSTOM) else charset
    m, corrected = decode_cells(cells, bank, calib.noise_floor,
                                color_distance(calib.border, calib.background), want)
    return ScreenRead(m.codes, m.charset, m.confidence, SOURCE_VIDEO, frame.at, frame,
                      cell_colors(core(norm), corrected), charset_scores=m.charset_scores, calibration=calib)


@dataclass(frozen=True)
class XCheck:
    disagreement: float                 # fraction of cells
    cells: tuple[tuple[int, int, int, int], ...]   # (row, col, video code, dma code)
    charset_agrees: bool

    @property
    def ok(self) -> bool:
        return self.disagreement <= XCHECK_LIMIT and self.charset_agrees


def xcheck(video: ScreenRead, dma: ScreenRead) -> XCheck:
    diff = np.flatnonzero(video.codes != dma.codes)
    cells = tuple((int(i // SCREEN_COLUMNS), int(i % SCREEN_COLUMNS), int(video.codes[i]), int(dma.codes[i]))
                  for i in diff)
    return XCheck(len(diff) / SCREEN_BYTES, cells, video.charset == dma.charset)


class ScreenChannel:
    """Reads and waits on the screen through the channel the policy picks."""

    def __init__(self, link: Link | None, source: FrameSource | None, calib: Calibration | None,
                 bank: GlyphBank | None, *, dma_allowed: Callable[[], bool] = lambda: False,
                 agree: int = 2, charset_hint: Callable[[], str | None] = lambda: None,
                 on_xcheck: Callable[[XCheck], None] | None = None):
        self.link = link
        self.source = source
        self.calib = calib
        self.bank = bank
        self.dma_allowed = dma_allowed
        self.agree = agree
        self.charset_hint = charset_hint
        self.on_xcheck = on_xcheck
        self.last_xcheck: XCheck | None = None

    @property
    def has_video(self) -> bool:
        return self.source is not None and self.calib is not None and self.bank is not None

    def read_dma(self) -> ScreenRead:
        if self.link is None:
            raise ScreenError("no link for a DMA screen read")
        if not self.dma_allowed():
            raise ScreenError("DMA screen reads are not allowed now (plain TR, MinimalBoot, post-reset hold-off or REU live)")
        return from_dump(self.link.read_screen())

    def read_video(self, frame: Frame | None = None) -> ScreenRead:
        if not self.has_video:
            raise ScreenError("no video channel: capture, calibration or glyph bank missing")
        frame = frame or self.source.next_frame()
        return read_video(frame, self.calib, self.bank, self.charset_hint())

    def read(self, exact: bool = False, xcheck_now: bool = False) -> ScreenRead:
        """Video unless ``exact`` and DMA is allowed; DMA when there is no
        video; a cross-check of the two when asked and both are available."""
        can_dma = self.link is not None and self.dma_allowed()
        if exact and can_dma:
            result = self.read_dma()
            if xcheck_now and self.has_video:
                self._xcheck(self.read_video(), result)
            return result
        if self.has_video:
            result = self.read_video()
            if xcheck_now and can_dma:
                self._xcheck(result, self.read_dma())
            return result
        if can_dma:
            return self.read_dma()
        raise ScreenError("no screen channel: neither video nor DMA is available")

    def _xcheck(self, video: ScreenRead, dma: ScreenRead) -> XCheck:
        x = xcheck(video, dma)
        self.last_xcheck = x
        if self.on_xcheck:
            self.on_xcheck(x)
        return x

    def wait(self, predicate: Callable[[ScreenRead], bool], timeout: float = DEFAULT_TIMEOUT,
             exact: bool = False, what: str = "the screen") -> ScreenRead:
        """Poll until ``predicate`` holds on ``agree`` consecutive video reads
        (one DMA read suffices). Raises ScreenTimeout with the last read."""
        deadline = time.monotonic() + timeout
        run = 0
        last: ScreenRead | None = None
        while True:
            last = self.read(exact)
            if predicate(last):
                run += 1
                if last.source == SOURCE_DMA or run >= self.agree:
                    return last
            else:
                run = 0
            if time.monotonic() >= deadline:
                raise ScreenTimeout(f"{what} did not appear within {timeout:.1f} s; last read "
                                    f"({last.source}, {last.mean_confidence():.2f}):\n{last.text()}")
            time.sleep(POLL_S)

    def wait_text(self, text: str, row: int | None = None, timeout: float = DEFAULT_TIMEOUT,
                  exact: bool = False) -> ScreenRead:
        """``text`` somewhere on the screen, or on one row."""
        def present(s: ScreenRead) -> bool:
            rows = s.rows()
            return text in (rows[row] if row is not None else "\n".join(rows))
        return self.wait(present, timeout, exact, what=f"{text!r}" + (f" on row {row}" if row is not None else ""))

    def wait_settled(self, timeout: float = DEFAULT_TIMEOUT, exact: bool = False) -> ScreenRead:
        """The first read that the next ``agree`` reads repeat exactly."""
        deadline = time.monotonic() + timeout
        prev: ScreenRead | None = None
        run = 0
        while True:
            cur = self.read(exact)
            if prev is not None and np.array_equal(prev.codes, cur.codes) and prev.charset == cur.charset:
                run += 1
                if run >= self.agree:
                    return cur
            else:
                run = 0
            prev = cur
            if time.monotonic() >= deadline:
                raise ScreenTimeout(f"the screen kept changing for {timeout:.1f} s")
            time.sleep(POLL_S)
