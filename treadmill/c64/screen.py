"""The VIC-II text screen: where it is in memory, and its screen codes as text.

The code-to-text table is the one the TeensyROM MCP server uses
(``tools/mcp/lib/screen.mjs``), reverse video and symbols included, so a
screen read by DMA, by video or by that server is the same text, and a
``read_screen`` result from a live board can be saved as a golden unchanged.
"""

from __future__ import annotations

# Memory locations (C64 unless named otherwise).
SCREEN_RAM_DEFAULT = 0x0400
COLOR_RAM = 0xD800
VIC_CTRL1 = 0xD011          # bit 4 is DEN, the screen-on bit
VIC_MEMPTR = 0xD018
VIC_BORDER = 0xD020
VIC_BACKGROUND = 0xD021
VIC_RASTER = 0xD012
CIA2_PORT_A = 0xDD00
VIC_DEN = 0x10

KEYBUF, KEYCOUNT = 0x0277, 0x00C6          # KERNAL keyboard buffer, and the count GETIN reads
KEYBUF_128, KEYCOUNT_128 = 0x034A, 0x00D0  # the same pair in C128 mode
KEYBUF_SIZE = 10
F1, F3, F5, F7, F8 = 0x85, 0x86, 0x87, 0x88, 0x8C

SCREEN_COLUMNS = 40
SCREEN_ROWS = 25
SCREEN_BYTES = SCREEN_COLUMNS * SCREEN_ROWS

_VIC_BANK_BYTES = 0x4000
_SCREEN_STEP = 0x400
_CHARSET_STEP = 0x800
# The VIC sees the character ROM, not RAM, at these offsets of banks 0 and 2.
_ROM_UPPER_GRAPHICS = 0x1000
_ROM_LOWER_UPPER = 0x1800
_ROM_BANKS = (0, 2)

CHARSET_LOWER_UPPER = "lower_upper"       # $D018 = $17: where the TeensyROM menu puts the C64
CHARSET_UPPER_GRAPHICS = "upper_graphics"  # the power-on default a launched program may return to
CHARSET_CUSTOM = "custom"                  # a RAM charset: decoded as upper_graphics

REVERSE_VIDEO = 0x80
_CODE_AT = 0
_CODE_A, _CODE_Z = 1, 26
_CODE_SHIFT_A, _CODE_SHIFT_Z = 65, 90
_SYMBOLS_AFTER_Z = "[£]↑←"
_PUNCTUATION_FIRST, _PUNCTUATION_LAST = 0x20, 0x3F
NO_LETTER = "."


def vic_bank(cia2_port_a: int) -> int:
    return 3 - (cia2_port_a & 3)


def screen_address(memptr: int, cia2_port_a: int) -> int:
    return vic_bank(cia2_port_a) * _VIC_BANK_BYTES + ((memptr >> 4) & 0xF) * _SCREEN_STEP


def charset_in(memptr: int, cia2_port_a: int) -> str:
    offset = ((memptr >> 1) & 7) * _CHARSET_STEP
    if vic_bank(cia2_port_a) not in _ROM_BANKS:
        return CHARSET_CUSTOM
    if offset == _ROM_LOWER_UPPER:
        return CHARSET_LOWER_UPPER
    if offset == _ROM_UPPER_GRAPHICS:
        return CHARSET_UPPER_GRAPHICS
    return CHARSET_CUSTOM


def code_as_text(screen_code: int, charset: str = CHARSET_LOWER_UPPER) -> str:
    """One screen code as a character. Codes 1-26 and 65-90 are different
    letters in the two ROM sets; anything with no letter in the set (graphics,
    line drawing) reads as '.'."""
    code = screen_code & ~REVERSE_VIDEO
    lower_upper = charset == CHARSET_LOWER_UPPER
    if code == _CODE_AT:
        return "@"
    if _CODE_A <= code <= _CODE_Z:
        return chr(code - _CODE_A + (0x61 if lower_upper else 0x41))
    if _CODE_Z < code < _PUNCTUATION_FIRST:
        return _SYMBOLS_AFTER_Z[code - _CODE_Z - 1]
    if lower_upper and _CODE_SHIFT_A <= code <= _CODE_SHIFT_Z:
        return chr(code - _CODE_SHIFT_A + 0x41)
    if _PUNCTUATION_FIRST <= code <= _PUNCTUATION_LAST:
        return chr(code)
    return NO_LETTER


def codes_for_text(text: str, charset: str = CHARSET_LOWER_UPPER) -> bytes:
    """Screen codes for a line of text: the inverse of ``code_as_text`` for
    what it can show; anything else becomes a space."""
    lower_upper = charset == CHARSET_LOWER_UPPER
    out = bytearray()
    for ch in text:
        if ch == "@":
            out.append(_CODE_AT)
        elif "a" <= ch <= "z" and lower_upper:
            out.append(ord(ch) - 0x61 + _CODE_A)
        elif "A" <= ch <= "Z":
            out.append(ord(ch) - 0x41 + (_CODE_SHIFT_A if lower_upper else _CODE_A))
        elif ch in _SYMBOLS_AFTER_Z:
            out.append(_CODE_Z + 1 + _SYMBOLS_AFTER_Z.index(ch))
        elif _PUNCTUATION_FIRST <= ord(ch) <= _PUNCTUATION_LAST:
            out.append(ord(ch))
        else:
            out.append(_PUNCTUATION_FIRST)
    return bytes(out)


def screen_rows(screen: bytes, charset: str = CHARSET_LOWER_UPPER) -> list[str]:
    """The 25 rows of a 1000-byte screen as text."""
    return ["".join(code_as_text(c, charset)
                    for c in screen[r * SCREEN_COLUMNS:(r + 1) * SCREEN_COLUMNS])
            for r in range(SCREEN_ROWS)]
