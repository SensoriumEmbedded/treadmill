"""``treadmill compare``: one station's timing margins on two builds, a base
and a tip, in each video standard asked for. For each standard, the full
margin set (``tests/sweep``) runs on the base, then the sweeps only the
newer probes reach (vicrom-high, vicrom-cpu, idread with nS_RWnReady held),
then both again on the tip, bisected finer; at the end the station's margin
records go to a CSV, and the mains are latched off if asked. Each step is
its own ``treadmill`` command, so one that fails leaves the others' records,
and the folder holds each step's JSON and log beside ``compare.json``.

One standard at a time, both builds in it: the margin run switches the
machine (``--region``), and ``treadmill sweep`` has none, so a probe sweep
runs in the standard the run before it left.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from treadmill.errors import EXIT_FAILED, EXIT_NO_STATION, EXIT_OK, EXIT_USAGE

DEFINES = ("Dbg_SerTimChg", "Dbg_SerDMA")      # the margin set's knobs, the DMA ones included
# Held for nS_PLAprop's walk down: at 0 the C64 hung on an early address read before the PLA's
# own edge was reached (the 250425 C64 with a Kawari, 2026-11-25); 115 was above every nS_RWnReady lower edge then.
RWNREADY_HOLD = 115
SWEEP_TIMEOUT_S = 40 * 60                     # a probe sweep took 2-8 min on the 250425 C64; a wedged one ends here
PROBE_SWEEPS = (                              # (name, the sweep's knob and probe); each runs --both
    ("rwnready-vicrom-high", ["r", "--probe", "vicrom-high"]),
    ("plaprop-vicrom-high", ["p", "--probe", "vicrom-high"]),
    ("plaprop-idread-rwhold", ["p", "--probe", "idread", "--set", "nS_RWnReady={hold}"]),
    ("vicdhold-vicrom-cpu", ["i", "--probe", "vicrom-cpu"]),
)


@dataclass
class Step:
    name: str
    argv: list[str]                   # after ``python -m treadmill``
    kind: str                         # run | sweep | power | csv
    timeout: float | None = None
    exit_code: int | None = None
    status: str = "pending"           # ok | failed | refused (a sweep the station cannot run) | timeout | skipped
    seconds: float | None = None
    run_dir: str | None = None
    note: str | None = None


def plan(station: str, base: str, tip: str, *, regions: list[str | None], defines: list[str], fine: int,
         rw_hold: int = RWNREADY_HOLD, probes: bool = True, power_off: bool = False, reports: str = "reports",
         pytest_args: list[str] | None = None) -> list[Step]:
    """The steps, in order: per standard, the base's margin run and probe
    sweeps, then the tip's (``fine`` ns, three samples a point); the CSV;
    the mains off."""
    fw_defines = [arg for d in defines for arg in ("--define", d)]
    steps: list[Step] = []
    for region in regions:
        std = (region or "now").lower()
        for side, ref in (("base", base), ("tip", tip)):
            fw = ["--station", station, "--reports", reports, "--fw", f"ref={ref}", *fw_defines]
            finer = side == "tip" and fine > 0
            extra = list(pytest_args or []) + (["--margin-fine", str(fine)] if finer else [])
            steps.append(Step(f"{std}-{side}-margins", ["run", *fw, *(["--region", region] if region else []),
                                                         "--tests", "tests/sweep", "--json",
                                                         *(["--", *extra] if extra else [])], "run"))
            if probes:
                for name, args in PROBE_SWEEPS:
                    sweep = [a.format(hold=rw_hold) for a in args]
                    steps.append(Step(f"{std}-{side}-{name}",
                                      ["sweep", *sweep, *fw, "--both", "--json",
                                       *(["--fine", str(fine), "--repeat", "3"] if finer else [])],
                                      "sweep", timeout=SWEEP_TIMEOUT_S))
    steps.append(Step("margins-csv", ["margins", "csv", "--station", station, "--reports", reports], "csv"))
    if power_off:
        steps.append(Step("power-off", ["power", "--station", station, "off", "--latch", "--json"], "power"))
    return steps


def command_line(step: Step) -> str:
    return " ".join(["treadmill", *step.argv])


def teensy_loader_open() -> bool:
    """The Teensy Loader running: it programs a board the recovery leaves in
    the bootloader with whatever hex it holds (backlog 64). Windows only."""
    if sys.platform != "win32":
        return False
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq teensy.exe"], capture_output=True, text=True).stdout
    return "teensy.exe" in out.lower()


Runner = Callable[[Step, Path, Path], int]


def run_subprocess(step: Step, out: Path, err: Path) -> int:
    """``python -m treadmill <argv>``: stdout to ``out``, stderr to ``err``."""
    with out.open("w", encoding="utf-8") as o, err.open("w", encoding="utf-8") as e:
        proc = subprocess.run([sys.executable, "-m", "treadmill", *step.argv], stdout=o, stderr=e,
                              timeout=step.timeout)
    return proc.returncode


def run_steps(steps: list[Step], folder: Path, *, runner: Runner = run_subprocess,
              loader_open: Callable[[], bool] = teensy_loader_open, log=print) -> int:
    """Each step in turn, its output in ``folder``. A station that cannot be
    had (exit 4) or an open Teensy Loader stops the station's steps; the CSV
    and the power-off still run. ``compare.json`` is rewritten after every
    step, so a compare cut short still says how far it got."""
    folder.mkdir(parents=True, exist_ok=True)
    stop: str | None = None
    for step in steps:
        if stop and step.kind in ("run", "sweep"):
            step.status, step.note = "skipped", stop
            continue
        if step.kind in ("run", "sweep") and loader_open():
            stop = "a Teensy Loader is open: it would program a board the recovery leaves in the bootloader"
            step.status, step.note = "skipped", stop
            log(f"[compare] {stop}; the station's steps stop here")
            continue
        out, err = folder / f"{step.name}.{'csv' if step.kind == 'csv' else 'json'}", folder / f"{step.name}.log"
        log(f"[compare] {datetime.now():%H:%M} {step.name}: {command_line(step)}")
        t0 = time.monotonic()
        try:
            step.exit_code = runner(step, out, err)
        except subprocess.TimeoutExpired:
            step.exit_code, step.status = None, "timeout"
            step.note = f"stopped after {step.timeout:.0f} s"
        step.seconds = round(time.monotonic() - t0, 1)
        if step.status != "timeout":
            step.status = ("ok" if step.exit_code == EXIT_OK
                           else "refused" if step.kind == "sweep" and step.exit_code == EXIT_USAGE
                           else "failed")
        step.run_dir, error = _from_output(out) if step.kind != "csv" else (None, None)
        step.note = step.note or error
        log(f"[compare] {datetime.now():%H:%M} {step.name}: {step.status}"
            + (f" ({step.note})" if step.note else "") + f", {step.seconds:.0f} s")
        if step.exit_code == EXIT_NO_STATION:
            stop = f"{step.name}: the station could not be had"
        _write(folder, steps)
    _write(folder, steps)
    return exit_code(steps)


def _from_output(path: Path) -> tuple[str | None, str | None]:
    """A step's run folder and error, from the JSON it printed."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    return payload.get("run_dir"), payload.get("error")


def exit_code(steps: list[Step]) -> int:
    if any(s.exit_code == EXIT_NO_STATION for s in steps):
        return EXIT_NO_STATION
    if any(s.status in ("failed", "timeout", "skipped") for s in steps):
        return EXIT_FAILED
    return EXIT_OK


def _write(folder: Path, steps: list[Step]) -> None:
    (folder / "compare.json").write_text(json.dumps({"steps": [asdict(s) for s in steps],
                                                     "exit_code": exit_code(steps)}, indent=2) + "\n",
                                         encoding="utf-8")


def folder_for(reports: str, station: str) -> Path:
    return Path(reports) / f"compare-{datetime.now():%Y%m%d-%H%M%S}-{station}"

