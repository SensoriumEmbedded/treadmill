"""Power control backends: cut and restore the machine's mains.

``none`` refuses every call and ``manual`` asks whoever is at the bench.
``serial-relay`` drives the mains relay: a Teensy that switches the bench
supply's mains through a solid-state relay, with the supply's own switch as
its bypass (the mains-relay repository; its contract is
``docs/Host_Protocol.md``, proto 1). The LAN smart plug and the Ultimate's
own power control would go behind the same calls.

The mains relay is found by its USB serial string, ``MainsRelay_<chip id>``,
without opening any port, and is opened for each call and closed after it,
so nothing holds its port between power cycles. A closed port is a DTR drop,
which the relay answers by restoring power: only ``off()`` keeps the port
open, until ``on()`` or ``close()``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from treadmill.errors import TreadmillError
from treadmill.link.transport import ByteStream, TransportError, open_serial
from treadmill.station.kbd import Reply, parse_reply
from treadmill.station.model import PowerSpec

RELAY_PROTO = 1
RELAY_PREFIX = "MAINSRELAY_"     # as Windows reports it; the relay says MainsRelay_
REPLY_S = 2.0                    # a reply comes within this of the end of its action
CYCLE_MAX_MS = 60000             # the longest CYCLE the relay takes


class PowerUnavailable(TreadmillError):
    pass


class Power:
    def check(self) -> str | None:
        """That the power control can be used, before anything needs it: the
        channel's state (``on``, ``off``, ``cycling``) where the backend can
        tell, else None. Raises PowerUnavailable when it can't be used."""
        return None

    def off(self) -> None:
        raise NotImplementedError

    def on(self) -> None:
        raise NotImplementedError

    def cycle(self, off_seconds: float) -> None:
        self.off()
        time.sleep(off_seconds)
        self.on()

    def close(self) -> None:
        """Let go of anything the backend holds open."""


class NullPower(Power):
    def __init__(self, why: str = "this station has no power control"):
        self.why = why

    def check(self) -> str | None:
        raise PowerUnavailable(self.why)

    def off(self) -> None:
        raise PowerUnavailable(self.why)

    def on(self) -> None:
        raise PowerUnavailable(self.why)


class ManualPower(Power):
    """Asks whoever is at the bench. Only for attended runs; an unattended
    run treats a manual station as having no power control."""

    def __init__(self, prompt=input):
        self._prompt = prompt

    def off(self) -> None:
        self._prompt("Switch the machine OFF, then press Enter: ")

    def on(self) -> None:
        self._prompt("Switch the machine ON, then press Enter: ")


# -- the mains relay ----------------------------------------------------------------

@dataclass(frozen=True)
class RelayPort:
    port: str
    serial: str      # the USB serial string as the OS reports it
    unit: str        # the relay's chip id


def relay_unit(serial_string: str | None) -> str | None:
    """The chip id in a mains relay's USB serial string, or None for any
    other device. Windows reports the string in capitals."""
    if not serial_string or not serial_string.upper().startswith(RELAY_PREFIX):
        return None
    unit = serial_string[len(RELAY_PREFIX):]
    return unit if unit.isdigit() else None


def find_relays() -> list[RelayPort]:
    """Every mains relay on USB, by serial string, whatever its vendor id; no
    port is opened."""
    from serial.tools import list_ports  # pyserial, imported here so the self-tests need no port

    out = []
    for p in list_ports.comports():
        unit = relay_unit(p.serial_number)
        if unit:
            out.append(RelayPort(p.device, p.serial_number, unit))
    return out


def find_relay(unit: str | None = None) -> RelayPort:
    """The mains relay with chip id ``unit``, or the only one on USB when
    ``unit`` is None."""
    relays = find_relays()
    match = [r for r in relays if unit is None or r.unit == str(unit)]
    if len(match) == 1:
        return match[0]
    seen = ", ".join(f"{r.port} (unit {r.unit})" for r in relays) or "none"
    if not match:
        want = f"unit {unit}" if unit else "at all"
        raise PowerUnavailable(f"no mains relay {want} on USB (a USB serial string MainsRelay_<chip id>); "
                               f"relays present: {seen}")
    raise PowerUnavailable(f"{len(match)} mains relays on USB ({seen}); name one as unit in the rig's [power]")


class SerialRelayPower(Power):
    """The mains relay. ``connect`` replaces discovery and the serial port
    (a fake relay in the self-tests)."""

    def __init__(self, spec: PowerSpec, connect: Callable[[], ByteStream] | None = None):
        self.spec = spec
        self._connect = connect
        self._held: ByteStream | None = None
        self.identity: dict[str, str] = {}      # the fields of the last ?
        self.port: str | None = None

    @property
    def channel(self) -> int:
        return self.spec.channel

    def _open(self) -> ByteStream:
        if self._connect is not None:
            return self._connect()
        found = find_relay(self.spec.unit)
        self.port = found.port
        try:
            return open_serial(found.port)
        except TransportError as e:
            raise PowerUnavailable(f"mains relay unit {found.unit}: {e}") from None

    @staticmethod
    def _send(stream: ByteStream, data: bytes) -> None:
        try:
            stream.write(data)
        except TransportError as e:
            raise PowerUnavailable(f"mains relay: {e}") from None

    def _command(self, stream: ByteStream, line: str, timeout: float = REPLY_S) -> Reply:
        """``line``'s reply. ``EV`` lines, and replies to other commands, are passed over."""
        want = line.split()[0].upper()
        self._send(stream, line.encode("ascii") + b"\n")
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise PowerUnavailable(f"no reply to {line!r} from the mains relay within {timeout:g} s")
            chunk = stream.read_until(b"\n", left)
            if chunk is None:
                if stream.ended:
                    raise PowerUnavailable(f"the mains relay's port closed before it answered {line!r}")
                continue
            reply = parse_reply(chunk.decode("ascii", "replace").strip(), time.monotonic())
            if reply is not None and reply.cmd in (want, "*"):
                return reply

    def _session(self) -> ByteStream:
        """The relay's port, open and checked: it speaks proto 1, it is the
        unit the rig names, and it has the rig's channel. A bare LF first
        ends whatever a previous host left half sent."""
        stream = self._open()
        try:
            self._send(stream, b"\n")
            reply = self._command(stream, "?")
            if not reply.ok:
                raise PowerUnavailable(f"the mains relay refused '?': {reply.raw}")
            if reply.get("proto") != str(RELAY_PROTO):
                raise PowerUnavailable(f"the mains relay speaks protocol {reply.get('proto')}; "
                                       f"treadmill speaks {RELAY_PROTO}")
            if self.spec.unit and reply.get("chip") != str(self.spec.unit):
                raise PowerUnavailable(f"the mains relay is unit {reply.get('chip')}, not {self.spec.unit}")
            channels = reply.get("ch") or "0"
            if not channels.isdigit() or not 1 <= self.channel <= int(channels):
                raise PowerUnavailable(f"the mains relay has {channels} channel(s); the rig names channel "
                                       f"{self.channel}")
            self.identity = dict(reply.fields)
        except BaseException:
            stream.close()
            raise
        return stream

    def _off_ms(self, off_seconds: float) -> int:
        ms = round(off_seconds * 1000)
        lo = int(self.identity.get("min_off_ms") or 2000)
        if not lo <= ms <= CYCLE_MAX_MS:
            raise PowerUnavailable(f"off_seconds {off_seconds:g} is outside the mains relay's "
                                   f"{lo / 1000:g} to {CYCLE_MAX_MS / 1000:g} s")
        return ms

    def check(self) -> str | None:
        stream = self._session()
        try:
            self._off_ms(self.spec.off_seconds)
            states = (self.identity.get("state") or "").split(",")
            return states[self.channel - 1] if len(states) >= self.channel else None
        finally:
            stream.close()

    def cycle(self, off_seconds: float) -> None:
        """Off for ``off_seconds``, timed by the relay, which replies once power is back."""
        stream = self._session()
        try:
            ms = self._off_ms(off_seconds)
            reply = self._command(stream, f"CYCLE {self.channel} {ms}", timeout=ms / 1000 + REPLY_S)
        finally:
            stream.close()
        if not reply.ok:
            raise PowerUnavailable(f"the mains relay refused CYCLE {self.channel}: {reply.raw}")

    def off(self) -> None:
        """Off until ``on()`` or ``close()``: the port stays open meanwhile,
        and the relay restores power by itself after its limit (10 minutes)."""
        if self._held is not None:
            return
        stream = self._session()
        try:
            reply = self._command(stream, f"OFF {self.channel}")
        except BaseException:
            stream.close()
            raise
        if not reply.ok:
            stream.close()
            raise PowerUnavailable(f"the mains relay refused OFF {self.channel}: {reply.raw}")
        self._held = stream

    def on(self) -> None:
        stream, self._held = self._held, None
        if stream is None:
            stream = self._session()
        try:
            min_off = int(self.identity.get("min_off_ms") or 2000) / 1000
            reply = self._command(stream, f"ON {self.channel}", timeout=min_off + REPLY_S)
        finally:
            stream.close()
        if not reply.ok:
            raise PowerUnavailable(f"the mains relay refused ON {self.channel}: {reply.raw}")

    def close(self) -> None:
        stream, self._held = self._held, None
        if stream is not None:
            stream.close()


def make_power(spec: PowerSpec | None, attended: bool = False) -> Power:
    if spec is None or spec.backend == "none":
        return NullPower()
    if spec.backend == "manual":
        return ManualPower() if attended else NullPower("manual power control asks a person: run with --attended")
    if spec.backend == "serial-relay":
        return SerialRelayPower(spec)
    if spec.backend in {"http-plug", "u64"}:
        raise PowerUnavailable(f"power backend {spec.backend!r} is not built yet")
    raise PowerUnavailable(f"unknown power backend {spec.backend!r}")
