"""Is the machine alive? The VIC keeps painting when the 6510 hangs, so no
single signal answers; these combine the ones the frame ring, the decoded
screens and the serial events offer (section 3 of the plan).

Classes: ALIVE (something moves: a heartbeat cell, a cursor, frame energy),
STATIC (a steady readable picture, which a hung program also shows),
GARBAGE (a picture the glyph bank cannot read), NO_SIGNAL (the dongle's
no-signal pattern or a blank frame), REBOOTING (a boot banner or crash
report arrived). ALIVE and STATIC are both normal states; a test says which
one it expects.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from treadmill.c64.screen import SCREEN_COLUMNS, SCREEN_ROWS
from treadmill.link.unsolicited import EventLog
from treadmill.observe.capture import Frame, frame_difference
from treadmill.observe.glyphs import CELL, PAD

HEARTBEAT_CELL = (SCREEN_ROWS - 1, SCREEN_COLUMNS - 1)     # row 24, column 39: heartbeat.i
ENERGY_ALIVE = 1.5             # mean frame-to-frame difference that counts as motion
CELL_CHANGE = 12.0             # mean difference inside one cell that counts as a change
GARBAGE_CONFIDENCE = 0.35      # mean confidence of non-blank cells below which a screen is unreadable
NO_SIGNAL_STD = 3.0            # a frame with less spread than this is blank
NO_SIGNAL_MATCH = 4.0          # mean difference from the learned no-signal frame


class Liveness(str, Enum):
    ALIVE = "alive"
    STATIC = "static"
    GARBAGE = "garbage"
    NO_SIGNAL = "no_signal"
    REBOOTING = "rebooting"


@dataclass(frozen=True)
class Verdict:
    kind: Liveness
    detail: str
    energy: float = 0.0
    heartbeat: float = 0.0
    confidence: float = 1.0


def frame_energy(frames: list[Frame]) -> float:
    """Mean frame-to-frame difference over the ring."""
    if len(frames) < 2:
        return 0.0
    return float(np.mean([frame_difference(a.image, b.image) for a, b in zip(frames, frames[1:])]))


def is_no_signal(frame: np.ndarray, pattern: np.ndarray | None = None) -> bool:
    if pattern is not None and pattern.shape == frame.shape:
        return frame_difference(frame, pattern) < NO_SIGNAL_MATCH
    return float(frame.std()) < NO_SIGNAL_STD


def cell_change_rate(norms: list[np.ndarray], cell: tuple[int, int] = HEARTBEAT_CELL) -> float:
    """How often one cell of the normalized picture changed between
    consecutive frames: 1.0 for a heartbeat ticking faster than the capture."""
    if len(norms) < 2:
        return 0.0
    r, c = cell
    ys, xs = slice(PAD + r * CELL, PAD + (r + 1) * CELL), slice(PAD + c * CELL, PAD + (c + 1) * CELL)
    changes = [frame_difference(a[ys, xs], b[ys, xs]) > CELL_CHANGE for a, b in zip(norms, norms[1:])]
    return float(np.mean(changes))


def classify(frames: list[Frame], *, norms: list[np.ndarray] | None = None,
             confidence: np.ndarray | None = None, blank: np.ndarray | None = None,
             events: EventLog | None = None, since: float | None = None,
             no_signal_pattern: np.ndarray | None = None, expect_heartbeat: bool = False) -> Verdict:
    """One verdict from whatever is available. ``confidence`` and ``blank``
    are per cell from the latest video read; ``norms`` are normalized frames
    of the ring for the heartbeat; ``events`` since ``since`` (monotonic)."""
    if events is not None:
        kinds = {e.kind for e in events.since(since if since is not None else 0.0)}
        if "crash_report" in kinds:
            return Verdict(Liveness.REBOOTING, "a CrashReport arrived on serial")
        if "boot_banner" in kinds:
            return Verdict(Liveness.REBOOTING, "a boot banner arrived on serial")
    if not frames:
        return Verdict(Liveness.NO_SIGNAL, "no frames")
    if is_no_signal(frames[-1].image, no_signal_pattern):
        return Verdict(Liveness.NO_SIGNAL, "the frame is blank or the dongle's no-signal picture")
    energy = frame_energy(frames)
    heartbeat = cell_change_rate(norms) if norms else 0.0
    conf = 1.0
    if confidence is not None:
        cells = confidence if blank is None else confidence[~blank]
        conf = float(cells.mean()) if cells.size else 1.0
        if conf < GARBAGE_CONFIDENCE and (blank is None or (~blank).sum() > 40):
            return Verdict(Liveness.GARBAGE, f"the glyph bank reads the picture at {conf:.2f} confidence",
                           energy, heartbeat, conf)
    if expect_heartbeat:
        if heartbeat >= 0.5:
            return Verdict(Liveness.ALIVE, f"heartbeat cell changing in {heartbeat:.0%} of frames", energy,
                           heartbeat, conf)
        return Verdict(Liveness.STATIC, f"heartbeat cell changed in only {heartbeat:.0%} of frames", energy,
                       heartbeat, conf)
    if heartbeat >= 0.5 or energy >= ENERGY_ALIVE:
        return Verdict(Liveness.ALIVE, f"motion: energy {energy:.1f}, heartbeat {heartbeat:.0%}", energy,
                       heartbeat, conf)
    return Verdict(Liveness.STATIC, f"steady picture: energy {energy:.1f}", energy, heartbeat, conf)
