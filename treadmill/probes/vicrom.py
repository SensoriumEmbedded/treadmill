"""vicrom: the VIC's own reads from the cartridge, judged by the picture.

``vicrom.a`` is an 8K Ultimax cartridge whose charset the VIC fetches from
ROMH, so every glyph on screen comes through the TeensyROM's VIC cycles
(``EmulateVicCycles``, timed by ``nS_VICStart`` and ``nS_VICDHold``). At the
defaults the probe takes a reference picture of the text area, the median
of several frames, and refuses one with no contrast (a black capture would
pass every point). Over ``LEARN_S`` more seconds it learns which pixels hold
still: a pixel that strays from the reference by more than ``LEVEL`` in
more than ``STEADY`` of those frames (a glyph's edge in the capture's
jitter) is left out of every count. A frame's count is the steady pixels
that differ from the reference by more than ``LEVEL``. A dwell's worth of
frames at the defaults gives the baseline: its ``PERCENTILE`` count, and
its spread, that count less the median. A sample is ``dwell`` seconds of
frames, judged on the same percentile, so one disturbed frame does not
decide it, and fails past the baseline plus ``SPREAD`` times the spread,
and plus at least ``CELL_SHARE`` of one character cell's pixels. Until
2026-11-27 the probe judged the worst frame against ``max(3 x baseline,
baseline + 100)``: with baselines of 48,000 to 104,000 pixels a point failed
only with a quarter to a half of the text area wrong. Every sample's count,
its worst frame and its frames go in the record. The C64 counts frames at
$0F00 and steps the border once a frame: the probe reads the count by DMA
on a TR+, else watches the border, and a C64 that stopped is a hang.

``vicrom-high`` is ``vicrom.a`` built with ``HIGH``: the screen matrix and
eight sprites are in ROMH too, and the VIC reads those while Phi2 is high,
in cycles the TeensyROM serves as it does the CPU's (``nS_RWnReady``,
``nS_PLAprop``), so the picture also shows those knobs' edges. Its sprites
cover a few percent of the text area, so the box they sit in (``SPRITE_BOX``)
is counted and judged on its own as well, with a baseline of its own: a
point fails when either the glyphs or the sprites stray. At the
default ``nS_VICDHold`` a TR+ running it stops answering USB (BACKLOG 59),
so in a sweep it holds ``nS_VICDHold`` lower (``pins``), and it cannot
sweep that knob itself; vicrom and vicrom-cpu do.

``vicrom-cpu`` and ``vicrom-high`` are built with ``CPU`` as well: the C64
reads two pages of ROMH in a loop running from RAM and counts the wrong
answers, as idread does, so a VIC-cycle drive held into the CPU's half
shows as wrong reads before it hangs the C64. The counts are read by DMA,
so on a plain TR those probes judge the picture alone.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from treadmill.board import Board
from treadmill.observe.calibrate import Calibration
from treadmill.observe.capture import FrameSource
from treadmill.probes.noise import LEVEL, MIN_CONTRAST, contrast
from treadmill.sweeps import Sample, Setting, SweepError, at_defaults

PASSES, READY, READY_VALUE = 0x0F00, 0x0F04, 0x5A
COUNTERS = 10                     # $0F00-$0F09: frames, ready, wrong CPU reads (24-bit), rounds (16-bit)
READS_PER_ROUND = 512             # CPU: two ROMH pages a round
START_S = 15.0
REFERENCE_FRAMES = 15
FRAME_WAIT_S = 2.0
BORDER_PX = 6                     # how far outside the text area the border strip is read
LEARN_S = 1.0                     # frames at the defaults that pick the steady pixels
STEADY = 0.05                     # a steady pixel strays past LEVEL in at most this share of them
PERCENTILE = 95                   # a sample's count: this percentile of its frames' counts
SPREAD = 4.0                      # the limit: the baseline's count plus this many times its spread
CELL_SHARE = 0.25                 # and at least this share of one cell's pixels above it
MIN_STEADY = 0.05                 # the share of an area that must hold still for it to be judged
# vicrom.a's sprites with HIGH, in text-area pixels (x, y, width, height): eight unexpanded sprites at
# y 120 and x 40 to 222 in sprite coordinates, which put the text area's corner at (24, 50).
SPRITE_BOX = (16, 70, 206, 21)
# vicrom-high's nS_VICDHold in a sweep. On the Kawari C64's TR+ (2026-11-25) the board kept answering USB up
# to 355 in PAL and 305 in NTSC, and NTSC 320 answered once and then went silent (BACKLOG 59).
HIGH_VICDHOLD = 280


@dataclass
class Area:
    """A part of the text area judged on its own: the steady pixels in it,
    and what the baseline's frames counted there."""
    name: str                     # glyphs | sprites
    rows: slice
    cols: slice
    steady: np.ndarray            # the steady pixels, over this area
    floor: float                  # CELL_SHARE of a cell's pixels
    base: float = 0.0             # the baseline's PERCENTILE count
    spread: float = 0.0           # that, less the baseline's median count
    limit: float = 0.0

    def counts(self, strays: list[np.ndarray]) -> list[int]:
        """Each frame's steady pixels off the reference, from its strays (the whole text area's)."""
        return [int(np.count_nonzero(s[self.rows, self.cols] & self.steady)) for s in strays]

    def learn(self, strays: list[np.ndarray]) -> None:
        counts = self.counts(strays)
        self.base = float(np.percentile(counts, PERCENTILE)) if counts else 0.0
        self.spread = self.base - (float(np.median(counts)) if counts else 0.0)
        self.limit = self.base + max(SPREAD * self.spread, self.floor)

    def info(self) -> dict:
        return {"baseline": round(self.base), "spread": round(self.spread), "limit": round(self.limit),
                "steady": round(float(self.steady.mean()), 3)}


class VicRomProbe:
    name = "vicrom"

    def __init__(self, board: Board, video: FrameSource | None, calibration: Calibration | None, program: Path,
                 *, dwell: float = 2.0, drive: str = "sd", name: str = "vicrom", remote: str | None = None,
                 cpu: bool = False, log=print):
        self.name = name
        self.cpu = cpu                   # the program counts its CPU's reads of ROMH (built with CPU)
        self.pins = {"i": HIGH_VICDHOLD} if name == "vicrom-high" else {}
        self.board = board
        self.video = video
        self.calibration = calibration
        self.program = Path(program)
        self.dwell = dwell
        self.drive = drive
        self.remote = remote or f"/treadmill/{name}.crt"
        self.log = log
        self.reference: np.ndarray | None = None
        self.areas: list[Area] = []
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

    def _strays(self, frames) -> list[np.ndarray]:
        """Each frame's pixels more than LEVEL off the reference."""
        return [cv2.absdiff(self._text(f), self.reference) > LEVEL for f in frames]

    def _areas(self, steady: np.ndarray) -> list[Area]:
        """The glyphs (the whole text area) and, for vicrom-high, the sprites' box, each with its steady pixels."""
        cal = self.calibration
        floor = CELL_SHARE * cal.cell_w * cal.cell_h
        areas = [Area("glyphs", slice(None), slice(None), steady, floor)]
        if self.name == "vicrom-high":
            _, _, w, h = cal.text_area
            sx, sy = w / 320, h / 200
            x0, y0 = int(SPRITE_BOX[0] * sx - cal.cell_w / 2), int(SPRITE_BOX[1] * sy - cal.cell_h / 2)
            rows = slice(max(0, y0), y0 + int(np.ceil(SPRITE_BOX[3] * sy)))
            cols = slice(max(0, x0), x0 + int(np.ceil(SPRITE_BOX[2] * sx)))
            areas.append(Area("sprites", rows, cols, steady[rows, cols], floor))
        for area in areas:
            if area.steady.mean() < MIN_STEADY:
                raise SweepError(f"{self.name}: only {area.steady.mean():.1%} of the {area.name} hold still at the "
                                 f"defaults, under {MIN_STEADY:.0%}: the capture is too unsteady to judge them")
        return areas

    # -- the C64 ----------------------------------------------------------------------

    def _counters(self) -> tuple[int, int, int]:
        """(frames, wrong CPU reads, rounds); the last two stay 0 without CPU."""
        b = self.board.link.read_memory(PASSES, COUNTERS if self.cpu else 2)
        if not self.cpu:
            return b[0] | b[1] << 8, 0, 0
        return b[0] | b[1] << 8, b[5] | b[6] << 8 | b[7] << 16, b[8] | b[9] << 8

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
        seen = contrast(self.reference)
        if seen < MIN_CONTRAST:
            raise SweepError(f"{self.name}: the reference has no contrast ({seen:.0f} grey levels between its dark "
                             f"and bright pixels, under {MIN_CONTRAST}): a black or blank capture judges nothing")
        learn = self._strays(self._frames(LEARN_S))
        if not learn:
            raise SweepError(f"{self.name}: no frames to learn the steady pixels from")
        self.areas = self._areas(np.mean(learn, axis=0) <= STEADY)
        baseline = self._strays(self._frames(self.dwell))
        if not baseline:
            raise SweepError(f"{self.name}: no frames for the baseline")
        for area in self.areas:
            area.learn(baseline)
        self._stalled = False
        self.log(f"[sweep] {self.name} reference at the defaults; " + "; ".join(
            f"{a.name}: {a.steady.mean():.0%} steady, baseline {a.base:.0f} pixels, spread {a.spread:.0f} "
            f"(limit {a.limit:.0f})" for a in self.areas))
        if self.cpu and not self.board.dma_allowed():
            self.log(f"[sweep] {self.name}: no DMA, so the CPU's reads are not counted; the picture alone judges")

    def sample(self, label: str, setting: Setting | None = None) -> Sample:
        if self._stalled:
            self.log(f"[sweep] {self.name} stopped at the last value; relaunching for {label}")
            with at_defaults(setting, always=True):
                self.board.reset()
                self.board.wait_for_menu()
                self._launch()
        dma = self.board.dma_allowed()
        before = self._counters() if dma else None
        frames = self._frames(self.dwell)
        if dma:
            after = self._counters()
            rounds = (after[2] - before[2]) & 0xFFFF
            alive = after[0] != before[0] and (not self.cpu or rounds > 0)
        else:
            alive = len({round(self._border(f)) for f in frames}) > 1
        if not alive:
            self._stalled = True
            return Sample(1, 1, f"{self.name} stopped: the C64 hung", hung=True)
        if not frames:
            return Sample(1, 1, f"{self.name}: no frames in {self.dwell} s: the capture gave nothing to judge",
                          measure={"frames": 0})
        failures, samples, notes = 0, len(self.areas), []
        strays = self._strays(frames)
        measure: dict = {"frames": len(frames)}
        for area in self.areas:
            counts = area.counts(strays)
            got, worst = float(np.percentile(counts, PERCENTILE)), max(counts)
            measure[area.name] = {"count": round(got), "worst": worst}
            if got > area.limit:
                failures += 1
                notes.append(f"{area.name} {got:.0f} pixels off the reference at the {PERCENTILE}th percentile "
                             f"of {len(frames)} frames (worst {worst}), over the limit {area.limit:.0f}")
        if self.cpu and dma:
            wrong, reads = (after[1] - before[1]) & 0xFFFFFF, rounds * READS_PER_ROUND
            failures, samples = failures + wrong, samples + reads
            notes.append(f"cpu {wrong}/{reads}")
        return Sample(failures, samples, "; ".join(notes) or None, measure=measure)

    def teardown(self) -> None:
        self.board.reset()
        self.board.wait_for_menu()

    def info(self) -> dict:
        return {**{a.name: a.info() for a in self.areas}, "level": LEVEL, "percentile": PERCENTILE,
                "spread_factor": SPREAD, "cell_share": CELL_SHARE, "steady_share": STEADY,
                "cpu_counted": self.cpu and self.board.dma_allowed()}
