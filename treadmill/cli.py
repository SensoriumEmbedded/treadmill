"""The ``treadmill`` command. Every subcommand takes ``--json`` and exits
non-zero on failure, so an agent can drive it from structured output.

Step 1 ships ``stations``; ``run``, ``matrix``, ``sweep``, ``shell``, ``exec``,
``calibrate``, ``harvest``, ``viewer``, ``bisect`` and ``export`` arrive with
their modules.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from treadmill import __version__
from treadmill.station import ConfigError, list_stations, load_station
from treadmill.station.model import tier

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NO_STATION = 0, 1, 2, 4


def _emit(payload: dict, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(text)


def cmd_stations(args: argparse.Namespace) -> int:
    ids = list_stations()
    rows = []
    for sid in ids:
        try:
            st = load_station(sid)
            rows.append({"id": sid, "rig": st.rig.id, "machine": st.machine.id,
                         "model": st.machine.model, "region": st.machine.region,
                         "tr_uid": st.tr.uid, "tier": tier(st), "caps": sorted(st.caps), "error": None})
        except ConfigError as e:
            rows.append({"id": sid, "error": str(e)})
    if args.station:
        rows = [r for r in rows if r["id"] == args.station]
        if not rows:
            _emit({"error": f"no station {args.station!r}"}, args.json, f"no station {args.station!r}")
            return EXIT_NO_STATION
    lines = []
    for r in rows:
        if r.get("error"):
            lines.append(f"{r['id']}: ERROR {r['error']}")
        else:
            lines.append(f"{r['id']}: {r['model']} {r['region']} on {r['rig']}, TR uid {r['tr_uid']}, "
                         f"tier {r['tier']}, caps {', '.join(r['caps'])}")
    _emit({"stations": rows}, args.json, "\n".join(lines) if lines else "no stations in data/stations")
    return EXIT_FAILED if any(r.get("error") for r in rows) else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="treadmill", description="TeensyROM hardware-in-the-loop validation")
    p.add_argument("--version", action="version", version=f"treadmill {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("stations", help="list the configured stations and their capabilities")
    s.add_argument("--station", help="only this station")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_stations)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(json.dumps({"error": str(e)}) if getattr(args, "json", False) else f"error: {e}",
              file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
