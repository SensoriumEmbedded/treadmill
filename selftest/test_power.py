"""The serial-relay power backend against the fake mains relay: it is found
by serial string, checks what answers before it trusts it, and leaves the
machine powered whenever it lets go of the port."""

import time

import pytest

import treadmill.station.power as power_module
from selftest.fake_relay import FakeRelay
from treadmill.station.model import PowerSpec
from treadmill.station.power import (ManualPower, NullPower, PowerUnavailable, RelayPort, SerialRelayPower,
                                     find_relay, make_power, relay_unit)

SPEC = PowerSpec(backend="serial-relay", off_seconds=5.0)


def _power(relay: FakeRelay, **spec) -> SerialRelayPower:
    return SerialRelayPower(PowerSpec(backend="serial-relay", **spec), connect=relay.connect)


def _until(cond, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


def test_a_relay_is_named_by_its_serial_string():
    assert relay_unit("MainsRelay_12345678") == "12345678"
    assert relay_unit("MAINSRELAY_12345678") == "12345678"        # as Windows reports it
    assert relay_unit("C64USBKeyboard_12345678") is None
    assert relay_unit("MainsRelay_x") is None and relay_unit(None) is None


def test_find_relay_takes_the_one_on_usb_or_the_unit_named(monkeypatch):
    two = [RelayPort("COM3", "MAINSRELAY_111", "111"), RelayPort("COM4", "MAINSRELAY_222", "222")]
    monkeypatch.setattr(power_module, "find_relays", lambda: two[:1])
    assert find_relay().port == "COM3"
    monkeypatch.setattr(power_module, "find_relays", lambda: two)
    assert find_relay("222").port == "COM4"
    with pytest.raises(PowerUnavailable, match="2 mains relays on USB"):
        find_relay()
    with pytest.raises(PowerUnavailable, match="no mains relay unit 333"):
        find_relay("333")
    monkeypatch.setattr(power_module, "find_relays", lambda: [])
    with pytest.raises(PowerUnavailable, match="no mains relay at all"):
        find_relay()


def test_check_reads_the_channel_and_keeps_the_identity():
    relay = FakeRelay(chip="12345678")
    power = _power(relay, unit="12345678")
    assert power.check() == "on"
    assert power.identity["proto"] == "1" and power.identity["chip"] == "12345678"
    assert relay.switched == []


@pytest.mark.parametrize("relay, spec, match", [
    (dict(proto=2), {}, "speaks protocol 2"),
    (dict(chip="1"), dict(unit="2"), "unit 1, not 2"),
    (dict(channels=1), dict(channel=2), "has 1 channel"),
    ({}, dict(off_seconds=1.0), "off_seconds 1 is outside"),
    ({}, dict(off_seconds=61.0), "off_seconds 61 is outside"),
])
def test_check_refuses_what_the_rig_did_not_ask_for(relay, spec, match):
    with pytest.raises(PowerUnavailable, match=match):
        _power(FakeRelay(**relay), **spec).check()


def test_cycle_is_timed_by_the_relay_and_returns_with_power_back():
    relay = FakeRelay()
    _power(relay).cycle(5.0)
    assert relay.switched == [(1, False), (1, True)]
    assert relay.cuts_ms and abs(relay.cuts_ms[0] - 5000) < 500
    assert "CYCLE 1 5000" in relay.received


def test_cycle_refuses_an_off_time_the_relay_would_not_take():
    relay = FakeRelay()
    with pytest.raises(PowerUnavailable, match="outside"):
        _power(relay).cycle(0.5)
    assert relay.switched == []


def test_off_holds_the_port_until_on():
    relay = FakeRelay()
    power = _power(relay)
    power.off()
    time.sleep(0.05)                                    # 5 s of the relay's time: past its minimum off
    assert not relay.power()
    power.on()
    assert relay.power() and relay.received[-1] == "ON 1"


def test_closing_a_held_power_off_restores_the_machine():
    relay = FakeRelay()
    power = _power(relay)
    power.off()
    power.close()                                       # the port closes: a DTR drop
    assert _until(relay.power)


def test_a_relay_that_does_not_answer_is_unavailable():
    def refused():
        raise PowerUnavailable("no mains relay at all on USB")
    with pytest.raises(PowerUnavailable, match="no mains relay"):
        SerialRelayPower(SPEC, connect=refused).check()


def test_make_power_picks_the_backend():
    assert isinstance(make_power(SPEC), SerialRelayPower)
    assert isinstance(make_power(PowerSpec(backend="manual"), attended=True), ManualPower)
    unattended = make_power(PowerSpec(backend="manual"))
    assert isinstance(unattended, NullPower)
    with pytest.raises(PowerUnavailable, match="--attended"):
        unattended.check()
    with pytest.raises(PowerUnavailable, match="not built yet"):
        make_power(PowerSpec(backend="http-plug"))
