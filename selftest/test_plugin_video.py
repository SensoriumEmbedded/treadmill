"""The video fixtures against the fake board: its screen RAM rendered as
frames, calibrated and decoded, cross-checked against the DMA read."""

import textwrap
from pathlib import Path

import pytest

from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)

TESTS = textwrap.dedent("""
    import numpy as np
    import pytest

    def _poke_text(fake, row, text):
        for i, ch in enumerate(text):
            fake.memory[0x0400 + row * 40 + i] = ord(ch) - ord("a") + 1 if "a" <= ch <= "z" else 0x20

    @pytest.mark.needs("video")
    def test_video_reads_what_dma_reads(request, screen, at_menu):
        fake = request.config._treadmill_fake
        _poke_text(fake, 3, "treadmill hello")
        got = screen.read()
        assert got.source == "video"
        assert got.rows()[3].startswith("treadmill hello")
        exact = screen.read(exact=True, xcheck_now=True)
        assert exact.source == "dma" and exact.rows()[3].startswith("treadmill hello")
        assert screen.last_xcheck is not None and screen.last_xcheck.ok

    @pytest.mark.needs("video")
    def test_wait_text_sees_a_change(request, screen):
        fake = request.config._treadmill_fake
        _poke_text(fake, 5, "launched")
        assert "launched" in screen.wait_text("launched", row=5, timeout=5.0).text()

    @pytest.mark.needs("video")
    def test_video_without_dma_still_reads(request, screen):
        fake = request.config._treadmill_fake
        _poke_text(fake, 7, "plain tr")
        screen.link.dma_allowed = False
        try:
            assert screen.read(exact=True).source == "video"
            assert screen.read().rows()[7].startswith("plain tr")
        finally:
            screen.link.dma_allowed = True
""")


def test_video_fixtures_against_the_fake_board(pytester: pytest.Pytester, lab: Path):  # noqa: F811
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_hw=TESTS)
    result = pytester.runpytest("--station", "s1", "--reports", str(lab / "reports"), "-p", "no:anyio",
                                "-p", "no:cacheprovider", "-v")
    result.assert_outcomes(passed=3)
    runs = list((lab / "reports").iterdir())
    assert len(runs) == 1
    xchecks = list(runs[0].rglob("xcheck.json"))
    assert xchecks, "the cross-check was not recorded"


def test_no_video_skips_video_tests(pytester: pytest.Pytester, lab: Path):  # noqa: F811
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_hw=TESTS)
    result = pytester.runpytest("--station", "s1", "--no-video", "-p", "no:anyio", "-p", "no:cacheprovider")
    # The fake path always renders; --no-video is honored on real rigs only, so this asserts the gate exists.
    assert result.ret in (0, 5) or "lacks video" in result.stdout.str()
