"""The audio measures on synthesized recordings: tones at known pitches, and a
tune against itself played slower, later and lower, and against another tune.
The measures were checked against the SID host's bench recordings when they
were ported; those recordings are private and stay out of the repo."""

from pathlib import Path

import numpy as np
import pytest

from treadmill.observe.sound import (
    CLIP_LEVEL, Recording, SoundError, compare_audio, level, load_wav, measure_tones, pitch_ratio,
    resample, save_wav,
)

RATE = 48000
PAL_ON_NTSC = 1.03804          # NTSC PHI2 over PAL PHI2


def _noise(seconds: float, rng: np.random.Generator, rms: float = 40.0) -> np.ndarray:
    return rng.normal(0, rms, int(seconds * RATE))


def _tune(seed: int, seconds: float = 24.0, lead_s: float = 0.0) -> Recording:
    """Notes of three harmonics at random pitches of a scale, each decaying,
    at irregular times: enough structure for the onset envelope to lock to."""
    rng = np.random.default_rng(seed)
    x = _noise(lead_s + seconds, rng)
    t = lead_s + 0.2
    while t < lead_s + seconds - 0.5:
        f = 110.0 * 2 ** (rng.integers(0, 36) / 12)
        n = int(rng.uniform(0.15, 0.5) * RATE)
        tt = np.arange(n) / RATE
        note = sum(np.sin(2 * np.pi * f * h * tt) / h for h in (1, 2, 3)) * np.exp(-tt / 0.12) * 4000
        a = int(t * RATE)
        x[a:a + n] += note[:len(x) - a]
        t += rng.uniform(0.1, 0.4)
    return Recording(RATE, x)


def _with_lead(rec: Recording, lead_s: float, seed: int = 99) -> Recording:
    return Recording(RATE, np.concatenate([_noise(lead_s, np.random.default_rng(seed)), rec.samples]))


def test_tones_are_found_and_measured_between_bins():
    rng = np.random.default_rng(1)
    x = _noise(6.0, rng)
    x[:RATE // 50] = 32000                                  # the stream's opening transient
    for start, hz in ((1.0, 439.97), (3.0, 523.25)):
        t = np.arange(int(1.2 * RATE)) / RATE
        x[int(start * RATE):int(start * RATE) + len(t)] += 3000 * np.sin(2 * np.pi * hz * t)
    tones = measure_tones(Recording(RATE, x))
    assert [round(t.start_s, 1) for t in tones] == [1.0, 3.0]
    assert abs(tones[0].hz - 439.97) < 0.01 and abs(tones[1].hz - 523.25) < 0.01


def test_level_counts_clipped_samples():
    t = np.arange(RATE) / RATE
    rec = Recording(RATE, np.clip(40000 * np.sin(2 * np.pi * 100 * t), -32768, 32767))
    lv = level(rec)
    assert lv.peak >= CLIP_LEVEL and 0.3 < lv.clipped < 0.7 and lv.rms > 20000
    assert level(Recording(RATE, np.zeros(0))).rms == 0.0


def test_a_tune_matches_itself_played_slower_and_later():
    ref = _tune(7, lead_s=0.3)
    factor = 1.0005                                         # the test plays 500 ppm slow
    test = _with_lead(resample(ref.cut(0.3), factor), 1.0)
    c = compare_audio(ref, 0.3, test, 1.0)
    assert c.used >= 8 and c.used == len(c.windows)
    assert abs(c.tempo_ppm - (1 / factor - 1) * 1e6) < 30
    assert c.residual_ms < 0.5
    assert abs(c.offset_ms - (0.3 - 1.0) * 1000) < 10
    assert c.cosine_median > 0.97
    d = c.to_dict()
    assert d["used"] == c.used and d["windows"][0]["test_s"] == c.windows[0].test_s


def test_another_tune_does_not_match():
    c = compare_audio(_tune(7), 0.0, _tune(8), 0.0)
    assert c.used <= 2 and c.cosine_median < compare_audio(_tune(7), 0.0, _tune(7), 0.0).cosine_median


def test_pitch_ratio_finds_a_tune_on_the_other_standard():
    ref = _tune(3, seconds=12.0)
    assert abs(pitch_ratio(ref, resample(ref, PAL_ON_NTSC)) * PAL_ON_NTSC - 1) < 3e-4
    assert abs(pitch_ratio(resample(ref, PAL_ON_NTSC), ref) / PAL_ON_NTSC - 1) < 3e-4


def test_wav_round_trip_and_refusals(tmp_path: Path):
    rec = _tune(5, seconds=2.0)
    back = load_wav(save_wav(rec, tmp_path / "t.wav"))
    assert back.rate == RATE and np.abs(back.samples - np.round(rec.samples)).max() <= 1
    assert back.cut(0.5, 1.0).seconds == pytest.approx(1.0)
    with pytest.raises(SoundError):
        compare_audio(rec, 0.0, Recording(44100, rec.samples), 0.0)
    with pytest.raises(SoundError):
        pitch_ratio(Recording(RATE, np.zeros(1000)), rec)
