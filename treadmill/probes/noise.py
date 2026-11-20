"""noise: screen noise beside another probe, from the capture alone.

A DMA cycle that drives the bus into the VIC's half of the cycle shows on
screen before it shows in any count: in PR #99's sweeps, nS_DMASetup low
enough to do it left the data clean 30-50 ns further down, while the
picture filled with stray cells. The measure is that script's: pixels
inside the text area that change by more than ``LEVEL`` between
consecutive frames. A point fails when the worst frame pair is ``FACTOR``
times the baseline and at least ``FLOOR`` above it. The baseline is the
same measure over one sample of the workload probe at the defaults, before
the first point and again after the last.

Over the PR #99 runs this rule flags the low end of nS_DMASetup, where the
worst pair runs to 100,000 pixels and more against a few thousand at the
defaults, and passes the window above it; the one point inside a window it
flagged was 4 pixels over a limit of 101. The median, which backlog 18
proposed for a looping workload, would miss C64 #14's REU run: its failing
point had a median of 11 pixels against 0 at the defaults, its worst pair
177 against 11.
"""

from __future__ import annotations

import threading

import cv2
import numpy as np

from treadmill.observe.calibrate import Calibration
from treadmill.observe.capture import FrameSource
from treadmill.sweeps import Probe, Sample, SweepError

LEVEL = 48                        # grey levels a pixel must move between frames to count
FACTOR, FLOOR = 3.0, 100          # a point fails past max(FACTOR x baseline, baseline + FLOOR) pixels
FRAME_WAIT_S = 2.0


class NoiseProbe:
    """Runs beside ``workload`` (``companion``): ``begin`` starts counting
    frame pairs, ``end`` judges the worst one against the baseline."""

    name = "noise"
    companion = True

    def __init__(self, video: FrameSource | None, calibration: Calibration | None, workload: Probe, *, log=print):
        self.video = video
        self.calibration = calibration
        self.workload = workload
        self.log = log
        self.before: int | None = None
        self.after: int | None = None
        self._counts: list[int] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def setup(self) -> None:
        if self.video is None or self.calibration is None:
            raise SweepError("the noise probe reads the picture: it needs the station's video and its calibration")
        x, y, w, h = self.calibration.text_area
        self._box = (int(x), int(y), int(x + w), int(y + h))
        self.before = self._baseline("baseline")
        self.log(f"[sweep] noise baseline at the defaults: {self.before} pixels (limit {self.limit()})")

    def _baseline(self, label: str) -> int:
        self.begin(label)
        try:
            self.workload.sample(label)
        finally:
            counts = self._finish()
        return max(counts, default=0)

    def limit(self) -> int:
        base = self.before or 0
        return max(int(FACTOR * base), base + FLOOR)

    def _run(self) -> None:
        x0, y0, x1, y1 = self._box
        last = prev = None
        while not self._stop.is_set():
            try:
                last = self.video.next_frame(after=last, timeout=FRAME_WAIT_S)
            except Exception:  # noqa: BLE001 - a dropped frame only thins the sample
                prev = None
                continue
            img = cv2.cvtColor(last.image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
            if prev is not None and prev.shape == img.shape:
                self._counts.append(int(np.count_nonzero(cv2.absdiff(img, prev) > LEVEL)))
            prev = img

    def begin(self, label: str) -> None:
        self._counts = []
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="noise-probe", daemon=True)
        self._thread.start()

    def _finish(self) -> list[int]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(FRAME_WAIT_S + 1.0)
        return list(self._counts)

    def end(self) -> Sample:
        counts = self._finish()
        if not counts:
            return Sample(0, 0, "no frame pairs: the capture gave nothing to judge")
        worst, limit = max(counts), self.limit()
        if worst > limit:
            return Sample(1, 1, f"noise {worst} pixels over the limit {limit} ({len(counts)} frame pairs)")
        return Sample(0, 1)

    def teardown(self) -> None:
        try:
            self.after = self._baseline("closing baseline")
        except Exception as e:  # noqa: BLE001 - a workload that cannot run any more leaves no closing baseline
            self.log(f"[sweep] noise: no closing baseline ({e})")

    def info(self) -> dict:
        return {"baseline": self.before, "closing_baseline": self.after, "limit": self.limit(), "level": LEVEL,
                "factor": FACTOR, "floor": FLOOR}
