"""What every screen code looks like, before and after the capture chain.

A ``Font`` is the 8x8 bit pattern of each screen code in both ROM character
sets; codes 128-255 are the inverted copies of 0-127, as in the character
ROM. A ``GlyphBank`` is what those codes look like in the normalized frame
(section 3 of the plan): one 24x24 foreground map per (charset, code), the
16x16 cell plus a 4-pixel margin all round, so a template records the
glyph *and* the blur it spills into its neighbors. It is bootstrapped from
a Font by upscaling and blurring, then refined by harvesting cells of the
sparse allcodes screens through the real pipeline, so the bank carries the
converter's and the dongle's softness instead of guessing it.

Decoding composites the neighbors' templates to predict what spilled into
a cell and subtracts it, so a blank cell next to a bar reads blank and a
bar at a cell's edge reads as its own. Matching is normalized correlation
on the cell core, blind to the text's brightness; the confidence is the gap
to the nearest *different* template, so the codes that share a glyph (space
and shifted space) do not read as uncertain.

The character ROM is Commodore's and is never bundled: ``load_chargen``
reads it from a path the rig names, and the self-tests use a synthetic font.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from treadmill.c64.screen import (
    CHARSET_LOWER_UPPER, CHARSET_UPPER_GRAPHICS, SCREEN_BYTES, SCREEN_COLUMNS, SCREEN_ROWS,
)

CELL = 16                                     # normalized cell size in pixels
PAD = 4                                       # margin around a cell that spill reaches
TCELL = CELL + 2 * PAD                        # template size
GLYPH = 8                                     # ROM glyph size
CODES = 256
CHARSETS = (CHARSET_UPPER_GRAPHICS, CHARSET_LOWER_UPPER)   # the ROM's order: $1000 then $1800
CHARGEN_BYTES = len(CHARSETS) * CODES * GLYPH
BOOTSTRAP_BLUR = 1.0                          # sigma, in normalized pixels, standing in for the analog chain
IDENTICAL = 1e-3                              # correlation above 1 - this: one glyph
DEGENERATE = 1e-3                             # a template core with no variation: blank or solid
SOLID_CV = 0.08                               # a cell whose interior varies less than this is a solid block
INTERIOR = 2                                  # pixels of margin left out of the interior tests
BLANK_CODE = 0x20
SOLID_CODE = 0xA0
HARVEST_BLANK = 0.1                           # a harvested core peaking below this (light grey is ~0.58) is blank
_INVERT_AT = 128
CORE = (slice(PAD, PAD + CELL), slice(PAD, PAD + CELL))


class GlyphError(Exception):
    pass


def charset_index(name: str) -> int:
    """A ``custom`` charset decodes as upper/graphics, as ``code_as_text`` does."""
    return 1 if name == CHARSET_LOWER_UPPER else 0


@dataclass(frozen=True)
class Font:
    bits: np.ndarray            # (2, 256, 8, 8) bool
    name: str

    def __post_init__(self):
        if self.bits.shape != (len(CHARSETS), CODES, GLYPH, GLYPH):
            raise GlyphError(f"a font is (2, 256, 8, 8), not {self.bits.shape}")


def load_chargen(path: Path | str) -> Font:
    """The 4 KB character ROM (characters.901225-01.bin): upper/graphics,
    then lower/upper, 8 bytes per code, bit 7 the leftmost pixel."""
    data = Path(path).read_bytes()
    if len(data) != CHARGEN_BYTES:
        raise GlyphError(f"{path} is {len(data)} bytes, not the {CHARGEN_BYTES} of a character ROM")
    rows = np.frombuffer(data, dtype=np.uint8).reshape(len(CHARSETS), CODES, GLYPH)
    bits = np.unpackbits(rows[..., None], axis=-1).astype(bool)   # (2, 256, 8, 8), bit 7 first
    return Font(bits, Path(path).name)


def synthetic_font(seed: int = 7, density: float = 0.45) -> Font:
    """A deterministic stand-in for the ROM with the ROM's structure: a blank
    space, inverted copies above 127, and two sets that differ."""
    rng = np.random.default_rng(seed)
    bits = rng.random((len(CHARSETS), _INVERT_AT, GLYPH, GLYPH)) < density
    bits[:, 0x20] = False
    bits = np.concatenate([bits, ~bits], axis=1)
    return Font(bits, f"synthetic-{seed}")


def _peak_normalize(templates: np.ndarray) -> np.ndarray:
    """Scale each template so its core peaks at 1 (a blank stays zero)."""
    core = templates[..., PAD:PAD + CELL, PAD:PAD + CELL]
    peak = core.reshape(*core.shape[:-2], -1).max(axis=-1)[..., None, None]
    return np.where(peak > 0, templates / np.maximum(peak, 1e-6), 0.0).astype(np.float32)


@dataclass
class GlyphBank:
    templates: np.ndarray       # (2, 256, 24, 24) float32, each core scaled to a peak of 1
    counts: np.ndarray          # (2, 256) int32: harvested samples behind each template, 0 = bootstrap
    capture_class: str
    font: str
    created: str = ""
    _flat: np.ndarray | None = field(default=None, repr=False, compare=False)
    _same: np.ndarray | None = field(default=None, repr=False, compare=False)
    _canonical: np.ndarray | None = field(default=None, repr=False, compare=False)
    _degenerate: np.ndarray | None = field(default=None, repr=False, compare=False)
    _solid: np.ndarray | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.templates.shape != (len(CHARSETS), CODES, TCELL, TCELL):
            raise GlyphError(f"a glyph bank is (2, 256, {TCELL}, {TCELL}), not {self.templates.shape}")
        self.templates = self.templates.astype(np.float32, copy=False)
        self.counts = np.asarray(self.counts, np.int32)
        self._invalidate()

    def _invalidate(self) -> None:
        self._flat = self._same = self._canonical = self._degenerate = self._solid = None

    @property
    def cores(self) -> np.ndarray:
        """(512, 256): every template's core as a row, charset-major."""
        return np.ascontiguousarray(self.templates[:, :, PAD:PAD + CELL, PAD:PAD + CELL].reshape(-1, CELL * CELL))

    @property
    def flat(self) -> np.ndarray:
        """(512, 256): every core as a zero-mean, unit-norm row (zero for a
        degenerate one), for correlation."""
        if self._flat is None:
            raw = self.cores
            centered = raw - raw.mean(axis=1, keepdims=True)
            norm = np.linalg.norm(centered, axis=1, keepdims=True)
            self._degenerate = (norm[:, 0] < DEGENERATE) | (raw.max(axis=1) <= 0)
            self._solid = self._degenerate & (raw.mean(axis=1) > 0.5)
            self._flat = np.where(self._degenerate[:, None], 0.0,
                                  centered / np.maximum(norm, 1e-6)).astype(np.float32)
        return self._flat

    def _groups(self) -> tuple[np.ndarray, np.ndarray]:
        """Which templates are the same glyph, and the lowest index of each group."""
        if self._same is None:
            t = self.flat
            same = (t @ t.T) > 1.0 - IDENTICAL
            same |= (self._degenerate[:, None] & self._degenerate[None, :]
                     & (self._solid[:, None] == self._solid[None, :]))
            # Within one character set only: the ROM draws A-Z at 1-26 in
            # upper/graphics and at 65-90 in lower/upper, and a group that
            # spanned both sets would hand lower/upper text the other set's code.
            block = np.arange(t.shape[0]) // CODES
            same &= block[:, None] == block[None, :]
            self._same = same
            self._canonical = np.argmax(same, axis=1)      # first True = lowest index in the group
        return self._same, self._canonical

    def harvested(self) -> int:
        return int((self.counts > 0).sum())

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        created = self.created or time.strftime("%Y-%m-%dT%H:%M:%S")
        np.savez_compressed(path, templates=self.templates, counts=self.counts,
                            meta=json.dumps({"capture_class": self.capture_class, "font": self.font,
                                             "created": created, "pad": PAD}))
        return path

    @classmethod
    def load(cls, path: Path | str) -> "GlyphBank":
        try:
            with np.load(Path(path)) as z:
                meta = json.loads(str(z["meta"]))
                if meta.get("pad", 0) != PAD:
                    raise GlyphError(f"{path} was harvested with a {meta.get('pad', 0)}-pixel margin, not {PAD}")
                return cls(z["templates"], z["counts"], meta["capture_class"], meta["font"],
                           meta.get("created", ""))
        except (OSError, KeyError, ValueError) as e:
            raise GlyphError(f"cannot load glyph bank {path}: {e}") from e


def empty_bank(capture_class: str, font: str = "harvest") -> GlyphBank:
    """A bank with nothing in it, to harvest into without a character ROM."""
    return GlyphBank(np.zeros((len(CHARSETS), CODES, TCELL, TCELL), np.float32),
                     np.zeros((len(CHARSETS), CODES), np.int32), capture_class, font)


def bootstrap_bank(font: Font, capture_class: str, blur: float = BOOTSTRAP_BLUR) -> GlyphBank:
    """Each glyph upscaled 2x, given its margin and blurred: a first guess at
    what the capture chain makes of it, good enough to harvest the real
    thing with."""
    big = np.kron(font.bits.astype(np.float32), np.ones((CELL // GLYPH, CELL // GLYPH), np.float32))
    padded = np.pad(big, ((0, 0), (0, 0), (PAD, PAD), (PAD, PAD)))
    flat = padded.reshape(-1, TCELL, TCELL)
    if blur > 0:
        flat = np.stack([cv2.GaussianBlur(g, (0, 0), blur) for g in flat])
    templates = _peak_normalize(flat.reshape(len(CHARSETS), CODES, TCELL, TCELL))
    return GlyphBank(templates, np.zeros((len(CHARSETS), CODES), np.int32), capture_class, font.name)


def harvest(bank: GlyphBank, cells: np.ndarray, codes: np.ndarray, charset: str) -> int:
    """Fold observed padded cells (N, 24, 24), each known to show ``codes[i]``
    in ``charset`` with blank neighbors, into the bank's templates as a
    running mean. Returns how many distinct codes were updated."""
    cells = np.asarray(cells, np.float32)
    codes = np.asarray(codes)
    if cells.ndim != 3 or cells.shape[1:] != (TCELL, TCELL) or cells.shape[0] != codes.shape[0]:
        raise GlyphError(f"harvest needs (N, {TCELL}, {TCELL}) padded cells and N codes")
    s = charset_index(charset)
    updated = 0
    for code in np.unique(codes):
        sample = cells[codes == code]
        n = sample.shape[0]
        fresh = sample.mean(axis=0)
        if fresh[CORE].max() < HARVEST_BLANK:
            fresh = np.zeros_like(fresh)        # a code that shows nothing (space) stays nothing, not scaled noise
        fresh = _peak_normalize(fresh[None])[0]
        old_n = int(bank.counts[s, code])
        # Templates are peak-normalized, so the running mean is over normalized
        # contributions: a harvest in a dimmer color weighs the same as a brighter one.
        mean = (bank.templates[s, code] * old_n + fresh * n) / (old_n + n) if old_n else fresh
        bank.templates[s, code] = _peak_normalize(mean[None])[0]
        bank.counts[s, code] = old_n + n
        updated += 1
    bank._invalidate()
    return updated


@dataclass(frozen=True)
class Match:
    codes: np.ndarray           # (N,) uint8, reverse bit included
    confidence: np.ndarray      # (N,) float32 in 0..1
    charset: str
    distance: np.ndarray        # (N,) float32: 1 - correlation with the chosen template
    charset_scores: dict[str, float]   # total distance per charset, lower is better
    amplitude: np.ndarray       # (N,) float32: the cell's level in template units (0 for blank)


def match(bank: GlyphBank, cells: np.ndarray, charset: str | None = None, noise_floor: float = 0.0,
          blank: np.ndarray | None = None) -> Match:
    """Nearest template per cell core by normalized correlation, which is
    blind to the text's brightness. A cell with nothing above the noise
    floor (or flagged ``blank``) is a space; one whose interior does not
    vary is a solid block (reverse space); the rest correlate against the
    non-degenerate templates. With no charset given, the set that fits the
    whole screen better wins, which is how a plain-TR station tells
    lower/upper from upper/graphics."""
    cells = np.asarray(cells, np.float32)
    n = cells.shape[0]
    if cells.shape[1:] != (CELL, CELL):
        raise GlyphError(f"match needs (N, {CELL}, {CELL}) cell cores")
    x = cells.reshape(n, CELL * CELL)
    peak = x.max(axis=1)
    is_blank = peak < noise_floor if blank is None else (np.asarray(blank, bool) | (peak <= 0))
    inner = cells[:, INTERIOR:CELL - INTERIOR, INTERIOR:CELL - INTERIOR].reshape(n, -1)
    inner_mean = inner.mean(axis=1)
    solid = (~is_blank & (inner.std(axis=1) < SOLID_CV * np.maximum(inner_mean, 1e-6))
             & (inner_mean > 0.5 * peak))
    centered = x - x.mean(axis=1, keepdims=True)
    xn = centered / np.maximum(np.linalg.norm(centered, axis=1, keepdims=True), 1e-6)
    t = bank.flat
    corr = xn @ t.T                                   # (N, 512), degenerate templates score 0
    corr[:, bank._degenerate] = -1.0
    same, canonical = bank._groups()
    per_set = corr.reshape(n, len(CHARSETS), CODES)
    usable = ~(is_blank | solid)
    scores = {name: float((1.0 - per_set[usable, i].max(axis=1)).sum()) if usable.any() else 0.0
              for i, name in enumerate(CHARSETS)}
    chosen = charset if charset is not None else min(scores, key=scores.get)
    s = charset_index(chosen)
    offset = s * CODES
    cs = per_set[:, s]
    best = cs.argmax(axis=1)
    best_c = cs[np.arange(n), best]
    blocked = same[best + offset][:, offset:offset + CODES]
    second_c = np.where(blocked, -1.0, cs).max(axis=1)
    conf = np.clip((best_c - second_c) / np.maximum(1.0 - second_c, 1e-6), 0.0, 1.0)
    codes = (canonical[best + offset] - offset).astype(np.uint8)
    codes = np.where(is_blank, BLANK_CODE, np.where(solid, SOLID_CODE, codes)).astype(np.uint8)
    conf = np.where(is_blank | solid, 1.0, conf).astype(np.float32)
    distance = np.where(is_blank | solid, 0.0, 1.0 - best_c).astype(np.float32)
    # The cell's level in template units: the least-squares amplitude of its template.
    cores = bank.cores[codes.astype(np.int32) + offset]
    amp = (x * cores).sum(axis=1) / np.maximum((cores * cores).sum(axis=1), 1e-6)
    amp = np.where(is_blank, 0.0, np.where(solid, inner_mean, np.maximum(amp, 0.0))).astype(np.float32)
    return Match(codes, conf, chosen, distance, scores, amp)


def screen_match(bank: GlyphBank, cells: np.ndarray, charset: str | None = None, **kw) -> Match:
    if cells.shape[0] != SCREEN_BYTES:
        raise GlyphError(f"a screen is {SCREEN_BYTES} cells, not {cells.shape[0]}")
    return match(bank, cells, charset, **kw)


def composite(bank: GlyphBank, codes: np.ndarray, amplitude: np.ndarray | float, charset: str,
              edge_level: float = 0.0) -> np.ndarray:
    """The padded normalized foreground map a screen of these codes at these
    levels should produce: every template placed at its cell, overlapping
    margins added, and the border drawn around the text area as a solid
    block at ``edge_level`` so its spill into the edge cells is predicted too."""
    codes = np.asarray(codes, np.uint8).reshape(SCREEN_BYTES)
    amp = np.broadcast_to(np.asarray(amplitude, np.float32), (SCREEN_BYTES,))
    s = charset_index(charset)
    h, w = SCREEN_ROWS * CELL + 2 * PAD, SCREEN_COLUMNS * CELL + 2 * PAD
    canvas = np.zeros((h, w), np.float32)
    templates = bank.templates[s]
    for i in range(SCREEN_BYTES):
        if amp[i] <= 0:
            continue
        r, c = divmod(i, SCREEN_COLUMNS)
        y, x = r * CELL, c * CELL
        canvas[y:y + TCELL, x:x + TCELL] += amp[i] * templates[codes[i]]
    if edge_level > 0:
        solid = templates[SOLID_CODE] if bank.counts[s, SOLID_CODE] or templates[SOLID_CODE].max() > 0 \
            else bank.templates[1 - s][SOLID_CODE]
        level = float(solid[CORE].mean())
        block = (edge_level / max(level, 1e-6)) * solid
        for r in range(-1, SCREEN_ROWS + 1):
            for c in range(-1, SCREEN_COLUMNS + 1):
                if 0 <= r < SCREEN_ROWS and 0 <= c < SCREEN_COLUMNS:
                    continue
                _add_clipped(canvas, block, r * CELL, c * CELL)
    return canvas


def _add_clipped(canvas: np.ndarray, block: np.ndarray, y: int, x: int) -> None:
    h, w = canvas.shape
    bh, bw = block.shape
    y0, x0 = max(y, 0), max(x, 0)
    y1, x1 = min(y + bh, h), min(x + bw, w)
    if y1 <= y0 or x1 <= x0:
        return
    canvas[y0:y1, x0:x1] += block[y0 - y:y1 - y, x0 - x:x1 - x]


def spill(bank: GlyphBank, codes: np.ndarray, amplitude: np.ndarray, charset: str,
          edge_level: float = 0.0) -> np.ndarray:
    """(1000, 16, 16): what the neighbors (and the border) are predicted to
    have spilled into each cell's core: the composite minus the cell's own
    template."""
    canvas = composite(bank, codes, amplitude, charset, edge_level)
    total = canvas[PAD:-PAD, PAD:-PAD].reshape(SCREEN_ROWS, CELL, SCREEN_COLUMNS, CELL).transpose(0, 2, 1, 3)
    total = total.reshape(SCREEN_BYTES, CELL, CELL)
    s = charset_index(charset)
    own = bank.templates[s][np.asarray(codes, np.uint8)][:, PAD:PAD + CELL, PAD:PAD + CELL]
    own = own * np.asarray(amplitude, np.float32)[:, None, None]
    return np.maximum(total - own, 0.0).astype(np.float32)
