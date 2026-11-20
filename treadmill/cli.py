"""The ``treadmill`` command. Every subcommand takes ``--json`` (the shell
excepted) and exits non-zero on failure, so an agent can drive it from
structured output: 0 is good, 1 a failure of what ran, 2 the request, 4 a
station that could not be had.

``run`` puts the firmware under test on a station's board and runs the
hardware tests against it; ``exec`` runs a script and ``shell`` opens a REPL
on a station opened the same way. ``stations`` lists what is configured;
``calibrate``, ``harvest``, ``screen``, ``frame`` and ``viewer`` are the video
channel's tools on a live station (``calibrate`` also works on a saved frame,
and ``score`` compares a saved frame with the DMA read taken with it).
``sweep`` finds a timing knob's margin; ``mfg`` is the manufacturing test of
new TR+ boards, the one command that prompts an operator. ``matrix``,
``bisect`` and ``export`` arrive with their modules.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path

from treadmill import __version__
from treadmill.errors import EXIT_FAILED, EXIT_NO_STATION, EXIT_OK, EXIT_USAGE, TreadmillError  # noqa: F401
from treadmill.station import ConfigError, list_stations, load_station
from treadmill.station.model import tier

# The driver and the video tools are imported inside their commands: they bring OpenCV and the
# capture stack with them, which `stations`, `score` and a usage error should not pay for.


class CommandError(TreadmillError):
    def __init__(self, message: str, code: int = EXIT_FAILED):
        super().__init__(message)
        self.code = code


def _emit(payload: dict, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(text)


def _log_for(args: argparse.Namespace):
    """Diagnostics go to stderr, so --json output on stdout stays clean."""
    return lambda m: print(m, file=sys.stderr)


# -- stations -------------------------------------------------------------------

def cmd_stations(args: argparse.Namespace) -> int:
    ids = list_stations()
    rows = []
    for sid in ids:
        try:
            st = load_station(sid)
            rows.append({"id": sid, "rig": st.rig.id, "machine": st.machine.id,
                         "model": st.machine.model, "region": st.machine.region,
                         "tr_uid": st.tr.uid, "keyboard_chip": st.keyboard.chip if st.keyboard else None,
                         "tier": tier(st), "caps": sorted(st.caps), "error": None})
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
            kbd = f", keyboard board chip {r['keyboard_chip']}" if r["keyboard_chip"] else ""
            lines.append(f"{r['id']}: {r['model']} {r['region']} on {r['rig']}, TR uid {r['tr_uid']}{kbd}, "
                         f"tier {r['tier']} before opening, caps {', '.join(r['caps'])}")
    _emit({"stations": rows}, args.json, "\n".join(lines) if lines else "no stations in data/stations")
    return EXIT_FAILED if any(r.get("error") for r in rows) else EXIT_OK


# -- run, exec, shell -------------------------------------------------------------

def _firmware_options(args: argparse.Namespace):
    from treadmill.driver import FirmwareOptions

    return FirmwareOptions(fw=args.fw, target=args.fw_target, defines=tuple(dict.fromkeys(args.define)),
                           tag=args.tag, allow_dirty=args.allow_dirty, no_flash=args.no_flash,
                           fw_defines=tuple(d for d in args.fw_defines.split(",") if d))


def cmd_run(args: argparse.Namespace) -> int:
    """Build, flash and verify the firmware under test, run the hardware
    tests, leave the run's folder with run.json and report.html. With
    ``--region both``, two runs, PAL then NTSC: the record is ``runs``, one
    entry each, and the exit code is the higher of the two."""
    from treadmill.driver import RunOptions, run_regions

    opts = RunOptions(
        station=args.station, tests=tuple(args.tests or ()), reports=args.reports, caps_mask=args.caps_mask,
        region=args.region,
        soak=args.soak, estimate=args.estimate, include_tolerant=args.include_tolerant, attended=args.attended,
        no_video=args.no_video, no_xcheck=args.no_xcheck, golden_dir=args.golden_dir,
        firmware=_firmware_options(args), pytest_args=tuple(args.pytest_args),
    )
    # pytest's own output is the diagnostics stream: stderr under --json, so stdout stays the record.
    results = run_regions(opts, sink=sys.stderr if args.json else sys.stdout, log=_log_for(args))
    if len(results) == 1:
        _emit(_run_payload(results[0]), args.json, results[0].describe())
        return results[0].code
    code = max(r.code for r in results)
    _emit({"exit_code": code, "runs": [_run_payload(r) for r in results]}, args.json,
          "\n".join(r.describe() for r in results))
    return code


def _run_payload(result) -> dict:
    meta = result.meta or {}
    return {
        "run_dir": str(result.run_dir), "report": str(result.report) if result.report else None,
        "exit_code": result.code, "pytest_status": result.pytest_status, "summary": result.summary,
        "aborted": meta.get("aborted"), "station": meta.get("station"), "board": meta.get("board"),
        "firmware": meta.get("firmware"),
        "tests": [{k: t.get(k) for k in ("nodeid", "outcome", "duration", "message", "folder")}
                  for t in meta.get("tests") or []],
    }


def _harness_options(args: argparse.Namespace):
    from treadmill.driver import HarnessOptions

    return HarnessOptions(station=args.station, reports=args.reports, attended=args.attended,
                          no_video=args.no_video, caps_mask=args.caps_mask, to_menu=not args.as_is,
                          no_link=getattr(args, "no_link", False), firmware=_firmware_options(args))


def cmd_exec(args: argparse.Namespace) -> int:
    """Run a script on an open station, with the artifacts a test gets."""
    from treadmill.driver import Harness, exec_script

    script = Path(args.script)
    if not script.is_file():
        raise CommandError(f"no script {script}", EXIT_USAGE)
    log = _log_for(args)
    with Harness(_harness_options(args), f"exec-{script.stem}", log) as h:
        outcome = exec_script(h, script, tuple(args.script_args), quiet_stdout=args.json)
        payload = {"script": str(script), "run_dir": str(h.run_dir.path), "folder": str(h.folder),
                   "exit_code": outcome.code, "result": outcome.result, "error": outcome.error,
                   "station": h.run_dir.meta.get("station"), "board": h.run_dir.meta.get("board")}
    text = f"{script.name}: {'ok' if outcome.code == EXIT_OK else outcome.error}; artifacts {h.folder}"
    _emit(payload, args.json, text)
    return outcome.code


def cmd_shell(args: argparse.Namespace) -> int:
    """A REPL on an open station: link, screen, menu, keys, dbg, video."""
    from treadmill.driver import Harness, shell

    with Harness(_harness_options(args), "shell", _log_for(args)) as h:
        return shell(h)


KRREAD_KERNAL = "/treadmill/krprobe.bin"


def _scan_range(text: str, flag: str = "--scan") -> tuple[int, int]:
    low, sep, high = text.partition(":")
    try:
        if sep:
            return int(low), int(high)
    except ValueError:
        pass
    raise CommandError(f"{flag} {text!r}: give the range as LO:HI", EXIT_USAGE)


def cmd_sweep(args: argparse.Namespace) -> int:
    """Walk one timing knob until a probe fails, bisect the edge (one way, or
    both), or scan a range; restore the defaults; record the margin."""
    from treadmill import margins
    from treadmill.c64.settings import REU, Settings
    from treadmill.driver import Harness
    from treadmill.observe.golden import Golden
    from treadmill.knobs import standard_of
    from treadmill.probes import IdReadProbe, MenuProbe
    from treadmill.probes.idread import MinimalIdReadProbe
    from treadmill.c64.menu import MenuDriver
    from treadmill.c64.settings import KERNAL_REPLACE
    from treadmill.probes.cpuwatch import CpuWatchProbe
    from treadmill.probes.krread import DMA, KERNAL, KERNAL_SIZE, SKOE, KrReadProbe, ksense_mode, probe_kernal
    from treadmill.probes.noise import NoiseProbe
    from treadmill.probes.reuloop import ReuLoopProbe
    from treadmill.probes.vicrom import VicRomProbe
    from treadmill.probes.zpattern import ZPatternProbe
    from treadmill.programs.build import AssembleError, assemble
    from treadmill.station.config import repo_root
    from treadmill.sweeps import SearchSpec, Sweep, SweepError, knob_named

    scan = _scan_range(args.scan) if args.scan else None
    grid = _scan_range(args.grid, "--grid") if args.grid else None
    if sum((args.to is not None, args.both, scan is not None)) > 1:
        raise CommandError("give one of --to, --both and --scan", EXIT_USAGE)
    try:
        knob = knob_named(args.knob)
        spec = SearchSpec(stop=args.to, start=args.start, step=args.step, fine=args.fine, repeat=args.repeat,
                          both=args.both, passes=args.passes, scan=scan, grid=grid)
    except SweepError as e:
        raise CommandError(str(e), EXIT_USAGE) from e
    log = _log_for(args)
    with Harness(_harness_options(args), f"sweep-{knob.letter}", log) as h:
        ns = h.namespace
        board, dbg = ns["board"], ns["dbg"]
        if not dbg.available("t"):
            raise CommandError(f"the flashed build lacks {knob.define}: build with --fw ... --define {knob.define}, "
                               f"or name the defines a build flashed by other means carries with --fw-defines",
                               EXIT_USAGE)
        names = list(dict.fromkeys(args.probe or ["idread"]))
        if args.image == "minimal" and names != ["idread"]:
            raise CommandError("under MinimalBoot the probe is idread, read by video: leave out --probe or give "
                               "only --probe idread", EXIT_USAGE)
        if names == ["noise"]:
            raise CommandError("the noise probe runs beside another: add --probe idread, zpattern, reuloop, "
                               "reuloop-sprites or cpuwatch", EXIT_USAGE)

        def program(name: str):
            try:
                return assemble(name, primary=h.firmware.primary if h.firmware else None)
            except AssembleError as e:
                raise CommandError(str(e), EXIT_USAGE) from e

        probes = []
        for name in names:
            if name == "idread" and args.image == "minimal":
                try:
                    shown = assemble("idread", primary=h.firmware.primary if h.firmware else None,
                                     extra={"SHOW": 1})
                except AssembleError as e:
                    raise CommandError(str(e), EXIT_USAGE) from e
                probes.append(MinimalIdReadProbe(board, ns["screen"], shown, dwell=args.dwell,
                                                 usb_load=args.usb_load, log=log))
            elif name == "idread":
                probes.append(IdReadProbe(board, program("idread"), dwell=args.dwell, usb_load=args.usb_load,
                                          log=log))
            elif name == "menu":
                golden_dir = Path(args.golden_dir) if args.golden_dir else repo_root() / "tests" / "golden"
                probes.append(MenuProbe(board, ns["screen"], Golden.load(golden_dir / "menu" / "root.screen.txt"),
                                        h.folder))
            elif name == "zpattern":
                probes.append(ZPatternProbe(board, dbg, pages=args.z_pages, log=log))
            elif name in ("reuloop", "reuloop-sprites"):
                sprites = name == "reuloop-sprites"
                probes.append(ReuLoopProbe(board, program("reuclient_sprites" if sprites else "reuclient"),
                                           dwell=args.dwell, sprites=sprites, log=log))
            elif name == "cpuwatch":
                probes.append(CpuWatchProbe(board, dbg, program("cpuwatch"), pages=args.z_pages, log=log))
            elif name == "vicrom":
                probes.append(VicRomProbe(board, ns["video"], getattr(ns["screen"], "calib", None), program("vicrom"),
                                          dwell=args.dwell, log=log))
            elif name == "krread":
                image, defines = probe_kernal(board.link.read_memory(KERNAL, KERNAL_SIZE))
                board.link.push_file(image, KRREAD_KERNAL, "sd")
                try:
                    prg = assemble("krread", primary=h.firmware.primary if h.firmware else None, extra=defines)
                except AssembleError as e:
                    raise CommandError(str(e), EXIT_USAGE) from e
                probes.append(KrReadProbe(board, prg, defines["LEN"], dwell=args.dwell, log=log))
        if "noise" in names:
            screen = ns["screen"]
            probes.append(NoiseProbe(ns["video"], getattr(screen, "calib", None), probes[0], log=log))
        with contextlib.ExitStack() as stack:
            settings = Settings(ns["screen"], ns["keys"], log=log)
            if any(isinstance(p, ReuLoopProbe) and not isinstance(p, CpuWatchProbe) for p in probes):
                stack.enter_context(settings.special_io(REU))
            if any(isinstance(p, KrReadProbe) for p in probes):
                stack.enter_context(settings.kernal_file(MenuDriver(ns["screen"], ns["keys"], board),
                                                         "SD:" + KRREAD_KERNAL))
                stack.enter_context(settings.special_io(KERNAL_REPLACE))
                stack.enter_context(ksense_mode(dbg, SKOE if knob.letter == "k" else DMA))
            # MinimalBoot's td names no standard: the main image's banner gives it before the hand-off.
            standard = (standard_of(board.banner.machine, board.banner.video)
                        if args.image == "minimal" and board.banner else None)
            margin = Sweep(dbg, knob, probes, spec, hold=args.hold, standard=standard, log=log).run()
        record = margins.build(margin.to_dict(), margins.context_for(board, h.firmware.record if h.firmware else None,
                                                                     image=args.image))
        h.run_dir.write_json(margins.NAME, record, h.folder)
        payload = {**record, "run_dir": str(h.run_dir.path), "folder": str(h.folder)}
    _emit(payload, args.json, margin.describe() + f"\n{margins.NAME} in {h.folder}")
    return EXIT_OK


def cmd_margins(args: argparse.Namespace) -> int:
    """The margin records under a reports folder: a table, CSV, the map, the
    floors a station's runs support; or records made from PR #99 runs."""
    from treadmill import knobs, margins
    from treadmill.report import margin_map

    if args.what == "import":
        if not args.runs:
            raise CommandError("import needs the run folders of scripts/2026-11-12-dmasetup-sweep.py", EXIT_USAGE)
        written = []
        for run in args.runs:
            try:
                records = margins.import_pr99(Path(run))
            except (OSError, ValueError) as e:
                raise CommandError(str(e), EXIT_USAGE) from e
            for r in records:
                path = margins.write(Path(run) / f"import-{r['probes'][0]}", r)
                written.append({"path": str(path), "label": margin_map.label(r),
                                "edges": {w: (e["clean_to"], e["first_fail"]) for w, e in r["edges"].items()}})
        _emit({"written": written}, args.json,
              "\n".join(f"{w['label']}: {w['edges']} -> {w['path']}" for w in written))
        return EXIT_OK
    if args.runs:
        raise CommandError(f"{args.what} takes no run folders; only import does", EXIT_USAGE)
    records = margins.load(Path(args.reports))
    if args.station:
        records = [r for r in records if r.get("station") == args.station]
    if args.knob:
        try:
            name = knobs.knob_named(args.knob).name
        except KeyError as e:
            raise CommandError(e.args[0], EXIT_USAGE) from e
        records = [r for r in records if (r.get("knob") or {}).get("name") == name]
    if args.what == "table":
        _emit({"records": margins.rows(records)}, args.json, margins.table(records))
    elif args.what == "csv":
        _emit({"records": margins.rows(records)}, args.json, margins.to_csv(records).rstrip("\n"))
    elif args.what == "map":
        marks: dict[str, list[int]] = {}
        for text in args.mark or []:
            name, sep, value = text.partition("=")
            try:
                marks.setdefault(knobs.knob_named(name).name, []).append(int(value))
            except (KeyError, ValueError):
                raise CommandError(f"--mark {text!r}: give KNOB=VALUE, as nS_DMASetup=414", EXIT_USAGE) from None
        if args.mermaid:
            text = margin_map.mermaid(records, marks)
            _emit({"mermaid": text}, args.json, text.rstrip("\n") or "no margin records")
        else:
            out = margin_map.render(records, Path(args.out or Path(args.reports) / "margin-map.html"), marks)
            _emit({"map": str(out), "records": len(records)}, args.json, f"{len(records)} record(s): {out}")
    else:
        if not args.station:
            raise CommandError("suggest needs --station: floors are per station", EXIT_USAGE)
        floors = margins.suggest(records, args.station, last=args.last)
        _emit({"station": args.station, "floors": {k: {"up": f.up, "down": f.down} for k, f in floors.items()}},
              args.json, margins.floors_toml(floors).rstrip("\n") if floors else "no margin records for the station")
    return EXIT_OK


# -- manufacturing test -------------------------------------------------------------

def cmd_mfg(args: argparse.Namespace) -> int:
    """New TR+ boards one after another: wait for one on USB, test it, log it,
    wait for it to leave, repeat. ``--snapshot-sd`` writes the SD golden instead."""
    from treadmill.mfg import (
        FAIL, Console, MfgProfile, RealBench, default_profile_path, open_video_kit, run_session, sd_snapshot,
    )
    from treadmill.report.artifacts import RunDir
    from treadmill.session import fake_requested
    from treadmill.station.lock import RigLock

    profile_path = Path(args.profile) if args.profile else default_profile_path()
    if args.snapshot_sd:
        if args.out:
            out = Path(args.out)
        elif profile_path.is_file():
            out = MfgProfile.load(profile_path).sd_golden
        else:
            out = profile_path.with_name("sd_root.json")
        snapshot = sd_snapshot(args.snapshot_sd)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        print(f"{len(snapshot['entries'])} root entries of {args.snapshot_sd} written to {out}")
        return EXIT_OK
    if not args.station:
        raise CommandError("mfg needs --station (or --snapshot-sd)", EXIT_USAGE)
    profile = MfgProfile.load(profile_path)
    template = load_station(args.station)
    console = Console()
    log = _log_for(args)
    with RigLock(template.rig.id, wait=5.0):
        if fake_requested():
            from selftest.fake_bench import FakeBench
            bench = FakeBench(console)
            video = bench.video_kit()
        else:
            bench = RealBench(console, template.keyboard.chip if template.keyboard else None)
            video = open_video_kit(template, log)
        run_dir = RunDir(Path(args.reports))
        try:
            summary = run_session(template, profile, bench, console, video, run_dir, boards=args.boards, log=log)
        finally:
            video.close()
    console.say(f"Evidence: {run_dir.path}")
    return EXIT_FAILED if summary["counts"][FAIL] else EXIT_OK


# -- video tools ------------------------------------------------------------------

def _calibration_payload(cal) -> dict:
    return {"origin": [round(cal.x0, 2), round(cal.y0, 2)], "cell": [round(cal.cell_w, 3), round(cal.cell_h, 3)],
            "frame": [cal.frame_w, cal.frame_h], "background": list(cal.background), "border": list(cal.border),
            "noise_floor": round(cal.noise_floor, 4), "layout": cal.layout, "method": cal.method,
            "residual": round(cal.residual, 4)}


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Find the text area: on a saved frame, or on the station's live picture
    at the menu (refined against DMA where allowed), saved per capture class
    and machine."""
    from dataclasses import replace

    import cv2

    from treadmill.observe.calibrate import Calibration, CalibrationError, find_text_area
    from treadmill.session import load_or_calibrate, open_station, station_calibration_path

    log = _log_for(args)
    try:
        if args.frame:
            image = cv2.imread(str(args.frame), cv2.IMREAD_COLOR)
            if image is None:
                raise CommandError(f"cannot read {args.frame}", EXIT_USAGE)
            cal = find_text_area(image, args.layout)
            saved = None
            if args.station and not args.no_save:
                st = load_station(args.station)
                region = st.machine.region.upper()
                if region not in ("PAL", "NTSC"):
                    raise CommandError(f"a saved frame does not say which standard it shows, and machine "
                                       f"{st.machine.id} switches standards: calibrate from the live menu "
                                       f"(treadmill calibrate --station {st.id}), or add --no-save", EXIT_USAGE)
                st = replace(st, standard=region)
                cal = Calibration(**{**cal.__dict__, "capture_class": st.rig.capture.capture_class,
                                     "machine_id": st.machine.id})
                saved = cal.save(station_calibration_path(st))
        else:
            if not args.station:
                raise CommandError("--station or --frame is needed", EXIT_USAGE)
            with open_station(args.station, attended=args.attended, calibrate=False, log=log) as s:
                if s.video is None:
                    raise CommandError("the station has no capture, or it did not open", EXIT_NO_STATION)
                cal = load_or_calibrate(s.board, s.video, s.bank, force=True, save=not args.no_save, log=log)
                saved = None if args.no_save else station_calibration_path(s.station)
    except CalibrationError as e:
        raise CommandError(str(e)) from e
    payload = {"calibration": _calibration_payload(cal), "saved": str(saved) if saved else None}
    text = (f"text area at ({cal.x0:.1f}, {cal.y0:.1f}), cells {cal.cell_w:.2f}x{cal.cell_h:.2f} px in a "
            f"{cal.frame_w}x{cal.frame_h} frame; background {cal.background}, border {cal.border}; "
            f"{cal.method}, residual {cal.residual:.3f}" + (f"\nsaved {saved}" if saved else ""))
    _emit(payload, args.json, text)
    return EXIT_OK


def cmd_harvest(args: argparse.Namespace) -> int:
    """Build the station's glyph bank: launch the allcodes program for each
    character set, average frames of it, save the bank."""
    import cv2
    import numpy as np

    from treadmill.c64.screen import CHARSET_LOWER_UPPER, CHARSET_UPPER_GRAPHICS, SCREEN_BYTES
    from treadmill.firmware.checkout import DEFAULT_WORK_ROOT
    from treadmill.observe.calibrate import CalibrationError, refine
    from treadmill.observe.glyphs import BLANK_CODE, GlyphBank, bootstrap_bank, empty_bank, harvest, load_chargen
    from treadmill.observe.normalize import distance_map, padded_cells
    from treadmill.programs.build import AssembleError, assemble, sparse_pages
    from treadmill.session import CHARGEN_ENV, bank_path, open_station

    log = _log_for(args)
    sets = {"lu": [CHARSET_LOWER_UPPER], "ug": [CHARSET_UPPER_GRAPHICS],
            "both": [CHARSET_LOWER_UPPER, CHARSET_UPPER_GRAPHICS]}[args.charset]
    programs = {CHARSET_LOWER_UPPER: "allcodes_lu", CHARSET_UPPER_GRAPHICS: "allcodes_ug"}
    pages = sparse_pages()
    with open_station(args.station, attended=args.attended, log=log) as s:
        if s.video is None or s.calibration is None:
            raise CommandError("harvest needs the capture open and a calibration (treadmill calibrate first)",
                               EXIT_NO_STATION)
        primary = s.station.rig.firmware_checkout or os.environ.get("TREADMILL_FW_REPO")
        try:
            prgs = {(cs, page): assemble(f"{programs[cs]}{page}", primary=Path(primary) if primary else None,
                                         extra={"COLOR": args.color})
                    for cs in sets for page in range(len(pages))}
        except AssembleError as e:
            raise CommandError(str(e), EXIT_USAGE) from e
        path = Path(args.out) if args.out else bank_path(s.station)
        if path.is_file() and not args.fresh:
            bank = GlyphBank.load(path)
        else:
            bank = empty_bank(s.station.rig.capture.capture_class, s.bank.font if s.bank else "harvest")
        # Each page is its own launch, and the capture chain locks onto each
        # picture with its own sub-pixel phase, so every page's grid is refined
        # against the glyphs it is known to show before its cells are cut;
        # the character ROM is the reference all pages are aligned to.
        rom = os.environ.get(CHARGEN_ENV)
        reference = bootstrap_bank(load_chargen(rom), s.station.rig.capture.capture_class) if rom else s.bank
        if reference is None:
            log(f"[harvest] no character ROM ({CHARGEN_ENV}) and no bank: pages are not aligned to each other")
        keep = DEFAULT_WORK_ROOT / "harvest" / s.station.rig.capture.capture_class
        keep.mkdir(parents=True, exist_ok=True)
        results = {}
        for (cs, page), prg in prgs.items():
            cells, shown = pages[page]
            screen = np.full(SCREEN_BYTES, BLANK_CODE, np.uint8)
            screen[np.asarray(cells)] = np.asarray(shown, np.uint8)
            remote = f"{args.folder.rstrip('/')}/{prg.name}"
            s.board.ensure_menu()
            s.link.push_file(prg.read_bytes(), remote, args.drive)
            s.link.launch(remote, args.drive)
            time.sleep(args.settle)
            first = s.video.wait_stable(agree=2, timeout=10.0)
            cal = s.calibration
            if reference is not None:
                try:
                    cal = refine(first.image, cal, screen, cs, reference)
                    log(f"[harvest] {cs} page {page}: grid refined, residual {cal.residual:.2f} px, "
                        f"origin ({cal.x0:.2f}, {cal.y0:.2f})")
                except CalibrationError as e:
                    log(f"[harvest] {cs} page {page}: grid not refined ({e})")
            cv2.imwrite(str(keep / f"{cs}-{page}-color{args.color}.png"), first.image)
            frames = 0
            last = first
            for _ in range(args.frames):
                dmap = distance_map(cal.normalize(last.image), cal.background)
                harvest(bank, padded_cells(dmap, np.asarray(cells)), np.asarray(shown, np.uint8), cs)
                frames += 1
                last = s.video.next_frame(last, timeout=5.0)
            results[f"{cs}/{page}"] = frames
            log(f"[harvest] {cs} page {page}: {frames} frames")
            s.board.reset()
        bank.save(path)
    payload = {"bank": str(path), "frames": results, "harvested": bank.harvested()}
    _emit(payload, args.json, f"glyph bank {path}: {bank.harvested()} of 512 codes harvested "
                              f"({', '.join(f'{k} x{v}' for k, v in results.items())})")
    return EXIT_OK


def cmd_screen(args: argparse.Namespace) -> int:
    """Print the screen as the policy reads it (video unless --exact and DMA is allowed)."""
    from treadmill.observe.channels import ScreenError
    from treadmill.session import open_station

    log = _log_for(args)
    with open_station(args.station, attended=args.attended, log=log) as s:
        if args.menu:
            s.board.ensure_menu()
        try:
            read = s.screen_channel().read(exact=args.exact, xcheck_now=args.xcheck)
        except ScreenError as e:
            raise CommandError(str(e), EXIT_NO_STATION) from e
        x = s.xchecks[-1] if s.xchecks else None
    payload = {"screen": read.rows(), **read.summary(),
               "xcheck": None if x is None else {"disagreement": x.disagreement, "cells": x.cells[:50]}}
    text = "\n".join(f"|{r}|" for r in read.rows()) + f"\n({read.source}, charset {read.charset}, " \
           f"confidence {read.mean_confidence():.2f})"
    if x is not None:
        text += f"\nvideo vs DMA: {x.disagreement:.1%} of cells differ"
    _emit(payload, args.json, text)
    return EXIT_OK


def cmd_frame(args: argparse.Namespace) -> int:
    """Save one frame (and the normalized text area when calibrated) as PNG."""
    import cv2

    from treadmill.session import open_station

    log = _log_for(args)
    with open_station(args.station, attended=args.attended, calibrate=not args.raw, log=log) as s:
        if s.video is None:
            raise CommandError("the station has no capture, or it did not open", EXIT_NO_STATION)
        frame = s.video.wait_stable(agree=2, timeout=10.0) if args.stable else s.video.next_frame()
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), frame.image)
        norm_path = None
        if s.calibration is not None:
            norm_path = out.with_name(out.stem + "_norm" + out.suffix)
            cv2.imwrite(str(norm_path), s.calibration.normalize(frame.image))
    payload = {"frame": str(out), "normalized": str(norm_path) if norm_path else None,
               "size": list(frame.size), "seq": frame.seq}
    _emit(payload, args.json, f"saved {out}" + (f" and {norm_path}" if norm_path else ""))
    return EXIT_OK


def cmd_score(args: argparse.Namespace) -> int:
    """Score a saved frame against the DMA screen read taken with it, as the
    live cross-check does: on the grid refined against those codes (the best
    the decoder can do) and on the stored calibration, as a plain read uses it."""
    import cv2
    import numpy as np

    from treadmill.c64.screen import SCREEN_BYTES
    from treadmill.observe.calibrate import Calibration, find_text_area, refine, with_bleed
    from treadmill.observe.capture import Frame
    from treadmill.observe.channels import SOURCE_DMA, ScreenRead, read_video, xcheck
    from treadmill.observe.glyphs import GlyphBank
    from treadmill.session import find_bank, find_calibration

    image = cv2.imread(str(args.frame), cv2.IMREAD_COLOR)
    if image is None:
        raise CommandError(f"cannot read {args.frame}", EXIT_USAGE)
    codes = np.load(args.dma).astype(np.uint8).reshape(-1)
    if codes.size != SCREEN_BYTES:
        raise CommandError(f"{args.dma} holds {codes.size} codes, not {SCREEN_BYTES}", EXIT_USAGE)
    bank_file, calib_file = args.bank, args.calib
    if args.station:
        st = load_station(args.station)
        bank_file = bank_file or find_bank(st)
        calib_file = calib_file or find_calibration(st)
    if not bank_file:
        raise CommandError("--station or --bank is needed", EXIT_USAGE)
    bank = GlyphBank.load(bank_file)
    dma = ScreenRead(codes, args.charset, np.ones(SCREEN_BYTES, np.float32), SOURCE_DMA, 0.0)
    frame = Frame(image, 0.0, 1)
    cal = with_bleed(image, refine(image, find_text_area(image, args.layout), codes, args.charset, bank))
    reads = {"dma_refined": read_video(frame, cal, bank, args.charset, follow=False)}
    if calib_file and Path(calib_file).is_file():
        stored = Calibration.load(calib_file)
        if stored.fits(image):
            if stored.bleed is None:
                stored = with_bleed(image, stored)
            reads["stored"] = read_video(frame, stored, bank)
    payload, lines = {}, []
    for name, read in reads.items():
        x = xcheck(read, dma)
        payload[name] = {"disagreement": x.disagreement, "cells": x.cells, "charset": read.charset,
                         "confidence": round(read.mean_confidence(), 3)}
        lines.append(f"{name}: {x.disagreement:.1%} ({len(x.cells)} cells), charset {read.charset}, "
                     f"confidence {read.mean_confidence():.2f}")
        lines += [f"   row {r:2d} col {c:2d}: video {v:3d} dma {d:3d}" for r, c, v, d in x.cells[:40]]
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK


def cmd_viewer(args: argparse.Namespace) -> int:
    """A live window: the frame with the cell grid, and the decoded text. q quits."""
    import cv2

    from treadmill.observe.channels import read_video
    from treadmill.session import open_station

    log = _log_for(args)
    with open_station(args.station, attended=args.attended, log=log) as s:
        if s.video is None:
            raise CommandError("the station has no capture, or it did not open", EXIT_NO_STATION)
        cal = s.calibration
        last = None
        while True:
            try:
                last = s.video.next_frame(last, timeout=5.0)
            except Exception as e:  # noqa: BLE001 - a viewer keeps going
                log(f"[viewer] {e}")
                continue
            img = last.image.copy()
            if cal is not None:
                x0, y0, w, h = cal.text_area
                cv2.rectangle(img, (int(x0), int(y0)), (int(x0 + w), int(y0 + h)), (0, 255, 0), 1)
                for c in range(1, 40):
                    x = int(x0 + c * cal.cell_w)
                    cv2.line(img, (x, int(y0)), (x, int(y0 + h)), (0, 96, 0), 1)
                for r in range(1, 25):
                    y = int(y0 + r * cal.cell_h)
                    cv2.line(img, (int(x0), y), (int(x0 + w), y), (0, 96, 0), 1)
                if s.has_video:
                    read = read_video(last, cal, s.bank)
                    for i, row in enumerate(read.rows()):
                        cv2.putText(img, row, (8, 16 + 14 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
            cv2.imshow(f"treadmill viewer: {args.station}", img)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
        cv2.destroyAllWindows()
    return EXIT_OK


# -- parser -----------------------------------------------------------------------

def _station_args(s: argparse.ArgumentParser, required: bool = True, json_flag: bool = True) -> None:
    s.add_argument("--station", required=required, help="the station id (data/stations/<id>.toml)")
    s.add_argument("--attended", action="store_true", help="a person is at the bench: manual power may prompt")
    if json_flag:
        s.add_argument("--json", action="store_true")


def _firmware_args(s: argparse.ArgumentParser) -> None:
    g = s.add_argument_group("firmware under test")
    g.add_argument("--fw", default="none",
                   help="ref=<git ref> | path=<checkout> | hex=<file> | none (default: what the board carries)")
    g.add_argument("--fw-target", default="auto", choices=["auto", "tr", "tr-plus"],
                   help="the cartridge to build for; auto is the station's board kind")
    g.add_argument("--define", action="append", default=[], metavar="NAME",
                   help="a debug define to compile in (repeatable); the build is tagged with its mark")
    g.add_argument("--tag", help="the build tag; default: the marks of the defines, none for a clean build")
    g.add_argument("--allow-dirty", action="store_true",
                   help="build from a checkout with uncommitted changes (recorded as a variant)")
    g.add_argument("--no-flash", action="store_true", help="build but do not flash; use what the board carries")
    g.add_argument("--fw-defines", default="", metavar="A,B",
                   help="with none or hex=: the defines the flashed build carries")


def _harness_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("--reports", default="reports", help="where run folders go (default: reports/)")
    s.add_argument("--caps-mask", default="", metavar="A,B",
                   help="capabilities to take away, to prove a lower tier on a richer rig")
    s.add_argument("--no-video", action="store_true", help="leave the capture device closed")
    s.add_argument("--as-is", action="store_true",
                   help="do not bring the board to the menu first; take it as it is")
    _firmware_args(s)


def _no_link_arg(s: argparse.ArgumentParser) -> None:
    s.add_argument("--no-link", action="store_true",
                   help="open the rig, the capture and the keyboard board but not the TeensyROM: board, link, "
                        "menu and dbg are None, and the screen reads video from the stored calibration")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="treadmill", description="TeensyROM hardware-in-the-loop validation")
    p.add_argument("--version", action="version", version=f"treadmill {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("run", help="put the firmware under test on the board and run the hardware tests",
                       description="Build, flash and verify the firmware named by --fw (nothing with none), "
                                   "then run the hardware tests under tests/ against the station. Everything "
                                   "after -- goes to pytest as it is (-k EXPR, -x, -v ...). The run's folder "
                                   "holds run.json, report.html and one folder per test.")
    _station_args(s)
    s.add_argument("--tests", action="append", metavar="PATH[::NODE]",
                   help="a test file, folder or node id (repeatable); default: the whole tests/ tree")
    s.add_argument("--reports", default="reports", help="where run folders go (default: reports/)")
    s.add_argument("--caps-mask", default="", metavar="A,B",
                   help="capabilities to take away, to prove a lower tier on a richer rig")
    s.add_argument("--region", type=str.upper, choices=("PAL", "NTSC", "BOTH"),
                   help="the video standard to run in, switched first when the station has region_switch; "
                        "both = a PAL run, then an NTSC run; default: whatever the C64 runs now")
    s.add_argument("--soak", type=int, default=0, metavar="N", help="run soak tests with N iterations")
    s.add_argument("--estimate", action="store_true", help="print the expected duration and run nothing")
    s.add_argument("--include-tolerant", action="store_true",
                   help="run timing_sensitive tests on timing-tolerant machines too")
    s.add_argument("--no-video", action="store_true", help="leave the capture device closed")
    s.add_argument("--no-xcheck", action="store_true", help="record video-vs-DMA disagreement, never fail on it")
    s.add_argument("--golden-dir", help="where the screen goldens live (default: tests/golden)")
    _firmware_args(s)
    s.add_argument("pytest_args", nargs="*", help="after --: arguments for pytest")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("exec", help="run a script on an open station, with a test's logging and artifacts",
                       description="The script runs as __main__ with the station's objects as globals: "
                                   "session, station, board, link, screen, menu, keys, dbg, kbd, video, firmware, "
                                   "artifacts, run_dir, log. A `result` it leaves is reported (--json). "
                                   "Everything after -- is the script's own argv.")
    _station_args(s)
    _harness_args(s)
    _no_link_arg(s)
    s.add_argument("script", help="the Python file to run")
    s.add_argument("script_args", nargs="*", help="after --: arguments for the script")
    s.set_defaults(func=cmd_exec)

    s = sub.add_parser("shell", help="a Python REPL on an open station",
                       description="The same names a script gets, at a prompt: link.ping(), screen.read(), "
                                   "menu.open('/Games'), keys.press(F1), dbg.letter('t'), kbd.status().")
    _station_args(s, json_flag=False)
    _harness_args(s)
    _no_link_arg(s)
    s.set_defaults(func=cmd_shell, json=False)

    s = sub.add_parser("sweep", help="find a timing knob's margin, one way or both: walk until a probe fails, bisect, "
                                     "restore",
                       description="A build with Dbg_SerTimChg. The knob (p, tp or nS_PLAprop) walks from its default "
                                   "toward --to (default: its upper limit) in --step steps until a point fails, then "
                                   "the edge is bisected to --fine; --both does that each way from one probe of the "
                                   "default, to the knob's limits; --scan LO:HI probes every step of a range instead. "
                                   "--passes repeats it, alternating the order, and a value is clean only if clean in "
                                   "every pass. The limits (treadmill/knobs.py) follow the other knobs: nS_PLAprop "
                                   "goes no lower than nS_RWnReady, and nS_DMASetup moves with nS_DMADataHold and "
                                   "nS_DMADataSetup the other way, so the release and the latch stay put (--free "
                                   "drops that). The defaults are restored whatever happens. The idread probe, a "
                                   "cartridge, counts wrong reads of its ROM and of its IO2 page apart on the C64 (a "
                                   "TR+, by DMA); the menu probe resets and compares the menu with its golden (any "
                                   "TR, by video; not for a knob that times DMA). margins.json goes in the run folder.")
    _station_args(s)
    _harness_args(s)
    s.add_argument("knob", help="the knob: its letter (p), the command (tp) or the variable (nS_PLAprop)")
    s.add_argument("--probe", choices=["idread", "menu", "zpattern", "reuloop", "reuloop-sprites", "cpuwatch",
                                       "noise", "vicrom", "krread"], action="append",
                   help="idread (default), menu, zpattern (z###, needs Dbg_SerDMA), reuloop or reuloop-sprites (the "
                        "C64's REU loop; Special IO is set to the REU for the sweep and put back), cpuwatch (the "
                        "C64's writes beside z###), noise (screen noise beside the first of the others), vicrom "
                        "(an Ultimax cartridge's glyphs fetched in VIC cycles, by video), krread (Kernal Replace's "
                        "HIRAM sense: the probe KERNAL, Special IO and KSenseMode are set for the sweep and put "
                        "back); repeat it for several probes at each point")
    s.add_argument("--z-pages", type=int, default=100, help="zpattern and cpuwatch: z### pages a sample (default 100)")
    s.add_argument("--image", choices=["main", "minimal"], default="main",
                   help="minimal: sweep MinimalBoot's knobs (m r p s h v): idread with a bank-64 chip, which the "
                        "main image hands to MinimalBoot, its counters read by video")
    s.add_argument("--usb-load", action="store_true",
                   help="idread: version requests as fast as the board answers while it counts (nS_MaxAdj's "
                        "upper edge needs interrupts that arrive late)")
    s.add_argument("--golden-dir", help="menu probe: where the screen goldens live (default: tests/golden)")
    s.add_argument("--from", dest="start", type=int, help="the first value; default: the knob's default")
    s.add_argument("--to", type=int, help="one edge: the last value to try; default: the knob's upper limit")
    s.add_argument("--both", action="store_true", help="both edges, each to the knob's limit")
    s.add_argument("--scan", metavar="LO:HI", help="every --step from LO to HI, no bisection; takes in the default")
    s.add_argument("--grid", metavar="LO:HI",
                   help="nS_DMASetup only, with --scan for its rows: nS_DMADataHold from LO to HI as the columns, "
                        "the latch held; a map, not edges")
    s.add_argument("--passes", type=int, default=1, help="repeat the search or scan, alternating order (default 1)")
    hold = s.add_mutually_exclusive_group()
    hold.add_argument("--hold", dest="hold", action="store_const", const=True, default=None,
                      help="nS_DMASetup: hold the release and latch sums (the default for it)")
    hold.add_argument("--free", dest="hold", action="store_const", const=False,
                      help="nS_DMASetup: move it alone")
    s.add_argument("--step", type=int, default=10, help="the coarse step (default 10)")
    s.add_argument("--fine", type=int, default=1, help="bisect until clean and failing are this close (default 1)")
    s.add_argument("--repeat", type=int, default=1, help="samples a point, each probe (default 1)")
    s.add_argument("--dwell", type=float, default=1.0,
                   help="idread, reuloop, vicrom, krread: seconds of counting per sample (default 1)")
    s.set_defaults(func=cmd_sweep)

    s = sub.add_parser("margins", help="the margin records sweeps left: a table, CSV, the map, floors for a station",
                       description="Reads every margins.json under --reports. table: one line a record, the clean "
                                   "window and where each side fails; csv: the same as CSV; map: an HTML chart per "
                                   "knob, a row per record, its clean window as a bar and every value probed "
                                   "beneath it (--mermaid: a gantt block per knob for a PR instead); suggest: the "
                                   "[margins] floors a station's last --last records of each knob support, one "
                                   "coarse step inside the worst edge each way, never past the default, to copy "
                                   "into its file; import RUN...: records from runs of "
                                   "scripts/2026-11-12-dmasetup-sweep.py, written beside each run.")
    s.add_argument("what", choices=["table", "csv", "map", "suggest", "import"])
    s.add_argument("runs", nargs="*", help="import: the run folders")
    s.add_argument("--reports", default="reports", help="where run folders are (default: reports/)")
    s.add_argument("--station", help="only this station's records (suggest needs it)")
    s.add_argument("--knob", help="only this knob's records")
    s.add_argument("--last", type=int, default=5, help="suggest: the records per knob it weighs (default 5)")
    s.add_argument("--mark", action="append", metavar="KNOB=VALUE",
                   help="map: a value under review, drawn across the knob's chart (repeatable)")
    s.add_argument("--mermaid", action="store_true", help="map: print Mermaid gantt blocks instead of the HTML")
    s.add_argument("--out", help="map: the HTML file (default: <reports>/margin-map.html)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_margins)

    s = sub.add_parser("mfg", help="the manufacturing test of new TR+ boards, one after another",
                       description="Waits for a TeensyROM on USB, then checks its firmware against the factory "
                                   "hex, runs the External Ports test (the operator presses the buttons and "
                                   "answers for the LEDs), an external reset, the Expansion Port test, the SD "
                                   "card's root and the REU checker, halting at the first failure with the C64's "
                                   "screen marked. Each board gets a row in the profile's CSV log and a folder "
                                   "under the run; then it waits for the board to leave and starts again. The "
                                   "station's rig and machine are used, not its board's chip id.")
    s.add_argument("--station", help="the station whose rig and machine run the test")
    s.add_argument("--profile", help="the manufacturing profile (default: data/mfg/trplus.toml)")
    s.add_argument("--reports", default="reports", help="where run folders go (default: reports/)")
    s.add_argument("--boards", type=int, metavar="N", help="stop after N boards (default: until Ctrl+C)")
    s.add_argument("--snapshot-sd", metavar="FOLDER",
                   help="write the SD golden from the master card image's folder, then exit")
    s.add_argument("--out", help="with --snapshot-sd: where to write it (default: the profile's sd_golden)")
    s.set_defaults(func=cmd_mfg, json=False)

    s = sub.add_parser("stations", help="list the configured stations and their capabilities")
    s.add_argument("--station", help="only this station")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_stations)

    s = sub.add_parser("calibrate", help="find the text area in the picture and save the calibration")
    _station_args(s, required=False)
    s.add_argument("--frame", help="calibrate from this PNG instead of the live picture")
    s.add_argument("--layout", choices=["menu", "basic"], default="menu",
                   help="what the frame shows: the TeensyROM menu (row 0 is a bar) or a BASIC-like screen")
    s.add_argument("--no-save", action="store_true", help="report only; do not write data/calib")
    s.set_defaults(func=cmd_calibrate)

    s = sub.add_parser("harvest", help="build the glyph bank from the allcodes program on the live station")
    _station_args(s)
    s.add_argument("--charset", choices=["lu", "ug", "both"], default="both")
    s.add_argument("--frames", type=int, default=20, help="frames averaged per character set")
    s.add_argument("--settle", type=float, default=2.5, help="seconds after launch before the first frame")
    s.add_argument("--drive", default="sd", help="where the program is pushed: sd or usb")
    s.add_argument("--folder", default="/treadmill", help="folder on that drive for the program")
    s.add_argument("--fresh", action="store_true", help="start a new bank instead of refining the stored one")
    s.add_argument("--color", type=int, default=15, choices=range(16), metavar="0-15",
                   help="C64 color of the harvested text (15 light grey); harvest each color the tests will read")
    s.add_argument("--out", help="write the bank here instead of data/glyphs/<capture class>__<machine>.npz")
    s.set_defaults(func=cmd_harvest)

    s = sub.add_parser("screen", help="print the screen as text")
    _station_args(s)
    s.add_argument("--exact", action="store_true", help="prefer DMA where the board allows it")
    s.add_argument("--xcheck", action="store_true", help="read both channels and report their disagreement")
    s.add_argument("--menu", action="store_true", help="bring the board to the menu first")
    s.set_defaults(func=cmd_screen)

    s = sub.add_parser("frame", help="save one frame as PNG")
    _station_args(s)
    s.add_argument("--out", required=True, help="the PNG to write")
    s.add_argument("--stable", action="store_true", help="wait for two frames that agree")
    s.add_argument("--raw", action="store_true", help="skip calibration (no normalized image)")
    s.set_defaults(func=cmd_frame)

    s = sub.add_parser("score", help="score a saved frame against the DMA read taken with it")
    s.add_argument("--frame", required=True, help="the frame PNG")
    s.add_argument("--dma", required=True, help="the 1000 screen codes the DMA read gave, as .npy")
    s.add_argument("--charset", required=True, choices=["lower_upper", "upper_graphics"])
    s.add_argument("--station", help="the station whose glyph bank and calibration to use")
    s.add_argument("--bank", help="a glyph bank .npz instead of the station's")
    s.add_argument("--calib", help="a calibration .json instead of the station's")
    s.add_argument("--layout", choices=["menu", "basic"], default="menu")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_score)

    s = sub.add_parser("viewer", help="live picture with the cell grid and decoded text (q quits)")
    _station_args(s)
    s.set_defaults(func=cmd_viewer)
    return p


def _fail(e: BaseException, as_json: bool, code: int) -> int:
    print(json.dumps({"error": str(e)}) if as_json else f"error: {e}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = getattr(args, "json", False)
    try:
        return args.func(args)
    except TreadmillError as e:
        return _fail(e, as_json, e.code)      # each error says what it stands for: the request, the station, a failure
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
