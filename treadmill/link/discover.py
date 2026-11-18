"""Find a station's board by its chip id, without guessing a port name.

A TeensyROM enumerates with PJRC's vendor id and a device serial string that
carries its chip id (the ``UID`` the version banner prints), so the port can
be matched without opening it. When the serial string does not say, the
candidates are opened one at a time and asked.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from treadmill.errors import EXIT_NO_STATION, TreadmillError
from treadmill.link.transport import TransportError, list_teensyrom_ports, open_serial
from treadmill.link.trlink import Link, LinkError


class NotFound(TreadmillError):
    code = EXIT_NO_STATION


@dataclass(frozen=True)
class Found:
    port: str
    uid: str
    by: str   # "serial-string" | "banner"


def _unnamed(serial_string: str | None) -> bool:
    """A PJRC device whose serial string doesn't say what it is: none at all,
    or PJRC's default number, as a TeensyROM firmware too old to name itself
    reports. Only those are opened and asked for a banner. A named device,
    another TeensyROM or a keyboard board, is never sent TeensyROM tokens."""
    return not serial_string or serial_string.isdigit()


def _matches(serial_string: str | None, uid: str) -> bool:
    return bool(serial_string) and serial_string.upper().endswith(uid.upper())


def teensyrom_uid(serial_string: str | None) -> str | None:
    """The chip id in a TeensyROM's USB serial string (``TeensyROM-Serial-12345678``,
    which Windows reports in capitals), or None for another PJRC device such
    as a keyboard board or a firmware too old to name itself."""
    if not serial_string or not serial_string.upper().startswith("TEENSYROM"):
        return None
    uid = serial_string.rsplit("-", 1)[-1]
    return uid if uid.isdigit() else None


def wait_for_teensyrom(exclude: tuple[str, ...] = (), timeout: float | None = None, poll: float = 0.5) -> Found:
    """The first TeensyROM to be on USB, whichever board it is, found by its
    serial string without opening any port. A TR+ is powered by the machine,
    so this is how a board that was just switched on is noticed."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        for port, serial_string in list_teensyrom_ports():
            uid = teensyrom_uid(serial_string)
            if uid and port not in exclude:
                return Found(port, uid, "serial-string")
        if deadline is not None and time.monotonic() >= deadline:
            raise NotFound(f"no TeensyROM appeared on USB within {timeout:g} s")
        time.sleep(poll)


def find_port(uid: str, port_hint: str | None = None, timeout: float = 0.0) -> Found:
    """The serial port of the board whose chip id is ``uid``.

    Looks at the device serial strings first; only when none names the uid are
    the unnamed PJRC ports opened and their banners read, the hinted port first. With a
    ``timeout`` the search repeats until a board appears, for reconnecting after
    a flash or a reboot.
    """
    deadline = time.monotonic() + timeout
    while True:
        ports = list_teensyrom_ports()
        for port, serial_string in ports:
            if _matches(serial_string, uid):
                return Found(port, uid, "serial-string")
        candidates = [p for p, s in ports if _unnamed(s)]
        if port_hint and port_hint in candidates:
            candidates.remove(port_hint)
            candidates.insert(0, port_hint)
        for port in candidates:
            try:
                with Link(open_serial(port)) as link:
                    banner = link.banner()
            except (TransportError, LinkError):
                continue
            if banner.uid == uid:
                return Found(port, uid, "banner")
        if time.monotonic() >= deadline:
            seen = ", ".join(f"{p} ({s or 'no serial string'})" for p, s in ports) or "none"
            raise NotFound(f"no TeensyROM with chip id {uid}; PJRC ports present: {seen}")
        time.sleep(0.5)
