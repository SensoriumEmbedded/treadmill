"""Where the text area is in the frame: found from the picture, refined
against a known screen, stored per (capture class, machine).

No calibration program is needed. The C64's text area is a rectangle of
the screen background color inside a border of another color, so its
bounding box in the frame gives the origin and the cell size. On the
TeensyROM menu the top row is a reverse-video bar in the border's own color
(the default theme is dark grey on black with a dark grey border), so the
rectangle found there is rows 1-24 and row 0 is one cell above it; the
BASIC power-on screen is the full 25 rows. A station with DMA refines the
fit against the screen it just read, by where the glyphs sit.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import cv2
import numpy as np

from treadmill.c64.screen import REVERSE_VIDEO, SCREEN_BYTES, SCREEN_COLUMNS, SCREEN_ROWS
from treadmill.errors import TreadmillError
from treadmill.observe.glyphs import BLANK_CODE, CELL, GlyphBank
from treadmill.observe.normalize import (
    NORM_H, NORM_W, core, core_cells, distance_map, dominant_color, median_color, noise_floor_of, normalize,
)
from treadmill.observe.render import expected_map

LAYOUT_MENU = "menu"          # rows 1-24 are background; row 0 is the reverse bar
LAYOUT_BASIC = "basic"        # all 25 rows are background
_ROWS_VISIBLE = {LAYOUT_MENU: SCREEN_ROWS - 1, LAYOUT_BASIC: SCREEN_ROWS}
MIN_CONTRAST = 24.0           # BGR distance below which background and border cannot be told apart
EDGE_BAND = 0.04              # fraction of the frame sampled as border, each side
FILL = 0.5                    # a profile crosses the text area's edge at half its peak
MIN_REFINE_CELLS = 20         # fewer known glyphs than this cannot pin an origin and a scale
BLEED_DECAY = 2.0             # x noise floor: a border bleed is back at the floor by the middle of the edge cell
BLEED_QUANTILE = 25           # percentile over an edge's cells: a quarter of them are blank on any text screen
REFINE_MIN_CORRELATION = 0.2  # below this the picture is not the screen the codes describe


class CalibrationError(TreadmillError):
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
    left_edge: float | None = None        # the background rectangle's edges when calibrated:
    bottom_edge: float | None = None      # tracking moves the origin by how far they move
    # What the border bleeds into the edge cells, per edge, pixel by pixel inward (see with_bleed).
    bleed: dict[str, list[float]] | None = None

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


def border_of(frame: np.ndarray) -> tuple[int, int, int]:
    """The border color: the median of a band around the frame's edges."""
    h, w = frame.shape[:2]
    bw, bh = max(2, int(w * EDGE_BAND)), max(2, int(h * EDGE_BAND))
    band = np.concatenate([frame[:bh].reshape(-1, 3), frame[-bh:].reshape(-1, 3),
                           frame[:, :bw].reshape(-1, 3), frame[:, -bw:].reshape(-1, 3)])
    return median_color(band)


def find_text_area(frame: np.ndarray, layout: str = LAYOUT_MENU) -> Calibration:
    """The text area's origin and cell size from one frame, by the bounding
    box of the background color against the border."""
    if layout not in _ROWS_VISIBLE:
        raise CalibrationError(f"layout is {LAYOUT_MENU!r} or {LAYOUT_BASIC!r}, not {layout!r}")
    h, w = frame.shape[:2]
    border = border_of(frame)
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
    cal = Calibration(x0, y0, cell_w, cell_h, w, h, background, border, noise, layout, residual,
                      "bounding-box", left_edge=float(left), bottom_edge=float(bottom))
    return with_bleed(frame, cal)


def with_bleed(frame: np.ndarray, calib: Calibration) -> Calibration:
    """The calibration with the border's bleed into the edge cells measured
    from this frame: per edge, a low quantile over that edge's cells of the
    distance at each pixel inward, which is what the blank cells among them
    hold. The border's edge is not a glyph's: on one C128 it ramps over ten
    frame pixels into the last column while a stroke is sharp, so a solid
    block's harvested margin cannot stand in for it. An edge whose profile
    does not decay to the floor (the menu's reverse top bar) takes the
    opposite edge's. Measured once the geometry is final: a read's own
    sub-pixel refinement shifts the ramp by less than a pixel."""
    cells = core_cells(distance_map(calib.normalize(frame), calib.background))
    grid = cells.reshape(SCREEN_ROWS, SCREEN_COLUMNS, CELL, CELL)
    raw = {
        "left": np.percentile(grid[:, 0].mean(axis=1), BLEED_QUANTILE, axis=0),
        "right": np.percentile(grid[:, -1].mean(axis=1), BLEED_QUANTILE, axis=0)[::-1],
        "top": np.percentile(grid[0].mean(axis=2), BLEED_QUANTILE, axis=0),
        "bottom": np.percentile(grid[-1].mean(axis=2), BLEED_QUANTILE, axis=0)[::-1],
    }
    limit = BLEED_DECAY * calib.noise_floor
    decays = {k: float(v[CELL // 2:].max()) <= limit for k, v in raw.items()}
    opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
    bleed = {}
    for k, v in raw.items():
        if decays[k]:
            use = v
        elif decays[opposite[k]]:
            use = raw[opposite[k]]
        else:
            use = np.zeros(CELL, np.float32)
        bleed[k] = [round(float(x), 4) for x in use]
    return replace(calib, bleed=bleed)


TRACK_LIMIT = 1.0             # cells: a larger apparent move is a different picture, not drift


def track(frame: np.ndarray, calib: Calibration) -> Calibration:
    """Follow small drift of the picture between frames: the left and bottom
    edges of the background rectangle are found again (both independent of
    the menu's top bar) and the origin moves by as much as they moved since
    the calibration, cell sizes kept, so a refined origin is not undone. The
    border is the frame's own, since a launched program sets its own color
    (hello.a's dark grey against the menu's purple moved the grid a pixel when
    the stored one was used). When the border is too like the background, no
    rectangle is found, or it moved more than a cell, the calibration is
    returned unchanged."""
    if not calib.fits(frame):
        return calib
    border = border_of(frame)
    if float(np.linalg.norm(np.subtract(calib.background, border, dtype=np.float32))) < MIN_CONTRAST:
        return calib
    f = frame.astype(np.float32)
    d_bg = np.linalg.norm(f - np.asarray(calib.background, np.float32), axis=-1)
    d_bd = np.linalg.norm(f - np.asarray(border, np.float32), axis=-1)
    try:
        left, _right, _top, bottom = _extents(d_bg < d_bd)
    except CalibrationError:
        return calib
    if calib.left_edge is None or calib.bottom_edge is None:   # a calibration saved before the edges were kept
        x0 = left + calib.cell_w / (2 * CELL)
        y0 = bottom - calib.cell_h * SCREEN_ROWS + calib.cell_h / (2 * CELL)
    else:
        x0 = calib.x0 + (left - calib.left_edge)
        y0 = calib.y0 + (bottom - calib.bottom_edge)
    if abs(x0 - calib.x0) > TRACK_LIMIT * calib.cell_w or abs(y0 - calib.y0) > TRACK_LIMIT * calib.cell_h:
        return calib
    return replace(calib, x0=x0, y0=y0, left_edge=float(left), bottom_edge=float(bottom),
                   method=calib.method if calib.method.endswith("+tracked") else calib.method + "+tracked")


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    return float((a * b).sum() / max(float(np.linalg.norm(a) * np.linalg.norm(b)), 1e-6))


def refine(frame: np.ndarray, calib: Calibration, codes: np.ndarray, charset: str, bank: GlyphBank,
           use: np.ndarray | None = None, cells: np.ndarray | None = None, fine_only: bool = False) -> Calibration:
    """Shift and scale the calibration so the glyphs sit where their
    templates sit: the screen the codes describe is rendered from the bank,
    and the shift and scale that best correlate it with the normalized frame
    are searched coarse to fine, warping the rendering rather than the frame
    (the map is a quarter of the frame and single-channel). ``use`` limits
    the comparison to the cells a caller trusts; ``cells`` are (1000, 16, 16)
    cores already cut under ``calib`` to compare against instead of the
    frame, such as a decoder's spill-corrected ones; ``fine_only`` skips the
    whole-pixel stage for a grid known to be close. Sub-pixel, and what
    makes sharp real templates usable: the picture's scale moves a part in
    a thousand between launches, which puts the last column a pixel off, and
    a pixel is half a stroke. The residual is one minus the correlation at
    the optimum."""
    codes = np.asarray(codes, np.uint8).reshape(SCREEN_BYTES)
    mask = np.ones(SCREEN_BYTES, bool) if use is None else np.asarray(use, bool).reshape(SCREEN_BYTES)
    mask &= (codes & (REVERSE_VIDEO - 1)) != BLANK_CODE
    if mask.sum() < MIN_REFINE_CELLS:
        raise CalibrationError(f"only {int(mask.sum())} readable cells to refine against; {MIN_REFINE_CELLS} needed")
    mask_px = np.repeat(np.repeat(mask.reshape(SCREEN_ROWS, SCREEN_COLUMNS), CELL, axis=0), CELL, axis=1)
    expected = np.ascontiguousarray(core(expected_map(codes, bank, charset)), np.float32)
    if expected.max() <= 0:
        raise CalibrationError("the bank has no template for any cell shown; harvest or bootstrap it first")
    if cells is not None:
        actual = np.asarray(cells, np.float32).reshape(SCREEN_ROWS, SCREEN_COLUMNS, CELL, CELL)             .transpose(0, 2, 1, 3).reshape(NORM_H, NORM_W)
    else:
        actual = core(distance_map(calib.normalize(frame), calib.background))
    actual = np.ascontiguousarray(actual, np.float32)
    sel = mask_px
    target = actual[sel]

    cx, cy = NORM_W / 2, NORM_H / 2                           # scale about the center: offsets stay valid

    def cost(dx: float, dy: float, sx: float, sy: float) -> float:
        m = np.array([[sx, 0.0, dx + (1.0 - sx) * cx], [0.0, sy, dy + (1.0 - sy) * cy]], np.float32)
        warped = cv2.warpAffine(expected, m, (NORM_W, NORM_H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        return 1.0 - _ncc(warped[sel], target)

    best = (0.0, 0.0, 1.0, 1.0)
    best_cost = cost(*best)
    if fine_only:          # a tracked grid: within the pixel or two a launch moves the text against the border
        stages = [("offset", 1.5, 0.5), ("scale", 0.004, 0.002), ("offset", 0.25, 0.125), ("scale", 0.001, 0.0005)]
    else:
        stages = [("offset", 3.0, 1.0), ("offset", 0.75, 0.25), ("scale", 0.008, 0.002), ("offset", 0.2, 0.1),
                  ("scale", 0.002, 0.0005), ("offset", 0.1, 0.05)]
    for kind, span, step in stages:                            # offsets in normalized px, scales as fractions
        dx, dy, sx, sy = best
        for a in np.arange(-span, span + step / 2, step):
            for b in np.arange(-span, span + step / 2, step):
                cand = (dx + a, dy + b, sx, sy) if kind == "offset" else (dx, dy, sx + a, sy + b)
                c = cost(*cand)
                if c < best_cost:
                    best, best_cost = cand, c
    if best_cost > 1.0 - REFINE_MIN_CORRELATION:
        raise CalibrationError(f"the frame does not correlate with the screen it should show ({1 - best_cost:.2f})")
    dx, dy, sx, sy = best
    # The rendering warped this way lands on the frame, so the frame's grid is the old
    # one moved by the warp's translation (in its own cells) and scaled by sx, sy.
    x0 = calib.x0 + (dx + (1.0 - sx) * cx) * calib.cell_w / CELL
    y0 = calib.y0 + (dy + (1.0 - sy) * cy) * calib.cell_h / CELL
    cw = calib.cell_w * sx
    ch = calib.cell_h * sy
    noise = noise_floor_of(core(distance_map(normalize(frame, x0, y0, cw, ch), calib.background)))
    method = "refined" + ("+tracked" if calib.method.endswith("+tracked") else "")
    return replace(calib, x0=x0, y0=y0, cell_w=cw, cell_h=ch, residual=best_cost, method=method,
                   noise_floor=noise)
