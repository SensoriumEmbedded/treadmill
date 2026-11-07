"""Frames from screen codes: a synthetic capture for the self-tests, and
the expected normalized image a calibration is refined against.

The synthetic frame is a text area drawn from a Font, placed in a border at
a non-integer position and scale, blurred and given noise: the same kind of
picture the capture chain produces, minus its analog character, which the
glyph harvest learns from real frames.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from treadmill.c64.screen import SCREEN_BYTES, SCREEN_COLUMNS, SCREEN_ROWS
from treadmill.observe.glyphs import GLYPH, Font, GlyphBank, charset_index, composite

TEXT_W, TEXT_H = SCREEN_COLUMNS * GLYPH, SCREEN_ROWS * GLYPH         # 320 x 200

# A VIC-II palette (Pepto), BGR, for synthetic frames; a rig measures its own.
PALETTE_BGR = np.array([
    (0x00, 0x00, 0x00), (0xFF, 0xFF, 0xFF), (0x2B, 0x37, 0x68), (0xB2, 0xA4, 0x70),
    (0x86, 0x3F, 0x6F), (0x43, 0x8D, 0x58), (0x79, 0x28, 0x35), (0x6F, 0xC7, 0xB8),
    (0x25, 0x4F, 0x6F), (0x00, 0x39, 0x43), (0x59, 0x67, 0x9A), (0x44, 0x44, 0x44),
    (0x6C, 0x6C, 0x6C), (0x84, 0xD2, 0x9A), (0xB8, 0x5C, 0x6C), (0x95, 0x95, 0x95),
], np.uint8)
BLACK, WHITE, DARK_GREY, MEDIUM_GREY, LIGHT_GREY = 0, 1, 11, 12, 15


@dataclass(frozen=True)
class Geometry:
    """Where the 320x200 text area lands in a frame of ``size``."""
    size: tuple[int, int] = (1280, 720)         # (width, height)
    x0: float = 236.5
    y0: float = 103.25
    width: float = 806.0                        # of the text area, in frame pixels
    height: float = 530.0

    @property
    def cell_w(self) -> float:
        return self.width / SCREEN_COLUMNS

    @property
    def cell_h(self) -> float:
        return self.height / SCREEN_ROWS


def text_image(codes: np.ndarray, font: Font, charset: str, colors: np.ndarray | int,
               background: int) -> np.ndarray:
    """(200, 320, 3) BGR: the text area at the VIC's own resolution."""
    codes = np.asarray(codes, np.uint8).reshape(SCREEN_BYTES)
    bits = font.bits[charset_index(charset)][codes]                          # (1000, 8, 8)
    bits = bits.reshape(SCREEN_ROWS, SCREEN_COLUMNS, GLYPH, GLYPH).transpose(0, 2, 1, 3)
    bits = bits.reshape(TEXT_H, TEXT_W)
    fg_idx = np.broadcast_to(np.asarray(colors, np.uint8), (SCREEN_BYTES,)) & 0x0F
    fg = PALETTE_BGR[fg_idx].reshape(SCREEN_ROWS, SCREEN_COLUMNS, 3)
    fg = np.repeat(np.repeat(fg, GLYPH, axis=0), GLYPH, axis=1)             # (200, 320, 3)
    bg = PALETTE_BGR[background & 0x0F]
    return np.where(bits[..., None], fg, bg).astype(np.uint8)


def render_frame(codes: np.ndarray, font: Font, charset: str, *, colors: np.ndarray | int = LIGHT_GREY,
                 background: int = BLACK, border: int = DARK_GREY, geometry: Geometry = Geometry(),
                 blur: float = 1.2, noise: float = 2.5, seed: int = 0) -> np.ndarray:
    """A capture-like BGR frame of the screen: placed, scaled, blurred, noisy."""
    text = text_image(codes, font, charset, colors, background)
    w, h = geometry.size
    m = np.array([[geometry.width / TEXT_W, 0.0, geometry.x0], [0.0, geometry.height / TEXT_H, geometry.y0]])
    border_bgr = tuple(int(v) for v in PALETTE_BGR[border & 0x0F])
    frame = cv2.warpAffine(text, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                           borderValue=border_bgr)
    out = frame.astype(np.float32)
    if blur > 0:
        out = cv2.GaussianBlur(out, (0, 0), blur)
    if noise > 0:
        out += np.random.default_rng(seed).normal(0.0, noise, out.shape).astype(np.float32)
    return np.clip(out, 0, 255).round().astype(np.uint8)


def expected_map(codes: np.ndarray, bank: GlyphBank, charset: str, edge_level: float = 0.0) -> np.ndarray:
    """(408, 648) float32: the padded normalized foreground map a screen of
    these codes should produce at unit brightness, spill included."""
    return composite(bank, codes, 1.0, charset, edge_level)
