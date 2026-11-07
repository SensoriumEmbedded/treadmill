"""The pytest side of TReadmill: options, markers, fixtures, invariants.

Every hardware test gets a ``board`` at the menu, a per-test artifact folder
with the serial log and events of its run, and after it the invariants that
hold whatever it asserted: no crash report, no unexpected boot banner, no
unknown-command reply, and the board back at the menu.
"""

from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from treadmill import __version__
from treadmill.board import Board, Wedged
from treadmill.c64.keys import KernalKeys, Keys
from treadmill.c64.menu import MenuDriver
from treadmill.firmware.build import BuildError, build
from treadmill.firmware.checkout import CheckoutError
from treadmill.firmware.flash import REAPPEAR_S, FlashError, flash, relaunch
from treadmill.firmware.hexinfo import HexInfo, inspect_hex
from treadmill.firmware.spec import FirmwareSpec, SpecError, parse_spec
from treadmill.fixtures.sdcard import FIXTURE_ROOT, FixtureError, provision, type_set
from treadmill.link import DebugCommands, Link, LinkError, NotFound, TransportError, find_port, open_serial
from treadmill.observe.calibrate import Calibration, CalibrationError
from treadmill.observe.capture import CaptureError, FrameSource, SyntheticSource, save_strip
from treadmill.observe.channels import XCHECK_LIMIT, ScreenChannel, ScreenRead, XCheck
from treadmill.observe.glyphs import GlyphBank
from treadmill.observe.golden import Golden, compare, diff_text, save_golden
from treadmill.programs.build import AssembleError, assemble
from treadmill.report import RunDir
from treadmill.session import load_bank, load_or_calibrate, open_video
from treadmill.station import ConfigError, load_station
from treadmill.station.model import unknown_caps

FAKE_ENV = "TREADMILL_FAKE_BOARD"   # the self-tests: a fake board instead of a port


def _log(message: str) -> None:
    print(message)


def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("treadmill")
    g.addoption("--station", action="store", default=None,
                help="the station id (data/stations/<id>.toml) the hardware tests run against")
    g.addoption("--caps-mask", action="store", default="",
                help="comma-separated capabilities to take away, to prove a lower tier on a richer rig")
    g.addoption("--include-tolerant", action="store_true", default=False,
                help="run timing_sensitive tests on timing-tolerant machines too")
    g.addoption("--soak", action="store", type=int, default=0,
                help="run soak tests with this many iterations (0 = skip them)")
    g.addoption("--estimate", action="store_true", default=False,
                help="print the expected duration of the selected tests and exit")
    g.addoption("--reports", action="store", default="reports",
                help="where run folders go (default: reports/)")
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


def pytest_configure(config: pytest.Config) -> None:
    for line in (
        "needs(*caps): station capabilities the test requires",
        "needs_fw(*defines): firmware defines the flashed build must carry",
        "needs_asset(*names): private assets the test needs",
        "timing_sensitive: skipped on timing-tolerant machines",
        "destructive: changes board settings and restores them",
        "soak: long-running; selected only with --soak N",
        "client_path: drives the board through TeensyROM-Web and trom",
        "duration(minutes): the declared estimate",
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


# -- fixtures ---------------------------------------------------------------

@pytest.fixture(scope="session")
def run_dir(request: pytest.FixtureRequest) -> RunDir:
    rd = RunDir(Path(request.config.getoption("--reports")))
    rd.meta.update({
        "suite": __version__,
        "python": platform.python_version(),
        "host": platform.node(),
        "options": {k: request.config.getoption(k) for k in
                    ("--station", "--caps-mask", "--soak", "--include-tolerant", "--fw", "--fw-target",
                     "--define", "--tag", "--allow-dirty", "--no-flash", "--fw-defines")},
    })
    yield rd
    rd.meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    rd.write_run()


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


@dataclass
class FirmwareRun:
    """What the run built and flashed, or found on the board."""
    spec: FirmwareSpec
    hex: HexInfo | None = None
    defines: tuple[str, ...] = ()
    primary: Path | None = None
    flashed: bool = False
    cached: bool = False


def _primary_checkout(station) -> Path | None:
    where = station.rig.firmware_checkout or os.environ.get("TREADMILL_FW_REPO")
    return Path(where) if where else None


def _current_banner(station):
    """A quick look at the board before any flash: its kind and build."""
    found = find_port(station.tr.uid, station.tr.port_hint)
    with Link(open_serial(found.port)) as link:
        image = link.fw_check()
        return (link.banner() if image else None), found.port


@pytest.fixture(scope="session")
def firmware(request: pytest.FixtureRequest, station, run_dir: RunDir) -> FirmwareRun:
    """Build, tag, flash and verify the firmware under test before the board
    is opened; with --fw none, only record what the board already carries."""
    opt = request.config.getoption
    try:
        spec = parse_spec(opt("--fw"), target=opt("--fw-target"), defines=tuple(opt("--define")),
                          tag=opt("--tag"), allow_dirty=opt("--allow-dirty"))
    except SpecError as e:
        pytest.exit(str(e), returncode=2)
    declared = tuple(d for d in opt("--fw-defines").split(",") if d)
    run = FirmwareRun(spec, defines=declared, primary=_primary_checkout(station))
    run_dir.meta["firmware"] = {"spec": spec.describe(), "variant": spec.is_variant}
    if spec.source_kind == "none" or os.environ.get(FAKE_ENV):
        return run
    if run.primary is None:
        pytest.exit("no firmware checkout: set the rig's firmware_checkout or TREADMILL_FW_REPO", returncode=2)
    try:
        current, port = _current_banner(station)
        target = spec.target
        if target == "auto":
            if current is None or current.kind is None:
                pytest.exit("--fw-target auto needs a board that answers; name tr or tr-plus", returncode=4)
            target = current.kind
        if spec.source_kind == "hex":
            run.hex = inspect_hex(spec.source)
        else:
            built = build(spec, run.primary, target, log=lambda m: print(f"[firmware] {m}"))
            run.hex, run.cached = built.hex, built.cached
            run.defines = spec.defines
            run_dir.meta["firmware"].update({
                "commit": built.checkout.commit, "describe": built.checkout.describe,
                "dirty": built.checkout.dirty, "diff_hash": built.checkout.diff_hash,
                "cache_key": built.key, "cached": built.cached, "memory": built.memory_lines,
            })
        run_dir.meta["firmware"].update({
            "hex": str(run.hex.path), "version": run.hex.version, "stamp": run.hex.stamp,
            "tag": run.hex.tag, "hex_dirty": run.hex.dirty, "defines": run.defines, "target": target,
        })
        if run.hex.kind and run.hex.kind != target:
            pytest.exit(f"{run.hex.path.name} is a {run.hex.kind} image; the target is {target}", returncode=2)
        if not opt("--no-flash"):
            flashed = flash(run.hex, station.tr.uid, run.primary, port_hint=port, current=current,
                            log=lambda m: print(f"[firmware] {m}"))
            run.flashed = not flashed.skipped
            run_dir.meta["firmware"]["flashed"] = run.flashed
    except (BuildError, CheckoutError, FlashError, SpecError, NotFound, TransportError, LinkError) as e:
        pytest.exit(f"firmware: {e}", returncode=1)
    return run


@pytest.fixture(scope="session")
def board(request: pytest.FixtureRequest, station, run_dir: RunDir, firmware: FirmwareRun) -> Board:
    """The station's board: rig locked, port open, probed, capabilities final."""
    factory = None
    if os.environ.get(FAKE_ENV):
        from selftest.fake_board import start_fake_board
        fake, stream = start_fake_board(debug_defines=set(firmware.defines))   # the build the run claims
        factory = lambda: Link(stream)  # noqa: E731
        request.config._treadmill_fake = fake  # type: ignore[attr-defined]
    mask = {c for c in request.config.getoption("--caps-mask").split(",") if c}
    try:
        b = Board.open(station, attended=request.config.getoption("--attended"), link_factory=factory,
                       fw_defines=firmware.defines, caps_mask=mask)
    except Exception as e:
        pytest.exit(f"station {station.id}: {e}", returncode=4)
    if factory is None:
        uid, hint = station.tr.uid, station.tr.port_hint

        def reconnect() -> Link:
            found = find_port(uid, hint, timeout=REAPPEAR_S)
            return Link(open_serial(found.port))
        b.reconnect_hook = reconnect
        if firmware.hex is not None and firmware.primary is not None:
            b.reboot_hook = lambda: relaunch(firmware.hex, uid, firmware.primary, hint)
    run_dir.meta["station"] = {
        "id": b.station.id, "rig": b.station.rig.id, "machine": b.station.machine.id,
        "model": b.station.machine.model, "region": b.station.machine.region,
        "caps": sorted(b.station.caps), "tr_uid": b.station.tr.uid, "tr_kind": b.station.tr.kind,
    }
    run_dir.meta["board"] = {
        "image": b.image,
        "version": b.banner.version if b.banner else None,
        "built": b.banner.built if b.banner else None,
        "tag": b.banner.tag if b.banner else None,
        "dirty": b.banner.dirty if b.banner else None,
        "machine_line": f"{b.banner.machine} {b.banner.video} {b.banner.hz}" if b.banner else None,
    }
    yield b
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
    tap_start = len(board.link.stream.tap.chunks)
    yield board
    # What the board said during the test, including lines nothing has read yet.
    stale = board.link.stream.drain(0.3)
    if stale:
        board.events.feed(stale)
    events = board.events.since(t0)
    folder = run_dir.for_test(request.node.nodeid)
    raw = b"".join(c for _, c in board.link.stream.tap.chunks[tap_start:])
    (folder / "serial.log").write_bytes(raw)
    run_dir.write_json("events.json", [e for e in events], folder)
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
    if os.environ.get(FAKE_ENV):
        from selftest.synth import fake_frame_source
        src = fake_frame_source(request.config._treadmill_fake)  # type: ignore[attr-defined]
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
    run_dir.meta["video"] = {"available": True, **src.describe()}
    yield src
    src.close()


@pytest.fixture(scope="session")
def glyph_bank(request: pytest.FixtureRequest, board: Board, video: FrameSource | None,
               run_dir: RunDir) -> GlyphBank | None:
    if video is None:
        return None
    if os.environ.get(FAKE_ENV):
        from selftest.synth import calibrated, harvested_bank
        return harvested_bank(calibrated())
    bank = load_bank(board.station, request.config.getoption("--chargen"), log=_log)
    if bank is None:
        _drop_video(board, run_dir, "no glyph bank for this capture class")
        return None
    run_dir.meta["glyph_bank"] = {"capture_class": bank.capture_class, "font": bank.font,
                                  "harvested": bank.harvested(), "created": bank.created}
    return bank


@pytest.fixture(scope="session")
def calibration(board: Board, video: FrameSource | None, glyph_bank: GlyphBank | None,
                run_dir: RunDir) -> Calibration | None:
    if video is None or glyph_bank is None:
        return None
    if os.environ.get(FAKE_ENV):
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
    if has_video and request.node.rep_failed:   # set by the report hook below
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
def artifacts(request: pytest.FixtureRequest, run_dir: RunDir) -> Path:
    """This test's folder under the run: where it writes what it saw."""
    return run_dir.for_test(request.node.nodeid)


@pytest.fixture
def keys(at_menu: Board) -> Keys:
    """A way to press keys on the station: the KERNAL buffer by DMA on a TR+
    (the keyboard board of a rig that has one comes with Phase 2)."""
    if not at_menu.station.has("keys_kernal"):
        pytest.skip(f"station {at_menu.station.id} lacks keys_kernal")
    return KernalKeys(at_menu)


@pytest.fixture
def menu(at_menu: Board, screen: ScreenChannel) -> MenuDriver:
    """The main menu through the screen channel, able to move when the station presses keys."""
    press = KernalKeys(at_menu) if at_menu.station.has("keys_kernal") else None
    return MenuDriver(screen, press, board=at_menu)


@pytest.fixture
def dbg(at_menu: Board) -> DebugCommands:
    """The single-letter debug commands, gated on the flashed build's defines."""
    defines = {c[len("fw:"):] for c in at_menu.station.caps if c.startswith("fw:")}
    return DebugCommands(at_menu.link, defines, allow_eeprom_reset=at_menu.station.allow_eeprom_reset)


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


@pytest.fixture(autouse=True)
def _capability_gate(request: pytest.FixtureRequest):
    item = request.node
    needs = _marker_args(item, "needs")
    needs_fw = _marker_args(item, "needs_fw")
    tolerant_only = item.get_closest_marker("timing_sensitive") is not None
    if not (needs or needs_fw or tolerant_only):
        return
    st = request.getfixturevalue("board").station
    missing = [c for c in needs if c not in st.caps]
    if missing:
        pytest.skip(f"station {st.id} lacks {', '.join(missing)}")
    fw_missing = [d for d in needs_fw if f"fw:{d}" not in st.caps]
    if fw_missing:
        pytest.skip(f"firmware lacks {', '.join(fw_missing)}; run with --define or --auto-rebuild")
    if tolerant_only and st.machine.timing_tolerant \
            and not request.config.getoption("--include-tolerant"):
        pytest.skip(f"{st.machine.model} is timing-tolerant; --include-tolerant to run anyway")
