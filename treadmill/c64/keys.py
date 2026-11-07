"""Key codes as the KERNAL hands them to GETIN, and the ways a key reaches
a program.

The TeensyROM menu reads keys through GETIN, so a code written into the
KERNAL keyboard buffer by DMA is a key press to it; a program that scans
the keyboard matrix itself never sees one. The keyboard board of Phase 2
presses real keys and is the other backend of ``Keys``.
"""

from __future__ import annotations

import time
from typing import Protocol

from treadmill.errors import TreadmillError

F1, F2, F3, F4, F5, F6, F7, F8 = 0x85, 0x89, 0x86, 0x8A, 0x87, 0x8B, 0x88, 0x8C
RETURN, SHIFT_RETURN = 0x0D, 0x8D
SPACE = 0x20
CRSR_UP, CRSR_DOWN, CRSR_LEFT, CRSR_RIGHT = 0x91, 0x11, 0x9D, 0x1D
HOME, CLEAR = 0x13, 0x93
UP_ARROW, LEFT_ARROW = 0x5E, 0x5F
DELETE, INSERT = 0x14, 0x94
STOP = 0x03

DMA_WAIT_S = 2.0        # longer than the C128's post-reset hold-off, the only wait a key should see


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
