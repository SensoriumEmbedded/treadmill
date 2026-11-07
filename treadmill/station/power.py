"""Power control backends: cut and restore the machine's mains.

Phase 1 ships the interface and the two backends that need no hardware:
``none`` (every call refuses) and ``manual`` (a person is asked). The
serial-relay backend for the user's own microcontroller and the LAN smart
plug arrive in Phase 2, behind the same two calls.
"""

from __future__ import annotations

import time

from treadmill.errors import TreadmillError
from treadmill.station.model import PowerSpec


class PowerUnavailable(TreadmillError):
    pass


class Power:
    def off(self) -> None:
        raise NotImplementedError

    def on(self) -> None:
        raise NotImplementedError

    def cycle(self, off_seconds: float) -> None:
        self.off()
        time.sleep(off_seconds)
        self.on()


class NullPower(Power):
    def off(self) -> None:
        raise PowerUnavailable("this station has no power control")

    on = off


class ManualPower(Power):
    """Asks whoever is at the bench. Only for attended runs; an unattended
    run treats a manual station as having no power control."""

    def __init__(self, prompt=input):
        self._prompt = prompt

    def off(self) -> None:
        self._prompt("Switch the machine OFF, then press Enter: ")

    def on(self) -> None:
        self._prompt("Switch the machine ON, then press Enter: ")


def make_power(spec: PowerSpec | None, attended: bool = False) -> Power:
    if spec is None or spec.backend == "none":
        return NullPower()
    if spec.backend == "manual":
        return ManualPower() if attended else NullPower()
    if spec.backend in {"serial-relay", "http-plug", "u64"}:
        raise PowerUnavailable(f"power backend {spec.backend!r} is a Phase 2 item")
    raise PowerUnavailable(f"unknown power backend {spec.backend!r}")
