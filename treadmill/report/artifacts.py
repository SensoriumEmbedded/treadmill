"""Where a run writes what it saw.

``reports/<YYYYmmdd-HHMMSS>/`` holds ``run.json`` (what ran against what) and
one folder per test with ``serial.log`` (the board's raw output during the
test), ``events.json`` and whatever frames or screens the test attached.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


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


class RunDir:
    def __init__(self, root: Path | None = None, stamp: str | None = None):
        root = root or Path("reports")
        self.stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
        self.path = root / self.stamp
        self.path.mkdir(parents=True, exist_ok=True)
        self.meta: dict[str, Any] = {"stamp": self.stamp, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}

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
