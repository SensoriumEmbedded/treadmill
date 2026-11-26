"""A latched OFF (mains relay fw 1.1.0): the backend latches the channel and
lets go of the port with the machine still off, a station whose channel is
latched is turned on by the next open (a status check reports it and
switches nothing), and ``treadmill power off --latch`` / ``on`` switch it
with no TeensyROM needed."""

import json

import pytest

import treadmill.session as session_module
from selftest.fake_relay import FakeRelay
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from selftest.test_power_ladder import POWER, power_lab  # noqa: F401
from selftest.test_station_keyboard import kb_lab  # noqa: F401
from treadmill.session import open_station
from treadmill.cli import EXIT_NO_STATION, EXIT_OK, EXIT_USAGE, main
from treadmill.station.model import PowerSpec
from treadmill.station.power import PoweredDown, PowerUnavailable, SerialRelayPower


def _power(relay: FakeRelay) -> SerialRelayPower:
    return SerialRelayPower(PowerSpec(backend="serial-relay", off_seconds=5.0), connect=relay.connect)


def test_off_latched_stays_off_with_the_port_closed_until_on():
    relay = FakeRelay()
    power = _power(relay)
    power.off_latched()
    assert relay.received[-1] == "OFF 1 LATCH" and not relay.power()
    assert power.check() == "off" and power.latched
    assert not relay.power()                     # a check's open and close (a DTR drop) leaves it off
    power.on()
    assert relay.power() and power.check() == "on" and not power.latched


def test_a_plain_off_is_not_latched():
    relay = FakeRelay()
    power = _power(relay)
    power.off()
    assert power.check() == "off" and not power.latched     # the check's DTR drop releases it
    power.close()


def test_off_latched_on_a_relay_without_latch_says_which_firmware():
    relay = FakeRelay(fw="1.0.0")
    with pytest.raises(PowerUnavailable, match="fw 1.0.0.*needs fw 1.1.0"):
        _power(relay).off_latched()
    assert relay.power()


@pytest.fixture
def shared_relay(power_lab, monkeypatch):  # noqa: F811
    """One fake relay behind every open in this process, so a latch outlasts a command."""
    relay = FakeRelay()
    real = session_module.fake_relay

    def shared(station, tr=None):
        made = real(station, tr)
        return None if made is None else (lambda: SerialRelayPower(station.rig.power, connect=relay.connect))
    monkeypatch.setattr(session_module, "fake_relay", shared)
    return relay


def test_treadmill_power_off_latch_then_on(shared_relay, capsys):
    assert main(["power", "--station", "kb", "off", "--latch", "--json"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["latched"] and out["power"] == "off" and not shared_relay.power()

    # status reports the latch and switches nothing
    assert main(["power", "--station", "kb", "status", "--json"]) == EXIT_NO_STATION
    err = capsys.readouterr().err
    assert "powered down on purpose" in err and "treadmill power --station kb on" in err
    assert not shared_relay.power()

    assert main(["power", "--station", "kb", "on", "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["power"] == "on" and shared_relay.power()
    assert main(["power", "--station", "kb", "status", "--json"]) == EXIT_OK


def test_opening_a_latched_station_turns_it_on(shared_relay):
    assert main(["power", "--station", "kb", "off", "--latch"]) == EXIT_OK
    logged = []
    with open_station("kb", video=False, log=logged.append) as s:
        assert s.board.latch_cleared and shared_relay.power()
        assert s.board.station.has("power") and s.board.power_state == "on"
    assert any("was latched off" in line and "turned it on" in line for line in logged)
    assert shared_relay.received.count("ON 1") == 1


def test_an_open_that_switches_nothing_refuses_a_latch(shared_relay):
    assert main(["power", "--station", "kb", "off", "--latch"]) == EXIT_OK
    with pytest.raises(PoweredDown, match="powered down on purpose.*A run turns it on"):
        open_station("kb", video=False, power_on_latched=False)
    assert not shared_relay.power()


def test_treadmill_power_off_needs_latch(power_lab, capsys):  # noqa: F811
    assert main(["power", "--station", "kb", "off"]) == EXIT_USAGE
    assert "--latch" in capsys.readouterr().err
    assert main(["power", "--station", "kb", "cycle", "--latch"]) == EXIT_USAGE


def test_a_bad_off_seconds_does_not_hide_a_latch():
    relay = FakeRelay()
    _power(relay).off_latched()
    power = SerialRelayPower(PowerSpec(backend="serial-relay", off_seconds=0.5), connect=relay.connect)
    with pytest.raises(PowerUnavailable, match="off_seconds"):
        power.check()
    assert power.latched
    assert power.on_if_latched("kb") and relay.power()
