"""Where a run writes what it saw.

``reports/<YYYYmmdd-HHMMSS>/`` holds ``run.json`` (what ran against what,
and how every test came out), ``report.html`` rendered from it, and one
folder per test with ``serial.log`` (the board's raw output during the
test), ``events.json``, ``kbd.log`` when the station has a keyboard board,
and whatever frames or screens the test attached. A script run by
``treadmill exec`` leaves the same folder.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from treadmill import __version__

if TYPE_CHECKING:
    from treadmill.board import Board
    from treadmill.link.unsolicited import Event
    from treadmill.session import LinklessSession
    from treadmill.station.kbd import KeyboardBoard

SETTLE_S = 0.3           # the link's idle time: what the board said last has arrived by then


def safe_name(nodeid: str) -> str:
    """A test node id as a folder name."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", nodeid.replace("::", "__"))
    return name.strip("_")[:120] or "test"


def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.decode("latin-1", "replace")
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def suite_commit() -> str | None:
    """The suite's own commit (``git describe --always --dirty``), or None
    outside a checkout."""
    root = Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(["git", "describe", "--always", "--dirty"], cwd=str(root), capture_output=True,
                             text=True, stdin=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def host_record(argv) -> dict[str, Any]:
    """What ran the run: the suite's version and commit, the interpreter,
    the machine, the command."""
    return {"suite": __version__, "suite_commit": suite_commit(), "python": platform.python_version(),
            "host": platform.node(), "argv": list(argv)}


class RunDir:
    """One run's folder: made under ``root`` with a timestamp, or at ``path``
    when the driver chose the folder first so it can read the run back."""

    def __init__(self, root: Path | None = None, stamp: str | None = None, *, path: Path | None = None):
        if path is not None:
            self.path = Path(path)
            self.stamp = stamp or self.path.name
        else:
            self.stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
            self.path = (root or Path("reports")) / self.stamp
        self.path.mkdir(parents=True, exist_ok=True)
        self.meta: dict[str, Any] = {"stamp": self.stamp, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.tests: dict[str, dict[str, Any]] = {}

    def write_json(self, name: str, obj: Any, folder: Path | None = None) -> Path:
        target = (folder or self.path) / name
        target.write_text(json.dumps(obj, indent=2, default=_jsonable), encoding="utf-8")
        return target

    def write_run(self) -> Path:
        return self.write_json("run.json", self.meta)

    def for_test(self, nodeid: str) -> Path:
        folder = self.path / safe_name(nodeid)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def finish(self, exit_status: int | None = None) -> None:
        """Close the record: when it ended, how the tests came out, where
        each one's folder is."""
        self.meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if exit_status is not None:
            self.meta["exit_status"] = exit_status
        tests = []
        for rec in self.tests.values():
            folder = safe_name(rec["nodeid"])
            rec = {**rec, "duration": round(rec.get("duration", 0.0), 3),
                   "folder": folder if (self.path / folder).is_dir() else None}
            tests.append(rec)
        summary: dict[str, int] = {}
        for rec in tests:
            summary[rec["outcome"]] = summary.get(rec["outcome"], 0) + 1
        self.meta["tests"] = tests
        self.meta["summary"] = summary


def describe_station(board: "Board | LinklessSession") -> dict[str, Any]:
    """The ``station`` entry of run.json, from the station as probed, with
    its keyboard board when it names one. ``standard`` is what the TR's
    banner reported, after any ``--region`` switch; ``region`` is what the
    machine's registry row says."""
    st = board.station
    return {"id": st.id, "rig": st.rig.id, "machine": st.machine.id, "model": st.machine.model,
            "region": st.machine.region, "standard": st.standard, "caps": sorted(st.caps), "tr_uid": st.tr.uid,
            "tr_kind": st.tr.kind, "keyboard": describe_keyboard(board)}


def describe_keyboard(board: "Board | LinklessSession") -> dict[str, Any] | None:
    spec, kbd = board.station.keyboard, board.kbd
    if spec is None:
        return None
    if kbd is None:
        return {"chip": spec.chip, "open": False, "problem": board.kbd_problem}
    ident = kbd.identity
    return {"chip": kbd.chip, "open": True, "port": kbd.port, "mcu": ident.get("mcu"), "fw": ident.get("fw"),
            "proto": ident.get("proto"), "fitted": sorted(kbd.fitted)}


def describe_board(board: "Board") -> dict[str, Any]:
    """The ``board`` entry of run.json: what answered when the run opened it."""
    b = board.banner
    return {"image": board.image, "version": b.version if b else None, "built": b.built if b else None,
            "tag": b.tag if b else None, "dirty": b.dirty if b else None,
            "machine_line": f"{b.machine} {b.video} {b.hz}" if b else None}


def write_board_artifacts(folder: Path, board: "Board", since: float, mark: int) -> "list[Event]":
    """What the board said from ``mark`` (a tap index taken at the start)
    on, as ``serial.log``, and the events classified from it since ``since``
    as ``events.json``; lines nothing has read yet are taken in first. With
    a keyboard board open, its traffic since ``since`` as ``kbd.log``."""
    stale = board.link.stream.drain(SETTLE_S)
    if stale:
        board.events.feed(stale)
    events = board.events.since(since)
    raw = b"".join(c for _, c in board.link.stream.tap.chunks[mark:])
    (folder / "serial.log").write_bytes(raw)
    (folder / "events.json").write_text(json.dumps([asdict(e) for e in events], indent=2, default=_jsonable),
                                        encoding="utf-8")
    if board.kbd is not None:
        write_kbd_log(folder, board.kbd, since)
    return events


def write_kbd_log(folder: Path, kbd: "KeyboardBoard", since: float) -> Path:
    """The keyboard board's lines both ways since ``since``, as ``kbd.log``."""
    path = folder / "kbd.log"
    path.write_text("".join(f"{row}\n" for row in kbd.transcript(since)), encoding="utf-8")
    return path
