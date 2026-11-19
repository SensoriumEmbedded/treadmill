"""Key codes as the KERNAL hands them to GETIN, and the ways a key reaches
a program.

The TeensyROM menu reads keys through GETIN, so a code written into the
KERNAL keyboard buffer by DMA is a key press to it; a program that scans
the keyboard matrix itself never sees one. The keyboard board presses real
keys on the matrix, which reach any program, and is the other backend of
``Keys``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

from treadmill.errors import TreadmillError

if TYPE_CHECKING:
    from treadmill.station.kbd import KeyboardBoard

F1, F2, F3, F4, F5, F6, F7, F8 = 0x85, 0x89, 0x86, 0x8A, 0x87, 0x8B, 0x88, 0x8C
RETURN, SHIFT_RETURN = 0x0D, 0x8D
SPACE = 0x20
CRSR_UP, CRSR_DOWN, CRSR_LEFT, CRSR_RIGHT = 0x91, 0x11, 0x9D, 0x1D
HOME, CLEAR = 0x13, 0x93
UP_ARROW, LEFT_ARROW = 0x5E, 0x5F
DELETE, INSERT = 0x14, 0x94
STOP = 0x03

DMA_WAIT_S = 2.0        # longer than the C128's post-reset hold-off, the only wait a key should see


class Matrix:
    """Keys by matrix position for the keyboard board's holds (``kbd.hold``):
    the CIA1 port A bit, then the port B bit. RESTORE is not in the matrix;
    it is ``kbd.restore()``."""
    RETURN = "01"
    LSHIFT = "17"
    RSHIFT = "64"
    CTRL = "72"
    SPACE = "74"
    CBM = "75"
    RUN_STOP = "77"


# The keyboard board's typed text for the GETIN codes that are not their
# own character; see typed_text().
_NAMED = {
    F1: "{f1}", F2: "{f2}", F3: "{f3}", F4: "{f4}", F5: "{f5}", F6: "{f6}", F7: "{f7}", F8: "{f8}",
    RETURN: "{return}", SHIFT_RETURN: "{shift-return}",
    CRSR_UP: "{up}", CRSR_DOWN: "{down}", CRSR_LEFT: "{left}", CRSR_RIGHT: "{right}",
    HOME: "{home}", CLEAR: "{clr}", DELETE: "{del}", INSERT: "{inst}", STOP: "{stop}",
    UP_ARROW: "^", LEFT_ARROW: "_", 0x5C: "\\",      # ↑, ← and £ as the protocol spells them
}


class KeysUnavailable(TreadmillError):
    pass


class Keys(Protocol):
    def press(self, codes: bytes | list[int]) -> None: ...


class KernalKeys:
    """Keys through the KERNAL buffer by DMA: a TR+ main image, outside the
    board's DMA hold-off (a C128 just reset)."""

    def __init__(self, board):
        self.board = board

    def press(self, codes: bytes | list[int]) -> None:
        deadline = time.monotonic() + DMA_WAIT_S
        while not self.board.dma_allowed():
            if time.monotonic() > deadline:
                raise KeysUnavailable("DMA is not allowed now, so no key can be written")
            time.sleep(0.05)
        self.board.link.keys(bytes(codes))


def typed_text(codes: bytes | list[int]) -> str:
    """GETIN codes as the keyboard board's ``S`` text: each one the key, or
    SHIFT and the key, that gives that code in the C64's default character
    set. A letter is ``$41-$5A`` plain and ``$C1-$DA`` with SHIFT, which the
    protocol writes in lower and upper case."""
    out = []
    for c in codes:
        if c in _NAMED:
            out.append(_NAMED[c])
        elif 0x41 <= c <= 0x5A:
            out.append(chr(c).lower())
        elif 0xC1 <= c <= 0xDA:
            out.append(chr(c - 0x80))
        elif 0x20 <= c <= 0x40 or c in (0x5B, 0x5D):     # space, digits, symbols and their SHIFT forms
            out.append(chr(c))
        else:
            raise KeysUnavailable(f"GETIN code ${c:02X} has no key in the keyboard board's typed text")
    return "".join(out)


class HwKeys:
    """Keys pressed on the C64's matrix by the keyboard board: any program,
    any TR, about 10 keys a second (7 with SHIFT). One ``S`` per ``press()``,
    which returns once the last key is up."""

    def __init__(self, kbd: KeyboardBoard):
        self.kbd = kbd

    def press(self, codes: bytes | list[int]) -> None:
        self.kbd.type(typed_text(codes))
