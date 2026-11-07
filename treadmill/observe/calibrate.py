"""Where the text area is in the frame: found from the picture, refined
against a known screen, stored per (capture class, machine).

No calibration program is needed. The C64's text area is a rectangle of
the screen background color inside a border of another color, so its
bounding box in the frame gives the origin and the cell size. On the
TeensyROM menu the top row is a reverse-video bar in the border's own color
(the default theme is dark grey on black with a dark grey border), so the
rectangle found there is rows 1-24 and row 0 is one cell above it; the
BASIC power-on screen is the full 25 rows. A station with DMA refines the
fit by correlating the normalized frame with the screen it just read.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np

from treadmill.c64.screen import SCREEN_COLUMNS, SCREEN_ROWS
from treadmill.observe.glyphs import CELL, GlyphBank
from treadmill.observe.normalize import core, distance_map, dominant_color, median_color, noise_floor_of, normalize
from treadmill.observe.render import expected_map

LAYOUT_MENU = "menu"          # rows 1-24 are background; row 0 is the reverse bar
LAYOUT_BASIC = "basic"        # all 25 rows are background
_ROWS_VISIBLE = {LAYOUT_MENU: SCREEN_ROWS - 1, LAYOUT_BASIC: SCREEN_ROWS}
MIN_CONTRAST = 24.0           # BGR distance below which background and border cannot be told apart
EDGE_BAND = 0.04              # fraction of the frame sampled as border, each side
FILL = 0.5                    # a profile crosses the text area's edge at half its peak


class CalibrationError(Exception):
    pass


@dataclass(frozen=True)
class Calibration:
    x0: float
    y0: float
    cell_w: float
    cell_h: float
    frame_w: int
    frame_h: int
    background: tuple[int, int, int]      # BGR
    border: tuple[int, int, int]          # BGR
    noise_floor: float
    layout: str
    residual: float                       # how well the fit matched: lower is better
    method: str                           # bounding-box | refined
    capture_class: str = ""
    machine_id: str = ""
    created: str = ""

    @property
    def text_area(self) -> tuple[float, float, float, float]:
        return self.x0, self.y0, self.cell_w * SCREEN_COLUMNS, self.cell_h * SCREEN_ROWS

    def normalize(self, frame: np.ndarray) -> np.ndarray:
        return normalize(frame, self.x0, self.y0, self.cell_w, self.cell_h)

    def fits(self, frame: np.ndarray) -> bool:
        return frame.shape[1] == self.frame_w and frame.shape[0] == self.frame_h

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamped = self if self.created else replace(self, created=time.strftime("%Y-%m-%dT%H:%M:%S"))
        path.write_text(stamped.to_json() + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | str) -> "Calibration":
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
            d["background"] = tuple(d["background"])
            d["border"] = tuple(d["border"])
            return cls(**d)
        except (OSError, KeyError, TypeError, ValueError) as e:
            raise CalibrationError(f"cannot load calibration {path}: {e}") from e


def calibration_path(data_dir: Path, capture_class: str, machine_id: str) -> Path:
    return Path(data_dir) / "calib" / f"{capture_class}__{machine_id}.json"


def _edge(profile: np.ndarray, level: float) -> tuple[float, float]:
    """Sub-pixel first and last crossings of ``level`` in a 1-D profile."""
    above = np.flatnonzero(profile >= level)
    if above.size == 0:
        raise CalibrationError("no run of background found")
    first, last = int(above[0]), int(above[-1])
    lo = float(first)
    if first > 0 and profile[first] > profile[first - 1]:
        lo = first - (profile[first] - level) / (profile[first] - profile[first - 1])
    hi = float(last + 1)
    if last + 1 < profile.size and profile[last] > profile[last + 1]:
        hi = last + 1 + (profile[last] - level) / (profile[last] - profile[last + 1])
    return lo, hi


def _extents(is_bg: np.ndarray) -> tuple[float, float, float, float]:
    """left, right, top, bottom of the background rectangle: profiles are
    thresholded at half their own peak, so the border's share of the frame
    does not matter, and each axis is refined within the other's extents."""
    rows = is_bg.mean(axis=1)
    top, bottom = _edge(rows, FILL * rows.max())
    r0, r1 = int(np.floor(top)), int(np.ceil(bottom))
    cols = is_bg[r0:r1].mean(axis=0)
    left, right = _edge(cols, FILL * cols.max())
    c0, c1 = int(np.floor(left)), int(np.ceil(right))
    rows = is_bg[:, c0:c1].mean(axis=1)
    top, bottom = _edge(rows, FILL * rows.max())
    return left, right, top, bottom


def find_text_area(frame: np.ndarray, layout: str = LAYOUT_MENU) -> Calibration:
    """The text area's origin and cell size from one frame, by the bounding
    box of the background color against the border."""
    if layout not in _ROWS_VISIBLE:
        raise CalibrationError(f"layout is {LAYOUT_MENU!r} or {LAYOUT_BASIC!r}, not {layout!r}")
    h, w = frame.shape[:2]
    bw, bh = max(2, int(w * EDGE_BAND)), max(2, int(h * EDGE_BAND))
    band = np.concatenate([frame[:bh].reshape(-1, 3), frame[-bh:].reshape(-1, 3),
                           frame[:, :bw].reshape(-1, 3), frame[:, -bw:].reshape(-1, 3)])
    border = median_color(band)
    center = frame[h // 3:2 * h // 3, w // 3:2 * w // 3]
    background = dominant_color(center)
    contrast = float(np.linalg.norm(np.subtract(background, border, dtype=np.float32)))
    if contrast < MIN_CONTRAST:
        raise CalibrationError(f"background {background} and border {border} are too alike "
                               f"({contrast:.0f}); the text area cannot be found")
    f = frame.astype(np.float32)
    d_bg = np.linalg.norm(f - np.asarray(background, np.float32), axis=-1)
    d_bd = np.linalg.norm(f - np.asarray(border, np.float32), axis=-1)
    is_bg = d_bg < d_bd
    left, right, top, bottom = _extents(is_bg)
    visible = _ROWS_VISIBLE[layout]
    cell_w = (right - left) / SCREEN_COLUMNS
    cell_h = (bottom - top) / visible
    # Edges to pixel centers: the first normalized pixel's center sits half a
    # normalized pixel inside the edge, which is what the warp maps to (0, 0).
    x0 = left + cell_w / (2 * CELL)
    y0 = top - cell_h * (SCREEN_ROWS - visible) + cell_h / (2 * CELL)
    if cell_w < 4 or cell_h < 4:
        raise CalibrationError(f"text area too small to read: cells {cell_w:.1f}x{cell_h:.1f} px")
    aspect = cell_h / cell_w
    if not 0.6 < aspect < 2.2:
        raise CalibrationError(f"implausible cell shape {cell_w:.1f}x{cell_h:.1f} px (aspect {aspect:.2f})")
    inside = is_bg[int(top):int(bottom), int(left):int(right)].mean()
    outside = 1.0 - np.concatenate([is_bg[:int(top)].ravel(), is_bg[int(bottom):].ravel(),
                                    is_bg[:, :int(left)].ravel(), is_bg[:, int(right):].ravel()]).mean()
    residual = float(1.0 - min(inside, outside))
    norm = normalize(frame, x0, y0, cell_w, cell_h)
    noise = noise_floor_of(core(distance_map(norm, background))[CELL * (SCREEN_ROWS - visible):])
    return Calibration(x0, y0, cell_w, cell_h, w, h, background, border, noise, layout, residual,
                       "bounding-box")


TRACK_LIMIT = 1.0             # cells: a larger apparent move is a different picture, not drift


def track(frame: np.ndarray, calib: Calibration) -> Calibration:
    """Follow small drift of the picture between frames: the left and bottom
    edges of the background rectangle are found again (both independent of
    the menu's top bar) and the origin moves with them, cell sizes kept.
    When no rectangle is found, or it moved more than a cell, the
    calibration is returned unchanged."""
    if not calib.fits(frame):
        return calib
    f = frame.astype(np.float32)
    d_bg = np.linalg.norm(f - np.asarray(calib.background, np.float32), axis=-1)
    d_bd = np.linalg.norm(f - np.asarray(calib.border, np.float32), axis=-1)
    try:
        left, _right, _top, bottom = _extents(d_bg < d_bd)
    except CalibrationError:
        return calib
    x0 = left + calib.cell_w / (2 * CELL)
    y0 = bottom - calib.cell_h * SCREEN_ROWS + calib.cell_h / (2 * CELL)
    if abs(x0 - calib.x0) > TRACK_LIMIT * calib.cell_w or abs(y0 - calib.y0) > TRACK_LIMIT * calib.cell_h:
        return calib
    return replace(calib, x0=x0, y0=y0, method=calib.method + "+tracked")


def refine(frame: np.ndarray, calib: Calibration, codes: np.ndarray, charset: str,
           bank: GlyphBank) -> Calibration:
    """Shift and scale the calibration to best match the screen the board
    reports (TR+ DMA): the normalized foreground map against the one the
    bank predicts, compared by correlation over the cells that show
    something, so the text's brightness does not matter."""
    expected = core(expected_map(codes, bank, charset))
    mask = expected.reshape(SCREEN_ROWS, CELL, SCREEN_COLUMNS, CELL).max(axis=(1, 3)) > 0
    if mask.sum() < 20:
        raise CalibrationError("the screen is too empty to refine against")
    mask_px = np.repeat(np.repeat(mask, CELL, axis=0), CELL, axis=1)
    e = expected[mask_px]
    e = (e - e.mean()) / max(float(np.linalg.norm(e - e.mean())), 1e-6)

    def cost(x0: float, y0: float, cw: float, ch: float) -> float:
        d = core(distance_map(normalize(frame, x0, y0, cw, ch), calib.background))[mask_px]
        d = d - d.mean()
        return float(1.0 - (d @ e) / max(float(np.linalg.norm(d)), 1e-6))

    best = (calib.x0, calib.y0, calib.cell_w, calib.cell_h)
    best_cost = cost(*best)
    for span, steps in ((0.5, 9), (0.15, 7), (0.05, 5)):            # offsets in cells, coarse to fine
        x0, y0, cw, ch = best
        for dx in np.linspace(-span, span, steps):
            for dy in np.linspace(-span, span, steps):
                c = cost(x0 + dx * cw, y0 + dy * ch, cw, ch)
                if c < best_cost:
                    best, best_cost = (x0 + dx * cw, y0 + dy * ch, cw, ch), c
    for span, steps in ((0.03, 7), (0.006, 5)):                      # scale, as a fraction
        x0, y0, cw, ch = best
        for sx in np.linspace(1 - span, 1 + span, steps):
            for sy in np.linspace(1 - span, 1 + span, steps):
                c = cost(x0, y0, cw * sx, ch * sy)
                if c < best_cost:
                    best, best_cost = (x0, y0, cw * sx, ch * sy), c
    x0, y0, cw, ch = best
    noise = noise_floor_of(core(distance_map(normalize(frame, x0, y0, cw, ch), calib.background)))
    return replace(calib, x0=x0, y0=y0, cell_w=cw, cell_h=ch, residual=best_cost, method="refined",
                   noise_floor=noise)
