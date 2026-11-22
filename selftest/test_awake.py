"""The screen-on hold that run, sweep and exec take (treadmill/awake.py)."""

import contextlib

import pytest

import treadmill.cli as cli
from treadmill import awake


def _recorder(monkeypatch, answer=1):
    calls = []

    def set_state(flags):
        calls.append(flags)
        return answer
    monkeypatch.setattr(awake, "WINDOWS", True)
    monkeypatch.setattr(awake, "_set_state", set_state)
    return calls


def test_the_hold_keeps_the_screen_on_and_lets_go(monkeypatch):
    calls = _recorder(monkeypatch)
    with awake.screen_on():
        assert calls == [awake.ES_CONTINUOUS | awake.ES_SYSTEM_REQUIRED | awake.ES_DISPLAY_REQUIRED]
    assert calls[-1] == awake.ES_CONTINUOUS


def test_the_hold_lets_go_when_the_command_fails(monkeypatch):
    calls = _recorder(monkeypatch)
    with pytest.raises(RuntimeError), awake.screen_on():
        raise RuntimeError("the station went away")
    assert calls == [awake.HOLD, awake.ES_CONTINUOUS]


def test_a_refused_hold_says_so_and_the_command_still_runs(monkeypatch):
    calls = _recorder(monkeypatch, answer=0)
    said = []
    ran = False
    with awake.screen_on(said.append):
        ran = True
    assert ran and calls == [awake.HOLD]
    assert "keep the screen on" in said[0]


def test_off_windows_it_does_nothing(monkeypatch):
    monkeypatch.setattr(awake, "WINDOWS", False)
    monkeypatch.setattr(awake, "_set_state", lambda flags: pytest.fail("called off Windows"))
    with awake.screen_on():
        pass


def test_run_sweep_and_exec_hold_it_and_the_others_do_not(monkeypatch):
    held = [False]
    seen = {}

    @contextlib.contextmanager
    def screen_on(log=print):
        held[0] = True
        yield
        held[0] = False
    monkeypatch.setattr(awake, "screen_on", screen_on)
    for name in ("cmd_run", "cmd_exec", "cmd_sweep", "cmd_stations", "cmd_screen"):
        monkeypatch.setattr(cli, name, lambda args, n=name: seen.__setitem__(n, held[0]) or 0)
    for argv in (["run", "--station", "s"], ["exec", "--station", "s", "x.py"], ["sweep", "p", "--station", "s"],
                 ["stations"], ["screen", "--station", "s"]):
        assert cli.main(argv) == 0
    assert seen == {"cmd_run": True, "cmd_exec": True, "cmd_sweep": True,
                    "cmd_stations": False, "cmd_screen": False}
