"""Rig and station files: ``data/rigs/<id>.toml`` and ``data/stations/<id>.toml``.

The data directory is ``TREADMILL_DATA`` when set, else ``<repo>/data``. A
station file names its rig and its machine (a registry id, or an inline
``[machine]`` table for a machine the registry does not list) and the board.
Nothing here opens a port: ``kind`` stays unknown until the link probes it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from treadmill.errors import TreadmillError
from treadmill.station.model import (
    CaptureSpec,
    KeysSpec,
    Machine,
    PowerSpec,
    Rig,
    Station,
    TRBoard,
    capabilities,
)
from treadmill.station.registry import read_registry


class ConfigError(TreadmillError):
    """A rig or station file is missing, malformed or names something unknown."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    env = os.environ.get("TREADMILL_DATA")
    return Path(env) if env else repo_root() / "data"


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"{path} does not exist")
    with path.open("rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from e


def _build(cls, table: dict[str, Any], where: str):
    """A dataclass from a TOML table, refusing keys the dataclass lacks so a
    typo in a config file fails loudly instead of silently doing nothing."""
    names = {f.name for f in fields(cls)}
    bad = sorted(set(table) - names)
    if bad:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(bad)}; known: {', '.join(sorted(names))}")
    return cls(**table)


def _optional(cls, table: dict[str, Any] | None, where: str):
    if table is None:
        return None
    spec = _build(cls, table, where)
    return None if getattr(spec, "backend", "x") == "none" else spec


def load_rig(rig_id: str, base: Path | None = None) -> Rig:
    base = base or data_dir()
    path = base / "rigs" / f"{rig_id}.toml"
    t = _read_toml(path)
    if t.get("id", rig_id) != rig_id:
        raise ConfigError(f"{path}: id {t.get('id')!r} does not match the file name")
    return Rig(
        id=rig_id,
        capture=_build(CaptureSpec, t["capture"], f"{path} [capture]") if "capture" in t else None,
        power=_optional(PowerSpec, t.get("power"), f"{path} [power]"),
        keys=_optional(KeysSpec, t.get("keys"), f"{path} [keys]"),
        region_switch=t.get("region_switch"),
        scope=bool(t.get("scope", False)),
        u64_url=t.get("u64_url"),
        firmware_checkout=t.get("firmware_checkout"),
        mcp_checkout=t.get("mcp_checkout"),
        trom_path=t.get("trom_path"),
    )


def registry_path(base: Path | None = None) -> Path | None:
    """Where systems.csv is: ``data/registry.toml`` names it, else the tracker
    folder beside the firmware checkout, else none."""
    base = base or data_dir()
    cfg = base / "registry.toml"
    if cfg.is_file():
        t = _read_toml(cfg)
        if "systems_csv" in t:
            return Path(t["systems_csv"])
    env = os.environ.get("TREADMILL_REGISTRY")
    return Path(env) if env else None


def _machine(t: dict[str, Any], path: Path, base: Path) -> Machine:
    inline = t.get("machine")
    ref = t.get("machine_id")
    if isinstance(inline, dict):
        m = _build(Machine, inline, f"{path} [machine]")
    elif ref:
        reg = registry_path(base)
        if reg is None or not reg.is_file():
            raise ConfigError(f"{path}: machine_id {ref!r} needs the registry; "
                              "set data/registry.toml systems_csv or TREADMILL_REGISTRY")
        machines = read_registry(reg)
        if str(ref) not in machines:
            raise ConfigError(f"{path}: machine_id {ref!r} is not in {reg}")
        m = machines[str(ref)]
    else:
        raise ConfigError(f"{path}: needs a [machine] table or a machine_id")
    overrides = t.get("overrides") or {}
    names = {f.name for f in fields(Machine)}
    bad = sorted(set(overrides) - names)
    if bad:
        raise ConfigError(f"{path} [overrides]: unknown key(s) {', '.join(bad)}")
    return replace(m, **overrides)


def load_station(station_id: str, base: Path | None = None) -> Station:
    base = base or data_dir()
    path = base / "stations" / f"{station_id}.toml"
    t = _read_toml(path)
    if t.get("id", station_id) != station_id:
        raise ConfigError(f"{path}: id {t.get('id')!r} does not match the file name")
    if "rig" not in t:
        raise ConfigError(f"{path}: needs rig = \"<rig id>\"")
    rig = load_rig(t["rig"], base)
    machine = _machine(t, path, base)
    if "tr" not in t or "uid" not in t["tr"]:
        raise ConfigError(f"{path}: needs a [tr] table with uid")
    tr = _build(TRBoard, t["tr"], f"{path} [tr]")
    st = Station(
        id=station_id,
        rig=rig,
        machine=machine,
        tr=tr,
        allow_eeprom_reset=bool(t.get("allow_eeprom_reset", False)),
        settings_baseline=dict(t.get("settings_baseline") or {}),
    )
    return replace(st, caps=capabilities(st))


def list_stations(base: Path | None = None) -> list[str]:
    base = base or data_dir()
    folder = base / "stations"
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.toml") if not p.name.endswith(".example.toml"))
