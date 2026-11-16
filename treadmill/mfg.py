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
import socket
import statistics
import struct
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple, Protocol, TypeVar

import cv2
import numpy as np

from treadmill.board import Board
from treadmill.c64.keys import RETURN, KernalKeys
from treadmill.c64.menu import MenuDriver, parse_listing
from treadmill.c64.screen import (
    CHARSET_LOWER_UPPER, CHARSET_UPPER_GRAPHICS, COLOR_RAM, REVERSE_VIDEO, SCREEN_BYTES, SCREEN_COLUMNS,
    SCREEN_ROWS, VIC_BORDER, codes_for_text,
)
from treadmill.errors import EXIT_USAGE, TreadmillError
from treadmill.firmware.hexinfo import HexError, HexInfo, inspect_hex
from treadmill.link import LinkError, NotFound, TransportError, list_teensyrom_ports, teensyrom_uid, wait_for_teensyrom
from treadmill.link.banner import TR_PLUS, Banner
from treadmill.observe.calibrate import Calibration, border_of, calibration_path
from treadmill.observe.capture import FrameSource, save_strip
from treadmill.observe.channels import ScreenChannel, ScreenRead
from treadmill.observe.glyphs import GlyphBank
from treadmill.observe.golden import save_golden
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
REU_SETTLE_S = 2.0        # the checker's bank map unchanged this long: it has tested every bank
VIDEO_SAME_READS = 2

# -- the clock -----------------------------------------------------------------------------

# This PC's clock is checked against NTP once a session, a sample from each of a few of the pool's
# servers, so one wrong server cannot move the median.
NTP_SAMPLES, NTP_TIMEOUT_S = 3, 2.0
NTP_EPOCH = 2208988800    # 1900 to 1970, in seconds
PC_CLOCK_WARN_S = 2.0     # an offset worth telling the operator about; the check corrects for any
# C64TODfromRTC adds the timezone setting in half hours, UTC-12:00 to UTC+14:00 (rwRegTimezone).
TZ_STEP_S = 1800
TZ_STEPS = range(-24, 29)

# REU-Checker v1.0's screen, measured from captures (it is a packed third-party program): a bank map of
# 8 rows x 32 cells from row 7, column 4, one cell per 64 KB, green OK, red error, checkered grey missing;
# "THE REU IS WORKING!" on row 5, columns 10-28, green while it passes; a green border.
REU_MAP_ROW, REU_MAP_COL, REU_MAP_ROWS, REU_MAP_COLS = 7, 4, 8, 32
REU_CELL_KB = 64
REU_WORKING_ROW, REU_WORKING_COLS = 5, range(10, 29)
CELL_OK, CELL_ERROR, CELL_OTHER = "G", "R", "."
# A pixel is green (or red) when that channel is this far above the other two. The screen's other colors
# stay well under it: yellow text 15, greys 0, light blue and purple below 0 (Pepto palette).
HUE_MARGIN = 30
CELL_INSET = 0.2          # of a cell, left off each side: the capture blurs the neighbors' colors in
OK_SHARE, ERROR_SHARE = 0.5, 0.3   # of a map cell's pixels: a solid block shows ~1.0, its neighbors ~0
# Of the WORKING line's pixels: about a quarter is ink, and S-Video's narrow chroma spreads its green
# further (0.86 on the bench); the yellow title line shows ~0.003.
INK_SHARE = 0.1

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

# The verdict on the SID before the freeze. The CPU is then paused, so whatever the SID holds keeps
# sounding: it ends silent. The menu plays its tune from CIA1's timer A interrupt (IRQwedge in
# SIDRelated.s), which is masked first; its own SID pause command toggles, so would start a tune that
# was off.
SID, SID_REGS, SID_VOLUME, SID_VOICE = 0xD400, 0x19, 0xD418, 7
CIA1_ICR, CIA1_TIMER_A_OFF = 0xDC0D, 0x01      # bit 7 clear: the 1 bits named are masked
SID_CLOCK_HZ = {"PAL": 985248, "NTSC": 1022727}
PULSE, NOISE, GATE = 0x40, 0x80, 0x01
VOICE_AD, VOICE_SR, PULSE_WIDTH = 0x09, 0x00, 0x0800     # a plucked note: no attack, decay 750 mS


class Jingle(NamedTuple):
    chords: tuple[tuple[int, int, int], ...]     # MIDI notes, one per voice
    times: tuple[tuple[float, float], ...]       # each chord's (held, then silent) seconds
    waves: tuple[int, int, int]                  # each voice's waveform


# C major rising from middle C, a long chord then three short; C minor falling two octaves lower, two
# short chords, a pause, two more, its lowest voice noise.
PASS_JINGLE = Jingle(((60, 64, 67), (64, 67, 72), (67, 72, 76), (72, 76, 79)),
                     ((0.4, 0.08), (0.2, 0.04), (0.2, 0.04), (0.2, 0.04)), (PULSE, PULSE, PULSE))
FAIL_JINGLE = Jingle(((48, 51, 55), (43, 48, 51), (39, 43, 48), (36, 39, 43)),
                     ((0.2, 0.04), (0.2, 0.32), (0.2, 0.04), (0.2, 0.04)), (NOISE, PULSE, PULSE))


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
    log: Path
    sd_ignore: tuple[str, ...] = ("System Volume Information",)
    reu_kb: int = 512
    xpt_passes: int = 3
    time_tolerance_s: float = 5.0
    ntp_server: str = "pool.ntp.org"
    epc_timeout_s: float = 180.0
    boot_timeout_s: float = 60.0
    reset_timeout_s: float = 60.0
    xpt_pass_timeout_s: float = 90.0
    reu_timeout_s: float = 30.0
    require_release: bool = True

    PATHS = ("factory_hex", "sd_golden", "log")

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
        defaults = {"sd_golden": "sd_root.json", "log": "boards.csv"}
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
        words = {PASS: "PASSED", FAIL: "FAILED"}.get(record.result, record.result)
        self.say(self._paint(f"    Board {record.n} {words}", color) +
                 f": chip id {record.chip_id}, {record.duration_s:.0f} s")
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
    are found by the serial string they enumerate with. A TeensyROM already on
    USB when the session starts belongs to another machine on the bench, or is
    a board under test that was switched on early: neither is taken until it
    has left USB."""

    def __init__(self, console: Console):
        self.console = console
        ports = list_teensyrom_ports()
        self.others = {port for port, _ in ports}
        if ports:
            names = ", ".join(f"{port} (chip id {teensyrom_uid(s) or '?'})" for port, s in ports)
            console.warn(f"Already on USB, so not tested: {names}. If the board under test is one of "
                         "them, switch its C64 off and on again.")

    def do(self, action: str) -> None:
        self.console.instruct(INSTRUCTIONS[action])

    def wait_for_board(self) -> tuple[str, str]:
        while True:
            self.others &= {port for port, _ in list_teensyrom_ports()}
            try:
                found = wait_for_teensyrom(exclude=tuple(self.others), timeout=1.0)
            except NotFound:
                continue
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


def arrival(chunks: list[tuple[float, bytes]], offset: int) -> float:
    """When the chunk holding byte ``offset`` of ``chunks`` arrived, by
    ``time.monotonic()``: the reader thread stamps each chunk as it reads
    it, while the step only polls, a quarter second and a video read apart."""
    end = 0
    for at, data in chunks:
        end += len(data)
        if offset < end:
            return at
    raise ValueError(f"byte {offset} is past the {end} received")


def utc_at(mono: float, pc_offset_s: float | None) -> datetime:
    """UTC at ``time.monotonic()`` time ``mono``: this PC's clock, corrected
    by its offset from NTP when that is known."""
    return datetime.now(timezone.utc) + timedelta(seconds=(pc_offset_s or 0.0) - (time.monotonic() - mono))


def utc_zone(steps: int) -> str:
    minutes = abs(steps) * TZ_STEP_S // 60
    return f"UTC{'-' if steps < 0 else '+'}{minutes // 60}:{minutes % 60:02d}"


def clock_problem(delta: float, tolerance: float, fresh_eeprom: bool | None, pc_checked: bool) -> str | None:
    """Why a board's clock fails, or None. A board whose settings were kept
    from earlier use may carry a timezone, which moves its clock by whole
    half hours. The wrap to half a day either way shows UTC+13:00 as
    UTC-11:00, and the line has no date to tell them apart."""
    if abs(delta) <= tolerance:
        return None
    steps = round(delta / TZ_STEP_S)
    residual = delta - steps * TZ_STEP_S
    if not fresh_eeprom and steps and abs(residual) <= tolerance:
        zones = " or ".join(utc_zone(s) for s in (steps, steps + 48) if s in TZ_STEPS)
        return f"the board's clock reads {zones}, {residual:+.0f} s off: its timezone setting is not UTC"
    return (f"the board's clock is {delta:+.0f} s from UTC: the time fetch over Ethernet, or the RTC"
            + ("" if pc_checked else "; this PC's clock was not checked against NTP"))


def _ntp_stamp(t: float) -> bytes:
    return struct.pack("!II", int(t) + NTP_EPOCH, min(int(t % 1 * 2**32), 0xFFFFFFFF))


def _ntp_time(stamp: bytes) -> float:
    seconds, fraction = struct.unpack("!II", stamp)
    return seconds - NTP_EPOCH + fraction / 2**32


def ntp_offset(server: str, port: int = 123, samples: int = NTP_SAMPLES,
               timeout: float = NTP_TIMEOUT_S) -> float | None:
    """Seconds to add to this PC's clock for NTP's time: the median of one
    SNTP exchange with each of up to ``samples`` of the addresses ``server``
    resolves to, or None when none answered. A reply counts only when it
    echoes this request's own time, comes from a synchronized server, and
    is not a kiss-of-death (stratum 0)."""
    try:
        addrs = list(dict.fromkeys(a[4] for a in socket.getaddrinfo(server, port, socket.AF_INET,
                                                                     socket.SOCK_DGRAM)))
    except OSError:
        return None
    offsets = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for i in range(samples):
            t1 = time.time()
            stamp = _ntp_stamp(t1)
            deadline = time.monotonic() + timeout
            try:
                sock.sendto(b"\x23" + bytes(39) + stamp, addrs[i % len(addrs)])   # version 4, client
                while (left := deadline - time.monotonic()) > 0:
                    sock.settimeout(left)
                    reply = sock.recv(512)
                    t4 = time.time()
                    if (len(reply) >= 48 and reply[24:32] == stamp and reply[0] & 7 == 4
                            and reply[0] >> 6 != 3 and reply[1] != 0):
                        t2, t3 = _ntp_time(reply[32:40]), _ntp_time(reply[40:48])
                        offsets.append(((t2 - t1) + (t3 - t4)) / 2)
                        break
            except OSError:
                continue
    return statistics.median(offsets) if offsets else None


def pc_clock_offset(server: str, console: Console) -> float | None:
    """This PC's offset from NTP for the session, said on the console."""
    if not server:
        console.say("This PC's clock is not checked against NTP (the profile's ntp_server is empty).")
        return None
    offset = ntp_offset(server)
    if offset is None:
        console.warn(f"No time from {server}: boards' clocks are checked against this PC's clock as it is.")
        return None
    where = f"{abs(offset):.1f} s {'behind' if offset > 0 else 'ahead of'} {server}"
    if abs(offset) > PC_CLOCK_WARN_S:
        console.warn(f"This PC's clock is {where}; boards are checked against NTP's time. "
                     "'w32tm /resync' from an administrator prompt sets it.")
    else:
        console.say(f"This PC's clock is {where}.")
    return offset


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


@dataclass(frozen=True)
class ReuView:
    """One frame of the REU checker, by color."""
    cells: str                # the bank map in order, one CELL_* per 64 KB
    working: bool             # the WORKING line's ink is green
    border_green: bool

    def describe(self, reads: int) -> str:
        rows = [self.cells[i:i + REU_MAP_COLS] for i in range(0, len(self.cells), REU_MAP_COLS)]
        return "\n".join([f"% the last of {reads} video reads; {CELL_OK} OK, {CELL_ERROR} error, "
                          f"{CELL_OTHER} anything else", *rows,
                          f"WORKING line green: {'yes' if self.working else 'no'}",
                          f"border green: {'yes' if self.border_green else 'no'}"]) + "\n"


def hue_shares(pixels: np.ndarray) -> tuple[float, float]:
    """The fractions of these BGR pixels that are green and that are red."""
    p = pixels.reshape(-1, 3).astype(np.int16)
    if not len(p):
        return 0.0, 0.0
    b, g, r = p[:, 0], p[:, 1], p[:, 2]
    return float(((g - np.maximum(r, b)) > HUE_MARGIN).mean()), float(((r - np.maximum(g, b)) > HUE_MARGIN).mean())


def cell_pixels(image: np.ndarray, cal: Calibration, row: int, col: int, inset: float = CELL_INSET) -> np.ndarray:
    x, y = cal.x0 + col * cal.cell_w, cal.y0 + row * cal.cell_h
    dx, dy = cal.cell_w * inset, cal.cell_h * inset
    return image[round(y + dy):round(y + cal.cell_h - dy), round(x + dx):round(x + cal.cell_w - dx)]


def reu_view(image: np.ndarray, cal: Calibration) -> ReuView:
    cells = []
    for row in range(REU_MAP_ROW, REU_MAP_ROW + REU_MAP_ROWS):
        for col in range(REU_MAP_COL, REU_MAP_COL + REU_MAP_COLS):
            green, red = hue_shares(cell_pixels(image, cal, row, col))
            cells.append(CELL_OK if green >= OK_SHARE else CELL_ERROR if red >= ERROR_SHARE else CELL_OTHER)
    line = np.concatenate([cell_pixels(image, cal, REU_WORKING_ROW, col, 0.0).reshape(-1, 3)
                           for col in REU_WORKING_COLS])
    green, _ = hue_shares(line)
    border_green, _ = hue_shares(np.asarray(border_of(image), np.uint8))
    return ReuView("".join(cells), green >= INK_SHARE, border_green > 0)


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
    pc_offset_s: float | None = None     # this PC's clock to NTP's; None when not known

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


def launch_from_menu(c: Ctx, path: str) -> None:
    """A Teensy Mem item launched the way an operator does, by keys through
    the menu. A remote launch would allocate the firmware's directory buffer
    in RAM2, and the External Ports test's first Ethernet start would then
    leave a hole there when the REU frees it: the REU checker gets 63 of 64
    banks, rounded down to 256K. The menu is read by DMA, which video's
    misreads cannot fool."""
    folder, _, name = path.rpartition("/")
    screen = ScreenChannel(c.link, None, None, None, dma_allowed=c.board.dma_allowed)
    menu = MenuDriver(screen, c.keys, board=c.board)
    try:
        menu.ensure_root("teensy")
        for part in filter(None, folder.split("/")):
            menu.open(f"/{part}")
        menu.select(name)
    except TreadmillError as e:
        raise StepFailed(f"could not launch {name} from the menu: {e}") from None


def step_external_ports(c: Ctx) -> str:
    """The board answers nothing while it waits for the buttons, so the wait
    sends nothing: it watches serial for the line sent after the buttons,
    and video for a fail the test printed before reaching them."""
    mark = c.mark()
    launch_from_menu(c, EPC_PATH)
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
    sent = utc_at(arrival(c.link.stream.tap.chunks[mark:], hello.start()), c.pc_offset_s)
    delta = clock_delta(hello, sent)
    c.record.epc_time_delta_s = round(delta, 1)
    problem = clock_problem(delta, c.profile.time_tolerance_s, c.record.fresh_eeprom, c.pc_offset_s is not None)
    if problem:
        raise StepFailed(problem)
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
    launch_from_menu(c, XPT_PATH)
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
    Its text is yellow and green on grey, which the glyph bank (learned on
    the menu) cannot read reliably, so the verdict is its colors."""
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
    banks = c.profile.reu_kb // REU_CELL_KB
    watch_reu_checker(c, banks, c.profile.reu_timeout_s)
    c.board.reset()
    c.board.reu_live = False
    try:
        c.board.wait_for_menu(c.profile.boot_timeout_s)
    except (LinkError, TransportError, NotFound) as e:
        raise StepFailed(f"the menu did not come back after the REU checker: {e}") from None
    return f"{size}K, the checker's {banks} banks OK"


def reu_error(view: ReuView) -> str | None:
    bad = view.cells.find(CELL_ERROR)
    return None if bad < 0 else f"the REU checker shows an error in the bank at {bad * REU_CELL_KB}K"


def reu_verdict(view: ReuView, banks: int) -> str | None:
    """What is wrong with the checker's finished screen, or None: ``banks``
    OK cells and nothing past them, no error, the WORKING line green."""
    error = reu_error(view)
    if error:
        return error
    ok = len(view.cells) - len(view.cells.lstrip(CELL_OK))
    if ok != banks or CELL_OK in view.cells[ok:]:
        if CELL_OK not in view.cells[ok:]:
            return f"the REU checker found {ok * REU_CELL_KB}K, expected {banks * REU_CELL_KB}K"
        odd = [i * REU_CELL_KB for i in range(len(view.cells)) if (view.cells[i] == CELL_OK) != (i < banks)]
        return f"the REU checker's bank map is not {banks * REU_CELL_KB}K OK (wrong at {odd[0]}K)"
    if not view.working:
        return "the REU checker's 'THE REU IS WORKING!' line is not green"
    return None


def watch_reu_checker(c: Ctx, banks: int, timeout: float) -> None:
    """The checker tests its banks quickly, then holds still. Once it is up
    (its border green, the menu's is not) a red cell fails at once, on two
    reads in a row; otherwise the verdict is its bank map once that has not
    changed for REU_SETTLE_S. The last read is kept as evidence."""
    start = time.monotonic()
    prev: ReuView | None = None
    reads, since = 0, start
    try:
        while True:
            frame = c.video.source.next_frame()
            view = reu_view(frame.image, c.video.calibration)
            now = time.monotonic()
            reads += 1
            if prev is None or view != prev:
                since = now
            up = view.border_green and prev is not None and prev.border_green
            if up and reu_error(view) and reu_error(view) == reu_error(prev):
                raise StepFailed(reu_error(view))
            prev = view
            if up and now - since >= REU_SETTLE_S:
                break
            if now - start >= timeout:
                raise StepFailed(f"the REU checker's screen did not settle within {timeout:g} s" if view.border_green
                                 else f"the REU checker did not come up (no green border) within {timeout:g} s")
            time.sleep(VIDEO_POLL_S)
        problem = reu_verdict(view, banks)
        if problem:
            raise StepFailed(problem)
    finally:
        if prev is not None:
            (c.folder / "reu.colors.txt").write_text(prev.describe(reads), encoding="utf-8")
            cv2.imwrite(str(c.folder / "reu.png"), frame.image)


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
    """A summary on green (``finish_screen`` then pauses the CPU)."""
    dump = c.link.read_screen()
    rows = pass_rows(c.record)
    rows += [""] * (SCREEN_ROWS - len(rows))
    c.link.write_memory(dump.address, b"".join(screen_codes(r, dump.charset) for r in rows))
    c.link.write_memory(COLOR_RAM, bytes([WHITE]) * SCREEN_BYTES)
    c.link.write_memory(VIC_BORDER, bytes([GREEN, GREEN]))


def mark_fail_screen(c: Ctx, step: str, reason: str) -> None:
    """The failing screen kept: inverted on red, so it reads from across the
    bench and its own reason stays legible, the step and reason on the bottom
    row (``finish_screen`` then pauses the CPU)."""
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


def sid_voice(note: int, clock_hz: int, control: int) -> bytes:
    freq = round(440.0 * 2 ** ((note - 69) / 12) * (1 << 24) / clock_hz)
    return bytes([freq & 0xFF, freq >> 8, PULSE_WIDTH & 0xFF, PULSE_WIDTH >> 8, control, VOICE_AD, VOICE_SR])


def sid_clock(board: Board, station: Station) -> int:
    video = board.banner.video if board.banner else None
    return SID_CLOCK_HZ.get(video or station.machine.region, SID_CLOCK_HZ["PAL"])


def play_chords(link, jingle: Jingle, clock_hz: int) -> None:
    """The jingle on the SID, timed from here, then the SID silent. The
    running program's timer interrupt is masked first, so a tune it plays
    stops writing the SID. The notes' writes skip the link's drain, which
    would add its 300 mS to every one."""
    link.write_memory(CIA1_ICR, bytes([CIA1_TIMER_A_OFF]))
    link.write_memory(SID, bytes(SID_REGS))
    link.write_memory(SID_VOLUME, bytes([0x0F]))
    for chord, (held, gap) in zip(jingle.chords, jingle.times):
        voices = list(zip(chord, jingle.waves))
        link.write_memory(SID, b"".join(sid_voice(n, clock_hz, w | GATE) for n, w in voices), drain=0.0)
        time.sleep(held)
        link.write_memory(SID, b"".join(sid_voice(n, clock_hz, w) for n, w in voices), drain=0.0)
        time.sleep(gap)
    link.write_memory(SID, bytes(SID_REGS), drain=0.0)


def finish_screen(c: Ctx, failure: tuple[str, StepFailed] | None) -> bool:
    """Play the verdict on the SID, mark the C64's screen with it, then pause
    the CPU so nothing redraws it (the TR's LED flashes while it is paused).
    The screen comes after the chords: the menu redraws its clock until the
    pause. False when the screen could not be marked."""
    board = c.board
    if board is None or board.link.stream.ended:
        return False
    try:
        if failure is None:
            play_chords(c.link, PASS_JINGLE, sid_clock(board, c.station))
            write_pass_screen(c)
            c.link.pause(True)
            return True
        step, err = failure
        if not err.markable:
            return False
        if board.reu_live:
            # While the REU is set up the firmware refuses memory commands, and a pause would share the
            # REU's DMA state: back to the menu, and the menu is marked.
            board.reset()
            board.reu_live = False
            board.wait_for_menu(c.profile.boot_timeout_s)
        if not board.dma_allowed():
            return False
        play_chords(c.link, FAIL_JINGLE, sid_clock(board, c.station))
        mark_fail_screen(c, step, err.reason)
        c.link.pause(True)
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
    pc_offset = pc_clock_offset(profile.ntp_server, console)
    run_dir.meta["pc_clock"] = {"ntp_server": profile.ntp_server,
                                "offset_s": None if pc_offset is None else round(pc_offset, 3)}
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
                    folder, log, pc_offset_s=pc_offset)
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
