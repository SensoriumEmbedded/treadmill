"""game: a game cartridge judged by its picture, with what moves on its own masked out.

A game is what the timing knobs are finally for, but it is not a test
program: its title or attract screen moves by itself, so a plain reference
picture would fail it at the defaults. The probe launches the cartridge,
waits ``settle`` seconds, types ``keys`` through the keyboard board if
given (a game whose sprites appear only in play), and learns over ``learn``
seconds at the defaults which pixels of the text area move on their own:
each pixel whose level ranges by more than ``LEVEL`` over those frames,
grown by ``GROW`` pixels, is masked. The reference is the median of the
same frames. A sample is the worst frame over ``dwell`` seconds, counted as
the pixels outside the mask off the reference by more than ``LEVEL``, and
fails past ``FACTOR`` times the baseline (the same count over a dwell at the
defaults) and ``FLOOR`` above it, as vicrom judges. When the masked pixels
moved while learning and a sample's median motion there falls under a tenth
of it, the game has stopped (the median, since a broken picture's stray
pixels can land in the mask). It may have stopped at an earlier value, so
the probe relaunches it, with the defaults back for the reset and the menu,
and samples again at this value; still not moving, the point is a hang. A game that hangs at a value whose picture is broken
anyway may not look stopped: that point fails on its pixels, and the next
point relaunches the game.

A game's picture is a weaker witness than vicrom's: an attract sequence can
change screens, and a mask that learned one screen fails the next. So the
probe is trusted on a machine only once it has failed at a value where
vicrom fails on the same machine; until then its edges are notes, not
margins. It resets the C64 to relaunch, so it is not for a knob that times
DMA.
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from treadmill.board import Board
from treadmill.observe.calibrate import Calibration
from treadmill.observe.capture import FrameSource
from treadmill.probes.noise import FACTOR, FLOOR, LEVEL
from treadmill.sweeps import Sample, Setting, SweepError, at_defaults

GROW = 3                          # px the motion mask grows by: a moving edge's blur and the capture's jitter
FRAME_WAIT_S = 2.0
ACTIVE_MIN = 50                   # px changing between frames inside the mask for a hang to be judged at all
HUNG_SHARE = 0.1                  # a sample whose motion falls under this share of the learned motion has hung


def game_source(spec: str) -> tuple[Path | None, str, str]:
    """``--game``: a file on the host (pushed to the card), or a file already
    on the board's card or stick as ``sd:/path`` or ``usb:/path``. Returns
    (local file or None, drive, path on the drive)."""
    drive, sep, rest = spec.partition(":")
    if sep and drive.lower() in ("sd", "usb") and rest.startswith("/"):
        return None, drive.lower(), rest
    return Path(spec), "sd", "/treadmill/game.crt"


class GameProbe:
    name = "game"
    resets = True                 # a hang is relaunched through a reset

    def __init__(self, board: Board, video: FrameSource | None, calibration: Calibration | None, game: str,
                 *, kbd=None, keys: str | None = None, settle: float = 8.0, learn: float = 5.0,
                 dwell: float = 2.0, log=print):
        self.board = board
        self.video = video
        self.calibration = calibration
        self.local, self.drive, self.remote = game_source(game)
        self.kbd = kbd
        self.keys = keys
        self.settle = settle
        self.learn = learn
        self.dwell = dwell
        self.log = log
        self.reference: np.ndarray | None = None
        self.mask: np.ndarray | None = None          # True where the game moves on its own
        self.baseline: int | None = None
        self.motion: float = 0.0                     # median pixels changing between frames inside the mask

    # -- the picture --------------------------------------------------------------------

    def _text(self, frame) -> np.ndarray:
        x, y, w, h = self.calibration.text_area
        dx, dy = self.calibration.cell_w / 2, self.calibration.cell_h / 2
        crop = frame.image[int(y + dy):int(y + h - dy), int(x + dx):int(x + w - dx)]
        return cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    def _frames(self, seconds: float) -> list[np.ndarray]:
        frames, last, end = [], None, time.monotonic() + seconds
        while time.monotonic() < end:
            last = self.video.next_frame(after=last, timeout=FRAME_WAIT_S)
            frames.append(self._text(last))
        return frames

    def _off(self, frame: np.ndarray) -> int:
        """Pixels outside the mask off the reference."""
        return int(np.count_nonzero((cv2.absdiff(frame, self.reference) > LEVEL) & ~self.mask))

    def _moving(self, frames: list[np.ndarray]) -> list[int]:
        """Pixels changing inside the mask between each frame and the next."""
        return [int(np.count_nonzero((cv2.absdiff(a, b) > LEVEL) & self.mask)) for a, b in zip(frames, frames[1:])]

    def limit(self) -> int:
        base = self.baseline or 0
        return max(int(FACTOR * base), base + FLOOR)

    # -- the C64 ------------------------------------------------------------------------

    def _launch(self) -> None:
        self.board.ensure_menu()
        link = self.board.link
        if self.local is not None:
            link.push_file(self.local.read_bytes(), self.remote, self.drive)
        link.launch(self.remote, self.drive)
        time.sleep(self.settle)
        if self.keys:
            self.kbd.type(self.keys)
            time.sleep(1.0)

    def setup(self) -> None:
        if self.video is None or self.calibration is None:
            raise SweepError("the game probe reads the picture: it needs the station's video and its calibration")
        if self.local is not None and not self.local.is_file():
            raise SweepError(f"the game {self.local} is not a file")
        if self.keys and self.kbd is None:
            raise SweepError("the game probe types its keys through the keyboard board, which the station lacks")
        self._launch()
        frames = self._frames(self.learn)
        if len(frames) < 2:
            raise SweepError("game: no frames to learn from")
        stack = np.stack(frames)
        moves = (stack.max(axis=0).astype(np.int16) - stack.min(axis=0)) > LEVEL
        grow = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * GROW + 1, 2 * GROW + 1))
        self.mask = cv2.dilate(moves.astype(np.uint8), grow).astype(bool)
        self.reference = np.median(stack, axis=0).astype(np.uint8)
        self.motion = float(np.median(self._moving(frames)))
        check = self._frames(self.dwell)
        self.baseline = max((self._off(f) for f in check), default=0)
        self.log(f"[sweep] game learned at the defaults: {int(self.mask.sum())} pixels move on their own "
                 f"({self.mask.mean():.1%}), motion {self.motion:.0f}; baseline {self.baseline} pixels "
                 f"(limit {self.limit()})" + ("" if self.motion >= ACTIVE_MIN else "; too still to tell a hang"))

    def _stopped(self, frames: list[np.ndarray]) -> bool:
        moving = self._moving(frames)
        return self.motion >= ACTIVE_MIN and (float(np.median(moving)) if moving else 0.0) < HUNG_SHARE * self.motion

    def sample(self, label: str, setting: Setting | None = None) -> Sample:
        frames = self._frames(self.dwell)
        if self._stopped(frames):
            self.log(f"[sweep] the game is not moving; relaunching it for {label}")
            with at_defaults(setting, always=True):        # a CPU knob past its edge may not boot the menu
                self.board.reset()
                self.board.wait_for_menu()
                self._launch()
            frames = self._frames(self.dwell)
            if self._stopped(frames):
                return Sample(1, 1, "the game stopped moving: the C64 hung", hung=True)
        worst = max((self._off(f) for f in frames), default=0)
        if worst > self.limit():
            return Sample(1, 1, f"{worst} still pixels off the reference, over the limit {self.limit()}")
        return Sample(0, 1)

    def teardown(self) -> None:
        self.board.reset()
        self.board.wait_for_menu()

    def info(self) -> dict:
        return {"baseline": self.baseline, "limit": self.limit(), "masked": float(self.mask.mean())
                if self.mask is not None else None, "motion": self.motion, "level": LEVEL, "grow": GROW,
                "game": self.remote if self.local is None else self.local.name}
