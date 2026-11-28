"""Synthetic screens and frames shared by the observe self-tests and the
fake-board video path: a synthetic font, a menu-like screen, a capture-like
frame, and a glyph bank harvested from rendered allcodes screens."""

from __future__ import annotations

import time

import numpy as np

from treadmill.c64.screen import CHARSET_LOWER_UPPER, SCREEN_BYTES, SCREEN_COLUMNS
from treadmill.observe.calibrate import LAYOUT_MENU, Calibration, find_text_area
from treadmill.observe.glyphs import GlyphBank, bootstrap_bank, harvest, synthetic_font
from treadmill.observe.normalize import distance_map, padded_cells
from treadmill.observe.render import DARK_GREY, LIGHT_GREY, MEDIUM_GREY, Geometry, render_frame

FONT = synthetic_font(density=0.3)
GEOMETRY = Geometry()
REVERSE_SPACE = 0xA0
SPACE = 0x20


def menu_like_screen(seed: int = 1, blank_fraction: float = 0.55) -> tuple[np.ndarray, np.ndarray]:
    """Random codes with a reverse bar on row 0 in the border's color, as the
    TeensyROM menu has; returns (codes, colors)."""
    rng = np.random.default_rng(seed)
    codes = rng.integers(0, 256, SCREEN_BYTES).astype(np.uint8)
    codes[rng.random(SCREEN_BYTES) < blank_fraction] = SPACE
    codes[:SCREEN_COLUMNS] = REVERSE_SPACE
    colors = np.full(SCREEN_BYTES, LIGHT_GREY, np.uint8)
    colors[:SCREEN_COLUMNS] = DARK_GREY
    colors[SCREEN_COLUMNS:2 * SCREEN_COLUMNS] = MEDIUM_GREY
    return codes, colors


def text_screen(lines: dict[int, str], charset: str = CHARSET_LOWER_UPPER) -> np.ndarray:
    """Codes for a screen with ASCII text on the given rows (letters a-z only)."""
    codes = np.full(SCREEN_BYTES, SPACE, np.uint8)
    for row, text in lines.items():
        for col, ch in enumerate(text[:SCREEN_COLUMNS]):
            if "a" <= ch <= "z":
                code = ord(ch) - ord("a") + 1
            elif ch == " ":
                code = SPACE
            elif "0" <= ch <= "9":
                code = 0x30 + ord(ch) - ord("0")
            else:
                code = ord(ch) & 0x3F
            codes[row * SCREEN_COLUMNS + col] = code
    return codes


def frame_of(codes: np.ndarray, colors=LIGHT_GREY, charset: str = CHARSET_LOWER_UPPER,
             geometry: Geometry = GEOMETRY, seed: int = 0, **kw) -> np.ndarray:
    return render_frame(codes, FONT, charset, colors=colors, geometry=geometry, seed=seed, **kw)


def allcodes_screens() -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """The sparse allcodes pages as (codes of the screen, cell indices, codes
    at those cells), the layout allcodes.a draws."""
    from treadmill.programs.build import sparse_pages
    pages = []
    for cells, shown in sparse_pages():
        codes = np.full(SCREEN_BYTES, SPACE, np.uint8)
        codes[cells] = shown
        pages.append((codes, np.asarray(cells), np.asarray(shown, np.uint8)))
    return pages


FRAME_S = 1 / 60


def fake_frame_source(fake):
    """Frames of the fake board's screen RAM, as its capture would show them:
    the fake's VIC registers pick the charset, each frame gets fresh noise."""
    import itertools
    from treadmill.c64.screen import CHARSET_CUSTOM, CHARSET_UPPER_GRAPHICS, VIC_MEMPTR, CIA2_PORT_A, SCREEN_BYTES
    from treadmill.c64.screen import charset_in, screen_address
    from treadmill.observe.capture import SyntheticSource
    seeds = itertools.count(1000)
    last = [0.0]

    def render() -> np.ndarray:
        # A capture delivers 60 frames a second: a reader polling for the next one waits for it,
        # rather than rendering as fast as the CPU goes beside the fake board's own thread.
        wait = last[0] + FRAME_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        last[0] = time.monotonic()
        fake.show_counters()                     # idread with SHOW and krread redraw their top row every pass
        if fake.heartbeat:                       # hello.a ticks its bottom-right cell once a frame
            fake.memory[0x07E7] = (fake.memory[0x07E7] + 1) & 0xFF
        memptr, cia2 = fake.memory[VIC_MEMPTR], fake.memory[CIA2_PORT_A]
        address = screen_address(memptr, cia2)
        if fake.game and fake.ultimax_since is not None:     # a game: one cell moves on its own until a hang
            if not fake.vic_outside() > fake.faults.get("vic_hang_ns", 10 ** 6):
                fake.memory[0x0400 + 12 * 40 + 20] = int((time.monotonic() - fake.ultimax_since) * 120) & 0xFF
        codes = np.frombuffer(bytes(fake.memory[address:address + SCREEN_BYTES]), np.uint8)
        seed = next(seeds)
        glitches, sprites = fake.vic_glitches(), fake.sprite_glitches()
        flicker = fake.faults.get("vic_flicker_cells", 0) if fake.ultimax_since is not None else 0
        if glitches or sprites or flicker:
            rng = np.random.default_rng(seed)
            codes = codes.copy()
            if glitches:                         # a DMA drive in the VIC's half: stray cells, new each frame
                codes[rng.integers(0, SCREEN_BYTES, glitches)] = rng.integers(0, 256, glitches)
            if sprites:                          # vicrom-high's sprites fetched wrong: cells of their box
                from selftest.fake_board import SPRITE_CELLS
                at = rng.choice(SPRITE_CELLS, sprites, replace=False)
                codes[at] = rng.integers(0, 256, sprites)
            if flicker:                          # cells that change every frame on their own
                from selftest.fake_board import FLICKER_CELLS
                at = np.asarray(FLICKER_CELLS[:flicker])
                codes[at] = codes[at] ^ (0x55 if seed % 2 else 0)
        charset = charset_in(memptr, cia2)
        if charset == CHARSET_CUSTOM:
            charset = CHARSET_UPPER_GRAPHICS
        if fake.faults.get("black_capture"):     # a capture that shows nothing: no signal, or the wrong input
            return np.zeros_like(frame_of(codes, LIGHT_GREY, charset, seed=seed))
        if fake.ultimax_since is not None:      # vicrom.a steps the border once a frame
            border = int((time.monotonic() - fake.ultimax_since) * 60) % 16
            return frame_of(codes, LIGHT_GREY, charset, seed=seed, border=border)
        look = getattr(fake.program, "look", None)          # a program drawing in its own colors
        if look is not None:
            colors, background, border = look()
            return frame_of(codes, np.frombuffer(colors, np.uint8), charset, seed=seed,
                            background=background, border=border)
        return frame_of(codes, LIGHT_GREY, charset, seed=seed)
    return SyntheticSource(render)


def calibrated(geometry: Geometry = GEOMETRY, seed: int = 1) -> Calibration:
    codes, colors = menu_like_screen(seed)
    return find_text_area(frame_of(codes, colors, geometry=geometry, seed=seed), LAYOUT_MENU)


def harvested_bank(calib: Calibration, geometry: Geometry = GEOMETRY, frames: int = 3,
                   charsets=(CHARSET_LOWER_UPPER,)) -> GlyphBank:
    """A bank bootstrapped from the synthetic font and harvested from
    rendered allcodes screens, as a rig does with the real program."""
    bank = bootstrap_bank(FONT, "synthetic")
    for charset in charsets:
        for codes, cells, shown in allcodes_screens():
            for s in range(frames):
                frame = frame_of(codes, LIGHT_GREY, charset, geometry, seed=100 + s)
                dmap = distance_map(calib.normalize(frame), calib.background)
                harvest(bank, padded_cells(dmap, cells), shown, charset)
    return bank
