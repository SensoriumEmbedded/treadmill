"""``treadmill compare``: the steps it plans, in what order and with what
arguments, and how it carries on, stops and exits when a step fails. The
steps themselves are ``treadmill run``, ``sweep``, ``margins`` and ``power``,
tested on their own; here a runner stands in for them."""

import json
from pathlib import Path

from treadmill import compare
from treadmill.cli import EXIT_OK, main
from treadmill.errors import EXIT_FAILED, EXIT_NO_STATION, EXIT_USAGE


def _plan(**kw):
    args = dict(regions=["PAL", "NTSC"], defines=list(compare.DEFINES), fine=2, power_off=True)
    args.update(kw)
    return compare.plan("s1", "base1", "tip1", **args)


def test_each_standard_runs_the_base_then_the_tip_and_the_mains_go_off_last():
    steps = _plan()
    names = [s.name for s in steps]
    assert names[:6] == ["pal-base-margins", "pal-base-rwnready-vicrom-high", "pal-base-plaprop-vicrom-high",
                         "pal-base-plaprop-idread-rwhold", "pal-base-vicdhold-vicrom-cpu", "pal-tip-margins"]
    assert names.index("ntsc-base-margins") == 10 and names[-2:] == ["margins-csv", "power-off"]
    base, tip = steps[0].argv, steps[5].argv
    assert "ref=base1" in base and "--region" in base and base[-3:] == ["--", "--margin-fine", "2"]
    assert "ref=tip1" in tip and tip[-3:] == ["--", "--margin-fine", "2"], "both builds at one grain"
    assert steps[3].argv[:5] == ["sweep", "p", "--probe", "idread", "--set"] and "nS_RWnReady=115" in steps[3].argv
    assert steps[6].argv[-4:] == steps[1].argv[-4:] == ["--fine", "2", "--repeat", "3"]
    assert steps[-1].argv == ["power", "--station", "s1", "off", "--latch", "--json"]


def test_without_a_region_the_runs_take_the_standard_as_it_is():
    steps = _plan(regions=[None], probes=False, power_off=False, fine=0, pytest_args=["-k", "PLAprop"])
    assert [s.name for s in steps] == ["now-base-margins", "now-tip-margins", "margins-csv"]
    assert "--region" not in steps[0].argv and steps[0].argv[-3:] == steps[1].argv[-3:] == ["--", "-k", "PLAprop"]


def _runner(codes: dict[str, int], seen: list[str]):
    def run(step, out: Path, err: Path) -> int:
        seen.append(step.name)
        code = codes.get(step.name, EXIT_OK)
        out.write_text(json.dumps({"run_dir": f"reports/{step.name}"} if code != EXIT_USAGE
                                  else {"error": "the idread probe reads its counters by DMA"}), encoding="utf-8")
        err.write_text("", encoding="utf-8")
        return code
    return run


def test_a_failed_step_leaves_the_rest_and_a_sweep_the_station_cannot_run_is_refused(tmp_path: Path):
    seen: list[str] = []
    steps = _plan(regions=[None])
    code = compare.run_steps(steps, tmp_path, runner=_runner({"now-base-margins": EXIT_FAILED,
                                                              "now-base-plaprop-idread-rwhold": EXIT_USAGE}, seen),
                             loader_open=lambda: False, log=lambda m: None)
    assert code == EXIT_FAILED and len(seen) == len(steps), "every step ran"
    by = {s.name: s for s in steps}
    assert by["now-base-margins"].status == "failed" and by["now-tip-margins"].status == "ok"
    assert by["now-base-plaprop-idread-rwhold"].status == "refused" and "by DMA" in by[
        "now-base-plaprop-idread-rwhold"].note
    record = json.loads((tmp_path / "compare.json").read_text(encoding="utf-8"))
    assert record["exit_code"] == EXIT_FAILED and record["steps"][0]["run_dir"] == "reports/now-base-margins"


def test_a_station_that_cannot_be_had_stops_its_steps_but_not_the_csv_or_the_power_off(tmp_path: Path):
    seen: list[str] = []
    steps = _plan(regions=[None])
    code = compare.run_steps(steps, tmp_path, runner=_runner({"now-base-margins": EXIT_NO_STATION}, seen),
                             loader_open=lambda: False, log=lambda m: None)
    assert code == EXIT_NO_STATION and seen == ["now-base-margins", "margins-csv", "power-off"]
    assert all(s.status == "skipped" for s in steps if s.name not in seen)


def test_an_open_teensy_loader_stops_the_station_s_steps(tmp_path: Path):
    seen: list[str] = []
    opened = iter([False, True])
    steps = _plan(regions=[None], probes=False)
    code = compare.run_steps(steps, tmp_path, runner=_runner({}, seen), loader_open=lambda: next(opened, True),
                             log=lambda m: None)
    assert code == EXIT_FAILED and seen == ["now-base-margins", "margins-csv", "power-off"]
    assert "Teensy Loader" in steps[1].note


def test_the_dry_run_prints_the_steps_and_runs_nothing(capsys):
    assert main(["compare", "--station", "s1", "--base", "b", "--tip", "t", "--no-probes", "--dry-run",
                 "--json"]) == EXIT_OK
    steps = json.loads(capsys.readouterr().out)["steps"]
    assert [s[0] for s in steps] == ["run", "run", "margins"] and "ref=t" in steps[1]
    assert main(["compare", "--station", "s1", "--base", "b", "--tip", "t", "--tip-fine", "-1", "--dry-run",
                 "--json"]) == EXIT_USAGE
    capsys.readouterr()
    for flag in ("--fine", "--tip-fine"):            # --tip-fine is --fine's old name, for both builds now
        assert main(["compare", "--station", "s1", "--base", "b", "--tip", "t", "--no-probes", flag, "4",
                     "--dry-run", "--json"]) == EXIT_OK
        base, tip = json.loads(capsys.readouterr().out)["steps"][:2]
        assert base[-3:] == tip[-3:] == ["--", "--margin-fine", "4"], (flag, base, tip)
