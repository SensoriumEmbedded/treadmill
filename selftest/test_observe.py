"""The video pipeline on synthetic frames: calibration, decoding, harvest,
tracking, goldens, liveness, capture sources and the screen channel."""

import time
from pathlib import Path

import numpy as np
import pytest

from selftest.synth import (
    FONT, GEOMETRY, calibrated, frame_of, harvested_bank, menu_like_screen, text_screen,
)
from treadmill.c64.screen import CHARSET_LOWER_UPPER, CHARSET_UPPER_GRAPHICS
from treadmill.link.unsolicited import EventLog
from treadmill.observe import (
    LAYOUT_BASIC, LAYOUT_MENU, Calibration, CalibrationError, CaptureTimeout, Frame, FrameSource, Golden,
    GlyphBank,
    Liveness, ReplaySource, ScreenChannel, ScreenError, SyntheticSource, bootstrap_bank, classify, compare,
    diff_text, find_text_area, from_dump, read_video, refine, save_golden, synthetic_font, track, xcheck,
)
from treadmill.observe.glyphs import CELL, PAD, Font, match
from treadmill.observe.liveness import cell_change_rate
from treadmill.observe.render import DARK_GREY, LIGHT_GREY, Geometry
from treadmill.link.trlink import ScreenDump


@pytest.fixture(scope="module")
def calib() -> Calibration:
    return calibrated()


@pytest.fixture(scope="module")
def bank(calib) -> GlyphBank:
    return harvested_bank(calib, charsets=(CHARSET_LOWER_UPPER, CHARSET_UPPER_GRAPHICS))


def _true_origin(g: Geometry) -> tuple[float, float]:
    # The renderer places pixel centers; the calibration reports the first normalized pixel's center.
    return g.x0 - g.cell_w / 16 + g.cell_w / 32, g.y0 - g.cell_h / 16 + g.cell_h / 32


def test_calibration_finds_the_text_area_under_the_menu_bar(calib):
    tx, ty = _true_origin(GEOMETRY)
    assert abs(calib.x0 - tx) < 0.6 and abs(calib.y0 - ty) < 0.6
    assert abs(calib.cell_w - GEOMETRY.cell_w) < 0.05 and abs(calib.cell_h - GEOMETRY.cell_h) < 0.05
    assert calib.layout == LAYOUT_MENU and calib.method == "bounding-box"
    assert calib.background == (1, 1, 1) or max(calib.background) < 6
    assert 0.04 < calib.noise_floor < 0.2


def test_calibration_basic_layout_and_other_geometry():
    codes, _ = menu_like_screen(3)
    codes[:40] = 0x20                                   # no bar: a BASIC-like full 25 rows
    g = Geometry((720, 480), 101.3, 61.7, 517.0, 352.0)
    cal = find_text_area(frame_of(codes, LIGHT_GREY, geometry=g, seed=3, blur=1.0), LAYOUT_BASIC)
    tx, ty = _true_origin(g)
    assert abs(cal.x0 - tx) < 0.6 and abs(cal.y0 - ty) < 0.6
    assert abs(cal.cell_w - g.cell_w) < 0.05 and abs(cal.cell_h - g.cell_h) < 0.05
    with pytest.raises(CalibrationError):
        find_text_area(np.full((480, 720, 3), 68, np.uint8), LAYOUT_MENU)      # border only
    with pytest.raises(CalibrationError):
        find_text_area(frame_of(codes, LIGHT_GREY, geometry=g), "nope")


def test_calibration_round_trips_through_json(calib, tmp_path: Path):
    p = calib.save(tmp_path / "c.json")
    back = Calibration.load(p)
    assert back == calib or (back.created and back.x0 == calib.x0 and back.background == calib.background)
    with pytest.raises(CalibrationError):
        Calibration.load(tmp_path / "missing.json")


def test_decoding_is_exact_after_harvest_and_detects_the_charset(calib, bank):
    codes, colors = menu_like_screen(5)
    for charset in (CHARSET_LOWER_UPPER, CHARSET_UPPER_GRAPHICS):
        frame = Frame(frame_of(codes, colors, charset, seed=5), 0.0, 1)
        read = read_video(frame, calib, bank)
        assert read.charset == charset
        assert int((read.codes != codes).sum()) == 0
        assert read.mean_confidence() > 0.6
        assert read.source == "video" and read.colors.shape == (1000, 3)
    # Bootstrap only (no harvest) reads most of it, which is what the harvest starts from.
    raw = bootstrap_bank(FONT, "synthetic")
    read = read_video(Frame(frame_of(codes, colors, seed=5), 0.0, 1), calib, raw)
    assert int((read.codes != codes).sum()) < 150
    # The harvested blank is nothing, not scaled-up noise.
    assert bank.templates[1, 0x20].max() == 0 and bank.counts[1, 0x20] > 0


def test_identical_glyphs_in_both_charsets_keep_their_own_codes():
    # The ROM draws A-Z at 1-26 in upper/graphics and again at 65-90 in lower/upper;
    # lower/upper text must read as 65-90, not as the other set's code for the same glyph.
    bits = FONT.bits.copy()
    bits[1, 65:91] = bits[0, 1:27]
    bank = bootstrap_bank(Font(bits, "rom-like"), "synthetic")
    core = (slice(None), slice(PAD, PAD + CELL), slice(PAD, PAD + CELL))
    assert match(bank, bank.templates[1, 65:91][core], CHARSET_LOWER_UPPER).codes.tolist() == list(range(65, 91))
    assert match(bank, bank.templates[0, 1:27][core], CHARSET_UPPER_GRAPHICS).codes.tolist() == list(range(1, 27))


def test_refine_keeps_a_good_fit_and_recovers_a_nudged_one(calib, bank):
    codes, colors = menu_like_screen(6)
    frame = frame_of(codes, colors, seed=6)
    nudged = Calibration(**{**calib.__dict__, "x0": calib.x0 + 2.0, "y0": calib.y0 - 1.5})
    assert int((read_video(Frame(frame, 0.0, 1), nudged, bank, follow=False).codes != codes).sum()) > 100
    fixed = refine(frame, nudged, codes, CHARSET_LOWER_UPPER, bank)
    assert fixed.method == "refined"
    assert abs(fixed.x0 - calib.x0) < 0.5 and abs(fixed.y0 - calib.y0) < 0.5
    assert int((read_video(Frame(frame, 0.0, 1), fixed, bank, follow=False).codes != codes).sum()) == 0
    with pytest.raises(CalibrationError):
        refine(frame, calib, np.full(1000, 0x20, np.uint8), CHARSET_LOWER_UPPER, bank)


def test_tracking_follows_a_shifted_picture(calib, bank):
    codes, colors = menu_like_screen(7)
    moved = Geometry(GEOMETRY.size, GEOMETRY.x0 + 3.0, GEOMETRY.y0 - 2.0, GEOMETRY.width, GEOMETRY.height)
    frame = Frame(frame_of(codes, colors, geometry=moved, seed=7), 0.0, 1)
    assert int((read_video(frame, calib, bank, follow=False).codes != codes).sum()) > 100
    followed = read_video(frame, calib, bank)
    assert int((followed.codes != codes).sum()) == 0
    assert followed.calibration.method.endswith("+tracked")
    assert abs(followed.calibration.x0 - (calib.x0 + 3.0)) < 0.5
    far = Geometry(GEOMETRY.size, GEOMETRY.x0 + 60.0, GEOMETRY.y0, GEOMETRY.width, GEOMETRY.height)
    assert track(frame_of(codes, colors, geometry=far, seed=8), calib) == calib     # too far: untouched
    assert track(np.zeros((10, 10, 3), np.uint8), calib) == calib                  # wrong size: untouched


def test_bank_round_trip_and_bad_shapes(bank, tmp_path: Path):
    p = bank.save(tmp_path / "bank.npz")
    back = GlyphBank.load(p)
    assert np.array_equal(back.templates, bank.templates) and back.harvested() == bank.harvested()
    assert back.capture_class == "synthetic" and back.font == FONT.name and back.created
    from treadmill.observe.glyphs import GlyphError
    with pytest.raises(GlyphError):
        GlyphBank(np.zeros((2, 256, 16, 16), np.float32), np.zeros((2, 256), np.int32), "x", "y")
    with pytest.raises(GlyphError):
        GlyphBank.load(tmp_path / "missing.npz")
    assert synthetic_font(1).bits[0, 0x20].sum() == 0 and synthetic_font(1).bits[0, 0xA0].all()


def test_dma_and_video_reads_agree(calib, bank):
    codes = text_screen({3: "treadmill hello", 10: "menu 1 2 3"})
    video = read_video(Frame(frame_of(codes, seed=9), 0.0, 1), calib, bank)
    dma = from_dump(ScreenDump(0x0400, CHARSET_LOWER_UPPER, bytes(codes), []))
    x = xcheck(video, dma)
    assert x.ok and x.disagreement == 0.0 and x.cells == ()
    assert video.rows()[3].startswith("treadmill hello") and dma.rows()[3] == video.rows()[3]
    assert dma.source == "dma" and dma.address == 0x0400 and dma.mean_confidence() == 1.0
    with pytest.raises(ScreenError):
        from_dump(ScreenDump(0x0400, CHARSET_LOWER_UPPER, b"\x20" * 10, []))


def test_goldens_parse_compare_and_diff(tmp_path: Path):
    rows = ["treadmill hello".ljust(40)] + [" " * 40] * 24
    rows[5] = "src: sd card".ljust(40)
    text = "% a header line\n" + "|tread?ill hello\n" + "|\n" * 3 + "|*\n" + "|src: sd card|\n"
    g = Golden.parse(text)
    assert g.header == ("% a header line",) and g.rows[4] is None and g.rows[5].startswith("src:")
    assert g.rows[6] is None and len(g.rows) == 25
    assert compare(rows, g) == []
    rows[5] = "src: usb drive".ljust(40)
    mm = compare(rows, g)
    assert (mm[0].row, mm[0].col, mm[0].expected, mm[0].actual) == (5, 5, "s", "u")
    assert "^" in diff_text(rows, g)
    p = save_golden(rows, tmp_path / "x.screen.txt", header=("% note",))
    again = Golden.load(p)
    assert again.rows[5].startswith("src: usb drive") and again.header == ("% note",)
    assert compare(rows, again) == []
    bare = Golden.parse("\n".join(rows))                      # no prefixes: bare rows
    assert bare.header == () and compare(rows, bare) == []
    assert Golden.parse(again.text()).rows == again.rows


def test_capture_sources_wait_and_time_out():
    a = np.zeros((20, 20, 3), np.uint8)
    b = np.full((20, 20, 3), 200, np.uint8)
    src = ReplaySource([a, a, b, b, b])
    first = src.next_frame()
    assert first.seq == 1
    stable = src.wait_stable(agree=2, timeout=1.0)
    assert stable.seq >= 2
    changed = src.wait_change(first, timeout=1.0)
    assert changed.image[0, 0, 0] == 200
    assert len(src.ring()) >= 2 and src.describe()["frames"] == 5
    calls = []
    syn = SyntheticSource(lambda: calls.append(1) or b)
    assert syn.next_frame().seq == 1 and calls
    with pytest.raises(CaptureTimeout):
        syn.wait_change(syn.next_frame(), timeout=0.3)
    with pytest.raises(Exception):
        ReplaySource([])


def test_a_feed_that_stopped_does_not_hand_out_its_last_frame():
    class Stopped(FrameSource):                 # frames arrive only when pushed from outside
        def _produce(self):
            pass

    src = Stopped()
    pushed = src._push(np.zeros((8, 8, 3), np.uint8))
    assert src.next_frame().seq == pushed.seq   # just grabbed: the picture now
    time.sleep(0.05)
    with pytest.raises(CaptureTimeout):
        src.next_frame(timeout=0.1, max_age=0.02)
    again = src._push(np.zeros((8, 8, 3), np.uint8))
    assert src.next_frame(max_age=0.02).seq == again.seq


def test_liveness_classes(calib, bank):
    codes = text_screen({2: "running"})
    frames, norms = [], []
    for i in range(6):
        c = codes.copy()
        c[999] = i                       # the heartbeat cell ticking
        img = frame_of(c, seed=20 + i)
        frames.append(Frame(img, float(i), i + 1))
        norms.append(calib.normalize(img))
    assert cell_change_rate(norms) == 1.0
    alive = classify(frames, norms=norms, expect_heartbeat=True)
    assert alive.kind == Liveness.ALIVE and alive.heartbeat == 1.0
    still = [Frame(frame_of(codes, seed=30), float(i), i + 1) for i in range(4)]
    still_norms = [calib.normalize(f.image) for f in still]
    v = classify(still, norms=still_norms, expect_heartbeat=True)
    assert v.kind == Liveness.STATIC
    assert classify(still, norms=still_norms).kind == Liveness.STATIC
    blank = [Frame(np.full((720, 1280, 3), 17, np.uint8), 0.0, 1)]
    assert classify(blank).kind == Liveness.NO_SIGNAL
    assert classify([]).kind == Liveness.NO_SIGNAL
    log = EventLog()
    t0 = time.monotonic()
    log.feed(b"\r\nTeensyROM+ v0.9 is on-line\r\n")
    assert classify(still, events=log, since=t0 - 1).kind == Liveness.REBOOTING
    noise = np.random.default_rng(0).random(1000).astype(np.float32) * 0.2
    garbage = classify(still, norms=still_norms, confidence=noise, blank=np.zeros(1000, bool))
    assert garbage.kind == Liveness.GARBAGE


class _FakeLink:
    """Enough of Link for the channel: a screen the test can change."""
    def __init__(self, codes):
        self.codes = codes
        self.dma_allowed = True
        self.reads = 0

    def read_screen(self):
        self.reads += 1
        return ScreenDump(0x0400, CHARSET_LOWER_UPPER, bytes(self.codes), [])


def test_screen_channel_policy_waits_and_xcheck(calib, bank):
    codes = text_screen({1: "menu"})
    link = _FakeLink(codes)
    source = SyntheticSource(lambda: frame_of(link.codes, seed=40))
    seen = []
    ch = ScreenChannel(link, source, calib, bank, dma_allowed=lambda: link.dma_allowed, agree=2,
                       on_xcheck=seen.append)
    assert ch.read().source == "video" and link.reads == 0
    assert ch.read(exact=True).source == "dma" and link.reads == 1
    link.dma_allowed = False
    assert ch.read(exact=True).source == "video"
    with pytest.raises(ScreenError):
        ch.read_dma()
    link.dma_allowed = True
    ch.read(exact=True, xcheck_now=True)
    assert seen and seen[-1].ok
    got = ch.wait_text("menu", row=1, timeout=2.0)
    assert got.source == "video"
    link.codes = text_screen({1: "menu", 2: "launched"})
    assert "launched" in ch.wait_text("launched", timeout=2.0).text()
    with pytest.raises(ScreenError):
        ch.wait_text("never", timeout=0.5)
    settled = ch.wait_settled(timeout=2.0)
    assert "launched" in settled.text()
    video_only = ScreenChannel(None, source, calib, bank)
    assert video_only.read(exact=True).source == "video"
    dma_only = ScreenChannel(link, None, None, None, dma_allowed=lambda: True)
    assert dma_only.read().source == "dma"
    with pytest.raises(ScreenError):
        ScreenChannel(None, None, None, None).read()
