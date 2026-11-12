"""The manufacturing test of new TR+ boards: one board after another on one rig.

``treadmill mfg --station <id>`` waits for a TeensyROM to appear on USB (a TR+
takes its power from the machine, so a board on USB is a machine switched on)
and runs the checks a board gets before it ships, in order:

1. power-up: the main image boots to the menu;
2. firmware: version and build stamp equal the factory hex's, no tag, not dirty;
3. External Ports: the built-in test passes, its clock line on serial and on
   screen agree and are within a tolerance of UTC (which proves the time
   fetch over Ethernet and the RTC), and the operator sees both LEDs on;
4. external reset: the TR+ senses a reset it did not make and returns to the menu;
5. Expansion Port: the built-in test passes three times;
6. SD root: the card's root matches a snapshot of the master image;
7. REU: hot key 2 launches the REU checker with a 512K REU and it passes.

The first failure halts the board where it is. Its screen is marked (red,
inverted, the reason on the bottom row) and the C64 is paused so nothing
redraws it; a pass gets a green summary screen instead. Each board gets a row
in a CSV log and a folder of evidence, then the operator powers off and the
next board starts.

The station supplies the rig (capture, calibration, glyph bank) and the
machine; its own board's chip id is not used: each board is found by the
serial string it enumerates with. The operator's hands go through ``Bench``,
so the self-tests run the whole sequence against fake boards.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, TypeVar

from treadmill.board import Board
from treadmill.c64.keys import RETURN, KernalKeys
from treadmill.c64.menu import parse_listing
from treadmill.c64.screen import (
    CHARSET_LOWER_UPPER, CHARSET_UPPER_GRAPHICS, COLOR_RAM, REVERSE_VIDEO, SCREEN_BYTES, SCREEN_COLUMNS,
    SCREEN_ROWS, VIC_BORDER, codes_for_text,
)
from treadmill.errors import EXIT_USAGE, TreadmillError
from treadmill.firmware.hexinfo import HexError, HexInfo, inspect_hex
from treadmill.link import LinkError, NotFound, TransportError, list_teensyrom_ports, wait_for_teensyrom
from treadmill.link.banner import TR_PLUS, Banner
from treadmill.observe.calibrate import Calibration, calibration_path
from treadmill.observe.capture import FrameSource, save_strip
from treadmill.observe.channels import ScreenChannel, ScreenRead
from treadmill.observe.glyphs import GlyphBank
from treadmill.observe.golden import Golden, compare, diff_text, save_golden
from treadmill.report.artifacts import RunDir, write_board_artifacts
from treadmill.station.config import ConfigError, data_dir
from treadmill.station.model import Station

T = TypeVar("T")

# -- the board's programs, as the firmware names and prints them ------------------------

EPC_PATH = "/TeensyROM Specific/TeensyROM External Ports Test"     # MainMenuItems.h, drive Teensy
XPT_PATH = "/TeensyROM Specific/TR+ C64 Expansion Port Test"
REU_NAME = "REU-Checker v1.0"                                      # hot key 2 at the EEPROM defaults on a TR+
KEY_O, KEY_2 = 0x4F, 0x32                                          # PETSCII as GETIN returns them

EPC_PASSED = "External Port Check Passed."
EPC_FAILED = "External Port Check Failed!"
# What TRExtPortCheck prints before its fail line, first to last: the reason a board is stopped.
EPC_REASONS = ("Could not init Ethernet", "Could not init SD", "Couldn't open SD file", "No SD filename",
               "Could not init USB", "Couldn't open USB file", "No USB filename")
XPT_PASSED = "Expansion Port Test Passed."
XPT_FAILED = "Expansion Port Test Failed!"
XPT_PROMPT = "o:Once"
XPT_FAULTS = ("Fail", "Miscompare", "not detected", "No read", "No write", "no clock", "timed out")
PATTERNS_PER_PASS = 5                                              # ExpPortDMA's bit-transition patterns

# 12-hour BCD as ExtPortCheck formats it: hour 00 is twelve o'clock, the pm flag is the hour's bit 7.
HELLO = re.compile(r"Hello from TR at: (\d\d):(\d\d):(\d\d) ([ap])m", re.I)
# PrintDMAPatternResult: a clean pattern's line ends "  CLEAN", a bad one's ends at its counts. The
# patterns are anchored on their own text, not a line start: the serial record holds every byte the
# board sent, so a line can follow a command's reply bytes directly.
PATTERN_LINE = re.compile(r"(\$\w\w(?:/\$\w\w alt)? over \$\w\w @ \$\w+: \d+ bad of \d+ bytes"
                          r"(?:, pre-fill \d+ bad)?)(  CLEAN)?[ \t\r]*$", re.M)
PATTERN_STALL = re.compile(r"\b(no \S.* on pass \d+:[^\n]*)")
LAUNCHED = re.compile(r"^F: (.+?)\s*$", re.M)
REU_HANDLER = re.compile(r"Loading IO handler: (\d+)KB REU")
REU_SIZE = re.compile(r"REU Size: (\d+)K bytes")
EEPROM_DEFAULTS = "--> Setting EEPROM to defaults"

# -- timing --------------------------------------------------------------------------------

SERIAL_POLL_S = 0.25
VIDEO_POLL_S = 0.1
VERDICT_S = 20.0          # from the test's own end signal to its verdict on screen
RESET_FOLLOW_S = 10.0     # 'Button detected' to 'Resetting C64'
REU_LAUNCH_S = 20.0
KEY_SETTLE_S = 0.5        # after the prompt shows: the program drains the key buffer right after printing it
# ExtPortCheck still holds loop() after its Hello line, until the C64 takes its last message (3 s at most).
EPC_SETTLE_S = 1.0
NO_VIDEO_SETTLE_S = 5.0   # an Expansion Port pass's C64 half, waited out blind when there is no video
REU_QUIET_S = 5.0         # with no golden yet: the checker's screen unchanged this long counts as its last
VIDEO_SAME_READS = 2

# -- the steps and the log -------------------------------------------------------------------

STEPS = ("power_up", "firmware", "external_ports", "external_reset", "expansion_port", "sd_root", "reu")
TITLES = {"power_up": "Power-up", "firmware": "Firmware", "external_ports": "External Ports",
          "external_reset": "External reset", "expansion_port": "Expansion Port", "sd_root": "SD root",
          "reu": "REU"}
PASS, FAIL, NOT_RUN, ABORTED = "PASS", "FAIL", "NOT RUN", "ABORTED"
CSV_FIELDS = ("start", "duration_s", "chip_id", "fw_version", "fw_built", "result", "failed_step", "reason",
              *STEPS, "led_ethernet", "led_tr", "epc_time_delta_s", "c64_marked", "evidence")

# C64 colors for the result screens.
BLACK, WHITE, RED, GREEN, YELLOW = 0, 1, 2, 5, 7


class MfgError(TreadmillError):
    """The session cannot start: a profile, a golden or the capture is missing."""
    code = EXIT_USAGE


class StepFailed(TreadmillError):
    """A check did not pass. ``markable`` is False when the board cannot take
    the DMA writes that mark its screen: it left USB, or it is inside a test
    that services nothing until a button is pressed."""

    def __init__(self, reason: str, markable: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.markable = markable


# -- the profile -----------------------------------------------------------------------------

@dataclass(frozen=True)
class MfgProfile:
    """``data/mfg/trplus.toml``: the standard a board is held to and the
    patience each step gets. Paths are absolute or relative to the profile."""
    factory_hex: Path
    sd_golden: Path
    reu_golden: Path
    log: Path
    sd_ignore: tuple[str, ...] = ("System Volume Information",)
    reu_kb: int = 512
    xpt_passes: int = 3
    time_tolerance_s: float = 120.0
    epc_timeout_s: float = 180.0
    boot_timeout_s: float = 60.0
    reset_timeout_s: float = 60.0
    xpt_pass_timeout_s: float = 90.0
    reu_timeout_s: float = 120.0
    require_release: bool = True

    PATHS = ("factory_hex", "sd_golden", "reu_golden", "log")

    @classmethod
    def load(cls, path: Path | str) -> "MfgProfile":
        path = Path(path)
        try:
            table = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"no manufacturing profile at {path}; copy data/mfg/trplus.example.toml") from None
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from None
        names = {f.name for f in fields(cls)}
        unknown = sorted(set(table) - names)
        if unknown:
            raise ConfigError(f"{path}: unknown key(s) {', '.join(unknown)}; known: {', '.join(sorted(names))}")
        defaults = {"sd_golden": "sd_root.json", "reu_golden": "reu_checker.pass.screen.txt", "log": "boards.csv"}
        values = {**defaults, **table}
        if "factory_hex" not in values:
            raise ConfigError(f"{path}: needs factory_hex, the hex every board must run")
        for key in cls.PATHS:
            p = Path(values[key])
            values[key] = p if p.is_absolute() else path.parent / p
        if "sd_ignore" in values:
            values["sd_ignore"] = tuple(values["sd_ignore"])
        if int(values.get("xpt_passes", 3)) < 1:
            raise ConfigError(f"{path}: xpt_passes must be at least 1")
        return cls(**values)


def default_profile_path() -> Path:
    return data_dir() / "mfg" / "trplus.toml"


def expected_firmware(profile: MfgProfile) -> HexInfo:
    """What every board must run, read from the factory hex."""
    try:
        info = inspect_hex(profile.factory_hex)
    except (OSError, HexError) as e:
        raise MfgError(f"factory hex {profile.factory_hex}: {e}") from None
    if info.version is None or info.stamp is None:
        raise MfgError(f"factory hex {profile.factory_hex}: no single version string and build stamp in it")
    return info


# -- the operator's terminal ---------------------------------------------------------------------

_LETTERS = {
    "P": ["#### ", "#   #", "#### ", "#    ", "#    "],
    "A": [" ### ", "#   #", "#####", "#   #", "#   #"],
    "S": [" ####", "#    ", " ### ", "    #", "#### "],
    "F": ["#####", "#    ", "#### ", "#    ", "#    "],
    "I": ["#####", "  #  ", "  #  ", "  #  ", "#####"],
    "L": ["#    ", "#    ", "#    ", "#    ", "#####"],
}


def big_word(word: str) -> list[str]:
    return ["  ".join(_LETTERS[ch][r] for ch in word) for r in range(5)]


def _color_terminal(out) -> bool:
    """ANSI colors when ``out`` is a terminal that shows them; on Windows the
    console is asked to interpret them first."""
    if not getattr(out, "isatty", lambda: False)():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


class Console:
    """What the operator reads and answers. ``ask`` is ``input`` at the bench
    and a script in the self-tests."""

    def __init__(self, out=None, ask: Callable[[str], str] | None = None, color: bool | None = None):
        self.out = out or sys.stdout
        self._ask = ask or (lambda prompt: input(prompt))
        self.color = _color_terminal(self.out) if color is None else color

    def _paint(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def heading(self, text: str) -> None:
        self.say()
        self.say(self._paint(f"=== {text} ===", "1"))

    def instruct(self, text: str) -> None:
        self.say(self._paint(f">>> {text}", "1;33"))

    def warn(self, text: str) -> None:
        self.say(self._paint(f"!!! {text}", "1;31"))

    def step_start(self, number: int, name: str) -> None:
        self.say(self._paint(f"  [{number}] {TITLES[name]} ...", "90"))

    def step(self, number: int, name: str, status: str, detail: str = "") -> None:
        color = {PASS: "32", FAIL: "1;31", NOT_RUN: "90"}.get(status, "0")
        tail = f"  {detail}" if detail else ""
        self.say(f"  [{number}] {TITLES[name]:<15} {self._paint(status, color)}{tail}")

    def ask_yes_no(self, question: str) -> bool:
        while True:
            answer = self._ask(self._paint(f"??? {question} [y/n] ", "1;36")).strip().lower()
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False

    def verdict(self, record: "BoardRecord") -> None:
        color = "1;32" if record.result == PASS else "1;31"
        self.say()
        if record.result in (PASS, FAIL):
            for line in big_word(record.result):
                self.say(self._paint("    " + line, color))
        else:
            self.say(self._paint(f"    {record.result}", color))
        self.say(f"    chip id {record.chip_id}, {record.duration_s:.0f} s")
        if record.result != PASS:
            where = TITLES.get(record.failed_step, record.failed_step)
            self.say(self._paint(f"    {where}: {record.reason}", color))
            if record.c64_marked is False:
                self.say("    (the C64's screen could not be marked)")
            self.out.write("\a")
            self.out.flush()
        self.say()


# -- the bench: boards appearing, and the operator's hands ------------------------------------------

CONNECT, BUTTONS, RESET_BUTTON, POWER_OFF = "connect", "buttons", "reset", "power_off"
INSTRUCTIONS = {
    CONNECT: "Plug in the next TR+. Connect USB to this PC, Ethernet, the USB thumb drive and the "
             "SD card, then power on the C64.",
    BUTTONS: "Follow the C64's screen: press and release the TR's MENU button, then its ALT button.",
    RESET_BUTTON: "Press the C64's reset button.",
    POWER_OFF: "Power off the C64 and remove the board.",
}


class Bench(Protocol):
    def do(self, action: str) -> None: ...
    def wait_for_board(self) -> tuple[str, str]: ...
    def open_board(self, station: Station, timeout: float) -> Board: ...
    def gone(self, port: str) -> bool: ...


class RealBench:
    """A person at the bench: actions are instructions on the console; boards
    are found by the serial string they enumerate with."""

    def __init__(self, console: Console):
        self.console = console

    def do(self, action: str) -> None:
        self.console.instruct(INSTRUCTIONS[action])

    def wait_for_board(self) -> tuple[str, str]:
        found = wait_for_teensyrom()
        return found.port, found.uid

    def open_board(self, station: Station, timeout: float) -> Board:
        """The board, opened once its main image answers. A port that drops
        right after it appears (MinimalBoot handing over) is opened again."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return Board.open(station)
            except (TransportError, NotFound, LinkError) as e:
                if time.monotonic() >= deadline:
                    raise StepFailed(f"the board on {station.tr.port_hint} did not answer within {timeout:g} s: {e}",
                                     markable=False) from None
                time.sleep(1.0)

    def gone(self, port: str) -> bool:
        return port not in {p for p, _ in list_teensyrom_ports()}


class ExternalReset(Protocol):
    def apply(self) -> None: ...


class OperatorReset:
    """The external reset until the keyboard board has a reset command: the
    operator presses the C64's reset button."""

    def __init__(self, bench: Bench):
        self.bench = bench

    def apply(self) -> None:
        self.bench.do(RESET_BUTTON)


# -- video ---------------------------------------------------------------------------------------------

@dataclass
class VideoKit:
    """The rig's capture with the calibration and glyph bank that read it,
    open for the whole session. The stored calibration is checked against
    the first board's menu, and remade from it when it does not fit."""
    source: FrameSource | None
    calibration: Calibration | None
    bank: GlyphBank | None
    fitted: bool = False

    @property
    def ok(self) -> bool:
        return self.source is not None and self.calibration is not None and self.bank is not None

    def channel(self, board: Board) -> ScreenChannel:
        return ScreenChannel(board.link, self.source if self.ok else None, self.calibration, self.bank,
                             dma_allowed=board.dma_allowed)

    def fit(self, board: Board, log) -> None:
        if self.fitted or self.source is None or self.bank is None:
            return
        from treadmill.session import load_or_calibrate
        self.calibration = load_or_calibrate(board, self.source, self.bank, log=log)
        self.fitted = True

    def close(self) -> None:
        if self.source is not None:
            self.source.close()


def open_video_kit(station: Station, log=print) -> VideoKit:
    """The capture, bank and stored calibration of the station's rig. The
    REU checker's verdict is only on video, so a session without them does
    not start."""
    from treadmill.session import load_bank, open_video
    source = open_video(station, log)
    if source is None:
        raise MfgError(f"the manufacturing test needs the capture of rig {station.rig.id}, and it did not open")
    bank = load_bank(station, log=log)
    if bank is None:
        source.close()
        raise MfgError(f"no glyph bank for station {station.id}; run treadmill harvest --station {station.id}")
    path = calibration_path(data_dir(), station.rig.capture.capture_class, station.machine.id)
    calibration = Calibration.load(path) if path.is_file() else None
    return VideoKit(source, calibration, bank)


# -- one board's record ------------------------------------------------------------------------------

@dataclass
class StepResult:
    status: str = NOT_RUN
    detail: str = ""
    seconds: float = 0.0
    problems: list[str] = field(default_factory=list)


@dataclass
class BoardRecord:
    n: int
    port: str
    chip_id: str
    start: datetime
    steps: dict[str, StepResult] = field(default_factory=lambda: {s: StepResult() for s in STEPS})
    result: str = ""
    failed_step: str = ""
    reason: str = ""
    duration_s: float = 0.0
    fw_version: str = ""
    fw_built: str = ""
    led_ethernet: str = ""
    led_tr: str = ""
    epc_time_delta_s: float | None = None
    c64_marked: bool | None = None
    fresh_eeprom: bool | None = None
    evidence: str = ""

    def csv_row(self) -> dict[str, object]:
        row: dict[str, object] = {
            "start": self.start.strftime("%Y-%m-%d %H:%M:%S"), "duration_s": f"{self.duration_s:.1f}",
            "chip_id": self.chip_id, "fw_version": self.fw_version, "fw_built": self.fw_built,
            "result": self.result, "failed_step": TITLES.get(self.failed_step, self.failed_step),
            "reason": self.reason, "led_ethernet": self.led_ethernet, "led_tr": self.led_tr,
            "epc_time_delta_s": "" if self.epc_time_delta_s is None else f"{self.epc_time_delta_s:.1f}",
            "c64_marked": "" if self.c64_marked is None else ("yes" if self.c64_marked else "no"),
            "evidence": self.evidence,
        }
        row.update({s: r.status for s, r in self.steps.items()})
        return row

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat(timespec="seconds")
        return d


class BoardLog:
    """The CSV every board of every session lands in. Excel locks a CSV it
    has open; a row it refuses goes to a file of this session's own beside it."""

    def __init__(self, path: Path, session_stamp: str, console: Console):
        self.path = Path(path)
        self.fallback = self.path.with_name(f"{self.path.stem}-{session_stamp}{self.path.suffix}")
        self.console = console

    def append(self, record: BoardRecord) -> Path:
        row = record.csv_row()
        try:
            self._write(self.path, row)
            return self.path
        except OSError as e:
            self.console.warn(f"{self.path.name} could not be written ({e.strerror or e}); "
                              f"this board's row went to {self.fallback.name}")
        self._write(self.fallback, row)
        return self.fallback

    @staticmethod
    def _write(path: Path, row: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists() or path.stat().st_size == 0
        # A byte-order mark makes Excel read the file as UTF-8; only a new file starts with one.
        with path.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            writer = csv.DictWriter(f, CSV_FIELDS)
            if new:
                writer.writeheader()
            writer.writerow(row)


# -- checks that need no board -------------------------------------------------------------------------

def firmware_problems(image: str | None, banner: Banner | None, expected: HexInfo,
                      require_release: bool = True) -> list[str]:
    """Why the firmware a board runs is not the factory firmware."""
    if banner is None:
        return ["no version banner"]
    out = []
    if image != "main":
        out.append(f"the {image or 'silent'} image answered, not the main image")
    if banner.kind != TR_PLUS:
        out.append(f"{banner.version or 'the board'} is not a TR+")
    if banner.version != expected.version:
        out.append(f"version {banner.version}, expected {expected.version}")
    if banner.built != expected.stamp:
        out.append(f"built {banner.built}, expected {expected.stamp}")
    if require_release and banner.tag:
        out.append(f"a tagged build (-{banner.tag})")
    if require_release and banner.dirty:
        out.append("a build from a tree with uncommitted changes (*)")
    return out


def clock_text(m: re.Match) -> str:
    return f"{m.group(1)}:{m.group(2)}:{m.group(3)} {m.group(4).lower()}m"


def clock_delta(m: re.Match, now: datetime) -> float:
    """Seconds from ``now`` (UTC) to the time a Hello line shows, wrapped to
    half a day either way. The line is UTC on a board at its default
    timezone setting."""
    hour = int(m.group(1)) % 12 + (12 if m.group(4).lower() == "p" else 0)
    shown = hour * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    now = now.astimezone(timezone.utc)
    actual = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    return (shown - actual + 43200) % 86400 - 43200


def epc_reason(text: str) -> str:
    for reason in EPC_REASONS:
        if reason in text:
            return reason
    return EPC_FAILED


def xpt_serial(text: str) -> tuple[list[tuple[str, str]], str | None]:
    """The bit-transition lines a pass sent, (line, verdict), and the stall
    line when one stage got no clock."""
    lines = [(m.group(1).strip(), "CLEAN" if m.group(2) else "BAD") for m in PATTERN_LINE.finditer(text)]
    stall = PATTERN_STALL.search(text)
    return lines, stall.group(1).strip() if stall else None


def xpt_reason(rows: list[str]) -> str:
    for row in rows:
        stripped = row.strip()
        if XPT_FAILED in stripped or XPT_PASSED in stripped:
            continue
        if any(f in stripped for f in XPT_FAULTS):
            return stripped
    return XPT_FAILED


def sd_snapshot(folder: Path | str) -> dict:
    """A folder's root as the board lists a card's: directories by name,
    files by name and size, dotfiles left out as the firmware leaves them out."""
    folder = Path(folder)
    if not folder.is_dir():
        raise MfgError(f"{folder} is not a folder")
    entries = []
    for entry in sorted(os.scandir(folder), key=lambda e: e.name.lower()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            entries.append({"type": "dir", "name": entry.name})
        else:
            entries.append({"type": "file", "name": entry.name, "size": entry.stat().st_size})
    return {"source": str(folder), "made": datetime.now().isoformat(timespec="seconds"), "entries": entries}


def compare_sd(listing: list[dict], golden: list[dict], ignore: tuple[str, ...] = ()) -> list[str]:
    """Every way a card's root listing differs from the golden one."""
    have = {e["name"]: e for e in listing if e.get("name") not in ignore}
    want = {e["name"]: e for e in golden if e.get("name") not in ignore}
    problems = [f"missing {name}" for name in sorted(set(want) - set(have), key=str.lower)]
    problems += [f"extra {name}" for name in sorted(set(have) - set(want), key=str.lower)]
    for name in sorted(set(have) & set(want), key=str.lower):
        h, w = have[name], want[name]
        if h.get("type") != w.get("type"):
            problems.append(f"{name} is a {h.get('type')}, expected a {w.get('type')}")
        elif w.get("type") == "file" and h.get("size") != w.get("size"):
            problems.append(f"{name} is {h.get('size')} bytes, expected {w.get('size')}")
    return problems


def summarize(problems: list[str], keep: int = 3) -> str:
    head = "; ".join(problems[:keep])
    return head + (f" (and {len(problems) - keep} more)" if len(problems) > keep else "")


def screen_codes(text: str, charset: str) -> bytes:
    """One 40-column row as screen codes; capitals only in the upper-case set."""
    if charset != CHARSET_LOWER_UPPER:
        text = text.upper()
    return codes_for_text(text.ljust(SCREEN_COLUMNS)[:SCREEN_COLUMNS], charset)


# -- one board -------------------------------------------------------------------------------------------

@dataclass
class Ctx:
    """What the steps of one board share."""
    record: BoardRecord
    station: Station
    profile: MfgProfile
    expected: HexInfo
    bench: Bench
    reset: ExternalReset
    console: Console
    video: VideoKit
    folder: Path
    log: Callable[[str], None]
    board: Board | None = None

    @property
    def link(self):
        return self.board.link

    @property
    def screen(self) -> ScreenChannel:
        return self.video.channel(self.board)

    @property
    def keys(self) -> KernalKeys:
        return KernalKeys(self.board)

    def mark(self) -> int:
        return len(self.board.link.stream.tap.chunks)

    def serial_since(self, mark: int) -> str:
        return b"".join(c for _, c in self.board.link.stream.tap.chunks[mark:]).decode("latin-1")

    def wait_serial(self, mark: int, found: Callable[[str], T], timeout: float, what: str) -> T:
        """Until ``found`` gives something truthy for what the board said since ``mark``."""
        deadline = time.monotonic() + timeout
        while True:
            hit = found(self.serial_since(mark))
            if hit:
                return hit
            if self.board.link.stream.ended:
                raise StepFailed(f"the board left USB while waiting for {what}", markable=False)
            if time.monotonic() >= deadline:
                raise StepFailed(f"no {what} within {timeout:g} s")
            time.sleep(SERIAL_POLL_S)

    def video_until(self, predicate: Callable[[list[str]], bool], timeout: float) -> ScreenRead | None:
        """The first of two video reads in a row that satisfy ``predicate``,
        or None when ``timeout`` passes first: a wait that sends nothing to
        the board."""
        screen = self.screen
        deadline = time.monotonic() + timeout
        run = 0
        while time.monotonic() < deadline:
            read = screen.read_video()
            run = run + 1 if predicate(read.rows()) else 0
            if run >= VIDEO_SAME_READS:
                return read
            time.sleep(VIDEO_POLL_S)
        return None

    def video_settled(self, quiet: float, timeout: float) -> ScreenRead | None:
        """The screen once video has read it unchanged for ``quiet`` seconds,
        or None when ``timeout`` passes first."""
        screen = self.screen
        deadline = time.monotonic() + timeout
        last, since = None, time.monotonic()
        while time.monotonic() < deadline:
            read = screen.read_video()
            if last is None or read.rows() != last.rows():
                last, since = read, time.monotonic()
            elif time.monotonic() - since >= quiet:
                return read
            time.sleep(VIDEO_POLL_S)
        return None

    def save_screen(self, name: str, read: ScreenRead) -> None:
        save_golden(read.rows(), self.folder / f"{name}.screen.txt", (f"% {read.source} read",))


def step_power_up(c: Ctx) -> str:
    c.board = c.bench.open_board(c.station, c.profile.boot_timeout_s)
    try:
        c.board.wait_for_menu(c.profile.boot_timeout_s)
    except (LinkError, TransportError, NotFound) as e:
        raise StepFailed(f"the menu did not come up: {e}", markable=False) from None
    c.board.probe()          # what answered when the port first opened may have been MinimalBoot: no DMA
    c.video.fit(c.board, c.log)
    c.record.fresh_eeprom = EEPROM_DEFAULTS in c.serial_since(0)
    return "menu up" + (", settings at defaults" if c.record.fresh_eeprom else "")


def step_firmware(c: Ctx) -> str:
    c.board.probe()
    b = c.board.banner
    if b is not None:
        c.record.fw_version, c.record.fw_built = b.version or "", b.built or ""
    problems = firmware_problems(c.board.image, b, c.expected, c.profile.require_release)
    if problems:
        raise StepFailed("; ".join(problems))
    return f"{b.version}, built {b.built}"


def step_external_ports(c: Ctx) -> str:
    """The board answers nothing while it waits for the buttons, so the wait
    sends nothing: it watches serial for the line sent after the buttons,
    and video for a fail the test printed before reaching them."""
    mark = c.mark()
    c.link.launch(EPC_PATH, "teensy")
    c.bench.do(BUTTONS)
    deadline = time.monotonic() + c.profile.epc_timeout_s
    hello, failed_reads = None, 0
    while True:
        hello = HELLO.search(c.serial_since(mark))
        if hello:
            break
        if c.link.stream.ended:
            raise StepFailed("the board left USB during the External Ports test", markable=False)
        if c.video.ok:
            failed_reads = failed_reads + 1 if EPC_FAILED in c.screen.read_video().text() else 0
            if failed_reads >= VIDEO_SAME_READS:
                break
        if time.monotonic() >= deadline:
            raise StepFailed(f"no 'Hello from TR at:' line within {c.profile.epc_timeout_s:g} s: a button "
                             "was not detected, or the test stopped before the buttons", markable=False)
        time.sleep(SERIAL_POLL_S)
    now = datetime.now(timezone.utc)
    time.sleep(EPC_SETTLE_S)
    read = c.screen.wait(lambda s: EPC_PASSED in s.text() or EPC_FAILED in s.text(), VERDICT_S, exact=True,
                         what="the External Ports verdict")
    c.save_screen("external_ports", read)
    text = read.text()
    if EPC_PASSED not in text:
        raise StepFailed(epc_reason(text))
    shown = HELLO.search(text)
    if shown is None:
        raise StepFailed("the screen does not show the 'Hello from TR at:' line")
    if clock_text(shown) != clock_text(hello):
        raise StepFailed(f"the screen shows {clock_text(shown)}, serial sent {clock_text(hello)}")
    delta = clock_delta(hello, now)
    c.record.epc_time_delta_s = round(delta, 1)
    if abs(delta) > c.profile.time_tolerance_s:
        raise StepFailed(f"the board's clock is {delta:+.0f} s from UTC: the time fetch over Ethernet, or the RTC")
    for attr, question, what in (("led_ethernet", "Is the Ethernet port's green LED on?", "the Ethernet green LED"),
                                 ("led_tr", "Is the TR LED (by the MENU button) on?", "the TR LED")):
        on = c.console.ask_yes_no(question)
        setattr(c.record, attr, "y" if on else "n")
        if not on:
            raise StepFailed(f"{what} is off (operator)")
    return f"clock {delta:+.0f} s from UTC, LEDs on"


def step_external_reset(c: Ctx) -> str:
    mark = c.mark()
    c.reset.apply()
    c.wait_serial(mark, lambda t: "Button detected" in t, c.profile.reset_timeout_s,
                  "'Button detected' on serial: the TR+ did not sense the reset")
    c.wait_serial(mark, lambda t: "Resetting C64" in t, RESET_FOLLOW_S, "'Resetting C64' after the reset")
    try:
        c.board.wait_for_menu(c.profile.boot_timeout_s)
    except (LinkError, TransportError, NotFound) as e:
        raise StepFailed(f"the menu did not come back: {e}") from None
    c.screen.wait(lambda s: parse_listing(s.rows()) is not None, VERDICT_S, exact=True, what="the menu")
    return "sensed, back at the menu"


def step_expansion_port(c: Ctx) -> str:
    """Nothing is sent while a pass runs, since a DMA read would add bus
    cycles to the test of the bus: serial says when the Teensy's half is
    done, video when the C64's half is, then one DMA read takes the verdict."""
    passes = c.profile.xpt_passes
    mark = c.mark()
    c.link.launch(XPT_PATH, "teensy")
    for n in range(1, passes + 1):
        def pass_over(text: str):
            lines, stall = xpt_serial(text)
            if stall or len(lines) >= PATTERNS_PER_PASS or any(v != "CLEAN" for _, v in lines):
                return lines, stall
            return None
        lines, stall = c.wait_serial(mark, pass_over, c.profile.xpt_pass_timeout_s,
                                     f"pass {n}'s DMA pattern lines on serial")
        if c.video.ok:
            c.video_until(lambda rows: XPT_PROMPT in "\n".join(rows), c.profile.xpt_pass_timeout_s)
        else:
            time.sleep(NO_VIDEO_SETTLE_S)
        read = c.screen.wait(lambda s: XPT_PROMPT in s.text(), VERDICT_S, exact=True, what=f"pass {n}'s verdict")
        c.save_screen(f"expansion_port_{n}", read)
        rows = read.rows()
        if XPT_PASSED not in "\n".join(rows):
            raise StepFailed(f"pass {n}: {xpt_reason(rows)}")
        bad = stall or next((line for line, verdict in lines if verdict != "CLEAN"), None)
        if bad:
            raise StepFailed(f"pass {n}: serial: {bad}")
        mark = c.mark()
        time.sleep(KEY_SETTLE_S)
        c.keys.press([KEY_O] if n < passes else [RETURN])
    try:
        c.board.wait_for_menu(c.profile.boot_timeout_s)
    except (LinkError, TransportError, NotFound) as e:
        raise StepFailed(f"the menu did not come back after the test: {e}") from None
    return f"{passes} passes clean"


def step_sd_root(c: Ctx) -> str:
    try:
        golden = json.loads(c.profile.sd_golden.read_text(encoding="utf-8"))["entries"]
    except (OSError, ValueError, KeyError) as e:
        raise StepFailed(f"no SD golden at {c.profile.sd_golden} ({e}); make it with "
                         "treadmill mfg --snapshot-sd <master image folder>") from None
    listing = c.link.list_directory("/", "sd")
    (c.folder / "sd_root.json").write_text(json.dumps(listing, indent=2), encoding="utf-8")
    problems = compare_sd(listing, golden, c.profile.sd_ignore)
    if problems:
        c.record.steps["sd_root"].problems = problems
        raise StepFailed(summarize(problems))
    return f"{len(listing)} entries match"


def step_reu(c: Ctx) -> str:
    """The checker's verdict is read by video only: the suite sends no DMA
    while the REU is live, since the checker's own transfers use the bus.
    With no golden yet, the checker's settled screen is saved for one to be
    made from, and the step fails."""
    path = c.profile.reu_golden
    golden = Golden.load(path) if path.is_file() else None
    if not c.video.ok:
        raise StepFailed("the REU checker's verdict needs the video channel")
    mark = c.mark()
    c.keys.press([KEY_2])
    c.board.reu_live = True
    name = c.wait_serial(mark, lambda t: (m := LAUNCHED.search(t)) and m.group(1), REU_LAUNCH_S,
                         "launch from hot key 2 on serial")
    if not name.startswith(REU_NAME):
        raise StepFailed(f"hot key 2 launched {name!r}, not the REU checker: the settings are not at their defaults")
    handler = c.wait_serial(mark, lambda t: (m := REU_HANDLER.search(t)) and int(m.group(1)), REU_LAUNCH_S,
                            "REU handler on serial")
    size = c.wait_serial(mark, lambda t: (m := REU_SIZE.search(t)) and int(m.group(1)), REU_LAUNCH_S,
                         "REU size on serial")
    if size != c.profile.reu_kb or handler != c.profile.reu_kb:
        raise StepFailed(f"the REU is {size}K (handler {handler}KB), expected {c.profile.reu_kb}K")
    if golden is None:
        read = c.video_settled(REU_QUIET_S, c.profile.reu_timeout_s)
        if read is not None:
            c.save_screen("reu", read)
        raise StepFailed(f"no REU checker golden at {path}" + (
            f"; this board's screen is in {c.folder.name}/reu.screen.txt: check it, then copy it there"
            if read is not None else "; the checker's screen never settled"))
    read = c.video_until(lambda rows: not compare(rows, golden), c.profile.reu_timeout_s)
    if read is None:
        last = c.screen.read_video()
        c.save_screen("reu", last)
        (c.folder / "reu.diff.txt").write_text(diff_text(last.rows(), golden), encoding="utf-8")
        raise StepFailed(f"the REU checker's screen did not match its pass golden within {c.profile.reu_timeout_s:g} s")
    c.save_screen("reu", read)
    c.board.reset()
    c.board.reu_live = False
    try:
        c.board.wait_for_menu(c.profile.boot_timeout_s)
    except (LinkError, TransportError, NotFound) as e:
        raise StepFailed(f"the menu did not come back after the REU checker: {e}") from None
    return f"{size}K, checker passed"


STEP_FUNCS: dict[str, Callable[[Ctx], str]] = {
    "power_up": step_power_up, "firmware": step_firmware, "external_ports": step_external_ports,
    "external_reset": step_external_reset, "expansion_port": step_expansion_port, "sd_root": step_sd_root,
    "reu": step_reu,
}


# -- the C64's screen at the end -------------------------------------------------------------------------

def pass_rows(record: BoardRecord) -> list[str]:
    rows = ["TR+ manufacturing test: PASS".center(SCREEN_COLUMNS), "",
            f" Chip id   {record.chip_id}", f" Firmware  {record.fw_version}", f"           {record.fw_built}", ""]
    rows += [f" {i}. {TITLES[s]:<16} {record.steps[s].status}" for i, s in enumerate(STEPS, 1)]
    rows += ["", f" Tested    {record.start:%Y-%m-%d %H:%M}", "", " Power off and remove the board."]
    return rows


def write_pass_screen(c: Ctx) -> None:
    """A summary on green, then the CPU paused so the menu cannot redraw it."""
    dump = c.link.read_screen()
    rows = pass_rows(c.record)
    rows += [""] * (SCREEN_ROWS - len(rows))
    c.link.write_memory(dump.address, b"".join(screen_codes(r, dump.charset) for r in rows))
    c.link.write_memory(COLOR_RAM, bytes([WHITE]) * SCREEN_BYTES)
    c.link.write_memory(VIC_BORDER, bytes([GREEN, GREEN]))
    c.link.pause(True)


def mark_fail_screen(c: Ctx, step: str, reason: str) -> None:
    """The failing screen kept: inverted on red, so it reads from across the
    bench and its own reason stays legible, the step and reason on the bottom
    row, then the CPU paused so nothing redraws it."""
    dump = c.link.read_screen()
    charset = dump.charset if dump.charset == CHARSET_LOWER_UPPER else CHARSET_UPPER_GRAPHICS
    codes = bytearray(b ^ REVERSE_VIDEO for b in dump.codes)
    bottom = screen_codes(f"FAIL {STEPS.index(step) + 1} {TITLES[step]}: {reason}", charset)
    last = SCREEN_BYTES - SCREEN_COLUMNS
    codes[last:] = bytes(b | REVERSE_VIDEO for b in bottom)
    colors = bytearray([WHITE]) * SCREEN_BYTES
    colors[last:] = bytes([YELLOW]) * SCREEN_COLUMNS
    c.link.write_memory(dump.address, bytes(codes))
    c.link.write_memory(COLOR_RAM, bytes(colors))
    c.link.write_memory(VIC_BORDER, bytes([RED, RED]))
    c.link.pause(True)


def finish_screen(c: Ctx, failure: tuple[str, StepFailed] | None) -> bool:
    """Mark the C64's screen with the verdict; False when it could not be."""
    board = c.board
    if board is None or board.link.stream.ended:
        return False
    try:
        if failure is None:
            write_pass_screen(c)
            return True
        step, err = failure
        if not err.markable:
            return False
        if board.reu_live:
            # Only once video shows the checker has stopped: its own transfers use the bus.
            if not c.video.ok:
                return False
            c.screen.wait_settled(timeout=10.0)
            board.reu_live = False
        if not board.dma_allowed():
            return False
        mark_fail_screen(c, step, err.reason)
        return True
    except (TreadmillError, OSError) as e:
        c.log(f"[mfg] the C64's screen could not be marked: {e}")
        return False


def save_evidence(c: Ctx) -> None:
    folder = c.folder
    if c.board is not None:
        try:
            write_board_artifacts(folder, c.board, since=0.0, mark=0)
        except (TreadmillError, OSError) as e:
            c.log(f"[mfg] serial log not written: {e}")
    if c.record.result != PASS and c.video.source is not None:
        try:
            save_strip(c.video.source.ring(), folder / "frames.png")
        except Exception as e:      # a picture for the record: never the reason a board's row goes missing
            c.log(f"[mfg] frames not saved: {e}")
    (folder / "result.json").write_text(json.dumps(c.record.to_dict(), indent=2, default=str), encoding="utf-8")


def first_line(e: BaseException) -> str:
    text = str(e).strip()
    return text.splitlines()[0] if text else type(e).__name__


def check_board(c: Ctx, board_log: BoardLog) -> BoardRecord:
    """Every step in order up to the first failure, the screen marked, the
    evidence saved and the row logged, whatever happened, Ctrl+C included."""
    record = c.record
    t0 = time.monotonic()
    failure: tuple[str, StepFailed] | None = None
    try:
        for number, step in enumerate(STEPS, 1):
            s0 = time.monotonic()
            c.console.step_start(number, step)
            try:
                detail = STEP_FUNCS[step](c)
            except StepFailed as e:
                failure = (step, e)
            except (TreadmillError, OSError) as e:
                ended = c.board is None or c.board.link.stream.ended
                failure = (step, StepFailed(first_line(e), markable=not ended))
            result = record.steps[step]
            result.seconds = round(time.monotonic() - s0, 1)
            if failure:
                result.status, result.detail = FAIL, failure[1].reason
                c.console.step(number, step, FAIL, failure[1].reason)
                break
            result.status, result.detail = PASS, detail
            c.console.step(number, step, PASS, detail)
        record.result = FAIL if failure else PASS
        if failure:
            record.failed_step, record.reason = failure[0], failure[1].reason
        record.c64_marked = finish_screen(c, failure)
    except KeyboardInterrupt:
        record.result, record.reason = ABORTED, "stopped by the operator"
        raise
    finally:
        record.duration_s = round(time.monotonic() - t0, 1)
        try:
            save_evidence(c)
        finally:
            board_log.append(record)
            if c.board is not None:
                c.board.close()
    return record


# -- the session ----------------------------------------------------------------------------------------

def board_station(template: Station, uid: str, port: str) -> Station:
    """The template station holding the board just found."""
    return replace(template, tr=replace(template.tr, uid=uid, port_hint=port, kind=None))


def run_session(template: Station, profile: MfgProfile, bench: Bench, console: Console, video: VideoKit,
                run_dir: RunDir, *, boards: int | None = None, reset: ExternalReset | None = None,
                log: Callable[[str], None] = print) -> dict:
    """Board after board until Ctrl+C, or ``boards`` of them."""
    expected = expected_firmware(profile)
    board_log = BoardLog(profile.log, run_dir.stamp, console)
    reset = reset or OperatorReset(bench)
    records: list[BoardRecord] = []
    console.heading(f"TR+ manufacturing test: {expected.version}, built {expected.stamp}")
    console.say(f"Log: {board_log.path}")
    try:
        while boards is None or len(records) < boards:
            n = len(records) + 1
            console.heading(f"Board {n}")
            bench.do(CONNECT)
            console.say("Waiting for a TeensyROM on USB (Ctrl+C ends the session) ...")
            port, uid = bench.wait_for_board()
            console.say(f"Chip id {uid} on {port}")
            folder = run_dir.path / "boards" / f"{n:03d}-{uid}"
            folder.mkdir(parents=True, exist_ok=True)
            record = BoardRecord(n, port, uid, datetime.now(), evidence=str(folder))
            c = Ctx(record, board_station(template, uid, port), profile, expected, bench, reset, console, video,
                    folder, log)
            try:
                check_board(c, board_log)
            finally:
                records.append(record)
                console.verdict(record)
            bench.do(POWER_OFF)
            seen = getattr(video.source, "no_signal_frames", None)
            while not bench.gone(port):
                time.sleep(0.5)
            after = getattr(video.source, "no_signal_frames", None)
            if seen is not None and after is not None:
                log(f"[mfg] {port} left USB; the capture dropped {after - seen} no-signal frames meanwhile")
    except KeyboardInterrupt:
        console.say()
        console.say("Session stopped.")
    counts = {s: sum(1 for r in records if r.result == s) for s in (PASS, FAIL, ABORTED)}
    console.heading(f"{len(records)} board(s): {counts[PASS]} passed, {counts[FAIL]} failed"
                    + (f", {counts[ABORTED]} stopped" if counts[ABORTED] else ""))
    summary = {"boards": [r.to_dict() for r in records], "counts": counts, "log": str(board_log.path),
               "expected": {"version": expected.version, "built": expected.stamp}}
    run_dir.meta.update({"mode": "mfg", "station": template.id, "profile": asdict(profile), **summary})
    run_dir.write_json("run.json", run_dir.meta)
    return summary
