"""The C64 USB Keyboard board's features, each tested on the C64 (2026-11-19).

Reads which outputs the keyboard board has fitted (``?``), runs every
protocol feature and every fitted output against the C64, judges what the
C64 did from the video capture, and prints one row per feature: fitted or
not, tested or not, PASS / FAIL / SKIP, the time it took and why.

    treadmill exec --no-link --station <id> scripts/2026-11-19-keyboard-board-features.py [-- --skip tod,kawari]

Firmware: none on a TR, whose link is not opened; the keyboard board speaks
protocol 1 (C64-USBKeyboard ``Docs/Host_Protocol.md``). The station needs
its keyboard board and video; a TR or TR+ in the cartridge port is optional.
``SETUP`` below is printed when it starts.

How the picture is read, without a calibration: the text typed with ``S``
POKEs the border and background, and the verdicts are their brightness
(black, white or between), so a mistyped line shows as no change. The text
area's shape tells PAL from NTSC (288 or 240 lines in the same frame
height) and the TR menu (24 background rows under its bar) from a BASIC
screen (25). A BASIC program with CIA1's interrupt off scans the matrix
itself, marks each key it sees in an 8x8 grid and lights the border while
any key is down: that is the matrix walk and the C64's view of holds,
releases, ``D`` and a closed port. A 13-byte NMI hook counts RESTORE pulses
in the background color. ``PEEK(678)`` is the KERNAL's own PAL flag.

``result`` holds ``rows`` (feature, fitted, tested, result, reason),
``board`` (the ``?`` fields at the start) and ``restored``; the same goes to
``features.json``, the board's exchanges to ``transcript.txt`` (and every
line both ways, through each reopen, to ``kbd-full.log``) and the frames
each verdict read to PNGs under ``artifacts``. Exits 1 when a
feature fails. Puts back J1, TOD and nothing held as ``?`` reported them at
the start, and never writes the fitted marks. The C64 is restarted many
times and is left at BASIC or the TR menu: anything in its memory is lost.
"""

import json
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field

import cv2
import numpy as np

from treadmill.observe.calibrate import LAYOUT_BASIC, CalibrationError, border_of, find_text_area
from treadmill.observe.capture import CaptureError
from treadmill.observe.normalize import dominant_color

SETUP = """\
C64 USB Keyboard feature test: setup
 1. The C64 has the C64 USB Keyboard board in place of its keyboard, is powered on, and the
    board's USB port is on this PC. Nobody types on the board's USB keyboard during the test.
 2. The board speaks protocol 1 and has its wired outputs marked fitted (F <output> 1). The
    test reads the marks and never writes them; an output not marked is listed as not fitted.
 3. The station's video capture shows the C64. No calibration or glyph bank is needed.
 4. The cartridge port is empty or holds a TR or TR+. When the C64 boots into the TR menu the
    test leaves it with F2 (Exit to BASIC). No other cartridge.
 5. The C64 is reset many times, its video standard is switched and switched back, and its TOD
    rate changes: whatever is in its memory is lost. J1 and TOD are put back as found.
 About 6 minutes. After --: --skip <feature,...> leaves features out.
"""

OUTPUTS = ("tod", "menu", "alt", "reset", "kawari")
BOOT_S = 25.0                 # a reset to a settled BASIC screen or TR menu
SETTLE_S = 8.0                # a typed line's effect on the picture
POLL_S = 0.3
# Cell height over width on rig A's capture (S-Video to HDMI into an MS2109), from the 002b
# calibrations: PAL puts 288 lines in the frame's height, NTSC 240. The text area's height over
# its width is then 24 or 25 of these over 40: the TR menu or a BASIC screen.
CELL_ASPECT = {"PAL": 21.03 / 25.72, "NTSC": 25.24 / 24.77}
ASPECT_TOLERANCE = 0.015      # the classes are 4 % apart
BLINK = 40.0                  # luma swing in one cell that counts as the cursor blinking
GRID_TOP, GRID_LEFT = 5, 10   # where the matrix program marks key <pa><pb>: row 5+pa, column 10+2*pb
KEYS = [f"{a}{b}" for a in range(8) for b in range(8)]
KEY_NAMES = dict(zip(KEYS, (
    "DEL RETURN CRSR-LR F7 F1 F3 F5 CRSR-UD "
    "3 W A 4 Z S E L-SHIFT "
    "5 R D 6 C F T X "
    "7 Y G 8 B H U V "
    "9 I J 0 M K O N "
    "+ P L - . : @ , "
    "POUND * ; HOME R-SHIFT = UP-ARROW / "
    "1 LEFT-ARROW CTRL 2 SPACE C= Q RUN/STOP").split()))
# The BASIC scanner takes about 200 ms from seeing a key to reading column 5: on the bench
# (2026-11-19) a 200 ms press was missed in PA5 now and then and always in PA6 and PA7. So a
# walk press holds three times that, and starts once the scanner is back waiting (border black).
HOLD_MS = 600
X_KEYS = {"17+12": f"d 17;w 20;d 12;w {HOLD_MS};u 12;w 20;u 17", "64": f"d 64;w {HOLD_MS};u 64"}

# The matrix walk: CIA1's interrupt off (no KERNAL scan, no cursor, no RUN/STOP), border white
# while any key is down, each key marked with a white block; RUN/STOP alone ends it.
MATRIX = (
    "10 poke56333,127:poke53280,0:poke53281,12:printchr$(147)",
    "20 poke56320,0:ifpeek(56321)=255thenpoke53280,0:goto20",
    "25 poke53280,1:n=0:m=1",
    "30 fora=0to7:poke56320,255-m:v=255-peek(56321):ifvthengosub60",
    "40 m=m*2:next:ifn=1andk=63thenpoke56333,129:end",
    "50 goto20",
    "60 c=1:forb=0to7:ifvandcthengosub80",
    "70 c=c*2:next:return",
    "80 k=a*8+b:n=n+1:p=210+a*40+b*2:poke1024+p,160:poke55296+p,1:return",
)
# TOD against TI for 600 jiffies (10 s): h is the CIA's 50/60 Hz setting, e the seconds
# expected; the border goes grey while it runs, then white when it read e, black when not.
TOD_PROGRAM = (
    "10 poke53280,12:c=peek(56334):poke56334,(cand127)-128*(h=50)",
    "20 poke56331,0:poke56330,0:poke56329,0:poke56328,0:t=ti",
    "30 ifti-t<600then30",
    "40 s=peek(56329):d=peek(56328):x=int(s/16)*10+(sand15)+d/10",
    "50 printh;e;x:poke53280,-(abs(x-e)<.4)",
)
# INC $D021 then on to the NMI handler the vector held: each RESTORE steps the background color.
NMI_HOOK = (
    "poke49152,238:poke49153,33:poke49154,208:poke49155,76:poke53280,12",
    "poke49156,peek(792):poke49157,peek(793):poke53281,0:poke792,0:poke793,192",
)

FEATURES = (
    # key, label, outputs it needs, runs on the C64
    ("identity", "Identity: ?, F ?, USB serial and product", (), False),
    ("clock", "Board clock: TIME", (), False),
    ("lines", "Line rules: case, CR, empty, partial, length, junk", (), False),
    ("errors", "Error replies: syntax, range, notfitted", (), False),
    ("events", "Key and debug lines switch: E", (), False),
    ("typing", "Typing: S, ST", (), True),
    ("walk", "Key presses: P, all 64 matrix keys", (), True),
    ("sequence", "Sequences: X", (), True),
    ("hold", "Holds: K d, K u, held=", (), True),
    ("hold_timeout", "Hold timeout: EV K ... timeout", (), True),
    ("busy", "Busy replies while a timed action runs", (), True),
    ("abort", "A: aborts an action, releases holds", (), True),
    ("loopback", "Loopback diagonal: D", (), True),
    ("dtr", "Release when the port closes", (), True),
    ("restore", "RESTORE pulse: N", (), True),
    ("stop_restore", "RUN/STOP+RESTORE: X with n", (), True),
    ("tod", "TOD clock: T 50|60|off", ("tod",), True),
    ("reset", "/RESET pulse: R, R <us>, X r", ("reset",), True),
    ("reset_hold", "/RESET hold: release, timeout, A, close", ("reset",), True),
    ("kawari", "Kawari video standard: V", ("kawari", "reset"), True),
    ("menu", "TR menu button: B menu", ("menu",), True),
    ("alt", "TR ALT button: B alt", ("alt",), True),
    ("usb_keyboard", "USB keyboard during a session", (), True),
)

T0 = time.monotonic()


class Skip(Exception):
    """A feature that cannot be tested here, with the reason."""


class C64Lost(Exception):
    """The C64 is not at BASIC READY any more, so typing into it is not safe."""


# -- the table ------------------------------------------------------------------

@dataclass
class Row:
    key: str
    label: str
    needs: tuple
    on_c64: bool
    fitted: str = ""
    tested: bool = False
    result: str = ""
    reason: str = ""
    fails: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    seconds: float = 0.0          # time in its test, a recovery before it not counted

    def check(self, ok, what: str) -> bool:
        self.tested = True
        if not ok:
            self.fails.append(what)
            log(f"  FAIL {self.key}: {what}")
        return bool(ok)

    def note(self, what: str) -> None:
        self.notes.append(what)

    def skip(self, why: str) -> None:
        if not self.result and not self.tested:
            self.result, self.reason = "SKIP", why

    def close(self) -> None:
        if self.result:
            return
        if not self.tested:
            self.result, self.reason = "SKIP", "; ".join(self.notes) or "not reached"
        else:
            self.result = "FAIL" if self.fails else "PASS"
            self.reason = "; ".join(self.fails or self.notes)

    def record(self) -> dict:
        return {"feature": self.key, "label": self.label, "fitted": self.fitted, "tested": self.tested,
                "result": self.result, "reason": self.reason, "fails": self.fails, "notes": self.notes,
                "seconds": round(self.seconds, 1)}


ROWS = {key: Row(key, label, needs, on_c64) for key, label, needs, on_c64 in FEATURES}


def table(reason_width: int | None) -> str:
    width = max(len(r.label) for r in ROWS.values())
    head = f"{'Feature':<{width}} {'Fitted':<7} {'Tested':<7} {'Result':<7} {'Time s':>6}  Reason"
    lines = [head, "-" * len(head)]
    for row in ROWS.values():
        reason = row.reason
        if reason_width and len(reason) > reason_width:
            reason = reason[:reason_width - 3] + "..."
        secs = f"{row.seconds:.1f}" if row.seconds >= 0.05 else "-"
        lines.append(f"{row.label:<{width}} {row.fitted:<7} {'yes' if row.tested else 'no':<7} {row.result:<7} "
                     f"{secs:>6}  {reason}")
    counts = {k: sum(r.result == k for r in ROWS.values()) for k in ("PASS", "FAIL", "SKIP")}
    lines.append(f"{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped; "
                 f"{time.monotonic() - T0:.0f} s in all")
    return "\n".join(lines)


# -- the keyboard board, through treadmill's client --------------------------------

class Kb:
    """The station's keyboard client (``kbd``), with a transcript of what this
    script sent and got. ``reopen()`` replaces the client, so the both-way logs
    of the ones it closed are kept for ``kbd-full.log``."""

    def __init__(self, board):
        self.b = board
        self.transcript: list[str] = []
        self.closed_logs: list[str] = []
        self.reopened = False

    def _log(self, direction: str, text: str) -> None:
        self.transcript.append(f"{time.monotonic() - T0:9.3f} {direction} {text}")

    def cmd(self, line: str, timeout: float | None = None):
        """One line, one reply; with no ``timeout`` the client's own, worked out from the command."""
        self._log(">", line)
        r = self.b.command(line, timeout)
        self._log("<", r.raw)
        return r

    def start(self, line: str):
        self._log(">", f"{line}   (not waiting)")
        return self.b.start(line)

    def wait(self, pending, timeout: float):
        r = pending.wait(timeout)
        self._log("<", r.raw)
        return r

    def abort(self) -> list:
        self._log(">", "A")
        replies = list(self.b.abort())
        for r in replies:
            self._log("<", r.raw)
        return replies

    def raw(self, data: bytes) -> None:
        self._log(">", f"raw {data!r}")
        self.b.write_raw(data)

    def raw_lines(self, data: bytes, seconds: float) -> list[str]:
        """Send ``data`` as it is; every line that arrived in the next ``seconds``."""
        n = len(self.b.lines)
        self.raw(data)
        time.sleep(seconds)
        lines = [text for _, text in self.b.lines[n:]]
        for line in lines:
            self._log("<", line)
        return lines

    def mark(self) -> float:
        return time.monotonic()

    def wait_event(self, pattern: str, timeout: float, mark: float):
        """The first EV line matching ``pattern`` since ``mark``: (host time, line), or None."""
        ev = self.b.wait_event(pattern, timeout, since=mark)
        if ev:
            self._log("<", ev[1])
        return ev

    def reopen(self, while_closed=None) -> dict:
        """Close the port (DTR drops), run ``while_closed``, open the same board again."""
        chip, port, cls = self.b.chip, self.b.port, type(self.b)
        self._log("!", "port closed")
        self.b.close()
        self.closed_logs += self.b.transcript(T0) + [f"{time.monotonic() - T0:10.3f} ! port closed"]
        if while_closed:
            while_closed()
        time.sleep(0.3)
        for attempt in range(3):
            try:
                self.b = cls.open(chip=chip, port_hint=port, timeout=5.0)
                break
            except Exception as e:                # noqa: BLE001 - the port may not be free yet
                if attempt == 2:
                    raise
                self._log("!", f"reopen: {e}")
                time.sleep(1.0)
        self.reopened = True
        self._log("!", "port reopened")
        return dict(self.b.identity)

    def full_log(self) -> list[str]:
        return self.closed_logs + self.b.transcript(T0)

    def close_own(self) -> None:
        if self.reopened:
            self.b.close()


def fld(r, name: str, default: str = "") -> str:
    return str(r.fields.get(name, default)) if r.fields else default


def width_us(r) -> int | None:
    try:
        return int(fld(r, "t1")) - int(fld(r, "t0"))
    except ValueError:
        return None


def is_err(r, cmd: str, reason: str) -> bool:
    return (not r.ok) and r.cmd == cmd and r.reason == reason


def fitted_list(text: str) -> set[str]:
    return set() if text in ("", "-") else set(text.split(","))


def held_set(r) -> set[str]:
    return fitted_list(fld(r, "held").replace("+", ","))


# -- the picture ---------------------------------------------------------------------

REF = {"black": 10.0, "white": 230.0}   # luma of black and white, learned from the marker screen
GEOM: dict = {}                          # standard -> the last 25-row geometry seen in it
STATE = {"lost": None, "tr_seen": False, "program": False, "snaps": 0}
LAST: dict = {"view": None}


def luma(bgr) -> float:
    b, g, r = (float(v) for v in bgr)
    return 0.114 * b + 0.587 * g + 0.299 * r


def level(y: float) -> str:
    span = REF["white"] - REF["black"]
    if y < REF["black"] + 0.3 * span:
        return "black"
    if y > REF["white"] - 0.3 * span:
        return "white"
    return "mid"


@dataclass
class View:
    frame: object
    border: tuple = (0, 0, 0)
    bg: tuple = (0, 0, 0)
    std: str | None = None
    rows: int | None = None
    aspect: float | None = None
    cal: object = None

    @property
    def kind(self) -> str:
        if self.frame is None:
            return "no picture"
        return {24: "menu", 25: "screen"}.get(self.rows, "unknown")

    @property
    def border_level(self) -> str:
        return level(luma(self.border))

    @property
    def bg_level(self) -> str:
        return level(luma(self.bg))

    def describe(self) -> str:
        if self.frame is None:
            return "no picture"
        shape = f"{self.kind}, {self.std or '?'}" + (f", aspect {self.aspect:.3f}" if self.aspect else "")
        return f"{shape}, border {self.border_level} ({luma(self.border):.0f}), background {self.bg_level} ({luma(self.bg):.0f})"


def grab(timeout: float = 12.0):
    try:
        return video.next_frame(timeout=timeout)
    except CaptureError:
        return None


def fresh(delay: float = 0.0):
    """A frame grabbed at least ``delay`` seconds from now."""
    at = time.monotonic() + delay
    f = grab()
    while f is not None and f.at < at:
        try:
            f = video.next_frame(f, timeout=5.0)
        except CaptureError:
            return None
    return f


def probe(frame) -> View:
    if frame is None:
        return View(None)
    img = frame.image
    h, w = img.shape[:2]
    v = View(frame, border_of(img), dominant_color(img[h // 3:2 * h // 3, w // 3:2 * w // 3]))
    try:
        cal = find_text_area(img, LAYOUT_BASIC)
    except CalibrationError:
        return v
    v.cal, v.aspect = cal, cal.cell_h * 25 / (cal.cell_w * 40)
    err, std, rows = min((abs(v.aspect / (n * a / 40) - 1), s, n) for s, a in CELL_ASPECT.items() for n in (24, 25))
    if err <= ASPECT_TOLERANCE:
        v.std, v.rows = std, rows
        if rows == 25:
            GEOM[std] = cal
    return v


def wait_view(pred, timeout: float, agree: int = 2):
    """The first view ``pred`` holds on for ``agree`` polls in a row, or None."""
    end = time.monotonic() + timeout
    run = 0
    while True:
        v = probe(grab())
        LAST["view"] = v
        if v.frame is not None and pred(v):
            run += 1
            if run >= agree:
                return v
        else:
            run = 0
        if time.monotonic() > end:
            return None
        time.sleep(POLL_S)


def wait_border(want: str, timeout: float = 2.0, agree: int = 2) -> bool:
    """The border at ``want`` (black, white, mid) on ``agree`` frames in a row."""
    end = time.monotonic() + timeout
    run = 0
    f = grab()
    while f is not None:
        if level(luma(border_of(f.image))) == want:
            run += 1
            if run >= agree:
                return True
        else:
            run = 0
        if time.monotonic() > end:
            break
        try:
            f = video.next_frame(f, timeout=2.0)
        except CaptureError:
            break
    return False


def border_now() -> str:
    f = fresh(0.1)
    return "no picture" if f is None else level(luma(border_of(f.image)))


def snap(name: str, frame=None) -> None:
    frame = frame if frame is not None else grab()
    if frame is None:
        return
    STATE["snaps"] += 1
    small = cv2.resize(frame.image, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(artifacts / f"{STATE['snaps']:02d}-{name}.png"), small)


def text_box(cal) -> tuple[int, int, int, int]:
    left, top = cal.x0 - cal.cell_w / 32, cal.y0 - cal.cell_h / 32
    return int(round(left)), int(round(top)), int(round(cal.cell_w * 40)), int(round(cal.cell_h * 25))


def cell_lumas(image, cal) -> np.ndarray:
    """(25, 40) the mean luma of each cell's middle."""
    x, y, w, h = text_box(cal)
    gray = cv2.cvtColor(image[max(0, y):y + h, max(0, x):x + w], cv2.COLOR_BGR2GRAY).astype(np.float32)
    cells = cv2.resize(gray, (40 * 4, 25 * 4), interpolation=cv2.INTER_AREA).reshape(25, 4, 40, 4)
    return cells[:, 1:3, :, 1:3].mean(axis=(1, 3))


def marks(frame, cal, bg_y: float) -> set[str]:
    grid = cell_lumas(frame.image, cal)
    threshold = bg_y + 0.5 * (REF["white"] - bg_y)
    return {k for k in KEYS if grid[GRID_TOP + int(k[0]), GRID_LEFT + 2 * int(k[1])] > threshold}


def cell_swing(seconds: float, cal) -> np.ndarray:
    """(25, 40) each cell's luma swing over ``seconds``; zeros with fewer than two frames."""
    end = time.monotonic() + seconds
    grids = []
    f = grab()
    while f is not None and time.monotonic() < end:
        grids.append(cell_lumas(f.image, cal))
        try:
            f = video.next_frame(f, timeout=2.0)
        except CaptureError:
            break
    if len(grids) < 2:
        return np.zeros((25, 40), np.float32)
    stack = np.stack(grids)
    return stack.max(axis=0) - stack.min(axis=0)


def blink_activity(seconds: float, cal) -> float:
    """The largest luma swing of any cell over ``seconds``: the cursor blinking, or not."""
    return float(cell_swing(seconds, cal).max())


def at_basic(v: View) -> bool:
    """BASIC's screen: 25 rows with one cell blinking, the cursor. A TR+'s splash screen after a
    reset has 25 rows too, but nothing blinks, and its change to the menu swings most cells."""
    if v.kind != "screen" or v.cal is None:
        return False
    return 1 <= int((cell_swing(0.8, v.cal) > BLINK).sum()) <= 3


def basic_or_menu(v: View) -> bool:
    return v.kind == "menu" or at_basic(v)


# -- the C64 ----------------------------------------------------------------------

def need_c64() -> None:
    if video is None:
        raise Skip("no video capture on this station")
    if STATE["lost"]:
        raise Skip(f"the C64 is not at BASIC ({STATE['lost']})")


def type_text(text: str, row: Row | None = None, timing: str = ""):
    """Type ``text`` with S (or ST with ``timing`` "hold gap")."""
    line = f"ST {timing} {text}" if timing else f"S {text}"
    r = kb.cmd(line, timeout=6.0 + 0.2 * len(text))
    if not r.ok:
        raise C64Lost(f"{line.split()[0]} refused the text: {r.raw}")
    return r


SHIFTED = set('!"#$%&\'()[]<>?ABCDEFGHIJKLMNOPQRSTUVWXYZ')
SHIFTED_NAMES = {"clr", "inst", "up", "left", "run", "f2", "f4", "f6", "f8"}


def typed_keys(text: str) -> tuple[int, int]:
    """(keys, keys with a modifier) that S types for ``text``."""
    keys = modified = 0
    for name, ch in re.findall(r"\{([^}]*)\}|(.)", text):
        keys += 1
        if ch in SHIFTED or name in SHIFTED_NAMES or re.match(r"(ctrl|c=|shift)-", name or ""):
            modified += 1
    return keys, modified


def typing_time_ok(r, text: str, hold: int, gap: int) -> tuple[bool, str]:
    """Whether S/ST's t1-t0 fits its key timing: hold and gap per key, a modifier 20 ms either side."""
    n, mod = typed_keys(text)
    want = (n * hold + (n - 1) * gap + 40 * mod) * 1000
    w = width_us(r)
    ok = fld(r, "n") == str(n) and w is not None and want - 20_000 <= w <= want * 1.04 + 30_000
    return ok, f"{r.cmd} typed n={fld(r, 'n')} of {n} keys in {w} us, expected about {want} us"


def type_lines(lines) -> None:
    for line in lines:
        type_text(line + "{return}")


def is_marker(v: View) -> bool:
    return v.border_level == "black" and v.bg_level == "white"


def to_marker() -> View:
    """Black border, white background: what a reset is seen to undo."""
    type_text("poke53280,0:poke53281,1{return}")
    v = wait_view(is_marker, SETTLE_S)
    if v is None:
        raise C64Lost(f"a typed POKE did not take effect ({LAST['view'].describe()})")
    REF["black"], REF["white"] = luma(v.border), luma(v.bg)
    return v


def leave_menu() -> View:
    STATE["tr_seen"] = True
    for _ in range(2):
        time.sleep(1.0)
        type_text("{f2}")
        v = wait_view(at_basic, 12.0)
        if v is not None:
            return v
    raise C64Lost(f"the TR menu did not leave for BASIC on F2 ({LAST['view'].describe()})")


def reach_basic(why: str, marker: bool = True) -> View:
    """After a restart: BASIC's screen or the TR menu (left with F2), then the marker."""
    STATE["nmi_hook"] = False
    v = wait_view(lambda v: not is_marker(v) and basic_or_menu(v), BOOT_S)
    if v is None:
        raise C64Lost(f"after {why}: no BASIC screen or TR menu within {BOOT_S:.0f} s "
                      f"({LAST['view'].describe()})")
    snap(f"after-{re.sub(r'[^a-z0-9]+', '-', why.lower()).strip('-')}", v.frame)
    if v.kind == "menu":
        v = leave_menu()
    time.sleep(1.0)                      # the KERNAL prints READY. after it clears the screen
    return to_marker() if marker else v


def stop_restore_seq() -> str:
    return "X d 77;w 50;n 50;w 50;u 77"


def start_c64(row: Row) -> None:
    """From wherever the C64 is to BASIC with the marker up: a reset when /RESET is fitted,
    RUN/STOP+RESTORE when not, F2 when it lands in the TR menu."""
    v = probe(fresh())
    snap("start", v.frame)
    log(f"  the C64 at the start: {v.describe()}")
    if v.frame is None:
        raise C64Lost("no picture from the capture")
    if "reset" in FITTED:
        kb.cmd("R")
        v = reach_basic("the first reset", marker=False)
    elif v.kind == "menu":
        v = leave_menu()
    else:
        for _ in range(3):
            kb.cmd(stop_restore_seq())
            v = wait_view(lambda v: not is_marker(v) and basic_or_menu(v), 6.0)
            if v is not None:
                break
        if v is None:
            raise C64Lost(f"RUN/STOP+RESTORE did not bring up BASIC ({LAST['view'].describe()})")
        if v.kind == "menu":
            v = leave_menu()
    row.note(f"{v.std or 'unknown standard'} picture" + (", left the TR menu with F2" if STATE["tr_seen"] else ""))


def recover() -> bool:
    """One try at getting the C64 back to BASIC after it was lost."""
    if not STATE["lost"] or video is None:
        return not STATE["lost"]
    STATE["recoveries"] = STATE.get("recoveries", 0) + 1
    if STATE["recoveries"] > 2:
        return False
    log(f"  recovering the C64 ({STATE['lost']})")
    try:
        if "reset" in FITTED:
            kb.cmd("A")
            kb.cmd("R")
            reach_basic("a recovery reset")
        else:
            kb.cmd(stop_restore_seq())
            v = wait_view(basic_or_menu, 8.0)
            if v is not None and v.kind == "menu":
                leave_menu()
            to_marker()
        STATE["lost"] = None
        STATE["program"] = False
        return True
    except (C64Lost, Skip) as e:
        log(f"  recovery failed: {e}")
        return False


def restarted(row: Row, why: str) -> bool:
    """The C64 restarted (the marker gone, BASIC or the menu up), and is back at the marker."""
    reach_basic(why)
    row.note(f"{why}: restarted")
    return True


def measure_standard(row: Row, tag: str) -> str:
    """The standard by the picture's shape and by PEEK(678), which must agree."""
    type_text("poke53281,12:poke53280,peek(678){return}")
    v = wait_view(lambda v: v.kind == "screen" and v.bg_level == "mid" and v.border_level in ("black", "white"),
                  SETTLE_S)
    if v is None:
        raise C64Lost(f"{tag}: PEEK(678) did not show ({LAST['view'].describe()})")
    snap(f"standard-{tag}", v.frame)
    c64 = "PAL" if v.border_level == "white" else "NTSC"
    row.check(v.std == c64, f"{tag}: the picture is {v.std} but PEEK(678) says {c64}")
    return v.std or c64


# -- features: the board alone ------------------------------------------------------

def usb_product(serial: str) -> str | None:
    """The USB product string of the device with this serial string, without opening a port:
    pyserial's where it has one (not on Windows, for this board), else Windows' bus-reported
    description of the USB device."""
    if not re.fullmatch(r"[A-Za-z0-9_]+", serial):
        return None
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if (p.serial_number or "").upper() == serial.upper() and p.product:
                return p.product
    except Exception:                             # noqa: BLE001 - fall through to the next way
        pass
    if sys.platform != "win32":
        return None
    query = (f"Get-PnpDevice -PresentOnly | Where-Object {{ $_.InstanceId -like '*\\{serial}' }} | "
             "ForEach-Object { (Get-PnpDeviceProperty -InstanceId $_.InstanceId "
             "-KeyName DEVPKEY_Device_BusReportedDeviceDesc).Data }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", query],
                             capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = [line.strip() for line in out.stdout.splitlines() if line.strip()]
    return found[0] if found else None


def t_identity(row: Row) -> None:
    q = kb.cmd("?")
    F = q.fields or {}
    row.check(q.ok, f"? answered {q.raw}")
    row.check(F.get("proto") == "1", f"proto={F.get('proto')}")
    row.check(F.get("name") == "C64USBKeyboard", f"name={F.get('name')}")
    row.check(re.fullmatch(r"\d+\.\d+\.\d+", F.get("fw", "")), f"fw={F.get('fw')}")
    row.check(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", F.get("build", "")), f"build={F.get('build')}")
    row.check(F.get("chip", "").isdigit() and F.get("chip") == str(kb.b.chip), "chip= differs from the client's chip")
    row.check(F.get("mcu") in ("teensy41", "teensy36"), f"mcu={F.get('mcu')}")
    fitted = fitted_list(F.get("fitted", ""))
    row.check(fitted <= set(OUTPUTS), f"fitted= names {', '.join(sorted(fitted - set(OUTPUTS)))}")
    row.check(fitted == set(kb.b.fitted), "fitted= differs from the client's fitted")
    f = kb.cmd("F ?")
    row.check(f.ok and fitted_list(fld(f, "fitted")) == fitted, f"F ? answered {f.raw}")
    for out, name in (("reset", "reset"), ("tod", "tod"), ("kawari", "j1")):
        row.check((F.get(name) == "-") == (out not in fitted), f"{name}={F.get(name)} with {out} "
                  f"{'' if out in fitted else 'not '}fitted")
    row.check(F.get("loop") in ("on", "off") and F.get("e") in ("0", "1"), f"loop={F.get('loop')} e={F.get('e')}")
    if "tod" not in fitted:
        t = kb.cmd("T ?")
        row.check(t.ok and fld(t, "tod") == "-", f"T ? with tod not fitted answered {t.raw}")
    if "kawari" not in fitted:
        v = kb.cmd("V ?")
        row.check(v.ok and fld(v, "j1") == "-" and fld(v, "creset") == "-", f"V ? with kawari not fitted: {v.raw}")
    serial = getattr(kb.b, "usb_serial", None)
    if serial is None:
        row.note("no USB serial string: the board is not on a USB port")
    else:
        row.check(serial.upper() == f"C64USBKEYBOARD_{F.get('chip')}", f"USB serial string {serial}")
    product = usb_product(serial) if serial else None
    if product is None:
        row.note("the USB product string is not readable on this host")
    else:
        row.check(product == "C64 USB Keyboard", f"USB product string {product!r}")
    row.notes.insert(0, f"fw {F.get('fw')} on {F.get('mcu')}, fitted {F.get('fitted')}")


def t_clock(row: Row) -> None:
    a = kb.cmd("TIME")
    time.sleep(2.0)
    b = kb.cmd("TIME")
    row.check(a.ok and b.ok, f"TIME answered {a.raw} / {b.raw}")
    board_us = int(fld(b, "us", "0")) - int(fld(a, "us", "0"))
    host_us = (b.t_host - a.t_host) * 1e6
    row.check(board_us > 0, "TIME went backwards")
    ratio = board_us / host_us if host_us else 0.0
    row.check(abs(ratio - 1) < 0.01, f"the board clock ran {ratio:.4f} times host time")
    row.note(f"{ratio:.4f} times host time")


def raw_replies(data: bytes, seconds: float = 0.6) -> list[str]:
    return [x for x in kb.raw_lines(data, seconds) if x.startswith(("OK ", "ERR "))]


def t_lines(row: Row) -> None:
    r = kb.cmd("time")
    row.check(r.ok and r.cmd == "TIME", f"a lower-case command: {r.raw}")
    got = raw_replies(b"\n", 0.4)
    row.check(not got, f"an empty line was answered: {got}")
    got = raw_replies(b"TIME\r\n")
    row.check(len(got) == 1 and got[0].startswith("OK TIME"), f"CR before LF: {got}")
    got = raw_replies(b"dv", 0.8)
    row.check(not got, f"dv with no LF was answered: {got}")
    r = kb.cmd("?")
    row.check(r.ok and r.cmd == "?", f"? after a partial dv: {r.raw}")
    kb.raw(b"TI")
    time.sleep(0.25)
    got = raw_replies(b"ME\n")
    row.check(len(got) == 1 and got[0].startswith("OK TIME"), f"a line split by 250 ms: {got}")
    got = raw_replies(b"TIME" + b" " * 116 + b"\n")
    row.check(len(got) == 1 and got[0].startswith("OK TIME"), f"a 120-character line: {got}")
    for data, what in ((b"TIME" + b" " * 117 + b"\n", "a 121-character line"),
                       (b"TIME\x07\n", "a control byte"), (b"TIME\xe9\n", "a byte above 0x7E")):
        got = raw_replies(data)
        row.check(got == ["ERR * syntax"], f"{what}: {got}")
    row.note("case, CR, empty, partial dv, 120/121 characters, control and high bytes")


def t_errors(row: Row) -> None:
    cases = [("ZZ", "*", "syntax"), ("K 88 d", "K", "range"), ("K 12 x", "K", "syntax"),
             ("P 12 20000", "P", "range"), ("S a|b", "S", "range"), ("S {nope}", "S", "range"),
             ("N 0", "N", "range"), ("X w 20000", "X", "range"), ("X w 6000;w 6000", "X", "range"),
             ("X " + ";".join(["w 0"] * 17), "X", "range"), ("X q 1", "X", "syntax")]
    if "reset" in FITTED:
        cases += [("R 5", "R", "range"), ("R hold 0", "R", "range")]
    if "tod" in FITTED:
        cases += [("T 55", "T", "syntax")]
    if "kawari" in FITTED and "reset" in FITTED:
        cases += [("V maybe", "V", "syntax"), ("V open 5", "V", "range")]
    absent = [o for o in OUTPUTS if o not in FITTED]
    notfitted = {"tod": [("T 50", "T")], "menu": [("B menu", "B"), ("X b menu 50", "X")],
                 "alt": [("B alt", "B"), ("X b alt 50", "X")],
                 "reset": [("R", "R"), ("R hold", "R"), ("X r 50000", "X")], "kawari": [("V open", "V")]}
    for out in absent:
        cases += [(line, cmd, "notfitted") for line, cmd in notfitted[out]]
    for line, cmd, reason in cases:
        r = kb.cmd(line)
        row.check(is_err(r, cmd, reason), f"{line}: wanted ERR {cmd} {reason}, got {r.raw}")
    kb.cmd("A")                                   # in case one of them was taken
    row.note(f"{len(cases)} cases" + (f", notfitted for {', '.join(absent)}" if absent
                                      else "; every output is fitted, so no notfitted case"))


def t_events(row: Row) -> None:
    for e in ("1", "0"):
        r = kb.cmd(f"E {e}")
        q = kb.cmd("?")
        row.check(r.ok and fld(r, "e") == e and fld(q, "e") == e, f"E {e}: {r.raw}, ? e={fld(q, 'e')}")
    row.note("E 1 and E 0 answered; EV key lines come from the USB keyboard, not exercised")


# -- features: the C64 ---------------------------------------------------------------

def t_typing(row: Row) -> None:
    need_c64()
    start_c64(row)
    text = "poke53280,0:poke53281,1{return}"
    r = type_text(text)
    v = wait_view(is_marker, SETTLE_S)
    row.check(v is not None, f"S: the typed POKEs did not take effect ({LAST['view'].describe()})")
    if v is None:
        raise C64Lost("S: the typed POKEs did not take effect")
    REF["black"], REF["white"] = luma(v.border), luma(v.bg)
    snap("typing-s", v.frame)
    ok, how = typing_time_ok(r, text, 50, 50)
    row.check(ok, how)
    row.note(f"typed POKEs took effect; {how.split(', expected')[0]}")
    text = 'poke53280,asc("b")-65{return}'
    r = type_text(text, timing="40 40")
    row.check(wait_border("white", SETTLE_S), "ST: shifted characters typed with ST 40 40 did not take effect")
    ok, how = typing_time_ok(r, text, 40, 40)
    row.check(ok, how)
    type_text("poke53280,0{return}")
    row.check(wait_border("black", SETTLE_S), "S: the border did not go back to black")
    row.note("ST 40 40 with shifted keys")


def walk_start(row: Row) -> None:
    need_c64()
    STATE["program_tried"] = True
    type_lines(["new"] + list(MATRIX))
    type_text("run{return}")
    v = wait_view(lambda v: v.kind == "screen" and v.border_level == "black" and v.bg_level == "mid", SETTLE_S)
    if v is None:
        raise C64Lost(f"the matrix program did not start ({LAST['view'].describe()})")
    STATE["program"] = True
    STATE["program_bg"] = luma(v.bg)
    STATE["program_cal"] = v.cal or GEOM.get(v.std)
    found = marks(v.frame, STATE["program_cal"], STATE["program_bg"])
    row.check(not found, f"marks before any key: {sorted(found)}")
    snap("walk-start", v.frame)


def need_program(row: Row) -> None:
    """The matrix program running: started here once when the walk did not start it."""
    need_c64()
    if STATE["program"]:
        return
    if STATE.get("program_tried"):
        raise Skip("the matrix program is not running")
    walk_start(row)


def end_program() -> None:
    """RUN/STOP alone ends the matrix program; a typed POKE that sticks shows it ended."""
    kb.cmd(f"P 77 {HOLD_MS} 0")
    STATE["program"] = False
    type_text("poke53280,1{return}")
    if not wait_border("white", SETTLE_S):
        raise C64Lost("the matrix program did not end on RUN/STOP")


def read_marks(delay: float = 0.3) -> set[str]:
    f = fresh(delay)
    return set() if f is None else marks(f, STATE["program_cal"], STATE["program_bg"])


WALK_SEEN: set = set()
WALK_BAD: dict = {}


def walk_press(row: Row, key: str, press) -> None:
    """Press ``key`` (one or more keys, +-joined) with ``press``; the marks must grow by exactly those keys."""
    want = set(key.split("+"))
    early = want & WALK_SEEN
    wait_border("black", 2.0)                   # the scanner is back waiting for a key
    r = press()
    row.check(r.ok, f"{key}: {r.raw}")
    got = read_marks(0.3)
    if got - WALK_SEEN != want - early:
        got = read_marks(0.8)                    # the capture is a few frames behind
    new = got - WALK_SEEN
    for k in want:
        if k in early:
            WALK_BAD[k] = "marked before its own press"
        elif k not in new:
            WALK_BAD[k] = "not seen" if not (new - want) else f"seen as {'+'.join(sorted(new - want))}"
    WALK_SEEN.update(new)                        # a stray mark is reported once, not again with every key
    return r


def t_walk(row: Row) -> None:
    need_c64()
    walk_start(row)
    plain = [k for k in KEYS if k not in ("17", "12", "64", "77")]
    for i, k in enumerate(plain):
        r = walk_press(row, k, lambda k=k: kb.cmd(f"P {k} {HOLD_MS} 0"))
        if i == 0:
            w = width_us(r)
            row.check(w and HOLD_MS * 1000 - 1000 <= w <= HOLD_MS * 1000 + 5000, f"P {k} {HOLD_MS} 0 held {w} us")
    snap("walk-p")


def t_sequence(row: Row) -> None:
    need_program(row)
    for key, seq in X_KEYS.items():
        walk = ROWS["walk"] if not ROWS["walk"].result else row     # the walk's keys, unless it was left out
        r = walk_press(walk, key, lambda seq=seq: kb.cmd(f"X {seq}"))
        steps = seq.count(";") + 1
        row.check(r.ok and fld(r, "steps") == str(steps), f"X {seq}: {r.raw}")
        row.check(all(k in WALK_SEEN for k in key.split("+")), f"X {seq}: the C64 did not see {key}")
    w = None
    r = kb.cmd(f"X {X_KEYS['17+12']}")
    w = width_us(r)
    waits = HOLD_MS + 40
    row.check(w and waits * 1000 - 2000 <= w <= waits * 1000 + 20000, f"X with {waits} ms of waits took {w} us")
    row.note("SHIFT with a 20 ms lead and A, and R-SHIFT, seen in the matrix; the n and r steps are "
             "in RUN/STOP+RESTORE and /RESET")


def t_hold(row: Row) -> None:
    need_program(row)
    r = kb.cmd("K 12 d")
    row.check(r.ok, f"K 12 d: {r.raw}")
    row.check(held_set(kb.cmd("?")) == {"12"}, "? did not show held=12")
    row.check(wait_border("white"), "the C64 did not see A held")
    time.sleep(0.6)
    row.check(border_now() == "white", "A did not stay down while held")
    r = kb.cmd("K 12 u")
    row.check(r.ok, f"K 12 u: {r.raw}")
    row.check(held_set(kb.cmd("?")) == set(), "? still showed a held key")
    row.check(wait_border("black"), "the C64 still saw a key after K 12 u")
    r = kb.cmd("K 12 u")
    row.check(r.ok, f"K 12 u on an open switch: {r.raw}")
    kb.cmd("K 17+12 d")
    row.check(held_set(kb.cmd("?")) == {"17", "12"}, "? did not show held=17+12")
    row.check(wait_border("white"), "the C64 did not see SHIFT+A held")
    kb.cmd("K 17+12 u")
    row.check(wait_border("black"), "the C64 still saw keys after K 17+12 u")
    row.note("held, seen down on the C64, released")


def t_hold_timeout(row: Row) -> None:
    need_program(row)
    e = fld(kb.cmd("?"), "e")
    mark, t0 = kb.mark(), time.monotonic()
    row.check(kb.cmd("K 12 d 800").ok, "K 12 d 800 refused")
    row.check(wait_border("white", 0.7), "the C64 did not see A held")
    ev = kb.wait_event(r"^EV K 12 timeout", 3.0, mark)
    row.check(ev is not None, "no EV K 12 timeout within 3 s")
    if ev:
        dt = ev[0] - t0
        row.check(0.75 <= dt <= 1.0, f"EV K 12 timeout after {dt:.2f} s, not 0.8")
        row.note(f"released after {dt:.2f} s" + (" with E 0" if e == "0" else ""))
    row.check(wait_border("black"), "the C64 still saw A after the timeout")
    row.check(held_set(kb.cmd("?")) == set(), "? still showed a held key after the timeout")


def t_busy(row: Row) -> None:
    need_program(row)
    p = kb.start("ST 100 100 aaaaaaaaaa")
    time.sleep(0.3)
    for line in ("P 12", "K 12 d", "N", "D on", "S a", "X w 1"):
        r = kb.cmd(line)
        row.check(is_err(r, line.split()[0], "busy"), f"{line} while ST ran: {r.raw}")
    for line in ("?", "TIME", "T ?", "V ?", "F ?", "E 0"):
        r = kb.cmd(line)
        row.check(r.ok, f"{line} while ST ran: {r.raw}")
    r = kb.wait(p, 6.0)
    row.check(r.ok, f"ST after the busy replies: {r.raw}")
    ok, how = typing_time_ok(r, "aaaaaaaaaa", 100, 100)
    row.check(ok, how)
    kb.cmd("A")
    row.note("six commands busy, six queries answered")


def t_abort(row: Row) -> None:
    need_program(row)
    kb.cmd("K 12 d")
    row.check(wait_border("white"), "the C64 did not see A held")
    replies = kb.abort()
    row.check(replies and replies[-1].raw == "OK A", f"A: {[r.raw for r in replies]}")
    row.check(wait_border("black"), "the C64 still saw A after A")
    row.check(held_set(kb.cmd("?")) == set(), "? still showed a held key after A")
    p = kb.start("ST 100 100 " + "a" * 30)
    time.sleep(0.5)
    replies = kb.abort()
    raws = [r.raw for r in replies]
    row.check(len(replies) >= 2 and is_err(replies[0], "ST", "aborted") and replies[1].raw == "OK A",
              f"A during ST answered {raws}, not ERR ST aborted then OK A")
    try:
        mine = kb.wait(p, 1.0)
        row.check(is_err(mine, "ST", "aborted"), f"the aborted ST's own reply: {mine.raw}")
    except Exception as e:                      # noqa: BLE001 - the client may hand the ERR to abort() only
        row.note(f"the pending ST gave no reply of its own ({type(e).__name__})")
    row.check(wait_border("black"), "keys still down after the abort")
    row.note("a hold released, a running ST aborted with its ERR before OK A")


def t_loopback(row: Row) -> None:
    need_program(row)
    r = kb.cmd("D on")
    row.check(r.ok and fld(r, "loop") == "on", f"D on: {r.raw}")
    row.check(fld(kb.cmd("?"), "loop") == "on", "? did not show loop=on")
    row.check(wait_border("white"), "the C64 saw no key with the diagonal closed")
    r = kb.cmd("D off")
    row.check(r.ok and fld(r, "loop") == "off", f"D off: {r.raw}")
    row.check(wait_border("black"), "the C64 still saw keys after D off")
    kb.cmd("D on")
    row.check(wait_border("white"), "the C64 saw no key with the diagonal closed")
    kb.abort()
    row.check(fld(kb.cmd("?"), "loop") == "off", "A left the diagonal closed")
    row.check(wait_border("black"), "the C64 still saw keys after A")
    row.note("on, off, and A opens it")


def t_dtr(row: Row) -> None:
    need_program(row)
    kb.cmd("E 0")
    kb.cmd("K 12 d 9000")
    row.check(wait_border("white"), "the C64 did not see A held")
    seen = {}
    identity = kb.reopen(lambda: seen.setdefault("black", wait_border("black", 2.0)))
    row.check(seen.get("black"), "the C64 still saw A with the port closed")
    q = kb.cmd("?")
    row.check(held_set(q) == set(), "? showed a held key after the reopen")
    if "e" in identity:
        row.check(identity["e"] == "1", f"E was {identity['e']} after the port closed, not back to 1")
    else:
        row.note("E's return to 1 not checked: the client's identity has no e")
    row.note("a hold released by the port closing; E back to 1")


def walk_end(row: Row) -> None:
    """RUN/STOP alone: its mark, and the program ends (the KERNAL's interrupt back on)."""
    need_program(row)
    walk_press(row, "77", lambda: kb.cmd(f"P 77 {HOLD_MS} 0"))
    snap("walk-all")
    STATE["program"] = False
    type_text("poke53280,1{return}")
    row.check(wait_border("white", SETTLE_S), "the program did not end on RUN/STOP (a typed POKE did not stick)")
    time.sleep(1.0)
    row.check(border_now() == "white", "the program did not end on RUN/STOP")
    for k in KEYS:
        if k not in WALK_SEEN and k not in WALK_BAD:
            WALK_BAD[k] = "not seen"
    if WALK_BAD:
        bad = ", ".join(f"{k} {KEY_NAMES[k]} {why}" for k, why in sorted(WALK_BAD.items()))
        row.check(False, f"{len(WALK_BAD)} of 64 keys wrong: {bad}")
    else:
        row.note("64 of 64 keys seen at their own matrix position")


def t_restore(row: Row) -> None:
    need_c64()
    type_lines(NMI_HOOK)
    v = wait_view(lambda v: v.bg_level == "black" and v.border_level == "mid", SETTLE_S)
    if v is None:
        raise C64Lost(f"the NMI hook did not go in ({LAST['view'].describe()})")
    STATE["nmi_hook"] = True
    r = kb.cmd("N")
    w = width_us(r)
    row.check(r.ok and w and 49_500 <= w <= 51_000, f"N: {r.raw}")
    v = wait_view(lambda v: v.bg_level == "white", 3.0)
    row.check(v is not None, "N: no NMI seen (the background did not step once, black to white)")
    snap("restore-n", v.frame if v else None)
    r = kb.cmd("N 10")
    w = width_us(r)
    row.check(r.ok and w and 9_900 <= w <= 10_500, f"N 10: {r.raw}")
    v = wait_view(lambda v: v.bg_level != "white", 3.0)
    row.check(v is not None, "N 10: no second NMI seen")
    row.note("one NMI per pulse, 50 ms and 10 ms")


def t_stop_restore(row: Row) -> None:
    need_c64()
    type_text("poke53280,0{return}")
    if not wait_border("black", SETTLE_S):
        raise C64Lost("a typed POKE did not take effect before RUN/STOP+RESTORE")
    # The KERNAL's NMI handler reads RUN/STOP from $DC01 with whatever column $DC00 selects, so an
    # NMI that lands inside the IRQ's keyboard scan misses it: on a real keyboard too, now and then.
    # A second try is allowed and noted; the NMI hook, when in, tells which part was missed.
    v = None
    for attempt in (1, 2):
        before = probe(fresh())
        r = kb.cmd(stop_restore_seq())
        row.check(r.ok and fld(r, "steps") == "5", f"{stop_restore_seq()}: {r.raw}")
        v = wait_view(lambda v: v.border_level != "black" and v.kind == "screen", 5.0)
        if v is not None:
            if attempt == 2:
                row.note("warm-started on the second RUN/STOP+RESTORE")
            break
        after = probe(fresh())
        snap(f"stop-restore-missed-{attempt}", after.frame)
        if STATE.get("nmi_hook"):
            stepped = float(np.linalg.norm(np.subtract(after.bg, before.bg, dtype=np.float32))) > 25
            why = ("the NMI ran (the hook stepped the background) but RUN/STOP was not read" if stepped
                   else "no NMI (the hook did not step the background)")
        else:
            why = "the border stayed black"
        log(f"  RUN/STOP+RESTORE try {attempt} did not warm-start BASIC: {why}")
        if attempt == 1:
            row.note(f"the first RUN/STOP+RESTORE did not warm-start BASIC: {why}")
            time.sleep(0.5)
    row.check(v is not None, "RUN/STOP+RESTORE did not warm-start BASIC on two tries")
    if v is not None:
        STATE["nmi_hook"] = False
        snap("stop-restore", v.frame)
        moved = float(np.linalg.norm(np.subtract(v.bg, before.bg, dtype=np.float32)))
        row.check(moved > 25, "RUN/STOP+RESTORE left the background as it was")
        row.note("BASIC warm-started: screen cleared, colors back to the default")
    time.sleep(1.0)
    to_marker()


def t_tod(row: Row) -> None:
    need_c64()
    type_lines(["new"] + list(TOD_PROGRAM))
    for tod, h, e in ((50, 50, 10.0), (60, 60, 10.0), (50, 60, 8.33), ("off", 50, 0.0)):
        r = kb.cmd(f"T {tod}")
        row.check(r.ok and fld(r, "tod") == str(tod), f"T {tod}: {r.raw}")
        row.check(fld(kb.cmd("T ?"), "tod") == str(tod), f"T ? after T {tod}")
        type_text(f"h={h}:e={e:g}:goto10{{return}}")
        what = f"T {tod} with the CIA at {h} Hz"
        if not wait_border("mid", SETTLE_S):
            row.check(False, f"{what}: the program did not start")
            continue
        v = wait_view(lambda v: v.border_level in ("black", "white"), 16.0)
        snap(f"tod-{tod}-{h}", v.frame if v else None)
        if v is None:
            row.check(False, f"{what}: no result within 16 s")
        else:
            row.check(v.border_level == "white", f"{what}: TOD did not count {e:g} s in 10 s of TI")
    row.note("10 s at 50/50 and 60/60, 8.3 s at 50 Hz into a 60 Hz CIA, 0 when off")
    to_marker()


def t_reset(row: Row) -> None:
    need_c64()
    r = kb.cmd("R")
    w = width_us(r)
    row.check(r.ok and w and 49_900 <= w <= 50_500, f"R: {r.raw}")
    restarted(row, f"R ({w} us)")
    r = kb.cmd("R 100")
    w = width_us(r)
    row.check(r.ok and w and 100 <= w <= 200, f"R 100: {r.raw}")
    restarted(row, f"R 100 ({w} us)")
    r = kb.cmd("X w 10;r 50000;w 10")
    row.check(r.ok and fld(r, "steps") == "3", f"X w 10;r 50000;w 10: {r.raw}")
    restarted(row, "X r 50000")


def t_reset_hold(row: Row) -> None:
    need_c64()
    cal = GEOM.get(probe(fresh()).std)
    before = blink_activity(1.2, cal) if cal else 0.0
    r = kb.cmd("R hold")
    row.check(r.ok, f"R hold: {r.raw}")
    row.check(fld(kb.cmd("?"), "reset") == "1", "? did not show reset=1 while held")
    time.sleep(0.3)
    during = blink_activity(1.5, cal) if cal else 0.0
    v = probe(fresh())
    snap("reset-held", v.frame)
    row.check(is_marker(v), "the C64 restarted while /RESET was held")
    if before > BLINK:
        row.check(during < BLINK, f"the cursor kept blinking while /RESET was held (swing {during:.0f})")
    else:
        row.note("no cursor blink seen before the hold to compare")
    row.check(kb.cmd("R release").ok, "R release refused")
    row.check(fld(kb.cmd("?"), "reset") == "0", "? did not show reset=0 after R release")
    restarted(row, "R hold, R release")

    mark, t0 = kb.mark(), time.monotonic()
    row.check(kb.cmd("R hold 1000").ok, "R hold 1000 refused")
    ev = kb.wait_event(r"^EV R timeout", 3.0, mark)
    row.check(ev is not None, "no EV R timeout within 3 s")
    if ev:
        row.check(0.95 <= ev[0] - t0 <= 1.2, f"EV R timeout after {ev[0] - t0:.2f} s, not 1.0")
    restarted(row, "R hold 1000 timing out")

    kb.cmd("R hold")
    time.sleep(0.5)
    row.check(is_marker(probe(fresh())), "the C64 restarted while /RESET was held")
    replies = kb.abort()
    row.check(replies and replies[-1].raw == "OK A", f"A: {[x.raw for x in replies]}")
    restarted(row, "R hold, A")

    kb.cmd("R hold")
    time.sleep(0.5)
    kb.reopen()
    row.check(fld(kb.cmd("?"), "reset") == "0", "? showed reset=1 after the port closed")
    restarted(row, "R hold, port closed")


def v_switch(row: Row, target: str, how: str) -> None:
    restart_s = 1.0
    if how == "plain":
        r = kb.cmd(f"V {target}", timeout=restart_s + 5)
        w = width_us(r)
        row.check(r.ok and fld(r, "j1") == target, f"V {target}: {r.raw}")
        row.check(w and 1_000_000 <= w <= 1_020_000, f"V {target} held /RESET {w} us, not about 1.0 s")
    elif how == "abort":
        sent = time.monotonic()
        p = kb.start(f"V {target}")
        time.sleep(0.3)
        replies = kb.abort()
        raws = [x.raw for x in replies]
        row.check(len(replies) >= 2 and is_err(replies[0], "V", "aborted") and replies[1].raw == "OK A",
                  f"A during V answered {raws}, not ERR V aborted then OK A")
        if replies and replies[0].t_host is not None:
            row.check(replies[0].t_host - sent >= 0.95 * restart_s,
                      f"V was cut short: ERR V aborted {replies[0].t_host - sent:.2f} s after V")
        try:
            kb.wait(p, 1.0)
        except Exception:                       # noqa: BLE001 - the ERR may have gone to abort() only
            pass
    else:
        kb.start(f"V {target}")
        time.sleep(0.3)
        kb.reopen(lambda: time.sleep(restart_s + 0.5))
    q = kb.cmd("V ?")
    row.check(fld(q, "j1") == target and fld(q, "creset") == "released", f"V ? after V {target} ({how}): {q.raw}")
    reach_basic(f"V {target}" + ("" if how == "plain" else f" ({how})"), marker=False)


def station_kawari_open() -> str | None:
    for path in (("keyboard", "kawari_open"), ("kawari_open",)):
        obj = station
        for name in path:
            obj = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if isinstance(obj, str) and obj.upper() in ("PAL", "NTSC"):
            return obj.upper()
    return None


def t_kawari(row: Row) -> None:
    need_c64()
    j1_0 = START.get("j1")
    if j1_0 not in ("open", "closed"):
        raise Skip(f"? reported j1={j1_0}")
    other = {"open": "closed", "closed": "open"}[j1_0]
    q = kb.cmd("V ?")
    row.check(fld(q, "j1") == j1_0 and fld(q, "creset") == "released", f"V ? at the start: {q.raw}")
    # The Kawari reads J1 only when it reloads, so the picture at the start can be the other
    # standard (J1 released by a board restart since). The switches are judged against each other.
    s0 = measure_standard(row, f"j1 {j1_0}")
    got: dict[str, list[str]] = {}
    seen = [f"{j1_0} {s0} before any V"]
    for target, how in ((other, "plain"), (j1_0, "plain"), (other, "abort"), (j1_0, "close")):
        v_switch(row, target, how)
        s = measure_standard(row, f"j1 {target} {how}")
        got.setdefault(target, []).append(s)
        seen.append(f"{target} {s}")
    for setting, stds in got.items():
        row.check(len(set(stds)) == 1, f"V {setting} gave {' then '.join(stds)}, not the same each time")
    row.check(got[other][0] != got[j1_0][0], f"J1 {other} and J1 {j1_0} both gave {got[j1_0][0]}")
    known = station_kawari_open()
    if known:
        row.check(got["open"][0] == known, f"J1 open gave {got['open'][0]}; the station says {known}")
    to_marker()
    row.note("J1 " + ", ".join(seen) + "; A and a port close mid-V both finished the switch")
    if s0 != got[j1_0][0]:
        row.note(f"the C64 started in {s0} although J1 {j1_0} gives {got[j1_0][0]} (the Kawari had not "
                 f"reloaded since J1 changed); J1 is back {j1_0}, so it is in {got[j1_0][0]} now")


def t_button(row: Row, which: str) -> None:
    r = kb.cmd(f"B {which}")
    w = width_us(r)
    row.check(r.ok and w and 49_500 <= w <= 51_000, f"B {which}: {r.raw}")
    if which == "menu" and STATE["tr_seen"] and video is not None and not STATE["lost"]:
        v = wait_view(lambda v: v.kind == "menu", 10.0)
        row.check(v is not None, "B menu: the TR menu did not come up")
        if v is not None:
            snap("button-menu", v.frame)
            leave_menu()
            to_marker()
    else:
        row.note("reply only: what the TR does with the button is not watched"
                 + ("" if STATE["tr_seen"] else " (no TR seen)"))


def t_usb_keyboard(row: Row) -> None:
    raise Skip("needs a person typing on the USB keyboard")


# -- running it -----------------------------------------------------------------------

def run(key: str, fn, *args) -> None:
    row = ROWS[key]
    if row.result:
        return
    if key in SKIPPED:
        row.skip("left out (--skip)")
        return
    missing = [o for o in row.needs if o not in FITTED]
    if missing:
        row.skip("not fitted: " + ", ".join(missing))
        return
    if row.on_c64 and STATE["lost"] and key not in ("usb_keyboard",):
        recover()
    log(f"--- {row.label}")
    t0 = time.monotonic()
    try:
        fn(row, *args)
    except Skip as e:
        if row.tested:
            row.note(str(e))
        else:
            row.skip(str(e))
    except C64Lost as e:
        row.check(False, str(e))
        STATE["lost"] = str(e)
        STATE["program"] = False
        snap(f"lost-{key}", LAST["view"].frame if LAST["view"] else None)
    except Exception as e:                        # noqa: BLE001 - one feature's failure is its row's
        row.check(False, f"{type(e).__name__}: {e}")
        (artifacts / f"{key}.traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        row.seconds += time.monotonic() - t0


def put_back() -> dict:
    """J1, TOD and nothing held, as ``?`` reported them at the start."""
    done = {}
    try:
        kb.cmd("A")
        if "tod" in FITTED and START.get("tod") not in (None, "-"):
            if fld(kb.cmd("T ?"), "tod") != START["tod"]:
                kb.cmd(f"T {START['tod']}")
        if "kawari" in FITTED and "reset" in FITTED and START.get("j1") in ("open", "closed"):
            if fld(kb.cmd("V ?"), "j1") != START["j1"]:
                kb.cmd(f"V {START['j1']}", timeout=8.0)
        q = kb.cmd("?")
        done = {k: fld(q, k) for k in ("held", "reset", "tod", "j1", "loop", "fitted")}
        same = {k: done[k] == START.get(k) for k in ("tod", "j1", "fitted")}
        same["held"] = done["held"] == "-"
        if not all(same.values()):
            log(f"WARNING: not as found: {done} (at the start: {START})")
        done["as_found"] = all(same.values())
    except Exception as e:                        # noqa: BLE001 - say so, and leave the rest of the report
        log(f"WARNING: putting the board back failed: {type(e).__name__}: {e}")
        done["error"] = str(e)
    return done


print(SETUP, flush=True)
args = sys.argv[1:]
SKIPPED: set[str] = set()
taking = False
for a in args:
    if a.startswith("--"):
        taking = a == "--skip"
    elif taking:
        SKIPPED |= {x for x in a.split(",") if x}
unknown = SKIPPED - set(ROWS)
if unknown:
    raise SystemExit(f"--skip: no feature {', '.join(sorted(unknown))}; features are {', '.join(ROWS)}")

if globals().get("kbd") is None:
    raise SystemExit("no keyboard board on this station: run under treadmill exec --no-link with the board connected")
if globals().get("video") is None:
    video = None
kb = Kb(kbd)
START = dict(kb.cmd("?").fields or {})
FITTED = fitted_list(START.get("fitted", ""))
for row in ROWS.values():
    row.fitted = ("yes" if all(o in FITTED for o in row.needs) else "no") if row.needs else "PCB"
log(f"keyboard board {START.get('fw')} on {START.get('mcu')}, fitted {START.get('fitted')}, "
    f"j1={START.get('j1')} tod={START.get('tod')}; video {'yes' if video is not None else 'none'}")

restored: dict = {}
try:
    run("identity", t_identity)
    run("clock", t_clock)
    run("lines", t_lines)
    run("errors", t_errors)
    run("events", t_events)
    run("typing", t_typing)
    run("walk", t_walk)
    run("sequence", t_sequence)
    run("hold", t_hold)
    run("hold_timeout", t_hold_timeout)
    run("busy", t_busy)
    run("abort", t_abort)
    run("loopback", t_loopback)
    run("dtr", t_dtr)
    if ROWS["walk"].tested and not ROWS["walk"].result:
        t0 = time.monotonic()
        try:
            walk_end(ROWS["walk"])
        except (Skip, C64Lost) as e:
            ROWS["walk"].check(False, str(e))
            STATE["lost"] = STATE["lost"] or str(e)
        finally:
            ROWS["walk"].seconds += time.monotonic() - t0
    elif STATE["program"]:
        try:
            end_program()
        except C64Lost as e:
            STATE["lost"] = str(e)
    run("restore", t_restore)
    run("stop_restore", t_stop_restore)
    run("tod", t_tod)
    run("reset", t_reset)
    run("reset_hold", t_reset_hold)
    run("kawari", t_kawari)
    run("menu", t_button, "menu")
    run("alt", t_button, "alt")
    run("usb_keyboard", t_usb_keyboard)
finally:
    restored = put_back()
    for row in ROWS.values():
        row.close()
    print("\n" + table(90))
    (artifacts / "transcript.txt").write_text("\n".join(kb.transcript) + "\n", encoding="utf-8")
    (artifacts / "kbd-full.log").write_text("\n".join(kb.full_log()) + "\n", encoding="utf-8")
    (artifacts / "features.txt").write_text(table(None) + "\n", encoding="utf-8")
    report = {"rows": [r.record() for r in ROWS.values()], "board": START, "restored": restored,
              "tr_seen": STATE["tr_seen"], "skipped": sorted(SKIPPED)}
    (artifacts / "features.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    kb.close_own()
    print(f"\nboard put back as found: {restored.get('as_found')}; artifacts in {artifacts}")

result = {"rows": [r.record() for r in ROWS.values()], "board": START, "restored": restored}
if any(r.result == "FAIL" for r in ROWS.values()):
    raise SystemExit(1)
