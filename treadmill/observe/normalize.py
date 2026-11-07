"""From a captured frame to the normalized text area and its cells.

The calibration says where the 40x25 text area sits in the frame; an affine
warp (never integer sampling: the scale is not an integer) brings it to 16x16
pixels per cell, with a margin of PAD pixels all round so the spill a cell
receives from the border is in the picture too. The foreground map is each
pixel's distance from the screen background color, so any text color on
any background reads the same way, reverse video included; only text in
the background's own color is unreadable, which is itself a finding.
"""

from __future__ import annotations

import cv2
import numpy as np

from treadmill.c64.screen import SCREEN_BYTES, SCREEN_COLUMNS, SCREEN_ROWS
from treadmill.observe.glyphs import CELL, INTERIOR, PAD, TCELL

NORM_W, NORM_H = SCREEN_COLUMNS * CELL, SCREEN_ROWS * CELL      # 640 x 400, the core
PADDED_W, PADDED_H = NORM_W + 2 * PAD, NORM_H + 2 * PAD
_MAX_DISTANCE = float(np.sqrt(3 * 255 ** 2))


def warp_matrix(x0: float, y0: float, cell_w: float, cell_h: float) -> np.ndarray:
    """Frame to padded normalized coordinates: frame (x0, y0) lands on the
    center of the core's first pixel, at (PAD, PAD)."""
    sx, sy = CELL / cell_w, CELL / cell_h
    return np.array([[sx, 0.0, PAD - x0 * sx], [0.0, sy, PAD - y0 * sy]], np.float64)


def normalize(frame: np.ndarray, x0: float, y0: float, cell_w: float, cell_h: float) -> np.ndarray:
    """The text area and its margin as a (408, 648, 3) BGR image."""
    return cv2.warpAffine(frame, warp_matrix(x0, y0, cell_w, cell_h), (PADDED_W, PADDED_H),
                          flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def core(padded: np.ndarray) -> np.ndarray:
    """The 400x640 text area of a padded normalized image or map."""
    return padded[PAD:PAD + NORM_H, PAD:PAD + NORM_W]


def distance_map(norm: np.ndarray, background: tuple[int, int, int]) -> np.ndarray:
    """float32 in 0..1, the shape of ``norm`` less its channels: how far each
    pixel is from the background color."""
    diff = norm.astype(np.float32) - np.asarray(background, np.float32)
    return np.sqrt((diff * diff).sum(axis=-1)) / _MAX_DISTANCE


def core_cells(dmap: np.ndarray) -> np.ndarray:
    """(1000, 16, 16) float32: the cells of a padded distance map, as they are."""
    cells = core(dmap).reshape(SCREEN_ROWS, CELL, SCREEN_COLUMNS, CELL).transpose(0, 2, 1, 3)
    return np.ascontiguousarray(cells.reshape(SCREEN_BYTES, CELL, CELL), dtype=np.float32)


def padded_cells(dmap: np.ndarray, which: np.ndarray | None = None) -> np.ndarray:
    """(N, 24, 24) float32: cells with their margins, for harvesting; all
    1000 or the indices in ``which``."""
    idx = np.arange(SCREEN_BYTES) if which is None else np.asarray(which)
    out = np.empty((idx.size, TCELL, TCELL), np.float32)
    for k, i in enumerate(idx):
        r, c = divmod(int(i), SCREEN_COLUMNS)
        out[k] = dmap[r * CELL:r * CELL + TCELL, c * CELL:c * CELL + TCELL]
    return out


def interior_blank(cells: np.ndarray, noise_floor: float) -> np.ndarray:
    """(N,) bool: nothing above the noise floor away from the cell's edges;
    the first guess at blankness, before spill is accounted for."""
    inner = cells[:, INTERIOR:CELL - INTERIOR, INTERIOR:CELL - INTERIOR].reshape(cells.shape[0], -1)
    return inner.max(axis=1) < noise_floor


def foreground_mask(cells: np.ndarray) -> np.ndarray:
    """(N, 16, 16) bool: the pixels at more than half of their cell's peak."""
    peak = cells.reshape(cells.shape[0], -1).max(axis=1)[:, None, None]
    return cells > 0.5 * np.maximum(peak, 1e-6)


def cell_colors(norm_core: np.ndarray, cells: np.ndarray) -> np.ndarray:
    """(1000, 3) uint8: the mean BGR of each cell's foreground pixels, or the
    background where a cell has none."""
    pix = norm_core.reshape(SCREEN_ROWS, CELL, SCREEN_COLUMNS, CELL, 3).transpose(0, 2, 1, 3, 4)
    pix = pix.reshape(-1, CELL * CELL, 3).astype(np.float32)
    fg = foreground_mask(cells).reshape(cells.shape[0], -1).astype(np.float32)
    n = fg.sum(axis=1)[:, None]
    mean = (pix * fg[:, :, None]).sum(axis=1) / np.maximum(n, 1.0)
    fallback = np.median(pix, axis=1)
    return np.where(n > 0, mean, fallback).round().clip(0, 255).astype(np.uint8)


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Two BGR colors' distance on the same 0..1 scale as the distance map."""
    diff = np.asarray(a, np.float32) - np.asarray(b, np.float32)
    return float(np.sqrt((diff * diff).sum()) / _MAX_DISTANCE)


def median_color(image: np.ndarray) -> tuple[int, int, int]:
    med = np.median(image.reshape(-1, image.shape[-1]), axis=0)
    return tuple(int(v) for v in med)


def dominant_color(image: np.ndarray, levels: int = 16) -> tuple[int, int, int]:
    """The most common color, coarsely binned, then the median within that bin:
    the screen background on any text screen, where the median would drift
    toward the text when the screen is busy."""
    pix = image.reshape(-1, 3)
    step = 256 // levels
    bins = (pix // step).astype(np.int32)
    keys = bins[:, 0] * levels * levels + bins[:, 1] * levels + bins[:, 2]
    top = np.bincount(keys).argmax()
    inside = pix[keys == top]
    return median_color(inside)


def noise_floor_of(dmap: np.ndarray, factor: float = 5.0, floor: float = 0.05, ceiling: float = 0.2) -> float:
    """What the background looks like when nothing is on it. The 20th
    percentile of the distance map is background whatever the screen shows;
    a blank cell's peak is a few times that, so the threshold sits above it
    and below the dimmest text (dark grey on black is about 0.27)."""
    return float(min(ceiling, max(floor, factor * np.percentile(dmap, 20))))
