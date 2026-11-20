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

from treadmill.errors import EXIT_USAGE, TreadmillError
from treadmill.knobs import knob_named
from treadmill.margins import Floor
from treadmill.station.model import (
    CaptureSpec,
    KeyboardSpec,
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

    code = EXIT_USAGE


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


def _region_switch(t: dict[str, Any], path: Path) -> str | None:
    value = t.get("region_switch")
    if value == "kawari":
        raise ConfigError(f"{path}: region_switch = \"kawari\" is gone: the keyboard board switches a Kawari's "
                          "standard now. Give the machine has_kawari = true and the station a [keyboard] table "
                          "with kawari_open")
    if value not in (None, "u64"):
        raise ConfigError(f"{path}: region_switch {value!r}; the one value is \"u64\"")
    return value


def _keys_table(t: dict[str, Any], path: Path) -> None:
    """A rig's old [keys] table: the keyboard board lives in one machine, so
    it moved to the station file."""
    keys = t.get("keys")
    if keys is not None and keys.get("backend", "none") != "none":
        raise ConfigError(f"{path} [keys]: the keyboard board is set in the station file now, as a [keyboard] "
                          "table with its chip id; drop the rig's [keys] table or leave backend = \"none\"")


def load_rig(rig_id: str, base: Path | None = None) -> Rig:
    base = base or data_dir()
    path = base / "rigs" / f"{rig_id}.toml"
    t = _read_toml(path)
    if t.get("id", rig_id) != rig_id:
        raise ConfigError(f"{path}: id {t.get('id')!r} does not match the file name")
    _keys_table(t, path)
    return Rig(
        id=rig_id,
        capture=_build(CaptureSpec, t["capture"], f"{path} [capture]") if "capture" in t else None,
        power=_optional(PowerSpec, t.get("power"), f"{path} [power]"),
        region_switch=_region_switch(t, path),
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


TOD_RATES = ("standard", 50, 60, "off")


def _keyboard(table: dict[str, Any] | None, path: Path) -> KeyboardSpec | None:
    if table is None:
        return None
    where = f"{path} [keyboard]"
    if "chip" not in table:
        raise ConfigError(f"{where}: needs chip, the board's chip id (its USB serial string is "
                          "C64USBKeyboard_<chip>)")
    spec = _build(KeyboardSpec, {**table, "chip": str(table["chip"])}, where)
    if not spec.chip.isdigit():
        raise ConfigError(f"{where}: chip {spec.chip!r} is not a chip id")
    if spec.kawari_open is not None:
        if str(spec.kawari_open).upper() not in ("PAL", "NTSC"):
            raise ConfigError(f"{where}: kawari_open {spec.kawari_open!r}; PAL or NTSC")
        spec = replace(spec, kawari_open=str(spec.kawari_open).upper())
    if spec.tod not in TOD_RATES:
        raise ConfigError(f"{where}: tod {spec.tod!r}; one of {', '.join(map(str, TOD_RATES))}")
    return spec


def _margins(table: dict[str, Any] | None, path: Path) -> dict[str, Floor]:
    """``nS_PLAprop = { up = 200, down = 140 }``: how far each edge of a
    knob must be found clean, by the knob's variable name."""
    floors: dict[str, Floor] = {}
    for name, value in (table or {}).items():
        where = f"{path} [margins] {name}"
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: give a floor per direction, {{ up = <value>, down = <value> }}")
        unknown = sorted(set(value) - {"up", "down"})
        if unknown:
            raise ConfigError(f"{where}: {', '.join(unknown)}: only up and down")
        try:
            knob = knob_named(name)
        except KeyError as e:
            raise ConfigError(f"{where}: {e.args[0]}") from None
        floors[knob.name] = Floor(up=int(value["up"]) if "up" in value else None,
                                  down=int(value["down"]) if "down" in value else None)
    return floors


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
        margins=_margins(t.get("margins"), path),
        keyboard=_keyboard(t.get("keyboard"), path),
    )
    return replace(st, caps=capabilities(st))


def list_stations(base: Path | None = None) -> list[str]:
    base = base or data_dir()
    folder = base / "stations"
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.toml") if not p.name.endswith(".example.toml"))
