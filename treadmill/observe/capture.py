"""Frames from the capture device, or from files and renderers for tests.

A ``FrameSource`` hands out the newest frame and keeps a short ring of
recent ones with monotonic stamps, so a failure can save the last seconds.
``CvCapture`` grabs on a thread from an OpenCV ``VideoCapture`` (Media
Foundation first on Windows, then DirectShow); it discards the first frames
after opening, which the MS2109 delivers before its signal settles. Nothing
here decides what a frame means: that is ``channels`` and ``liveness``.
"""

from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import cv2
import numpy as np

from treadmill.station.model import CaptureSpec

RING_SECONDS = 3.0
DISCARD_FIRST = 5
STABLE_TOLERANCE = 2.0        # mean absolute pixel difference between "the same" frames
DEFAULT_TIMEOUT = 10.0


class CaptureError(Exception):
    pass


class CaptureTimeout(CaptureError):
    pass


@dataclass(frozen=True)
class Frame:
    image: np.ndarray          # BGR uint8
    at: float                  # time.monotonic() when grabbed
    seq: int

    @property
    def size(self) -> tuple[int, int]:
        return self.image.shape[1], self.image.shape[0]


def frame_difference(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute difference per channel, 0..255."""
    if a.shape != b.shape:
        return 255.0
    return float(np.mean(cv2.absdiff(a, b)))


class FrameSource(ABC):
    """Newest-frame access plus a ring of the recent past."""

    def __init__(self, ring_seconds: float = RING_SECONDS):
        self._ring: deque[Frame] = deque()
        self._ring_seconds = ring_seconds
        self._lock = threading.Lock()
        self._seq = 0
        self._new = threading.Condition(self._lock)

    def _push(self, image: np.ndarray) -> Frame:
        with self._lock:
            self._seq += 1
            frame = Frame(image, time.monotonic(), self._seq)
            self._ring.append(frame)
            cutoff = frame.at - self._ring_seconds
            while len(self._ring) > 1 and self._ring[0].at < cutoff:
                self._ring.popleft()
            self._new.notify_all()
        return frame

    @abstractmethod
    def _produce(self) -> None:
        """Make a new frame available (poll sources) or do nothing (threaded)."""

    def latest(self) -> Frame | None:
        self._produce()
        with self._lock:
            return self._ring[-1] if self._ring else None

    def next_frame(self, after: Frame | None = None, timeout: float = DEFAULT_TIMEOUT) -> Frame:
        """A frame newer than ``after`` (the newest, when ``after`` is None)."""
        deadline = time.monotonic() + timeout
        floor = after.seq if after else 0
        while True:
            frame = self.latest()
            if frame is not None and frame.seq > floor:
                return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaptureTimeout(f"no new frame within {timeout:.1f} s from {self.describe()['kind']}")
            self._wait_for_new(min(remaining, 0.05))

    def _wait_for_new(self, seconds: float) -> None:
        with self._lock:
            self._new.wait(seconds)

    def wait_stable(self, agree: int = 2, timeout: float = DEFAULT_TIMEOUT,
                    tolerance: float = STABLE_TOLERANCE) -> Frame:
        """The first frame that ``agree`` consecutive frames agree with."""
        deadline = time.monotonic() + timeout
        last = self.next_frame(timeout=timeout)
        run = 1
        while run < agree:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaptureTimeout(f"frames never settled within {timeout:.1f} s")
            nxt = self.next_frame(last, timeout=remaining)
            run = run + 1 if frame_difference(last.image, nxt.image) <= tolerance else 1
            last = nxt
        return last

    def wait_change(self, reference: Frame, timeout: float = DEFAULT_TIMEOUT,
                    tolerance: float = STABLE_TOLERANCE) -> Frame:
        deadline = time.monotonic() + timeout
        last = reference
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaptureTimeout(f"the picture did not change within {timeout:.1f} s")
            last = self.next_frame(last, timeout=remaining)
            if frame_difference(reference.image, last.image) > tolerance:
                return last

    def ring(self) -> list[Frame]:
        with self._lock:
            return list(self._ring)

    def describe(self) -> dict:
        return {"kind": type(self).__name__}

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class CvCapture(FrameSource):
    """A capture device through OpenCV, grabbed on a thread."""

    BACKENDS = {"msmf": cv2.CAP_MSMF, "dshow": cv2.CAP_DSHOW, "any": cv2.CAP_ANY}

    def __init__(self, spec: CaptureSpec, backend: str | None = None, discard: int = DISCARD_FIRST):
        super().__init__()
        self.spec = spec
        self.backend = backend or ("msmf" if os.name == "nt" else "any")
        self._cap = self._open(spec, self.backend)
        self._discard = discard
        self._stop = threading.Event()
        self._errors = 0
        self._thread = threading.Thread(target=self._run, name="treadmill-capture", daemon=True)
        self._thread.start()

    @staticmethod
    def _open(spec: CaptureSpec, backend: str) -> cv2.VideoCapture:
        api = CvCapture.BACKENDS.get(backend)
        if api is None:
            raise CaptureError(f"unknown capture backend {backend!r}; one of {', '.join(CvCapture.BACKENDS)}")
        cap = cv2.VideoCapture(spec.device, api)
        if not cap.isOpened():
            raise CaptureError(f"capture device {spec.device!r} did not open with {backend}")
        if spec.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*spec.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, spec.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, spec.height)
        cap.set(cv2.CAP_PROP_FPS, spec.fps)
        return cap

    def _run(self) -> None:
        skipped = 0
        while not self._stop.is_set():
            ok, image = self._cap.read()
            if not ok or image is None:
                self._errors += 1
                time.sleep(0.02)
                continue
            if skipped < self._discard:
                skipped += 1
                continue
            self._push(image)

    def _produce(self) -> None:
        pass

    def describe(self) -> dict:
        return {"kind": "CvCapture", "backend": self.backend, "device": self.spec.device,
                "mode": f"{self.spec.fourcc} {self.spec.width}x{self.spec.height}@{self.spec.fps}",
                "actual": f"{int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
                          f"{int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}@{self._cap.get(cv2.CAP_PROP_FPS):g}",
                "read_errors": self._errors}

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._cap.release()


class SyntheticSource(FrameSource):
    """Frames from a callable, one per request: the fake board's screen
    rendered on demand, in the self-tests."""

    def __init__(self, render: Callable[[], np.ndarray]):
        super().__init__()
        self._render = render

    def _produce(self) -> None:
        self._push(self._render())

    def _wait_for_new(self, seconds: float) -> None:
        pass

    def describe(self) -> dict:
        return {"kind": "SyntheticSource"}


class ReplaySource(FrameSource):
    """Recorded frames (images, or a folder of PNGs) handed out in order; the
    last one repeats, so waits on a settled picture terminate."""

    def __init__(self, frames: Iterable[np.ndarray] | Path | str):
        super().__init__()
        if isinstance(frames, (str, Path)):
            paths = sorted(Path(frames).glob("*.png"))
            if not paths:
                raise CaptureError(f"no PNG frames in {frames}")
            frames = [cv2.imread(str(p), cv2.IMREAD_COLOR) for p in paths]
        self._frames = list(frames)
        if not self._frames:
            raise CaptureError("ReplaySource needs at least one frame")
        self._index = 0

    def _produce(self) -> None:
        self._push(self._frames[min(self._index, len(self._frames) - 1)])
        self._index += 1

    def _wait_for_new(self, seconds: float) -> None:
        pass

    def describe(self) -> dict:
        return {"kind": "ReplaySource", "frames": len(self._frames)}


def open_capture(spec: CaptureSpec, backend: str | None = None) -> FrameSource:
    if spec.backend == "opencv":
        return CvCapture(spec, backend)
    raise CaptureError(f"capture backend {spec.backend!r} is not implemented (opencv is)")


def save_strip(frames: list[Frame], path: Path | str, columns: int = 6, scale: float = 0.25) -> Path | None:
    """The ring as one PNG, oldest first, for a failure report."""
    if not frames:
        return None
    thumbs = [cv2.resize(f.image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) for f in frames]
    h, w = thumbs[0].shape[:2]
    rows = (len(thumbs) + columns - 1) // columns
    sheet = np.zeros((rows * h, columns * w, 3), np.uint8)
    for i, t in enumerate(thumbs):
        r, c = divmod(i, columns)
        sheet[r * h:(r + 1) * h, c * w:(c + 1) * w] = t
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), sheet)
    return path
