"""ASID over the board's USB MIDI, and a check of what reached the C64.

The firmware's side is ``IOH_ASID.c`` (``ASIDOnSystemExclusive``,
``DecodeSendSIDRegData``): a SID1 register message names its registers by
bit in four 7-bit masks, carries each value's top bit in four more, and
queues one (type | SID register, value) pair per register in id order. The
C64 reads each pair from IO1 in its interrupt ($DEC2, then $DEC4).
``treadmill/programs/asidlog.a`` sits in front of the TeensyROM ASID
Player's interrupt and records every pair the C64 read in a ring that
:class:`PairLog` reads back by DMA; :func:`compare_pairs` lines that up
with what was sent.
"""

from __future__ import annotations

import contextlib
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterator, Sequence

from treadmill.errors import TreadmillError

ASID_ID = 0x2D
APT_SID1 = 0x4E
APT_WRITE_ORDER = 0x30
NUM_IDS = 28                      # register ids a message can name: 25 and 3 secondaries
TYPE_MASK = 0xE0
TYPE_SID1 = 0x80
TYPE_ERROR = 0xE0                 # the parser's error marker (AddErrorToASIDRxQueue)

# ASIDidToReg in IOH_ASID.c: the SID register each id writes.
ID_TO_REG = (0, 1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 17, 19, 20, 21, 22, 23, 24, 4, 11, 18,
             4, 11, 18)
# Ids 22-27 are the voice control registers: the player's voice mutes redirect
# them in the firmware, and their gate bits would sound notes. The test leaves
# them out.
FRAME_IDS = tuple(range(22))

# asidlog.a's layout and where the test writes it.
LOGGER = 0xC000
LOG_COUNT = LOGGER                # 3 bytes: pairs logged
LOG_ENTRY = LOGGER + 3            # what $0314 points at
LOG = 0x4000
LOG_END = 0xA000
# The player's interrupt handler starts with the two reads the logger takes over.
PLAYER_IRQ_HEAD = bytes([0xAE, 0xC2, 0xDE, 0xAC, 0xC4, 0xDE])     # ldx $DEC2 / ldy $DEC4
IRQ_VECTOR = 0x0314

Pair = tuple[int, int]


class LogError(TreadmillError):
    pass


def frame_values(frame: int) -> list[int]:
    """The value each of FRAME_IDS gets in ``frame``: every register's value
    changes each frame, and all 8 bits change across frames."""
    return [(frame * 37 + i * 11 + 0x5A) & 0xFF for i in range(len(FRAME_IDS))]


def sid1_message(values: Sequence[int]) -> bytes:
    """A SID1 register message writing ``values`` to ids 0.. in order, F0 to F7."""
    if len(values) > NUM_IDS:
        raise ValueError(f"{len(values)} values: a message names at most {NUM_IDS} registers")
    masks, msbs, data = [0] * 4, [0] * 4, []
    for i, v in enumerate(values):
        masks[i // 7] |= 1 << (i % 7)
        if v & 0x80:
            msbs[i // 7] |= 1 << (i % 7)
        data.append(v & 0x7F)
    return bytes([0xF0, ASID_ID, APT_SID1, *masks, *msbs, *data, 0xF7])


def frame_message(frame: int) -> bytes:
    return sid1_message(frame_values(frame))


def frame_pairs(frame: int) -> list[Pair]:
    """The pairs the C64 reads for ``frame``'s message, in order."""
    return [(TYPE_SID1 | ID_TO_REG[i], v) for i, v in zip(FRAME_IDS, frame_values(frame))]


def write_order_message() -> bytes:
    """A write-order message that puts every id at its own position: the
    firmware keeps a forced order from any earlier client until the player
    disables it, and this one is the order the firmware uses with none."""
    body: list[int] = []
    for reg in range(NUM_IDS):
        body += [reg, 0]              # position = id, no wait cycles
    return bytes([0xF0, ASID_ID, APT_WRITE_ORDER, *body, 0xF7])


# -- the log ------------------------------------------------------------------

class PairLog:
    """asidlog.a's ring, read by DMA as it fills: ``poll()`` returns the
    pairs logged since the last poll. ``read(address, length)`` is the
    link's memory read."""

    def __init__(self, read: Callable[[int, int], bytes], *, log: int = LOG, end: int = LOG_END,
                 count_at: int = LOG_COUNT):
        self._read = read
        self._log, self._size, self._count_at = log, end - log, count_at
        self.capacity = self._size // 2
        self.consumed = 0

    def count(self) -> int:
        return int.from_bytes(self._read(self._count_at, 3), "little")

    def poll(self) -> list[Pair]:
        total = self.count()
        new = total - self.consumed
        if new < 0:
            raise LogError(f"the logger's count went back from {self.consumed} to {total}")
        # Pairs keep arriving while the read runs, so a poll this far behind
        # may already be reading pairs the ring has overwritten.
        if new > self.capacity * 3 // 4:
            raise LogError(f"{new} pairs since the last read: the ring holds {self.capacity}, read it more often")
        start = (self.consumed * 2) % self._size
        length = new * 2
        first = min(length, self._size - start)
        data = self._read(self._log + start, first) if first else b""
        if length > first:
            data += self._read(self._log, length - first)
        self.consumed = total
        return [(data[k], data[k + 1]) for k in range(0, len(data), 2)]


# -- the comparison -----------------------------------------------------------

@dataclass
class Comparison:
    expected: int                     # SID1 pairs sent
    received: int                     # SID1 pairs logged
    matched: int = 0
    garbled: int = 0                  # in place, with the wrong register or value
    missing: int = 0
    extra: int = 0                    # logged but never sent, or logged twice
    other: dict[int, int] = field(default_factory=dict)   # non-SID1 pairs logged, by type
    examples: list[str] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return self.garbled + self.missing + self.extra + self.other.get(TYPE_ERROR, 0)

    def describe(self) -> str:
        text = (f"{self.received} of {self.expected} pairs logged: {self.matched} intact, {self.garbled} garbled, "
                f"{self.missing} missing, {self.extra} extra")
        if self.other:
            text += "; other types " + ", ".join(f"${t:02X} x{n}" for t, n in sorted(self.other.items()))
        if self.examples:
            text += "; first: " + "; ".join(self.examples)
        return text

    def to_dict(self) -> dict:
        return {"expected": self.expected, "received": self.received, "matched": self.matched,
                "garbled": self.garbled, "missing": self.missing, "extra": self.extra,
                "other": {f"${t:02X}": n for t, n in sorted(self.other.items())},
                "errors": self.errors, "examples": self.examples}


RESYNC_WINDOW = 8                 # how far either side may run ahead before a slip counts as garbled
RESYNC_RUN = 3                    # pairs that must agree after a slip
MAX_EXAMPLES = 5


def _agree(got: Sequence[Pair], i: int, want: Sequence[Pair], j: int) -> bool:
    run = min(RESYNC_RUN, len(got) - i, len(want) - j)
    return run > 0 and all(got[i + k] == want[j + k] for k in range(run))


def _resync(got: Sequence[Pair], i: int, want: Sequence[Pair], j: int) -> tuple[int, int] | None:
    """The smallest step (logged, expected) after which the two agree again,
    a substitution (1, 1) first among steps of the same size."""
    for total in range(1, RESYNC_WINDOW + 1):
        steps = [(total // 2, total - total // 2)] if total % 2 == 0 else []
        steps += [(d, total - d) for d in range(total + 1) if (d, total - d) not in steps]
        for di, dj in steps:
            if _agree(got, i + di, want, j + dj):
                return di, dj
    return None


def compare_pairs(logged: Sequence[Pair], expected: Sequence[Pair]) -> Comparison:
    """Line the SID1 pairs the C64 read up with those sent. A late answer on
    IO1 shows as a garbled pair, or as a missing or doubled one when the
    queue moved on or did not."""
    got = [p for p in logged if p[0] & TYPE_MASK == TYPE_SID1]
    result = Comparison(expected=len(expected), received=len(got))
    for p in logged:
        if p[0] & TYPE_MASK != TYPE_SID1:
            result.other[p[0] & TYPE_MASK] = result.other.get(p[0] & TYPE_MASK, 0) + 1
    i = j = 0
    while i < len(got) and j < len(expected):
        if got[i] == expected[j]:
            result.matched += 1
            i += 1
            j += 1
            continue
        step = _resync(got, i, expected, j) or (1, 1)
        di, dj = step
        if (di, dj) == (1, 1) or di == dj:
            result.garbled += di
            (ga, gv), (wa, wv) = got[i], expected[j]
            note = f"pair {j}: got ${ga:02X}=${gv:02X}, expected ${wa:02X}=${wv:02X}"
        else:
            result.garbled += min(di, dj)
            result.extra += max(di - dj, 0)
            result.missing += max(dj - di, 0)
            note = f"pair {j}: {di} logged and {dj} expected skipped to resync"
        if len(result.examples) < MAX_EXAMPLES:
            result.examples.append(note)
        i += di
        j += dj
    result.missing += len(expected) - j
    result.extra += len(got) - i
    return result


# -- the board's MIDI port ------------------------------------------------------

def board_midi_name(uid: str, names: Sequence[str]) -> str:
    """The MIDI output of the board with chip id ``uid``: the firmware names
    its USB device ``TeensyROM-<uid>``."""
    want = f"TeensyROM-{uid}"
    exact = re.compile(rf"{re.escape(want)}(?!\d)")       # 1234 is not 12345678
    hits = [n for n in names if exact.search(n)]
    if len(hits) != 1:
        raise LogError(f"{len(hits)} MIDI outputs named {want}; the outputs are: {', '.join(names) or 'none'}")
    return hits[0]


@contextlib.contextmanager
def fine_timer() -> Iterator[None]:
    """The 1 ms Windows timer while a stream runs: the MIDI backend sleeps a
    millisecond after each SysEx, which the default 15.6 ms tick stretches."""
    if sys.platform != "win32":
        yield
        return
    import ctypes
    winmm = ctypes.WinDLL("winmm")
    winmm.timeBeginPeriod(1)
    try:
        yield
    finally:
        winmm.timeEndPeriod(1)
