"""The pytest side of TReadmill: options, markers, fixtures, invariants, the
run record.

Every hardware test gets a ``board`` at the menu, a per-test artifact folder
with the serial log and events of its run, and after it the invariants that
hold whatever it asserted: no crash report, no unexpected boot banner, no
unknown-command reply, and the board back at the menu. The session leaves
``run.json`` (what ran against what, and how every test came out) and
``report.html`` rendered from it in the run's folder.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from treadmill.board import Board, Wedged
from treadmill.c64.keys import Keys
from treadmill.errors import EXIT_NO_STATION, EXIT_USAGE
from treadmill.c64.menu import MenuDriver
from treadmill.firmware.prepare import FirmwareError, FirmwareRun, prepare_firmware, primary_checkout
from treadmill.firmware.spec import SpecError, parse_spec
from treadmill.fixtures.sdcard import FIXTURE_ROOT, FixtureError, provision, type_set
from treadmill.link import DebugCommands, Link, LinkError, TransportError
from treadmill.link.trlink import NotAllowed
from treadmill.observe.calibrate import Calibration, CalibrationError
from treadmill.observe.capture import CaptureError, FrameSource, save_strip
from treadmill.observe.channels import XCHECK_LIMIT, ScreenChannel, ScreenRead, XCheck
from treadmill.observe.glyphs import GlyphBank
from treadmill.observe.golden import Golden, compare, diff_text, save_golden
from treadmill.programs.build import AssembleError, assemble
from treadmill.report import (
    RunDir, describe_board, describe_station, host_record, render_report, write_board_artifacts,
)
from treadmill.session import (  # noqa: F401
    FAKE_ENV, dbg_for, fake_keyboard, fake_relay, fake_requested, keyboard_line, keys_for, load_bank,
    load_or_calibrate, menu_for, open_video, picture_ok,
)
from treadmill.station.power import PowerUnavailable
from treadmill.station import ConfigError, load_station
from treadmill.station.kawari import KawariError, set_standard
from treadmill.station.kbd import KbdError, KeyboardBoard
from treadmill.station.lock import RigBusy, RigLock
from treadmill.station.model import KEYBOARD_CAPS, unknown_caps

RUN_ATTR = "_treadmill_run"          # the RunDir on the pytest config, for hooks and fixtures alike
FAKE_ATTR = "_treadmill_fake"
WARNING_PROPERTY = "treadmill_warning"   # a user property: the warn fixture's lines


def _log(message: str) -> None:
    print(message)


def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("treadmill")
    g.addoption("--station", action="store", default=None,
                help="the station id (data/stations/<id>.toml) the hardware tests run against")
    g.addoption("--caps-mask", action="store", default="",
                help="comma-separated capabilities to take away, to prove a lower tier on a richer rig")
    g.addoption("--region", action="store", default=None, type=str.upper, choices=("PAL", "NTSC"),
                help="the video standard to run in; a station that runs the other one switches when it has "
                     "region_switch, else the run stops")
    g.addoption("--include-tolerant", action="store_true", default=False,
                help="run timing_sensitive tests on timing-tolerant machines too")
    g.addoption("--soak", action="store", type=int, default=0,
                help="run soak tests with this many iterations (0 = skip them)")
    g.addoption("--estimate", action="store_true", default=False,
                help="print the expected duration of the selected tests and exit")
    g.addoption("--reports", action="store", default="reports",
                help="where run folders go (default: reports/)")
    g.addoption("--run-dir", action="store", default=None,
                help="this run's folder, made by the driver; default: <reports>/<timestamp>")
    g.addoption("--attended", action="store_true", default=False,
                help="a person is at the bench: manual power control may prompt")
    g.addoption("--fw", action="store", default="none",
                help="firmware under test: ref=<git ref> | path=<checkout> | hex=<file> | none (default)")
    g.addoption("--fw-target", action="store", default="auto",
                help="tr | tr-plus | auto (the station's board kind)")
    g.addoption("--define", action="append", default=[],
                help="a debug define to compile in (repeatable); tags the build with its mark")
    g.addoption("--tag", action="store", default=None,
                help="the build tag; default: the marks of the defines, none for a clean build")
    g.addoption("--allow-dirty", action="store_true", default=False,
                help="build from a checkout with uncommitted changes (recorded as a variant)")
    g.addoption("--no-flash", action="store_true", default=False,
                help="build but do not flash; run against whatever the board carries")
    g.addoption("--fw-defines", action="store", default="",
                help="with --fw none or hex=: the defines the flashed build carries")
    g.addoption("--no-video", action="store_true", default=False,
                help="leave the capture device closed; tests that need video skip")
    g.addoption("--chargen", action="store", default=None,
                help="a C64 character ROM to bootstrap a glyph bank from when none is harvested")
    g.addoption("--no-xcheck", action="store_true", default=False,
                help="record video-vs-DMA disagreement but never fail a test on it")
    g.addoption("--golden-dir", action="store", default=None,
                help="where the screen goldens live (default: <rootdir>/tests/golden)")
    g.addoption("--power-check", action="store_true", default=False,
                help="one power cycle before the first test, through the ladder's own rung: proves the "
                     "station's recovery by mains before an unattended run")
    g.addoption("--margin-fine", action="store", type=int, default=None, metavar="NS",
                help="the margin tests bisect every edge to NS ns (default 5) and take 3 samples a point "
                     "everywhere: 2 resolves a build whose waits land within about 4 ns")


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--station") and not config.getoption("--estimate") and not config.getoption("collectonly"):
        config.pluginmanager.register(_RunRecorder(config), "treadmill_run_recorder")
    for line in (
        "needs(*caps): station capabilities the test requires",
        "needs_fw(*defines): firmware defines the flashed build must carry",
        "needs_asset(*names): private assets the test needs",
        "timing_sensitive: skipped on timing-tolerant machines",
        "destructive: changes board settings and restores them",
        "soak: long-running; selected only with --soak N",
        "client_path: drives the board through TeensyROM-Web and trom",
        "duration(minutes): the declared estimate",
        "alike: covers another case's code path with another knob; the self-tests leave it out",
    ):
        config.addinivalue_line("markers", line)


def _marker_args(item: pytest.Item, name: str) -> tuple[str, ...]:
    out: list[str] = []
    for m in item.iter_markers(name=name):
        out.extend(str(a) for a in m.args)
    return tuple(out)


def _declared_minutes(item: pytest.Item) -> float:
    m = item.get_closest_marker("duration")
    if m and m.args:
        return float(m.args[0])
    if m and "minutes" in m.kwargs:
        return float(m.kwargs["minutes"])
    return 1.0


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    soak_n = config.getoption("--soak")
    for item in items:
        bad = unknown_caps(_marker_args(item, "needs"))
        if bad:
            raise pytest.UsageError(f"{item.nodeid}: unknown capability {', '.join(bad)}")
        if item.get_closest_marker("soak") and not soak_n:
            item.add_marker(pytest.mark.skip(reason="soak test; run with --soak N"))
    if config.getoption("--estimate"):
        total = sum(_declared_minutes(i) for i in items if not i.get_closest_marker("skip"))
        print(f"\nestimate: {len(items)} test(s), about {total:.0f} minute(s) from declared durations")
        items.clear()


# -- the run record -------------------------------------------------------------

def _run_record(config: pytest.Config) -> RunDir:
    """The run's folder and record, made once per session."""
    rd = getattr(config, RUN_ATTR, None)
    if rd is None:
        chosen = config.getoption("--run-dir")
        rd = RunDir(path=Path(chosen)) if chosen else RunDir(Path(config.getoption("--reports")))
        rd.meta.update({
            **host_record(config.invocation_params.args),
            "options": {k: config.getoption(k) for k in
                        ("--station", "--caps-mask", "--soak", "--include-tolerant", "--fw", "--fw-target",
                         "--define", "--tag", "--allow-dirty", "--no-flash", "--fw-defines", "--no-video",
                         "--no-xcheck")},
        })
        setattr(config, RUN_ATTR, rd)
    return rd


class _RunRecorder:
    """Registered for a hardware run (``--station`` given): keeps the run
    record from the first hook to the last, whatever the tests do."""

    def __init__(self, config: pytest.Config):
        self.config = config

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        _run_record(self.config)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Every phase of every test folds into one record: failed in the
        call, error in setup or teardown, skipped, else passed."""
        rd = _run_record(self.config)
        rec = rd.tests.setdefault(report.nodeid, {"nodeid": report.nodeid, "outcome": "passed",
                                                  "duration": 0.0, "message": None})
        rec["duration"] += report.duration
        for name, text in report.user_properties:
            if name == WARNING_PROPERTY and text not in rec.setdefault("warnings", []):
                rec["warnings"].append(text)
        if report.failed:
            if rec["outcome"] in ("failed", "error"):
                # A failure in the call and another in teardown (the invariants, say): the first is the
                # outcome, the second is added so neither is lost.
                rec["message"] = f"{rec['message']}\n\n--- {report.when} ---\n{report.longreprtext[-2000:]}"
            else:
                rec["outcome"] = "failed" if report.when == "call" else "error"
                rec["failed_in"] = report.when
                rec["message"] = report.longreprtext[-4000:]
        elif report.skipped and rec["outcome"] == "passed":
            rec["outcome"] = "skipped"
            reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
            rec["message"] = reason.removeprefix("Skipped: ")

    def pytest_keyboard_interrupt(self, excinfo) -> None:
        """Also called for ``pytest.exit()``: a firmware or station problem
        that ended the session before any test is the run's abort."""
        _run_record(self.config).meta["aborted"] = str(excinfo.value)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        rd = _run_record(self.config)
        rd.finish(int(exitstatus))
        rd.write_run()
        try:
            render_report(rd.path)
        except Exception as e:  # noqa: BLE001 - the record is written; a report that will not render is a line
            print(f"[treadmill] report.html not rendered: {e}")


# -- fixtures ---------------------------------------------------------------

@pytest.fixture(scope="session")
def run_dir(request: pytest.FixtureRequest) -> RunDir:
    return _run_record(request.config)


@pytest.fixture(scope="session")
def station(request: pytest.FixtureRequest):
    """The station named by --station, before any port is opened."""
    station_id = request.config.getoption("--station")
    if not station_id:
        pytest.skip("no --station given")
    try:
        return load_station(station_id)
    except ConfigError as e:
        pytest.exit(f"station {station_id!r}: {e}", returncode=2)


@pytest.fixture(scope="session")
def firmware(request: pytest.FixtureRequest, station, run_dir: RunDir) -> FirmwareRun:
    """Build, tag, flash and verify the firmware under test before the board
    is opened; with --fw none, only record what the board already carries.
    The rig lock is taken here and held through the run, so the flasher and
    the board share one hold."""
    opt = request.config.getoption
    try:
        spec = parse_spec(opt("--fw"), target=opt("--fw-target"), defines=tuple(opt("--define")),
                          tag=opt("--tag"), allow_dirty=opt("--allow-dirty"))
    except SpecError as e:
        pytest.exit(str(e), returncode=2)
    declared = tuple(d for d in opt("--fw-defines").split(",") if d)
    try:
        lock = RigLock(station.rig.id, wait=5.0).acquire()
    except RigBusy as e:
        pytest.exit(f"station {station.id}: {e}", returncode=e.code)
    try:
        if fake_requested():
            run = FirmwareRun(spec, defines=declared, primary=primary_checkout(station))
            run.record = {"spec": spec.describe(), "variant": spec.is_variant, "defines": declared}
        else:
            try:
                run = prepare_firmware(spec, station, declared_defines=declared, flash_board=not opt("--no-flash"),
                                       log=lambda m: print(f"[firmware] {m}"))
            except FirmwareError as e:
                pytest.exit(str(e), returncode=e.code)
        run_dir.meta["firmware"] = run.record
        yield run
    finally:
        lock.release()


@pytest.fixture(scope="session")
def board(request: pytest.FixtureRequest, station, run_dir: RunDir, firmware: FirmwareRun) -> Board:
    """The station's board: rig locked, port open, probed, capabilities final,
    the recovery ladder's rungs attached, and the C64 in the --region
    standard when one was given."""
    factory = kbd_factory = power_factory = None
    if fake_requested():
        from selftest.fake_board import start_fake_board
        fake, stream = start_fake_board(debug_defines=set(firmware.defines))   # the build the run claims
        factory = lambda: Link(stream)  # noqa: E731
        kbd_factory = fake_keyboard(station, fake)
        power_factory = fake_relay(station, fake)
        setattr(request.config, FAKE_ATTR, fake)
    mask = {c for c in request.config.getoption("--caps-mask").split(",") if c}
    try:
        b = Board.open(station, attended=request.config.getoption("--attended"), link_factory=factory,
                       fw_defines=firmware.defines, caps_mask=mask, firmware=firmware, kbd_factory=kbd_factory,
                       power_factory=power_factory)
    except Exception as e:
        pytest.exit(f"station {station.id}: {e}", returncode=4)
    if fake_requested():
        b.reconnect_hook = lambda: Link(fake.host_stream)       # the fake's USB coming back
    line = keyboard_line(b.kbd, b.kbd_problem)
    if line:
        _log(line)
    if b.power_problem:
        _log(f"[power] off: {b.power_problem}")
    region = request.config.getoption("--region")
    if region:
        try:
            if set_standard(b, region, log=_log):
                _log(f"[kawari] the C64 runs {region} now")
        except (NotAllowed, KawariError, KbdError, LinkError, TransportError) as e:
            b.close()
            pytest.exit(f"station {station.id}: --region {region}: {e}",
                        returncode=EXIT_USAGE if isinstance(e, NotAllowed) else EXIT_NO_STATION)
        b.region = region                               # a power cycle undoes it; the ladder puts it back
    if request.config.getoption("--power-check"):
        try:
            timings = b.power_cycle()
        except (PowerUnavailable, NotAllowed, KawariError, KbdError, LinkError, TransportError) as e:
            b.close()
            pytest.exit(f"station {station.id}: --power-check: {e}", returncode=EXIT_NO_STATION)
        run_dir.meta["power_check"] = timings
        _log(f"[power] check passed: the board left USB {timings['left_usb_s']} s after the cut, and was at "
             f"the menu {timings['menu_s']} s after power came back")
    run_dir.meta["station"] = describe_station(b)
    run_dir.meta["board"] = describe_board(b)
    yield b
    run_dir.meta["recoveries"] = list(b.recoveries)
    b.close()


@pytest.fixture
def at_menu(request: pytest.FixtureRequest, board: Board, run_dir: RunDir):
    """Before: the board at the menu, by the recovery ladder if needed. After:
    the test's artifacts written and the invariants checked."""
    if board.wedged_reason:
        pytest.skip(f"station wedged: {board.wedged_reason}")
    try:
        board.ensure_menu()
    except Wedged as e:
        pytest.skip(f"station wedged: {e}")
    t0 = time.monotonic()
    mark = len(board.link.stream.tap.chunks)
    yield board
    events = write_board_artifacts(run_dir.for_test(request.node.nodeid), board, t0, mark)
    kinds = {e.kind for e in events}
    broken = [k for k in ("crash_report", "boot_banner", "unknown_command") if k in kinds]
    try:
        board.ensure_menu()
    except Wedged as e:
        broken.append(f"not back at the menu: {e}")
    if broken:
        raise AssertionError(f"invariants after {request.node.name}: {', '.join(broken)}")


@pytest.fixture
def link(at_menu: Board) -> Link:
    return at_menu.link


# -- video ----------------------------------------------------------------------

def _drop_video(board: Board, run_dir: RunDir, why: str) -> None:
    _log(f"[video] {why}; tests that need video skip")
    board.mask({"video"})
    run_dir.meta["video"] = {"available": False, "reason": why}


@pytest.fixture(scope="session")
def video(request: pytest.FixtureRequest, board: Board, run_dir: RunDir) -> FrameSource | None:
    """The rig's capture (the fake board's rendered screen under the fake),
    or None when the rig has none, --no-video was given, or it will not open."""
    if request.config.getoption("--no-video"):
        _drop_video(board, run_dir, "--no-video")
        yield None
        return
    if fake_requested():
        from selftest.synth import fake_frame_source
        src = fake_frame_source(getattr(request.config, FAKE_ATTR))
        run_dir.meta["video"] = {"available": True, **src.describe()}
        yield src
        src.close()
        return
    src = open_video(board.station, log=_log)
    if src is None:
        _drop_video(board, run_dir, "no capture device" if board.station.rig.capture is None
                    else "the capture device did not open")
        yield None
        return
    if not picture_ok(board, src, log=_log):
        src.close()
        _drop_video(board, run_dir, "the capture shows only black while the board is at its menu")
        yield None
        return
    run_dir.meta["video"] = {"available": True, **src.describe()}
    yield src
    src.close()


@pytest.fixture(scope="session")
def glyph_bank(request: pytest.FixtureRequest, board: Board, video: FrameSource | None,
               run_dir: RunDir) -> GlyphBank | None:
    if video is None:
        return None
    if fake_requested():
        from selftest.synth import calibrated, harvested_bank
        return harvested_bank(calibrated())
    bank = load_bank(board.station, request.config.getoption("--chargen"), log=_log)
    if bank is None:
        _drop_video(board, run_dir, "no glyph bank for this station or its capture class")
        return None
    run_dir.meta["glyph_bank"] = {"capture_class": bank.capture_class, "font": bank.font,
                                  "harvested": bank.harvested(), "created": bank.created}
    return bank


@pytest.fixture(scope="session")
def calibration(board: Board, video: FrameSource | None, glyph_bank: GlyphBank | None,
                run_dir: RunDir) -> Calibration | None:
    if video is None or glyph_bank is None:
        return None
    if fake_requested():
        from selftest.synth import calibrated
        return calibrated()
    try:
        cal = load_or_calibrate(board, video, glyph_bank, log=_log)
    except (CalibrationError, CaptureError, LinkError, TransportError, Wedged) as e:
        _drop_video(board, run_dir, f"calibration failed: {e}")
        return None
    run_dir.meta["calibration"] = {
        "method": cal.method, "residual": round(cal.residual, 4), "cell": [round(cal.cell_w, 3), round(cal.cell_h, 3)],
        "origin": [round(cal.x0, 2), round(cal.y0, 2)], "noise_floor": round(cal.noise_floor, 4),
        "frame": [cal.frame_w, cal.frame_h],
    }
    return cal


@pytest.fixture
def screen_now(request: pytest.FixtureRequest, at_menu: Board, video: FrameSource | None):
    """For a test that switches the video standard, where the session's
    calibration and glyph bank stop fitting: a function that returns a screen
    channel read with the bank and calibration for the standard the board
    runs at the call (stored ones when they fit, else a calibration made
    from the menu and saved). Without video, the channel reads by DMA alone."""
    def make() -> ScreenChannel:
        bank = cal = None
        if video is not None and fake_requested():
            from selftest.synth import calibrated, harvested_bank
            cal = calibrated()
            bank = harvested_bank(cal)
        elif video is not None:
            bank = load_bank(at_menu.station, request.config.getoption("--chargen"), log=_log)
            cal = load_or_calibrate(at_menu, video, bank, log=_log) if bank is not None else None
        live = video if cal is not None and bank is not None else None
        return ScreenChannel(at_menu.link, live, cal, bank, dma_allowed=at_menu.dma_allowed)
    return make


@pytest.fixture
def screen(request: pytest.FixtureRequest, at_menu: Board, video: FrameSource | None,
           calibration: Calibration | None, glyph_bank: GlyphBank | None, run_dir: RunDir) -> ScreenChannel:
    """The screen through the channel policy; after the test, the video-vs-DMA
    cross-checks it made are written out and the worst one is an invariant."""
    checks: list[XCheck] = []
    has_video = video is not None and calibration is not None and glyph_bank is not None
    channel = ScreenChannel(at_menu.link, video if has_video else None, calibration, glyph_bank,
                            dma_allowed=at_menu.dma_allowed, on_xcheck=checks.append)
    yield channel
    folder = run_dir.for_test(request.node.nodeid)
    if has_video and getattr(request.node, "rep_failed", False):   # set by the report hook below, on a call
        save_strip(video.ring(), folder / "frames.png")
    if not checks:
        return
    worst = max(checks, key=lambda x: x.disagreement)
    run_dir.write_json("xcheck.json", [
        {"disagreement": round(x.disagreement, 4), "charset_agrees": x.charset_agrees, "cells": x.cells[:50]}
        for x in checks], folder)
    if worst.disagreement > XCHECK_LIMIT and not request.config.getoption("--no-xcheck"):
        raise AssertionError(f"video disagrees with DMA on {worst.disagreement:.1%} of cells "
                             f"(limit {XCHECK_LIMIT:.0%}); see xcheck.json")


# -- the menu, keys, debug letters, programs, the card, goldens -----------------

@pytest.fixture
def warn(request: pytest.FixtureRequest):
    """``warn(text)``: a line in the test's ``warnings`` in run.json and in
    report.html, for something to look at that does not fail the test (a
    margin that shrank against an earlier build)."""
    def add(text: str) -> None:
        request.node.user_properties.append((WARNING_PROPERTY, text))
        print(f"[treadmill] warning: {text}")
    return add


@pytest.fixture
def artifacts(request: pytest.FixtureRequest, run_dir: RunDir) -> Path:
    """This test's folder under the run: where it writes what it saw."""
    return run_dir.for_test(request.node.nodeid)


@pytest.fixture
def keys(at_menu: Board) -> Keys:
    """A way to press keys that reach GETIN: the KERNAL buffer by DMA when
    the TR+ can write it, else the keyboard board's matrix."""
    press = keys_for(at_menu)
    if press is None:
        pytest.skip(_lacks(at_menu, ["dma", "keys_hw"] if at_menu.station.has("keys_kernal") else ["keys_kernal"]))
    return press


@pytest.fixture
def kbd(at_menu: Board) -> KeyboardBoard:
    """The station's keyboard board, for real matrix keys, holds, RESTORE and
    its other outputs on any TR. After the test, ``A`` lets go of whatever
    it still holds before the menu is checked, and the TOD line goes back to
    the station's rate."""
    if not at_menu.station.has("keys_hw"):
        pytest.skip(_lacks(at_menu, ["keys_hw"]))
    yield at_menu.kbd
    at_menu.kbd.abort()
    at_menu.apply_tod()


@pytest.fixture
def menu(at_menu: Board, screen: ScreenChannel) -> MenuDriver:
    """The main menu through the screen channel, able to move when the station presses keys."""
    return menu_for(at_menu, screen)


@pytest.fixture
def dbg(at_menu: Board) -> DebugCommands:
    """The single-letter debug commands, gated on the flashed build's defines."""
    return dbg_for(at_menu)


@pytest.fixture(scope="session")
def programs(firmware: FirmwareRun):
    """``programs("hello")``: one of the suite's C64 programs, assembled; a
    skip when no assembler is reachable."""
    def assemble_or_skip(name: str, **defines: int) -> Path:
        try:
            return assemble(name, primary=firmware.primary, extra=defines or None)
        except AssembleError as e:
            pytest.skip(f"cannot assemble {name}: {e}")
    return assemble_or_skip


@pytest.fixture(scope="session")
def sd_card(board: Board) -> str:
    """The card answers a listing of its root, or every test on it skips.
    Returns the root the fixtures live under."""
    try:
        board.ensure_menu()
        board.link.list_directory("/", "sd")
    except (Wedged, LinkError, TransportError) as e:
        pytest.skip(f"no SD card on {board.station.id}: {e}")
    return FIXTURE_ROOT


@pytest.fixture(scope="session")
def sd_fixtures(board: Board, sd_card: str) -> list[dict]:
    """The type set on the card, provisioned once per run."""
    try:
        board.ensure_menu()
        return provision(board.link, type_set(), log=_log)
    except (Wedged, FixtureError, LinkError, TransportError) as e:
        pytest.skip(f"the SD fixtures could not be put on the card: {e}")


class Goldens:
    """Screen goldens by name (``menu/root`` is ``<golden dir>/menu/root.screen.txt``);
    every check leaves the screen it saw, and the diff when it differed, in
    the test's artifact folder."""

    def __init__(self, root: Path, folder: Path):
        self.root = root
        self.folder = folder

    def load(self, name: str) -> Golden:
        path = self.root / f"{name}.screen.txt"
        if not path.is_file():
            pytest.fail(f"no golden {path}", pytrace=False)
        return Golden.load(path)

    def check(self, read: ScreenRead, name: str) -> None:
        self.check_rows(read.rows(), self.load(name), name)

    def check_rows(self, rows: list[str], golden: Golden | list[str], name: str) -> None:
        if not isinstance(golden, Golden):
            golden = Golden.parse("\n".join(f"|{r}|" for r in golden), name)
        stem = name.replace("/", "-")
        save_golden(rows, self.folder / f"{stem}.screen.txt")
        mismatches = compare(rows, golden)
        if mismatches:
            diff = diff_text(rows, golden)
            (self.folder / f"{stem}.diff.txt").write_text(diff + "\n", encoding="utf-8")
            pytest.fail(f"{len(mismatches)} cell(s) differ from golden {name}:\n{diff}", pytrace=False)


@pytest.fixture
def goldens(request: pytest.FixtureRequest, run_dir: RunDir) -> Goldens:
    root = request.config.getoption("--golden-dir") or (Path(request.config.rootpath) / "tests" / "golden")
    return Goldens(Path(root), run_dir.for_test(request.node.nodeid))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call":
        item.rep_failed = rep.failed  # type: ignore[attr-defined]


def _lacks(board: Board, missing: list[str]) -> str:
    """The skip reason for capabilities the station lacks, with why the
    keyboard board's are off when it did not open."""
    why = ""
    if board.kbd_problem and KEYBOARD_CAPS.intersection(missing):
        why = f" (keyboard board: {board.kbd_problem})"
    elif board.power_problem and "power" in missing:
        why = f" (power: {board.power_problem})"
    return f"station {board.station.id} lacks {', '.join(missing)}{why}"


@pytest.fixture(autouse=True)
def _capability_gate(request: pytest.FixtureRequest):
    item = request.node
    needs = _marker_args(item, "needs")
    needs_fw = _marker_args(item, "needs_fw")
    tolerant_only = item.get_closest_marker("timing_sensitive") is not None
    if not (needs or needs_fw or tolerant_only):
        return
    board = request.getfixturevalue("board")
    st = board.station
    missing = [c for c in needs if c not in st.caps]
    if missing:
        pytest.skip(_lacks(board, missing))
    fw_missing = [d for d in needs_fw if f"fw:{d}" not in st.caps]
    if fw_missing:
        pytest.skip(f"firmware lacks {', '.join(fw_missing)}; run with "
                    + " ".join(f"--define {d}" for d in fw_missing))
    if tolerant_only and st.machine.timing_tolerant \
            and not request.config.getoption("--include-tolerant"):
        pytest.skip(f"{st.machine.model} is timing-tolerant; --include-tolerant to run anyway")
