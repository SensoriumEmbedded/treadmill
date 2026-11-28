"""The hardware tests under ``tests/`` run end to end against the fake board:
every test of Phase 1 passes or skips for a reason the fake cannot meet, none
fails or errors. What the fake cannot show (timing, a real card, the picture)
is the bench's to confirm. Cases marked ``alike`` (a sweep of another knob down
a path another case already takes) are left out: on the fake they add minutes
and no path."""

from pathlib import Path

import pytest

from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)

REPO = Path(__file__).resolve().parents[1]


def _run(pytester: pytest.Pytester, lab: Path, *extra: str):  # noqa: F811
    return pytester.runpytest(
        str(REPO / "tests"), "--rootdir", str(REPO), "--station", "s1", "--fw-defines", "Dbg_SerTimChg,Dbg_SerDMA",
        "--golden-dir", str(REPO / "tests" / "golden"), "--reports", str(lab / "reports"),
        "-m", "not alike", "-p", "no:anyio", "-p", "no:cacheprovider", "-v", *extra)


def test_the_hardware_tests_pass_against_the_fake_board(pytester: pytest.Pytester, lab: Path):  # noqa: F811
    result = _run(pytester, lab)
    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) == 0 and outcomes.get("errors", 0) == 0, result.stdout.str()
    assert outcomes.get("passed", 0) >= 13, outcomes
    runs = list((lab / "reports").iterdir())
    assert len(runs) == 1
    assert list(runs[0].rglob("menu-root.screen.txt")), "the golden check left no screen behind"


def test_the_baseline_tier_runs_without_dma(pytester: pytest.Pytester, lab: Path):  # noqa: F811
    """With DMA and keys masked, the tier-0 tests (identity, reset, files,
    launch, the root listing) pass on video alone and the key-driven ones skip.
    The MinimalBoot margins read by video whatever the tier, so the full run
    covers them."""
    result = _run(pytester, lab, "--caps-mask", "dma,screen_dma,reset_sense,keys_kernal",
                  "-k", "(identity or reset or files or hello or root_listing or timing) and not minimalboot")
    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) == 0 and outcomes.get("errors", 0) == 0, result.stdout.str()
    assert outcomes.get("passed", 0) >= 10, outcomes
    assert not list((lab / "reports").rglob("xcheck.json")), "a cross-check ran with DMA masked"


def test_margin_fine_narrows_every_edge_with_three_samples(pytester: pytest.Pytester, lab: Path):  # noqa: F811
    import json
    result = _run(pytester, lab, "--margin-fine", "2", "-k", "test_cpu_margin and nS_PLAprop and not handler")
    outcomes = result.parseoutcomes()
    assert outcomes.get("passed", 0) == 1 and outcomes.get("failed", 0) == 0, result.stdout.str()
    record = json.loads(next((lab / "reports").rglob("margins.json")).read_text(encoding="utf-8"))
    assert (record["spec"]["fine"], record["spec"]["repeat"]) == (2, 3)
    for edge in record["edges"].values():
        if edge["first_fail"] is not None:
            assert abs(edge["first_fail"] - edge["clean_to"]) <= 2, edge
