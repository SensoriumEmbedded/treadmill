"""The warm-up before a margin run: what it waits and why, the refusal when
nothing says how long the machine has been on, the relay switching it on,
the step a compare starts taking its word, and the records carrying it. The
compare's preflight too: what it states before it starts."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from treadmill import compare, warmup
from treadmill.cli import EXIT_OK, EXIT_USAGE, main

NOW = datetime(2026, 11, 27, 15, 0)


def test_the_wait_is_what_is_left_and_the_text_says_what_it_rests_on():
    w = warmup.warmup_for(now=NOW, minutes=30, since=NOW - timedelta(minutes=12))
    assert w.wait_s == 18 * 60 and w.record() == {"powered_since": "2026-11-27T14:48", "how": warmup.STATED}
    assert w.describe() == "warm-up: powered since 2026-11-27T14:48, given; on 12 of 30 min; waiting 18 min"
    assert warmup.warmup_for(now=NOW, minutes=30, since=NOW - timedelta(minutes=45)).wait_s == 0
    relay = warmup.warmup_for(now=NOW, minutes=30, relay_off=True)
    assert relay.wait_s == 30 * 60 and relay.how == warmup.RELAY and relay.powered_since == "2026-11-27T15:00"
    warm = warmup.warmup_for(now=NOW, minutes=30, assume_warm=True)
    assert warm.wait_s == 0 and warm.record() == {"powered_since": None, "how": warmup.ASSUMED}
    assert warmup.warmup_for(now=NOW, minutes=0).how == warmup.NOT_NEEDED


def test_with_nothing_said_and_no_relay_off_it_is_refused():
    for relay_off in (None, False):            # no relay to ask, or the mains already on since nobody knows when
        with pytest.raises(warmup.WarmupUnknown, match="when was #24 switched on"):
            warmup.warmup_for(now=NOW, minutes=30, relay_off=relay_off, machine="#24")


def test_powered_since_is_a_time_today_or_a_full_date_and_never_the_future():
    assert warmup.parse_since("14:20", NOW) == datetime(2026, 11, 27, 14, 20)
    assert warmup.parse_since("2026-11-26T22:05", NOW) == datetime(2026, 11, 26, 22, 5)
    for bad, match in (("15:30", "later than now"), ("25:00", "give HH:MM"), ("soon", "give HH:MM")):
        with pytest.raises(ValueError, match=match):
            warmup.parse_since(bad, NOW)


def test_the_wait_says_what_is_left_every_few_minutes():
    slept, said = [], []
    warmup.wait_out(warmup.warmup_for(now=NOW, minutes=12, relay_off=True), said.append, slept.append)
    assert slept == [300, 300, 120] and len(said) == 2 and said[-1].endswith("2 min left")


def test_a_step_a_compare_started_takes_its_word_and_waits_no_more(monkeypatch):
    monkeypatch.setenv(warmup.ENV, json.dumps({"powered_since": "2026-11-27T14:00", "how": warmup.STATED}))
    w = warmup.from_env(NOW, 30)
    assert w.wait_s == 0 and w.how == warmup.STATED and "from the compare" in w.note
    monkeypatch.setenv(warmup.ENV, json.dumps({"powered_since": None, "how": warmup.ASSUMED}))
    assert warmup.from_env(NOW, 30).how == warmup.ASSUMED
    monkeypatch.setenv(warmup.ENV, "not json")
    assert warmup.from_env(NOW, 30) is None


def test_a_sweep_or_a_margin_run_on_its_own_is_refused_without_the_power_on_time(monkeypatch, capsys):
    monkeypatch.setattr(warmup, "relay_is_off", lambda station: None)
    assert main(["sweep", "p", "--station", "s1", "--both", "--json"]) == EXIT_USAGE
    assert "when was s1 switched on" in json.loads(capsys.readouterr().err)["error"]
    assert main(["run", "--station", "s1", "--tests", "tests/sweep/test_cpu_margins.py", "--json"]) == EXIT_USAGE
    assert main(["run", "--station", "s1", "--soak", "10", "--json"]) == EXIT_USAGE
    capsys.readouterr()


@pytest.mark.parametrize("argv, margin", [
    (["--tests", "tests/sweep"], True),
    (["--tests", "{root}/tests/sweep"], True),             # absolute, as the run resolves it
    (["--tests", "tests"], False),                         # the whole tree, named, on a build without the knobs
    (["--tests", "tests", "--define", "Dbg_SerTimChg"], True),
    (["--tests", "./tests/soak/test_soak.py::test_idread"], True),
    (["--soak", "5"], True),
    (["--define", "Dbg_SerTimChg"], True),                 # the whole tree on a build with the knobs sweeps too
    (["--fw-defines", "Dbg_SerDMA,Dbg_SerTimChg"], True),  # a build flashed by other means, named
    ([], False),
    (["--tests", "tests/smoke"], False),
    (["--tests", "tests/sweep", "--estimate"], False),
])
def test_which_runs_need_the_warm_up(argv, margin):
    from treadmill.cli import _margin_run, build_parser
    from treadmill.station.config import repo_root
    argv = [a.format(root=repo_root().as_posix()) for a in argv]
    assert _margin_run(build_parser().parse_args(["run", "--station", "s1", *argv])) is margin


def test_mains_off_are_switched_on_and_the_whole_warm_up_waited(monkeypatch):
    from treadmill import cli
    switched, slept = [], []
    monkeypatch.setattr(warmup, "relay_is_off", lambda station: True)
    monkeypatch.setattr(warmup, "switch_on", switched.append)
    monkeypatch.setattr(warmup.time, "sleep", slept.append)
    args = cli.build_parser().parse_args(["sweep", "p", "--station", "s1", "--warmup", "10"])
    cli._warm_up(args, lambda m: None)
    assert switched == ["s1"] and sum(slept) == 600
    assert json.loads(warmup.os.environ[warmup.ENV])["how"] == warmup.RELAY


def test_the_compare_states_its_plan_before_it_starts(monkeypatch, capsys):
    monkeypatch.setattr(warmup, "relay_is_off", lambda station: None)
    args = ["compare", "--station", "s1", "--base", "b", "--tip", "t", "--power-off"]
    assert main(args + ["--json"]) == EXIT_USAGE
    assert "when was s1 switched on" in json.loads(capsys.readouterr().err)["error"]
    assert main(args + ["--dry-run", "--json"]) == EXIT_OK          # the dry run shows the refusal instead
    pre = json.loads(capsys.readouterr().out)["preflight"]
    assert pre["warm_up"].startswith("warm-up: the compare would refuse")
    assert main(args + ["--powered-for", "50", "--dry-run", "--json"]) == EXIT_OK
    pre = json.loads(capsys.readouterr().out)["preflight"]
    assert pre["warm_up"].endswith("on 50 of 30 min; no wait") and pre["at_the_end"] == "mains latched off"
    assert pre["estimate_min"] == 75 + 160 + 8 * 3
    monkeypatch.setattr(warmup, "relay_is_off", lambda station: True)
    assert main(args + ["--dry-run", "--json"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["steps"][0][0] == "run", "the compare switches the mains on itself, not as a step"
    assert "the mains go on through the relay now; waiting 30 min" in out["preflight"]["warm_up"]
    assert out["preflight"]["estimate_min"] == 30 + 75 + 160 + 8 * 3
    with pytest.raises(SystemExit) as e:                            # one way of saying it, not two
        main(args + ["--powered-since", "14:20", "--assume-warm", "--dry-run"])
    assert e.value.code == EXIT_USAGE


def test_the_preflight_names_the_machine_its_port_setup_and_both_builds():
    station = SimpleNamespace(id="rigA-sys24-trplus", machine=SimpleNamespace(id="24", model="C64"),
                              port_setup="direct")
    steps = compare.plan(station.id, "53eadf61", "fix/x", regions=[None], defines=["Dbg_SerTimChg"], fine=2,
                         probes=False)
    pre = compare.preflight(station, "53eadf61", "fix/x", regions=[None], defines=["Dbg_SerTimChg"],
                            power_off=False, steps=steps, warmup=None, warmup_said="warm-up: assumed warm",
                            resolve={"53eadf61": "53eadf61", "fix/x": "785bad01"}.get)
    lines = compare.preflight_lines(pre)
    assert "  station     rigA-sys24-trplus: #24, C64" in lines and "  port setup  direct" in lines
    assert "  base        53eadf61" in lines and "  tip         fix/x (785bad01)" in lines
    assert "  at the end  mains left on" in lines and lines[-1] == "  takes about 3 h 55 min"


def test_the_records_carry_what_the_warm_up_rested_on(monkeypatch):
    from treadmill.margins import context_for
    from treadmill.report import describe_station
    st = SimpleNamespace(id="s1", machine=SimpleNamespace(id="24", model="C64", region="PAL"),
                         tr=SimpleNamespace(kind="tr-plus", uid="1"), port_setup="direct", rig=SimpleNamespace(
                             id="rigA", power=None), standard="PAL", caps=frozenset(), keyboard=None)
    board = SimpleNamespace(station=st, banner=None, kbd=None, kbd_problem=None)
    assert context_for(board)["warm_up"] is None and describe_station(board)["warm_up"] is None
    warmup.hand_on(warmup.warmup_for(now=NOW, minutes=30, since=NOW - timedelta(minutes=40)))
    said = {"powered_since": "2026-11-27T14:20", "how": warmup.STATED}
    assert context_for(board)["warm_up"] == said and describe_station(board)["warm_up"] == said


def test_the_compare_waits_once_no_teensy_loader_is_open_and_before_its_first_step(tmp_path):
    from selftest.test_compare import _runner
    seen, slept, said = [], [], []
    steps = compare.plan("s1", "b", "t", regions=[None], defines=["Dbg_SerTimChg"], fine=2, probes=False)

    def run(step, out, err):
        seen.append((step.name, sum(slept)))
        return _runner({}, [])(step, out, err)
    wait = warmup.warmup_for(now=NOW, minutes=12, relay_off=True)
    compare.run_steps(steps, tmp_path, runner=run, loader_open=lambda: False, log=said.append, warmup=wait,
                      sleep=slept.append)
    assert seen[0] == ("now-base-margins", 12 * 60) and slept == [300, 300, 120]
    assert json.loads((tmp_path / "compare.json").read_text(encoding="utf-8"))["warmup"]["how"] == warmup.RELAY
    slept.clear()
    code = compare.run_steps(steps, tmp_path, runner=run, loader_open=lambda: True, log=said.append, warmup=wait,
                             sleep=slept.append)
    assert slept == [] and code != EXIT_OK, "a Teensy Loader open: the steps stop, with no half hour spent first"


def test_the_compare_switches_mains_that_are_off_on_before_its_steps(monkeypatch, tmp_path, capsys):
    order = []
    monkeypatch.setattr(warmup, "relay_is_off", lambda station: True)
    monkeypatch.setattr(warmup, "switch_on", lambda station: order.append(("switch_on", station)))
    monkeypatch.setattr(compare, "folder_for", lambda reports, station: tmp_path)

    def run_steps(steps, folder, **kw):
        order.append(("run_steps", kw["warmup"].wait_s, json.loads(warmup.os.environ[warmup.ENV])["how"]))
        (folder / "compare.json").write_text(json.dumps({"steps": []}), encoding="utf-8")
        return EXIT_OK
    monkeypatch.setattr(compare, "run_steps", run_steps)
    assert main(["compare", "--station", "s1", "--base", "b", "--tip", "t", "--no-probes", "--json"]) == EXIT_OK
    assert order == [("switch_on", "s1"), ("run_steps", 30 * 60, warmup.RELAY)]
    assert "[compare] preflight" in capsys.readouterr().err


def test_a_rig_in_use_is_named_not_asked_about(monkeypatch, capsys):
    from treadmill.errors import EXIT_NO_STATION
    from treadmill.station.lock import RigBusy

    def busy(station):
        raise RigBusy("rigA", "treadmill compare --station rigA-sys24-trplus")
    monkeypatch.setattr(warmup, "relay_is_off", busy)
    args = ["compare", "--station", "s1", "--base", "b", "--tip", "t", "--no-probes"]
    assert main(args + ["--json"]) == EXIT_NO_STATION
    assert "in use by treadmill compare --station rigA-sys24-trplus" in json.loads(capsys.readouterr().err)["error"]
    assert main(args + ["--dry-run", "--json"]) == EXIT_OK
    assert "in use by" in json.loads(capsys.readouterr().out)["preflight"]["warm_up"]


def test_without_git_the_preflight_shows_the_ref_as_given(monkeypatch):
    import subprocess
    from treadmill.cli import _resolve_ref

    def no_git(*a, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setenv("TREADMILL_FW_REPO", "C:/somewhere")
    monkeypatch.setattr(subprocess, "run", no_git)
    assert _resolve_ref("s1", "main") is None
