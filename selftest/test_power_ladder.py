"""The power-cycle rung on a fake station whose rig has a mains relay: the
cut takes the fake TeensyROM and keyboard board off USB, the board comes
back to its menu, and what the cycle undid is put back. Also the check at
open, the not-found hint, ``treadmill power`` and the report's power line."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

import treadmill.board as board_module
from selftest.fake_board import start_fake_board
from selftest.fake_relay import FakeRelay
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from selftest.test_station_keyboard import kb_lab  # noqa: F401
from treadmill.board import STAYED_ON, Board
from treadmill.cli import EXIT_NO_STATION, EXIT_OK, main
from treadmill.link import Link, LinkError, NotFound
from treadmill.report.html import _power
from treadmill.session import open_station
from treadmill.station import load_station
from treadmill.station.kawari import set_standard
from treadmill.station.model import PowerSpec
from treadmill.station.power import PowerUnavailable, SerialRelayPower

POWER = '\n[power]\nbackend = "serial-relay"\noff_seconds = 2.0\n'


@pytest.fixture
def power_lab(kb_lab: Path) -> Path:  # noqa: F811
    rig = kb_lab / "data" / "rigs" / "rigA.toml"
    rig.write_text(rig.read_text(encoding="utf-8") + POWER, encoding="utf-8")
    return kb_lab


def test_the_station_opens_with_its_relay_checked_and_switches_nothing(power_lab):
    with open_station("kb", video=False) as s:
        b = s.board
        assert b.station.has("power") and b.power_problem is None and b.power_state == "on"
        assert b.power.identity["proto"] == "1"


def test_a_power_cycle_brings_the_menu_and_the_keyboard_board_back(power_lab):
    with open_station("kb", video=False) as s:
        b = s.board
        before = b.kbd
        timings = b.power_cycle()
        assert set(timings) == {"off_s", "left_usb_s", "usb_back_s", "menu_s"}
        assert all(v is not None and v >= 0 for v in timings.values())
        assert before.closed and b.kbd is not before and not b.kbd.closed
        assert b.station.has("keys_hw", "tod_select", "power") and b.recoveries == []
        b.link.ping()


def test_a_power_cycle_puts_the_run_s_region_back(power_lab):
    """A Kawari's V setting lasts until the C64 powers off: after the cycle
    the fake comes up in J1 open's NTSC, and the rung switches it back."""
    with open_station("kb", video=False) as s:
        b = s.board
        assert set_standard(b, "PAL", log=lambda _: None) and b.station.standard == "PAL"
        b.region = "PAL"
        b.power_cycle()
        assert b.station.standard == "PAL"
        assert [line.strip() for _, line in b.kbd.sent if line.startswith("V ")] == ["V closed"]


def test_the_ladder_climbs_to_the_power_cycle_when_nothing_below_works(power_lab):
    with open_station("kb", video=False) as s:
        b = s.board

        def fails() -> None:
            raise LinkError("no answer")
        b._rung_reset = fails
        b.ext_reset = fails
        b.link.stream.close()                           # the board stopped answering
        b.ensure_menu()
        assert b.recoveries == ["power-cycle"] and b.power_timings["menu_s"] is not None
        b.link.ping()


def test_a_board_that_stays_on_usb_fails_the_rung(lab):  # noqa: F811
    """The supply's switch on ON, or another machine on the relay: the cut
    happens, the board never sees it."""
    _, stream = start_fake_board()
    relay = FakeRelay()                                 # nothing on its outlet
    spec = PowerSpec(backend="serial-relay", off_seconds=2.0)
    station = load_station("s1")
    station = replace(station, rig=replace(station.rig, power=spec), caps=station.caps | {"power"})
    board = Board.open(station, link_factory=lambda: Link(stream),
                       power_factory=lambda: SerialRelayPower(spec, connect=relay.connect))
    try:
        assert board.station.has("power")
        with pytest.raises(PowerUnavailable, match="stayed on USB"):
            board.power_cycle()
        assert relay.switched == [(1, False), (1, True)]
        assert STAYED_ON.startswith("the board stayed on USB")
    finally:
        board.close()


def test_a_relay_that_does_not_answer_turns_power_off_with_the_reason(power_lab):
    station = load_station("kb")

    def refused():
        raise PowerUnavailable("no mains relay at all on USB")
    _, stream = start_fake_board()
    board = Board.open(station, link_factory=lambda: Link(stream),
                       power_factory=lambda: SerialRelayPower(station.rig.power, connect=refused))
    try:
        assert not board.station.has("power") and "no mains relay" in board.power_problem
        board.probe()                                   # a re-probe keeps it off
        assert not board.station.has("power")
        with pytest.raises(PowerUnavailable, match="lacks power .no mains relay"):
            board.power_cycle()
    finally:
        board.close()


def test_a_missing_board_s_error_says_what_the_relay_shows(power_lab, monkeypatch):
    def nowhere(uid, hint=None, timeout=0.0):
        raise NotFound(f"no TeensyROM with chip id {uid}")
    monkeypatch.setattr(board_module, "find_port", nowhere)
    station = load_station("kb")
    relay = FakeRelay()
    with pytest.raises(NotFound, match="channel 1 on: check that the machine's power switch is on"):
        Board.open(station, power_factory=lambda: SerialRelayPower(station.rig.power, connect=relay.connect))

    def refused():
        raise PowerUnavailable("no mains relay at all on USB")
    with pytest.raises(NotFound, match="doesn't answer .no mains relay.*AUTO"):
        Board.open(station, power_factory=lambda: SerialRelayPower(station.rig.power, connect=refused))


def test_treadmill_power_status_and_cycle(power_lab, capsys):
    assert main(["power", "--station", "kb", "status", "--json"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["power"]["backend"] == "serial-relay" and out["power"]["state"] == "on"
    assert main(["power", "--station", "kb", "cycle", "--json"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert set(out["timings"]) == {"off_s", "left_usb_s", "usb_back_s", "menu_s"}


def test_treadmill_power_status_fails_without_a_working_relay(kb_lab, capsys):  # noqa: F811
    rig = kb_lab / "data" / "rigs" / "rigA.toml"
    rig.write_text(rig.read_text(encoding="utf-8") + '\n[power]\nbackend = "manual"\n', encoding="utf-8")
    assert main(["power", "--station", "kb", "status", "--json"]) == EXIT_NO_STATION
    out = json.loads(capsys.readouterr().out)
    assert not out["ok"] and "--attended" in out["power"]["problem"]


def test_power_check_cycles_once_before_the_first_test(pytester: pytest.Pytester, power_lab: Path):
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_hw="def test_ping_at_menu(at_menu):\n    at_menu.link.ping()\n")
    result = pytester.runpytest("--station", "kb", "--power-check", "--reports", str(power_lab / "reports"),
                                "-p", "no:anyio", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    meta = json.loads((next((power_lab / "reports").iterdir()) / "run.json").read_text(encoding="utf-8"))
    assert set(meta["power_check"]) == {"off_s", "left_usb_s", "usb_back_s", "menu_s"}
    assert meta["station"]["power"]["on"] and meta["recoveries"] == []


def test_power_check_ends_the_run_when_the_cycle_fails(pytester: pytest.Pytester, kb_lab: Path):  # noqa: F811
    rig = kb_lab / "data" / "rigs" / "rigA.toml"
    rig.write_text(rig.read_text(encoding="utf-8") + '\n[power]\nbackend = "manual"\n', encoding="utf-8")
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_hw="def test_ping_at_menu(at_menu):\n    at_menu.link.ping()\n")
    result = pytester.runpytest("--station", "kb", "--power-check", "--reports", str(kb_lab / "reports"),
                                "-p", "no:anyio", "-p", "no:cacheprovider")
    assert result.ret == EXIT_NO_STATION
    result.stdout.fnmatch_lines(["*--power-check: station kb lacks power (manual power control asks a person*"])


def test_the_report_s_power_line():
    relay = {"backend": "serial-relay", "unit": "21474836", "channel": 1, "on": True}
    assert _power(None, None, None) == ""
    assert _power(relay, None, []) == "serial-relay unit 21474836, channel 1"
    check = {"left_usb_s": 0.4, "menu_s": 9.1}
    assert _power(relay, check, ["reset", "power-cycle", "power-cycle"]) == (
        "serial-relay unit 21474836, channel 1; --power-check: left USB 0.4 s after the cut, menu 9.1 s after "
        "power came back; the ladder cycled the mains 2 times")
    assert _power({**relay, "on": False, "problem": "no mains relay"}, None, None).endswith(": off (no mains relay)")
