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
from dataclasses import dataclass, field, replace
from typing import Callable

import numpy as np

from treadmill.c64.screen import (
    CHARSET_CUSTOM, REVERSE_VIDEO, SCREEN_BYTES, SCREEN_COLUMNS, SCREEN_ROWS, code_as_text,
)
from treadmill.errors import TreadmillError
from treadmill.link.trlink import Link, ScreenDump
from treadmill.observe.calibrate import Calibration, CalibrationError, refine, track
from treadmill.observe.capture import Frame, FrameSource
from treadmill.observe.glyphs import BLANK_CODE, CELL, GlyphBank, Match, screen_match, spill
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
REFINE_CONFIDENCE = 0.5          # cells at least this sure anchor the grid refinement of a read
REFINE_ROUNDS = 3                # a grid a pixel or two off reads its far cells unsurely, so one round on the
REFINE_SETTLED = (0.25, 0.001)   # sure ones lands short; each further round goes only when the last moved (px, scale)
RESIDUAL_ABS = 2.4               # x noise floor: what a blank cell's margin may keep after spill is subtracted
RESIDUAL_REL = 0.6               # x predicted spill: a cell's own ink is never a small fraction of the prediction
# x the brightest neighbor's mean level (the border counts at the edges): a run of solid cells spills more
# than one harvested block does, a third of its level into the row below on the PAL picture and up to four
# tenths from the border, while a cell's own stroke is at its text level, which is the neighbor's or close.
SPILL_FRACTION = 0.5
# A cell fitted below this many template units (the harvest color's level) and correlating worse than
# FAINT_CORRELATION is spill the model did not predict, not ink: the darkest C64 colors on black (blue,
# brown) fit at about 0.38, while on the PAL Kawari menu unpredicted spill along a cell's top row or left
# column fit edge-line graphics ($63, $74) at 0.04-0.24 with correlations up to 0.78.
FAINT_AMPLITUDE = 0.3
FAINT_CORRELATION = 0.85


class ScreenError(TreadmillError):
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


def neighbor_level(cells: np.ndarray, edge_level: float) -> np.ndarray:
    """(1000,) the mean level of the brightest of each cell's eight neighbors,
    the border standing in beyond the edges: how much the cell's margins may
    hold from spill alone."""
    mean = cells.reshape(SCREEN_BYTES, -1).mean(axis=1).reshape(SCREEN_ROWS, SCREEN_COLUMNS)
    padded = np.full((SCREEN_ROWS + 2, SCREEN_COLUMNS + 2), edge_level, np.float32)
    padded[1:-1, 1:-1] = mean
    best = np.zeros((SCREEN_ROWS, SCREEN_COLUMNS), np.float32)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                np.maximum(best, padded[1 + dy:SCREEN_ROWS + 1 + dy, 1 + dx:SCREEN_COLUMNS + 1 + dx], out=best)
    return best.reshape(SCREEN_BYTES)


def drop_faint(m: Match) -> Match:
    """The match with its faint cells (see FAINT_AMPLITUDE) read as spaces."""
    faint = (m.codes != BLANK_CODE) & (m.amplitude < FAINT_AMPLITUDE) & (1.0 - m.distance < FAINT_CORRELATION)
    if not faint.any():
        return m
    return replace(m, codes=np.where(faint, BLANK_CODE, m.codes).astype(np.uint8),
                   confidence=np.where(faint, 1.0, m.confidence).astype(np.float32),
                   distance=np.where(faint, 0.0, m.distance).astype(np.float32),
                   amplitude=np.where(faint, 0.0, m.amplitude).astype(np.float32))


def decode_cells(cells: np.ndarray, bank: GlyphBank, noise_floor: float, edge_level: float,
                 charset: str | None = None, passes: int = PASSES,
                 bleed: dict[str, list[float]] | None = None):
    """Match the cell cores, predict what the neighbors spilled into each
    from that reading, subtract it and match again. After subtraction a
    cell is blank when its interior is at the noise floor and what is left
    in its margins is a small fraction of the spill predicted there, or of
    the brightest neighbor's level: the model is not exact, a cell's own
    ink is never that small. A cell matched faintly and poorly is a space too."""
    m = drop_faint(screen_match(bank, cells, charset, noise_floor=noise_floor,
                                blank=interior_blank(cells, noise_floor)))
    corrected = cells
    from_neighbors = SPILL_FRACTION * neighbor_level(cells, edge_level)
    for _ in range(passes - 1):
        predicted = spill(bank, m.codes, m.amplitude, m.charset, edge_level, bleed)
        corrected = np.maximum(cells - predicted, 0.0)
        residual = corrected.reshape(corrected.shape[0], -1).max(axis=1)
        allowed = np.maximum(RESIDUAL_ABS * noise_floor,
                             RESIDUAL_REL * predicted.reshape(predicted.shape[0], -1).max(axis=1))
        allowed = np.maximum(allowed, from_neighbors)
        blank = interior_blank(corrected, noise_floor) & (residual < allowed)
        m = drop_faint(screen_match(bank, corrected, charset, noise_floor=noise_floor, blank=blank))
    return m, corrected


def read_video(frame: Frame, calib: Calibration, bank: GlyphBank, charset: str | None = None,
               follow: bool = True, refine_grid: bool = True) -> ScreenRead:
    """Decode one frame: normalize (following small drift of the picture
    unless ``follow`` is off), foreground map, cells, spill-corrected glyphs;
    then, unless ``refine_grid`` is off, refine the grid on the cells that
    read confidently and decode again, since the picture's sub-pixel phase
    moves between launches and a sharp template forgives half a pixel."""
    if not calib.fits(frame.image):
        raise ScreenError(f"frame is {frame.size[0]}x{frame.size[1]} but the calibration is for "
                          f"{calib.frame_w}x{calib.frame_h}")
    if follow:
        calib = track(frame.image, calib)
    want = None if charset in (None, CHARSET_CUSTOM) else charset
    edge = color_distance(calib.border, calib.background)

    def decode(cal: Calibration):
        norm = cal.normalize(frame.image)
        cells = core_cells(distance_map(norm, cal.background))
        m, corrected = decode_cells(cells, bank, cal.noise_floor, edge, want, bleed=cal.bleed)
        return norm, m, corrected

    norm, m, corrected = decode(calib)
    if refine_grid:
        for _ in range(REFINE_ROUNDS):
            sure = (m.confidence >= REFINE_CONFIDENCE) & (m.codes != 0x20)
            try:
                refined = refine(frame.image, calib, m.codes, m.charset, bank, use=sure, cells=corrected,
                                 fine_only=True)
            except CalibrationError:
                break                                  # too little text to pin the grid: the grid stands
            moved = max(abs(refined.x0 - calib.x0) * CELL / calib.cell_w,
                        abs(refined.y0 - calib.y0) * CELL / calib.cell_h)
            scaled = max(abs(refined.cell_w / calib.cell_w - 1.0), abs(refined.cell_h / calib.cell_h - 1.0))
            norm, m, corrected = decode(refined)
            calib = refined
            if moved < REFINE_SETTLED[0] and scaled < REFINE_SETTLED[1]:
                break
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
