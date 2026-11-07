"""Find a station's board by its chip id, without guessing a port name.

A TeensyROM enumerates with PJRC's vendor id and a device serial string that
carries its chip id (the ``UID`` the version banner prints), so the port can
be matched without opening it. When the serial string does not say, the
candidates are opened one at a time and asked.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from treadmill.errors import TreadmillError
from treadmill.link.transport import TransportError, list_teensyrom_ports, open_serial
from treadmill.link.trlink import Link, LinkError


class NotFound(TreadmillError):
    pass


@dataclass(frozen=True)
class Found:
    port: str
    uid: str
    by: str   # "serial-string" | "banner"


def _matches(serial_string: str | None, uid: str) -> bool:
    return bool(serial_string) and serial_string.upper().endswith(uid.upper())


def find_port(uid: str, port_hint: str | None = None, timeout: float = 0.0) -> Found:
    """The serial port of the board whose chip id is ``uid``.

    Looks at the device serial strings first; only when none names the uid are
    the PJRC ports opened and their banners read, the hinted port first. With a
    ``timeout`` the search repeats until a board appears, for reconnecting after
    a flash or a reboot.
    """
    deadline = time.monotonic() + timeout
    while True:
        ports = list_teensyrom_ports()
        for port, serial_string in ports:
            if _matches(serial_string, uid):
                return Found(port, uid, "serial-string")
        candidates = [p for p, _ in ports]
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
