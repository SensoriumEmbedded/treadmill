"""Put the firmware under test on the board, or record what is there.

``prepare_firmware()`` is the one path from a spec to a board that runs it:
look at what the board carries, settle the target, build (cached) or
inspect the hex, refuse an image for the other cartridge, flash unless the
board already runs that build, verify. The pytest plugin's ``firmware``
fixture and the driver's ``exec`` and ``shell`` call it, so a test run and a
one-off script put the same build on the board the same way, and the
record it leaves is what ``run.json`` carries.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from treadmill.errors import EXIT_FAILED, EXIT_NO_STATION, EXIT_USAGE, TreadmillError
from treadmill.firmware.build import BuildError, build
from treadmill.firmware.checkout import CheckoutError
from treadmill.firmware.flash import FlashError, flash
from treadmill.firmware.hexinfo import HexInfo, inspect_hex
from treadmill.firmware.spec import FirmwareSpec, SpecError
from treadmill.link import Link, LinkError, NotFound, TransportError, find_port, open_serial
from treadmill.link.banner import Banner
from treadmill.station.model import Station

CHECKOUT_ENV = "TREADMILL_FW_REPO"


class FirmwareError(TreadmillError):
    """The firmware under test could not be put on the board; ``code`` says
    why: the request (2), the station (4) or the build or flash itself (1)."""

    def __init__(self, message: str, code: int = EXIT_FAILED):
        super().__init__(message)
        self.code = code


@dataclass
class FirmwareRun:
    """What a run built and flashed, or found on the board."""
    spec: FirmwareSpec
    hex: HexInfo | None = None
    defines: tuple[str, ...] = ()           # what the flashed build carries, built or declared
    primary: Path | None = None             # the checkout whose tools build and flash
    target: str | None = None
    flashed: bool = False
    cached: bool = False
    record: dict = field(default_factory=dict)   # the ``firmware`` entry of run.json


def primary_checkout(station: Station) -> Path | None:
    """The TeensyROM checkout whose tools build and flash: the rig's, else
    the one TREADMILL_FW_REPO names."""
    where = station.rig.firmware_checkout or os.environ.get(CHECKOUT_ENV)
    return Path(where) if where else None


def current_banner(station: Station) -> tuple[Banner | None, str]:
    """A quick look at the board before any flash: its kind and build, and
    the port it answered on."""
    found = find_port(station.tr.uid, station.tr.port_hint)
    with Link(open_serial(found.port)) as link:
        image = link.fw_check()
        return (link.banner() if image else None), found.port


def prepare_firmware(spec: FirmwareSpec, station: Station, *, declared_defines: tuple[str, ...] = (),
                     flash_board: bool = True, primary: Path | None = None, log=print) -> FirmwareRun:
    """Build or inspect, then flash and verify, the firmware the spec asks
    for; with ``none`` only record the declared defines. The caller holds
    the rig lock: the flasher opens the board's port."""
    run = FirmwareRun(spec, defines=declared_defines, primary=primary or primary_checkout(station))
    run.record = {"spec": spec.describe(), "variant": spec.is_variant, "defines": run.defines}
    if spec.source_kind == "none":
        return run
    if run.primary is None:
        raise FirmwareError(f"no firmware checkout: set the rig's firmware_checkout or {CHECKOUT_ENV}",
                            EXIT_USAGE)
    try:
        current, port = current_banner(station)
    except (NotFound, TransportError, LinkError) as e:
        raise FirmwareError(f"firmware: {e}", EXIT_NO_STATION) from e
    target = spec.target
    if target == "auto":
        if current is None or current.kind is None:
            raise FirmwareError("--fw-target auto needs a board that answers; name tr or tr-plus",
                                EXIT_NO_STATION)
        target = current.kind
    run.target = target
    try:
        if spec.source_kind == "hex":
            run.hex = inspect_hex(spec.source)
        else:
            built = build(spec, run.primary, target, log=log)
            run.hex, run.cached, run.defines = built.hex, built.cached, spec.defines
            run.record.update({
                "commit": built.checkout.commit, "describe": built.checkout.describe,
                "dirty": built.checkout.dirty, "diff_hash": built.checkout.diff_hash,
                "cache_key": built.key, "cached": built.cached, "memory": built.memory_lines,
                "build_log": str(built.out_dir / "build.log"),
            })
        run.record.update({
            "hex": str(run.hex.path), "version": run.hex.version, "stamp": run.hex.stamp,
            "tag": run.hex.tag, "hex_dirty": run.hex.dirty, "defines": run.defines, "target": target,
        })
        if run.hex.kind and run.hex.kind != target:
            raise FirmwareError(f"{run.hex.path.name} is a {run.hex.kind} image; the target is {target}",
                                EXIT_USAGE)
        if flash_board:
            flashed = flash(run.hex, station.tr.uid, run.primary, port_hint=port, current=current, log=log)
            run.flashed = not flashed.skipped
        run.record["flashed"] = run.flashed
    except SpecError as e:
        raise FirmwareError(f"firmware: {e}", EXIT_USAGE) from e
    except (BuildError, CheckoutError, FlashError) as e:
        raise FirmwareError(f"firmware: {e}", EXIT_FAILED) from e
    except (NotFound, TransportError, LinkError) as e:
        raise FirmwareError(f"firmware: {e}", EXIT_NO_STATION) from e
    return run
