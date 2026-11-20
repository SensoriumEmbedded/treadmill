"""The fixtures end to end against the fake board, through pytester: a run
locks the rig, probes, gates capabilities, recovers to the menu, writes the
artifacts and checks the invariants."""

import json
import textwrap
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def lab(tmp_path: Path, monkeypatch) -> Path:
    data = tmp_path / "data"
    (data / "rigs").mkdir(parents=True)
    (data / "stations").mkdir()
    (data / "rigs" / "rigA.toml").write_text('id = "rigA"\n[capture]\ndevice = 0\n', encoding="utf-8")
    (data / "stations" / "s1.toml").write_text(
        'id = "s1"\nrig = "rigA"\n[machine]\nid = "bench"\nmodel = "C64"\n[tr]\nuid = "19277260"\n',
        encoding="utf-8")
    monkeypatch.setenv("TREADMILL_DATA", str(data))
    monkeypatch.setenv("TREADMILL_FAKE_BOARD", "1")
    monkeypatch.setenv("TREADMILL_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("TREADMILL_LOCK_HELD", raising=False)
    return tmp_path


TESTS = textwrap.dedent("""
    import pytest

    def test_ping_at_menu(at_menu):
        assert at_menu.link.ping().endswith("ready!")

    @pytest.mark.needs("dma")
    def test_screen_by_dma(link):
        assert len(link.read_screen().rows) == 25

    @pytest.mark.needs("power")
    def test_needs_power(at_menu):
        raise AssertionError("must be skipped: the station has no power control")

    @pytest.mark.needs_fw("Dbg_SerDMA")
    def test_needs_define(at_menu):
        raise AssertionError("must be skipped: no --fw-defines")

    def test_launch_then_recovered_by_the_ladder(at_menu):
        at_menu.link.launch("/A.PRG")        # leaves the board busy; teardown resets it
""")


def test_fixtures_against_the_fake_board(pytester: pytest.Pytester, lab: Path):
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_hw=TESTS)
    result = pytester.runpytest("--station", "s1", "--reports", str(lab / "reports"), "-p", "no:anyio",
                                "-p", "no:cacheprovider", "-v")
    result.assert_outcomes(passed=3, skipped=2)
    runs = list((lab / "reports").iterdir())
    assert len(runs) == 1
    meta = json.loads((runs[0] / "run.json").read_text(encoding="utf-8"))
    assert meta["station"]["tr_kind"] == "tr-plus" and "dma" in meta["station"]["caps"]
    assert meta["board"]["image"] == "main" and meta["board"]["version"] == "TeensyROM+ v0.9"
    assert meta["summary"] == {"passed": 3, "skipped": 2} and meta["exit_status"] == 0 and len(meta["tests"]) == 5
    by_name = {t["nodeid"].rsplit("::", 1)[1]: t for t in meta["tests"]}
    assert by_name["test_needs_power"]["outcome"] == "skipped" and "lacks power" in by_name["test_needs_power"]["message"]
    assert by_name["test_ping_at_menu"]["folder"] and by_name["test_needs_power"]["folder"] is None
    report = (runs[0] / "report.html").read_text(encoding="utf-8")
    assert "3 passed, 2 skipped" in report and "lacks power" in report
    folders = sorted(p.name for p in runs[0].iterdir() if p.is_dir())
    assert any("test_ping_at_menu" in f for f in folders)
    launched = next(p for p in runs[0].iterdir() if "launch_then_recovered" in p.name)
    events = json.loads((launched / "events.json").read_text(encoding="utf-8"))
    assert any(e["kind"] == "handler" for e in events)
    assert (launched / "serial.log").read_bytes()


def test_a_failure_in_the_call_survives_a_teardown_error(pytester: pytest.Pytester, lab: Path):
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_hw=textwrap.dedent("""
        import pytest

        @pytest.fixture
        def breaks_on_teardown():
            yield
            raise RuntimeError("the teardown broke too")

        def test_twice(at_menu, breaks_on_teardown):
            assert False, "the call failed first"
    """))
    result = pytester.runpytest("--station", "s1", "--reports", str(lab / "reports"), "-p", "no:anyio",
                                "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1, errors=1)
    meta = json.loads((next((lab / "reports").iterdir()) / "run.json").read_text(encoding="utf-8"))
    rec = meta["tests"][0]
    assert rec["outcome"] == "failed" and rec["failed_in"] == "call"
    assert "the call failed first" in rec["message"] and "the teardown broke too" in rec["message"]


def test_a_skip_in_setup_after_the_screen_is_a_skip(pytester: pytest.Pytester, lab: Path):
    """Backlog 30: a fixture after ``screen`` that skips (``sd_card`` on a
    station with no card, as test_card_type_set meets it) leaves no call
    phase, so ``screen``'s teardown must not need its result."""
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_hw=textwrap.dedent("""
        import pytest

        @pytest.fixture
        def no_card():
            pytest.skip("no SD card on this station")

        def test_listing(screen, no_card):
            pass
    """))
    result = pytester.runpytest("--station", "s1", "--reports", str(lab / "reports"), "-p", "no:anyio",
                                "-p", "no:cacheprovider")
    result.assert_outcomes(skipped=1)


def test_a_run_that_ends_before_any_test_records_why(pytester: pytest.Pytester, lab: Path):
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_hw=TESTS)
    result = pytester.runpytest("--station", "nowhere", "--reports", str(lab / "reports"), "-p", "no:anyio",
                                "-p", "no:cacheprovider")
    assert result.ret == 2
    runs = list((lab / "reports").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text(encoding="utf-8"))
    assert meta["exit_status"] == 2 and "nowhere" in meta["aborted"] and meta["tests"] == []
    assert "nowhere" in (runs[0] / "report.html").read_text(encoding="utf-8")


def test_caps_mask_removes_dma(pytester: pytest.Pytester, lab: Path):
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_hw=TESTS)
    result = pytester.runpytest("--station", "s1", "--caps-mask", "dma,screen_dma", "-p", "no:anyio",
                                "-p", "no:cacheprovider", "--reports", str(lab / "reports"))
    result.assert_outcomes(passed=2, skipped=3)


def test_estimate_runs_nothing(pytester: pytest.Pytester, lab: Path):
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_hw=textwrap.dedent("""
        import pytest
        @pytest.mark.duration(7)
        def test_a(at_menu): pass
        def test_b(at_menu): pass
    """))
    result = pytester.runpytest("--station", "s1", "--estimate", "-p", "no:anyio", "-p", "no:cacheprovider")
    result.stdout.fnmatch_lines(["*estimate: 2 test(s), about 8 minute(s)*"])
    result.assert_outcomes()


def test_unknown_capability_fails_collection(pytester: pytest.Pytester, lab: Path):
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_hw=textwrap.dedent("""
        import pytest
        @pytest.mark.needs("dmx")
        def test_a(at_menu): pass
    """))
    result = pytester.runpytest("--station", "s1", "-p", "no:anyio", "-p", "no:cacheprovider")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*unknown capability dmx*"])
