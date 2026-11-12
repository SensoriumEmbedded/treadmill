"""Hearing the machine: what a recording of the C64's audio says.

The C64's audio reaches the PC through the S-Video-to-HDMI converter and the
capture dongle's USB audio endpoint. ``audio.py`` records it; this module
measures recordings. The methods are the ones the SID host work proved on
the bench (2026-11-09 to 11-10):

- **a tone's frequency**, where a register value is known: bursts found in
  50 ms blocks, then a Hann-windowed, zero-padded FFT with a parabolic peak;
- **a comparison with a reference recording** of the same tune: onset
  envelopes matched window by window give the tempo ratio and the timing
  residual, the aligned spectra a cosine similarity, and the long-term
  spectra on a log-frequency axis the pitch ratio.

The comparison, not an absolute measurement, is the check for a tune: the
converter and the SID chip differ between machines, which moves the loudest
partial of a tune but not its timing or the shape of its spectrum. What
counts as close enough comes from A/A runs on the station, never a guess.
"""

from __future__ import annotations

import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from treadmill.errors import TreadmillError

FULL_SCALE = 32767
CLIP_LEVEL = 32700             # a sample this far out counts as clipped
OPEN_TRANSIENT_S = 0.1         # the dongle starts a stream with a near-full-scale transient

TONE_BAND = (300.0, 700.0)
TONE_BLOCK_S = 0.05
TONE_ON_RATIO = 8.0            # a block whose band peak is this far over the median is part of a burst
TONE_MIN_BLOCKS = 4            # a burst gives up a block at each end, and needs two left
TONE_FFT = 1 << 22

SPEC_FFT = 4096
HOP_S = 0.001
SPEC_BAND = (60.0, 6000.0)
FIRST_WINDOW_S = 0.5           # the first window starts this far after the test's start
WINDOW_S = 3.0
WINDOW_EVERY_S = 2.0
SEARCH_S = 0.6                 # how far either way of the expected offset a window is looked for
MIN_CORRELATION = 0.6          # a window that matches less well does not count
SIMILARITY_STEP = 10           # spectra compared every this many hops
SPEC_CHUNK = 2048              # frames transformed at a time, to bound memory

PITCH_FFT = 1 << 16
PITCH_BAND = (100.0, 4000.0)
PITCH_STEPS = 2000             # grid steps per octave
PITCH_MAX_SHIFT = 200          # steps either way: about 7 %, past PAL against NTSC (3.7 %)


class SoundError(TreadmillError):
    """A recording that cannot be measured: the wrong format, too short, two rates."""


@dataclass(frozen=True)
class Recording:
    rate: int
    samples: np.ndarray            # mono, at int16 scale, as float64

    @property
    def seconds(self) -> float:
        return len(self.samples) / self.rate

    def cut(self, start_s: float, seconds: float | None = None) -> "Recording":
        a = min(max(0, int(round(start_s * self.rate))), len(self.samples))
        b = len(self.samples) if seconds is None else min(len(self.samples), a + int(round(seconds * self.rate)))
        return Recording(self.rate, self.samples[a:b])


def load_wav(path: Path | str) -> Recording:
    """A 16-bit WAV file; the first channel of a stereo one."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise SoundError(f"{path}: {8 * w.getsampwidth()}-bit samples; only 16-bit WAV files are read")
        channels, rate = w.getnchannels(), w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    return Recording(rate, data[::channels].astype(float))


def save_wav(recording: Recording, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.clip(np.round(recording.samples), -FULL_SCALE - 1, FULL_SCALE).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(recording.rate)
        w.writeframes(data.tobytes())
    return path


def resample(recording: Recording, factor: float) -> Recording:
    """The recording played ``factor`` times slower: longer by that factor
    and lower in pitch by it. A PAL CIA tune played natively on NTSC runs
    3.8 % fast and high, and a reference stretched by the clock ratio
    matches it again."""
    n = int(round(len(recording.samples) * factor))
    positions = np.arange(n) / factor
    return Recording(recording.rate, np.interp(positions, np.arange(len(recording.samples)), recording.samples))


# -- level ---------------------------------------------------------------


@dataclass(frozen=True)
class Level:
    rms: float
    peak: float
    clipped: float                 # the share of samples at full scale


def level(recording: Recording) -> Level:
    x = recording.samples
    if not len(x):
        return Level(0.0, 0.0, 0.0)
    centered = x - x.mean()
    return Level(float(np.sqrt(np.mean(centered ** 2))), float(np.abs(x).max()),
                 float(np.mean(np.abs(x) >= CLIP_LEVEL)))


# -- tones -----------------------------------------------------------------


@dataclass(frozen=True)
class Tone:
    start_s: float
    seconds: float
    hz: float


def peak_frequency(x: np.ndarray, rate: int, band: tuple[float, float], n_fft: int = TONE_FFT) -> float:
    """The strongest frequency in ``band``, between FFT bins: a parabola
    through the log magnitudes of the peak bin and its neighbors."""
    x = x - x.mean()
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x)), n_fft))
    freqs = np.fft.rfftfreq(n_fft, 1 / rate)
    inside = np.flatnonzero((freqs > band[0]) & (freqs < band[1]))
    k = int(inside[np.argmax(spectrum[inside])])
    return float(freqs[k] + _vertex(*np.log(spectrum[k - 1:k + 2] + 1e-12)) * rate / n_fft)


def find_bursts(recording: Recording, band: tuple[float, float] = TONE_BAND, *,
                block_s: float = TONE_BLOCK_S, ratio: float = TONE_ON_RATIO) -> list[tuple[int, int]]:
    """Runs of blocks where a tone in ``band`` stands over the recording's
    median, as (first block, block after the last); the stream's opening
    transient is left out. A tone sounding for most of the recording raises
    the median itself and is not found, so tones are recorded with silence
    between them."""
    x = recording.samples.copy()
    x[:int(recording.rate * OPEN_TRANSIENT_S)] = 0
    block = int(recording.rate * block_s)
    n = len(x) // block
    if n == 0:
        return []
    spectra = np.abs(np.fft.rfft(x[:n * block].reshape(n, block) * np.hanning(block), axis=1))
    freqs = np.fft.rfftfreq(block, 1 / recording.rate)
    energy = spectra[:, (freqs > band[0]) & (freqs < band[1])].max(axis=1)
    on = energy > np.median(energy) * ratio
    bursts, i = [], 0
    while i < n:
        if on[i]:
            j = i
            while j < n and on[j]:
                j += 1
            bursts.append((i, j))
            i = j
        else:
            i += 1
    return bursts


def measure_tones(recording: Recording, band: tuple[float, float] = TONE_BAND, *,
                  block_s: float = TONE_BLOCK_S) -> list[Tone]:
    """The frequency of each burst long enough to measure, its first and
    last block left out so the attack and the release do not pull it."""
    block = int(recording.rate * block_s)
    tones = []
    for a, b in find_bursts(recording, band, block_s=block_s):
        if b - a < TONE_MIN_BLOCKS:
            continue
        stretch = recording.samples[(a + 1) * block:(b - 1) * block]
        tones.append(Tone(a * block / recording.rate, len(stretch) / recording.rate,
                          peak_frequency(stretch, recording.rate, band)))
    return tones


# -- comparison --------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    test_s: float                  # where the window starts in the test recording
    offset_ms: float               # reference time minus test time there
    correlation: float


@dataclass(frozen=True)
class Comparison:
    """The test recording against the reference. ``tempo_ppm`` is positive
    when the test plays faster (reference time = test time x (1 + ppm/1e6));
    ``residual_ms`` is how far the matched windows stray from that line.
    Both are None when fewer than two windows matched."""
    windows: tuple[Window, ...]
    used: int
    tempo_ppm: float | None
    residual_ms: float | None
    offset_ms: float
    cosine_median: float
    cosine_p10: float
    pitch_ratio: float             # test pitch over reference pitch
    seconds: float                 # the stretch the spectra were compared over

    def to_dict(self) -> dict:
        d = asdict(self)
        d["windows"] = [asdict(w) for w in self.windows]
        return d


def log_spectrogram(recording: Recording, hop: int, *, n_fft: int = SPEC_FFT,
                    band: tuple[float, float] = SPEC_BAND) -> np.ndarray:
    """log(1 + |FFT|) of Hann-windowed frames every ``hop`` samples, inside
    ``band``: one row per frame."""
    x = recording.samples - recording.samples.mean()
    if len(x) < n_fft:
        raise SoundError(f"a recording of {recording.seconds:.2f} s is shorter than one {n_fft}-point frame")
    frames = sliding_window_view(x, n_fft)[::hop]
    freqs = np.fft.rfftfreq(n_fft, 1 / recording.rate)
    inside = (freqs > band[0]) & (freqs < band[1])
    window = np.hanning(n_fft)
    out = np.empty((len(frames), int(inside.sum())))
    for i in range(0, len(frames), SPEC_CHUNK):
        out[i:i + SPEC_CHUNK] = np.log1p(np.abs(np.fft.rfft(frames[i:i + SPEC_CHUNK] * window, axis=1))[:, inside])
    return out


def onset_envelope(spectrogram: np.ndarray) -> np.ndarray:
    """Positive spectral flux: how much new energy each frame brings."""
    return np.maximum(np.diff(spectrogram, axis=0), 0).sum(axis=1)


def match_windows(ref_env: np.ndarray, test_env: np.ndarray, frame_s: float, ref_start_s: float,
                  test_start_s: float, *, search_s: float = SEARCH_S) -> list[Window]:
    """Each window of the test's envelope against the reference's, near the
    offset the two start times predict, with the peak placed between frames."""
    expected = int(round((ref_start_s - test_start_s) / frame_s))
    win, search = int(round(WINDOW_S / frame_s)), int(round(search_s / frame_s))
    first = int(round(test_start_s / frame_s)) + int(round(FIRST_WINDOW_S / frame_s))
    windows = []
    for s in range(first, len(test_env) - win, int(round(WINDOW_EVERY_S / frame_s))):
        lo = s + expected - search
        if lo < 0 or s + expected + search + win > len(ref_env):
            continue
        seg = test_env[s:s + win] - test_env[s:s + win].mean()
        ref = ref_env[lo:s + expected + search + win]
        c = np.correlate(ref - ref.mean(), seg, mode="valid")
        k = int(np.argmax(c))
        d = _vertex(*c[k - 1:k + 2]) if 0 < k < len(c) - 1 else 0.0
        part = ref[k:k + win] - ref[k:k + win].mean()
        norm = np.sqrt((seg ** 2).sum() * (part ** 2).sum())
        windows.append(Window(s * frame_s, (expected - search + k + d) * frame_s * 1000,
                              float(c[k] / norm) if norm else 0.0))
    return windows


def long_term_spectrum(recording: Recording, n_fft: int = PITCH_FFT) -> tuple[np.ndarray, np.ndarray]:
    """The magnitude spectra of consecutive frames, summed: what notes and
    timbres the recording holds, whatever their order."""
    x = recording.samples - recording.samples.mean()
    frames = len(x) // n_fft
    if frames == 0:
        raise SoundError(f"a recording of {recording.seconds:.2f} s is shorter than one {n_fft}-point frame")
    window = np.hanning(n_fft)
    total = np.abs(np.fft.rfft(x[:frames * n_fft].reshape(frames, n_fft) * window, axis=1)).sum(axis=0)
    return np.fft.rfftfreq(n_fft, 1 / recording.rate), total


def pitch_ratio(ref: Recording, test: Recording, *, band: tuple[float, float] = PITCH_BAND,
                steps: int = PITCH_STEPS, max_shift: int = PITCH_MAX_SHIFT) -> float:
    """The test's pitch over the reference's: the shift that best lines up
    their long-term spectra on a log-frequency axis, between grid steps."""
    grid = 2 ** np.arange(np.log2(band[0]), np.log2(band[1]), 1 / steps)
    curves = []
    for rec in (ref, test):
        freqs, spectrum = long_term_spectrum(rec)
        curve = np.log1p(np.interp(grid, freqs, spectrum))
        curves.append(curve - curve.mean())
    a, b = curves
    shifts = np.arange(-max_shift, max_shift + 1)
    scores = np.array([np.dot(a[max(0, k):len(a) + min(0, k)], b[max(0, -k):len(b) - max(0, k)])
                       for k in shifts])
    i = int(np.argmax(scores))
    best = shifts[i] + (_vertex(*scores[i - 1:i + 2]) if 0 < i < len(scores) - 1 else 0.0)
    return float(2 ** (-best / steps))


def compare_audio(ref: Recording, ref_start_s: float, test: Recording, test_start_s: float, *,
            search_s: float = SEARCH_S) -> Comparison:
    """The test recording against a reference of the same tune. The start
    times are where each recording's tune starts (its launch mark), good to
    within ``search_s``."""
    if ref.rate != test.rate:
        raise SoundError(f"the reference is at {ref.rate} Hz and the test at {test.rate} Hz")
    hop = max(1, int(round(ref.rate * HOP_S)))
    frame_s = hop / ref.rate
    spec_ref, spec_test = log_spectrogram(ref, hop), log_spectrogram(test, hop)
    windows = match_windows(onset_envelope(spec_ref), onset_envelope(spec_test), frame_s,
                            ref_start_s, test_start_s, search_s=search_s)
    good = [w for w in windows if w.correlation > MIN_CORRELATION]
    tempo = residual = None
    if len(good) >= 2:
        t = np.array([w.test_s * 1000 for w in good])
        off = np.array([w.offset_ms for w in good])
        fit = np.polyfit(t, off, 1)
        tempo, residual = float(fit[0] * 1e6), float(np.std(off - np.polyval(fit, t)))
    offset_ms = (float(np.median([w.offset_ms for w in good])) if good
                 else (ref_start_s - test_start_s) * 1000)
    shift = int(round(offset_ms / 1000 / frame_s))
    t0 = max(int(round((test_start_s + FIRST_WINDOW_S) / frame_s)), -shift)
    t1 = min(len(spec_test), len(spec_ref) - shift)
    if t1 - t0 < SIMILARITY_STEP:
        raise SoundError("the two recordings do not overlap once aligned")
    sb = spec_test[t0:t1:SIMILARITY_STEP]
    sa = spec_ref[t0 + shift:t1 + shift:SIMILARITY_STEP]
    cosine = (sa * sb).sum(axis=1) / np.sqrt((sa ** 2).sum(axis=1) * (sb ** 2).sum(axis=1) + 1e-9)
    return Comparison(tuple(windows), len(good), tempo, residual, offset_ms,
                      float(np.median(cosine)), float(np.percentile(cosine, 10)),
                      pitch_ratio(ref.cut(ref_start_s), test.cut(test_start_s)), (t1 - t0) * frame_s)


def _vertex(y0: float, y1: float, y2: float) -> float:
    """Where a parabola through three equally spaced points peaks, in steps
    from the middle one."""
    den = y0 - 2 * y1 + y2
    return float(0.5 * (y0 - y2) / den) if den else 0.0
