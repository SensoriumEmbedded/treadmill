"""The driver: ``treadmill run``, ``exec`` and ``shell`` as library calls.

``run_tests`` starts pytest in a child process from the repository root with
the run's folder chosen up front, streams its output, then reads the record
the plugin left (``run.json``) and answers with the suite's exit codes.
``Harness`` opens a station the way a test session does, the firmware under
test prepared and the rig held, and gives a script or a shell the same
objects a test gets (``board``, ``link``, ``screen``, ``menu``, ``keys``,
``dbg``, ``kbd``, ``video``, ``artifacts``); what the board said meanwhile
lands in a folder of the run like any test's. With ``no_link`` it opens the
station without its TeensyROM: the TR's names are None. Nothing here
imports pytest.
"""

from __future__ import annotations

import code
import contextlib
import json
import os
import runpy
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path

from treadmill.errors import EXIT_FAILED, EXIT_NO_STATION, EXIT_OK, EXIT_USAGE, TreadmillError
from treadmill.firmware.prepare import FirmwareRun, prepare_firmware, primary_checkout
from treadmill.firmware.spec import FirmwareSpec, parse_spec
from treadmill.report import (
    RunDir, describe_board, describe_station, host_record, render_report, write_board_artifacts, write_kbd_log,
)
from treadmill.session import LinklessSession, Session, fake_requested, open_linkless, open_station
from treadmill.station import load_station
from treadmill.station.config import repo_root
from treadmill.station.lock import RigLock

# pytest's own exit statuses to the suite's: tests failed (1) is a failure; an
# interruption (2), a usage error (4) and nothing collected (5) are the
# request's fault; an internal error (3) is ours. When the plugin ended the
# session itself (run.json says `aborted`), the status is already one of the
# suite's codes and is passed through: its 4 means the station, not usage.
PYTEST_EXIT = {0: EXIT_OK, 1: EXIT_FAILED, 2: EXIT_USAGE, 3: EXIT_FAILED, 4: EXIT_USAGE, 5: EXIT_USAGE}
PLUGIN_EXITS = (EXIT_FAILED, EXIT_USAGE, EXIT_NO_STATION)
NO_TESTS_COLLECTED = 5


@dataclass(frozen=True)
class FirmwareOptions:
    """The firmware under test as the command line names it; one set of
    options for ``run``, ``exec`` and ``shell``."""
    fw: str = "none"
    target: str = "auto"
    defines: tuple[str, ...] = ()
    tag: str | None = None
    allow_dirty: bool = False
    no_flash: bool = False
    fw_defines: tuple[str, ...] = ()           # what a build flashed by other means carries

    def spec(self) -> FirmwareSpec:
        return parse_spec(self.fw, target=self.target, defines=self.defines, tag=self.tag,
                          allow_dirty=self.allow_dirty)

    def pytest_args(self) -> list[str]:
        args = ["--fw", self.fw, "--fw-target", self.target]
        for d in self.defines:
            args += ["--define", d]
        if self.tag:
            args += ["--tag", self.tag]
        if self.allow_dirty:
            args.append("--allow-dirty")
        if self.no_flash:
            args.append("--no-flash")
        if self.fw_defines:
            args += ["--fw-defines", ",".join(self.fw_defines)]
        return args


@dataclass(frozen=True)
class RunOptions:
    station: str
    tests: tuple[str, ...] = ()                # paths or node ids; the whole tests/ tree when empty
    reports: str = "reports"
    caps_mask: str = ""
    region: str | None = None                  # PAL | NTSC | BOTH; None runs in whatever standard the C64 is in
    soak: int = 0
    estimate: bool = False
    include_tolerant: bool = False
    attended: bool = False
    no_video: bool = False
    no_xcheck: bool = False
    golden_dir: str | None = None
    firmware: FirmwareOptions = field(default_factory=FirmwareOptions)
    pytest_args: tuple[str, ...] = ()          # handed to pytest as they are


@dataclass
class RunResult:
    run_dir: Path
    pytest_status: int
    code: int
    meta: dict | None

    @property
    def report(self) -> Path | None:
        path = self.run_dir / "report.html"
        return path if path.is_file() else None

    @property
    def summary(self) -> dict[str, int]:
        return (self.meta or {}).get("summary") or {}

    def describe(self) -> str:
        counts = ", ".join(f"{n} {k}" for k, n in sorted(self.summary.items())) or "no tests ran"
        line = f"run {self.run_dir.name}: {counts}"
        if self.meta and self.meta.get("aborted"):
            line += f"; aborted: {self.meta['aborted']}"
        if self.report:
            line += f"\nreport {self.report}"
        return line


def _resolve_test(target: str, root: Path) -> str:
    """A test path or node id, against the current directory first and the
    repository second, so ``tests/smoke`` works from anywhere."""
    path, sep, node = target.partition("::")
    p = Path(path)
    if not p.is_absolute():
        here = Path.cwd() / p
        p = here if here.exists() else root / p
    return f"{p}{sep}{node}"


def pytest_command(opts: RunOptions, run_dir: Path) -> list[str]:
    root = repo_root()
    targets = [_resolve_test(t, root) for t in opts.tests] or [str(root / "tests")]
    cmd = [sys.executable, "-m", "pytest", *targets, "--rootdir", str(root), "-p", "treadmill.pytest_plugin",
           "--station", opts.station, "--run-dir", str(run_dir), *opts.firmware.pytest_args()]
    if opts.caps_mask:
        cmd += ["--caps-mask", opts.caps_mask]
    if opts.region:
        cmd += ["--region", opts.region]
    if opts.soak:
        cmd += ["--soak", str(opts.soak)]
    if opts.golden_dir:
        cmd += ["--golden-dir", opts.golden_dir]
    for flag, on in (("--estimate", opts.estimate), ("--include-tolerant", opts.include_tolerant),
                     ("--attended", opts.attended), ("--no-video", opts.no_video), ("--no-xcheck", opts.no_xcheck)):
        if on:
            cmd.append(flag)
    cmd += list(opts.pytest_args)
    return cmd


def run_tests(opts: RunOptions, *, sink=None, log=print) -> RunResult:
    """Run the hardware tests against a station in a child pytest and read
    back its record. The station file and the firmware spec are checked
    first, so a typo fails before anything is built. ``region="BOTH"`` is
    two runs: ``run_regions``."""
    if (opts.region or "").upper() == "BOTH":
        raise HarnessUsage("region BOTH is two runs, one per standard: call run_regions, not run_tests")
    load_station(opts.station)
    opts.firmware.spec()
    # A standard's run is named for it: --region both starts two runs, perhaps within one second.
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{opts.region.lower()}" if opts.region else None
    rd = RunDir(Path(opts.reports).resolve(), stamp)
    cmd = pytest_command(opts, rd.path)
    log("running: " + " ".join(cmd))
    sink = sink or sys.stdout
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(cmd, cwd=str(repo_root()), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", env=env)
    assert proc.stdout is not None
    for line in proc.stdout:
        sink.write(line)
        sink.flush()
    status = proc.wait()
    record = rd.path / "run.json"
    if record.is_file():
        rd.meta = json.loads(record.read_text(encoding="utf-8"))
        rewrite = False
    else:
        # The child died before its session finished: say so where the record would be.
        rd.meta.update({"aborted": f"pytest ended with status {status} before writing its record",
                        "station": {"id": opts.station}})
        rd.finish(status)
        rewrite = True
    meta = rd.meta
    if status == NO_TESTS_COLLECTED and opts.estimate:
        code = EXIT_OK
    elif status == NO_TESTS_COLLECTED:
        code = EXIT_USAGE
        if "aborted" not in meta:
            meta["aborted"] = "no tests were selected"
            rewrite = True
    elif meta.get("aborted") and status in PLUGIN_EXITS:
        code = status
    else:
        code = PYTEST_EXIT.get(status, EXIT_FAILED)
    if rewrite:
        rd.write_run()
    return RunResult(rd.path, status, code, meta)


def run_regions(opts: RunOptions, *, sink=None, log=print) -> list[RunResult]:
    """``run_tests``, once per standard for ``region="BOTH"`` (PAL, then
    NTSC: two pytest sessions, each with its own folder and run.json, the
    second one whatever the first did); once otherwise."""
    if (opts.region or "").upper() != "BOTH":
        return [run_tests(opts, sink=sink, log=log)]
    return [run_tests(replace(opts, region=r), sink=sink, log=log) for r in ("PAL", "NTSC")]


# -- scripts and the shell ------------------------------------------------------

@dataclass(frozen=True)
class HarnessOptions:
    station: str
    reports: str = "reports"
    attended: bool = False
    no_video: bool = False
    caps_mask: str = ""
    to_menu: bool = True                       # bring the board to the menu before handing it over
    no_link: bool = False                      # open the rig, the capture and the keyboard board, never the TR
    firmware: FirmwareOptions = field(default_factory=FirmwareOptions)


class HarnessUsage(TreadmillError):
    code = EXIT_USAGE


class Harness:
    """``with Harness(opts, "exec-script", log) as h:`` opens the station with
    the firmware under test on it and the rig held, and ``h.namespace`` is
    what a script or shell sees. On exit the board's output since the open
    goes to ``<run>/<name>/`` and the run record is written."""

    def __init__(self, opts: HarnessOptions, name: str, log=print):
        self.opts = opts
        self.name = name
        self.log = log
        self.run_dir: RunDir | None = None
        self.session: Session | None = None
        self.linkless: LinklessSession | None = None
        self.firmware: FirmwareRun | None = None
        self.namespace: dict = {}
        self.outcome: dict = {"nodeid": name, "outcome": "passed", "message": None}
        self._lock: RigLock | None = None
        self._t0 = 0.0
        self._mark = 0

    @property
    def folder(self) -> Path:
        assert self.run_dir is not None
        return self.run_dir.for_test(self.name)

    def __enter__(self) -> "Harness":
        opts = self.opts
        station = load_station(opts.station)
        spec = opts.firmware.spec()
        asked = opts.firmware
        if opts.no_link and (asked.fw != "none" or asked.defines or asked.tag or asked.fw_defines):
            raise HarnessUsage("--no-link opens no TeensyROM, so it takes no firmware: leave out --fw, --define, "
                               "--tag and --fw-defines")
        self.run_dir = RunDir(Path(opts.reports).resolve())
        self.run_dir.meta.update({"command": self.name, **host_record(["treadmill", *sys.argv[1:]])})
        self._lock = RigLock(station.rig.id, wait=5.0).acquire()
        try:
            if opts.no_link:
                self._open_linkless()
                return self
            declared = tuple(opts.firmware.fw_defines)
            if fake_requested():
                fw = FirmwareRun(spec, defines=declared, primary=primary_checkout(station))
                fw.record = {"spec": spec.describe(), "variant": spec.is_variant, "defines": declared}
            else:
                fw = prepare_firmware(spec, station, declared_defines=declared,
                                      flash_board=not opts.firmware.no_flash, log=self.log)
            self.firmware = fw
            self.run_dir.meta["firmware"] = fw.record
            mask = {c for c in opts.caps_mask.split(",") if c}
            self.session = open_station(opts.station, attended=opts.attended, video=not opts.no_video,
                                        firmware=fw, caps_mask=mask, log=self.log)
            board = self.session.board
            if opts.to_menu:
                board.ensure_menu()
        except BaseException as e:
            # The folder exists already: leave the record of why nothing ran in it.
            self.run_dir.meta["aborted"] = str(e) or type(e).__name__
            self.run_dir.finish(getattr(e, "code", EXIT_FAILED))
            self.run_dir.write_run()
            if self.session is not None:
                self.session.close()
            if self.linkless is not None:
                self.linkless.close()
            self._lock.release()
            raise
        self.run_dir.meta["station"] = describe_station(board)
        self.run_dir.meta["board"] = describe_board(board)
        self._t0 = time.monotonic()
        self._mark = len(board.link.stream.tap.chunks)
        screen = self.session.screen_channel()
        self.namespace = {
            "session": self.session, "station": board.station, "board": board, "link": board.link,
            "screen": screen, "video": self.session.video, "keys": self.session.keys(),
            "menu": self.session.menu(screen), "dbg": self.session.dbg(), "kbd": board.kbd, "firmware": fw,
            "artifacts": self.folder, "run_dir": self.run_dir.path, "log": self.log,
        }
        return self

    def _open_linkless(self) -> None:
        """``no_link``: the rig held, the capture and the keyboard board open, no TR."""
        mask = {c for c in self.opts.caps_mask.split(",") if c}
        self.linkless = ls = open_linkless(self.opts.station, video=not self.opts.no_video, caps_mask=mask,
                                           log=self.log)
        self.run_dir.meta["firmware"] = {"spec": "none", "no_link": True}
        self.run_dir.meta["station"] = describe_station(ls)
        self.run_dir.meta["board"] = None
        self._t0 = time.monotonic()
        self.namespace = {
            "session": ls, "station": ls.station, "board": None, "link": None, "screen": ls.screen_channel(),
            "video": ls.video, "keys": ls.keys(), "menu": None, "dbg": None, "kbd": ls.kbd, "firmware": None,
            "artifacts": self.folder, "run_dir": self.run_dir.path, "log": self.log,
        }

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.run_dir is not None and self._lock is not None
        opened = self.session or self.linkless
        assert opened is not None
        try:
            if self.session is not None:
                board = self.session.board
                write_board_artifacts(self.folder, board, self._t0, self._mark)
                self.run_dir.meta["recoveries"] = list(board.recoveries)
            elif opened.kbd is not None:
                write_kbd_log(self.folder, opened.kbd, self._t0)
            self.outcome["duration"] = time.monotonic() - self._t0
            self.run_dir.tests[self.name] = self.outcome
            self.run_dir.finish(EXIT_OK if self.outcome["outcome"] == "passed" else EXIT_FAILED)
            self.run_dir.write_run()
            render_report(self.run_dir.path)
        finally:
            try:
                opened.close()
            finally:
                self._lock.release()


@dataclass
class ExecResult:
    code: int
    result: object = None                      # the script's ``result`` variable, when it set one
    error: str | None = None


def exec_script(harness: Harness, script: Path, argv: tuple[str, ...] = (), *, quiet_stdout: bool = False) -> ExecResult:
    """Run ``script`` with the harness namespace as its globals, as
    ``__main__`` with ``sys.argv`` set. A ``result`` the script leaves comes
    back; an exception is its traceback, on stderr and in the record, and a
    failure code. With ``quiet_stdout`` the script's prints go to stderr, so
    a ``--json`` caller's stdout stays its own."""
    script = Path(script)
    saved_argv = sys.argv
    sys.argv = [str(script), *argv]
    out = contextlib.redirect_stdout(sys.stderr) if quiet_stdout else contextlib.nullcontext()
    try:
        with out:
            globals_after = runpy.run_path(str(script), init_globals=dict(harness.namespace), run_name="__main__")
        return ExecResult(EXIT_OK, globals_after.get("result"))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (EXIT_OK if e.code is None else EXIT_FAILED)
        if code != EXIT_OK:
            harness.outcome.update(outcome="failed", message=f"exit {e.code}")
        return ExecResult(code, error=None if code == EXIT_OK else f"the script exited with {e.code}")
    except KeyboardInterrupt:
        harness.outcome.update(outcome="failed", message="interrupted")
        return ExecResult(130, error="interrupted")
    except BaseException:  # noqa: BLE001 - a script's own failure is reported, not raised through the harness
        text = traceback.format_exc()
        harness.outcome.update(outcome="failed", message=text[-4000:])
        print(text, file=sys.stderr, end="")
        return ExecResult(EXIT_FAILED, error=text.strip().splitlines()[-1])
    finally:
        sys.argv = saved_argv


SHELL_NAMES = ("session", "station", "board", "link", "screen", "menu", "keys", "dbg", "kbd", "video", "firmware",
               "artifacts", "run_dir", "log")


def shell(harness: Harness) -> int:
    """A REPL with the harness namespace; ``quit()`` or end of input leaves it."""
    board = harness.namespace["board"]
    if board is None:
        what = "no TR link"
    else:
        what = board.banner.version if board.banner else board.image
    names = ", ".join(n for n in SHELL_NAMES if harness.namespace.get(n) is not None)
    banner = f"treadmill shell: {harness.namespace['station'].id}, {what}; names: {names}"
    code.interact(banner=banner, local=harness.namespace, exitmsg="")
    return EXIT_OK
