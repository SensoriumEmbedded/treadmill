"""A TeensyROM main image at the far end of an in-process pipe, answering the
commands as ``docs/ControlComms.md`` describes them. The self-tests run the
link against it with no hardware; it is the Python counterpart of the MCP
server's ``tools/mcp/test/fake-board.mjs`` and models the same details,
including the two 32-bit stats the firmware appends after a menu-only
command."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field

from selftest.fake_menu import TREE, UP_DIR, FakeMenu
from selftest.fake_programs import ExpPort, ExtPorts, ReuChecker
from treadmill.c64.screen import (
    KEYBUF, KEYBUF_SIZE, KEYCOUNT, REVERSE_VIDEO, SCREEN_BYTES, SCREEN_COLUMNS, SCREEN_RAM_DEFAULT, SCREEN_ROWS,
    VIC_MEMPTR, codes_for_text,
)
from treadmill.knobs import KNOBS, standard_of
from treadmill.link.banner import parse_banner
from treadmill.mfg import IGETIN, JIFFY_CLOCK, LED_ROW
from treadmill.link.protocol import (
    ACK, C64_PAUSE_OFF, C64_PAUSE_ON, DELETE_FILE, DIR_END, DIR_START, DRIVE_TEENSY, DRIVE_USB, FAIL, FW_CHECK,
    FW_FULL, FW_MINIMAL, GET_DIR_NDJSON, GET_FILE, GOOD_SID, LAUNCH_FILE, MENU_ONLY, PING, POST_FILE,
    READ_C64_MEM, RESET_C64, VERSION_INFO, WRITE_C64_MEM, board_reply, checksum,
)
from treadmill.link.transport import PipeEnd, open_pipe

WAIT_S = 2.0
LETTER_ARG_S = 0.2              # a bare 't': the firmware's read() finds nothing and lists at once
DISCARD_QUIET_S = 1.0           # DiscardCmdInput: twice SerialTimoutMillis of nothing arriving
STATS_TRAILER = bytes([1, 0, 0, 0, 0x00, 0x2C, 0xA3, 0x30])
BANNER = ("\n  FW: TeensyROM+ v0.9\r\n      Oct 30 2026, 10:00:00\r\n"
          "  Teensy: 816MHz  55.0C  UID: 19277260\r\n  C64  NTSC Vid  60 Hz\n  Boot: complete\n")
BANNER_TR = BANNER.replace("TeensyROM+", "TeensyROM")
TR_BASIC_ITEM = "BASIC TR Command IO access only"      # the menu item that leaves BASIC under the TR BASIC handler
READY_LINE = "TeensyROM+ v0.9 ready!\n"
# Commands the firmware answers whatever else it is doing (ProcessCommand's first switch).
ALWAYS = {FW_CHECK, VERSION_INFO, RESET_C64, LAUNCH_FILE, C64_PAUSE_ON, C64_PAUSE_OFF,
          READ_C64_MEM, WRITE_C64_MEM}
# The timing knobs at the firmware's NTSC C64 defaults, by their 't' letter (treadmill.knobs).
KNOB_DEFAULTS = {k.letter: k.default("NTSC") for k in KNOBS.values()}
# idread.a as the fake runs it: passes a second, and the share of a path's reads that go wrong
# per nanosecond the sample point lies outside that path's clean window.
IDREAD_RESULTS = 0xC000
IDREAD_PASSES_PER_S = 1500.0
IDREAD_WRONG_PER_NS = 0.02
REU_ROUNDS_PER_S = 142.0          # the bench's rate: about 1,420 rounds in 10 s
REU_NAMES = ("512KB REU", "REU+USB Mouse/Disk")
# The built-in programs the manufacturing test launches, by the name ending their path.
PROGRAMS = {"TeensyROM External Ports Test": ExtPorts, "TR+ C64 Expansion Port Test": ExpPort,
            "REU-Checker v1.0            +REU": ReuChecker}
# Hot keys at the EEPROM defaults on a TR+ (Teensy.ino): 2 is the REU checker.
HOT_KEYS = {2: "/Test+Diags/REU-Checker v1.0            +REU"}
KERNAL_GETIN = (0xF13E).to_bytes(2, "little")     # IGETIN as RESTOR sets it
LED_QUESTION_ROW = SCREEN_RAM_DEFAULT + LED_ROW * SCREEN_COLUMNS
LED_QUESTION_MARK = codes_for_text("Y/N")


@dataclass
class C64Loop:
    """reuclient.a's or cpuwatch.a's counters at $C110 (clean rounds, bad
    rounds), advanced by the clock: every round of the REU client is bad
    without an REU at Special IO, and a share of either's while the DMA
    knobs are outside their windows."""
    last: float
    needs_reu: bool = True
    rounds: float = 0.0
    bad: float = 0.0
    collapse_ns: int | None = None    # this far outside, the client crashes and counts nothing more
    crashed: bool = False

    def advance(self, now: float, reu: bool, dma_outside_ns: int) -> None:
        reu = reu or not self.needs_reu
        dt = max(0.0, now - self.last)
        self.last = now
        if self.collapse_ns is not None and dma_outside_ns > self.collapse_ns:
            self.crashed = True
        if self.crashed:
            return
        n = dt * REU_ROUNDS_PER_S
        share = 1.0 if not reu else min(1.0, dma_outside_ns * IDREAD_WRONG_PER_NS)
        self.bad += n * share
        self.rounds += n * (1.0 - share)

    def bytes(self) -> bytes:
        good, bad = int(self.rounds) & 0xFFFF, int(self.bad) & 0xFFFF
        return bytes([good & 0xFF, good >> 8, bad & 0xFF, bad >> 8])


@dataclass
class IdRead:
    """The counters idread.a would leave at $C000, advanced by the clock each
    time they are read: a path's wrong reads accrue only while the sample
    point is outside its clean window."""
    last: float
    passes: float = 0.0
    rom_wrong: float = 0.0
    io_wrong: float = 0.0

    def advance(self, now: float, rom_outside_ns: int, io_outside_ns: int) -> None:
        dt = max(0.0, now - self.last)
        self.last = now
        passes = dt * IDREAD_PASSES_PER_S
        self.passes += passes
        self.rom_wrong += passes * 512 * min(1.0, rom_outside_ns * IDREAD_WRONG_PER_NS)
        self.io_wrong += passes * 256 * min(1.0, io_outside_ns * IDREAD_WRONG_PER_NS)

    def bytes(self) -> bytes:
        passes = int(self.passes) & 0xFFFF
        rom, io = int(self.rom_wrong) & 0xFFFFFF, int(self.io_wrong) & 0xFFFFFF
        return bytes([passes & 0xFF, passes >> 8, rom & 0xFF, (rom >> 8) & 0xFF, rom >> 16,
                      io & 0xFF, (io >> 8) & 0xFF, io >> 16])


@dataclass
class FakeBoard:
    image: str = "main"                  # main | minimal
    banner: str = BANNER
    busy: bool = False                   # a launched program's handler is active
    silent: bool = False                 # an extension image: answers nothing
    deaf: bool = False                   # TR BASIC's handler: the port's bytes go to the C64 program until a reset
    dma: bool = True                     # a TR+; a plain TR has no DMA commands at all
    memory: bytearray = field(default_factory=lambda: bytearray(b"\x20" * 0x10000))
    files: dict[str, bytes] = field(default_factory=dict)
    directories: list[str] = field(default_factory=lambda: ["/"])
    listing: list[dict] = field(default_factory=list)
    launched: list[tuple[int, str]] = field(default_factory=list)
    received: list[int] = field(default_factory=list)
    chatter_on_launch: str = "Loading IO handler: TeensyROM\n"
    boot_polls: int = 0                  # how many version polls say "in progress" after a reset
    booting_for: int = 0
    startup_sid: bool = False            # the menu's startup SID, loaded partway through a boot, still to report
    heartbeat: bool = False              # a launched hello.a is ticking its bottom-right cell
    flooding: bool = False               # a line printed in a loop (flood())
    dma_bit_once: tuple = ()             # screen cells the next DMA read of the screen returns with bit 1 low
    born: float = field(default_factory=time.monotonic)     # power-on, for faults timed from it
    menu_draws_by_item: bool = False     # FakeMenu.draw_by_item from the first redraw on
    menu: object = field(default=None, repr=False)
    # The bus timing model: the firmware samples the select lines at max(nS_PLAprop, nS_RWnReady),
    # both counted from the cycle's start, and a path's reads come back right only while that point
    # is inside its window. IO goes wrong first both ways: its selects settle later (a decoder after
    # the PLA) and its data leaves later (after both ROM tests and a handler call). Outside the IO
    # window the menu shows wrong item-type marks and idread counts wrong IO2 reads.
    windows: dict = field(default_factory=lambda: {"io": (142, 210), "rom": (130, 230)})
    # The other CPU-cycle knobs, each with a window of its own: outside any of them a path's reads go
    # wrong too. nS_DataSetup latches the C64's writes, so it counts against IO (idread's IO2 page) only.
    knob_windows: dict = field(default_factory=lambda: {"s": (160, 330), "h": (330, 460), "m": (930, 1100)})
    # The DMA knobs' windows: outside any of them the board's own DMA (z###, the REU) goes wrong. Wide
    # for nS_DMADataHold and nS_DMADataSetup, so that nS_DMASetup with the sums held decides alone.
    dma_windows: dict = field(default_factory=lambda: {"e": (380, 480), "w": (360, 520), "y": (300, 480),
                                                       "a": (0, 300), "b": (100, 330)})
    # Below this nS_DMASetup the DMA drives into the VIC's half of the cycle: stray cells on screen, a
    # cell for every ns, before the data goes wrong at the window's edge (as PR #99's sweeps showed).
    vic_noise_below: int = 400
    idread: IdRead | None = field(default=None, repr=False)
    c64loop: C64Loop | None = field(default=None, repr=False)
    special_io: str = "None"             # the Settings page's Special IO: what a launched PRG runs beside
    # The manufacturing test's side: the program running and what breaks it (fake_programs.py), the
    # board's power, and the serve loop held while a program services nothing (the External Ports wait).
    faults: dict = field(default_factory=dict)
    program: object = field(default=None, repr=False)
    powered: bool = True
    buttons_pending: bool = False
    led_answers: list[str] | None = None  # what the operator types for each LED question still to come
    led_asked: bool = False               # a question is on screen, and its answer comes once the buffer is emptied
    key_on_screen_read: str = ""          # pressed on the C64 when a read of VIC_MEMPTR comes next
    unblocked: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    end: PipeEnd | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # A C64 at the TeensyROM menu: screen at $0400, lower/upper charset, VIC bank 0, no key waiting.
        self.memory[0xD018] = 0x17
        self.memory[0xDD00] = 0x97
        self.cold_start()
        self.unblocked.set()
        self.menu = FakeMenu(self)       # draws the Teensy Mem root into screen RAM
        self.menu.draw_by_item = self.menu_draws_by_item

    def version_string(self) -> str:
        return parse_banner(self.banner).version or "TeensyROM"

    def run(self, path: str) -> None:
        """What a launched program does to the screen: hello.a paints its text
        and starts its heartbeat; anything else leaves the screen as the menu
        left it. Either way the board is busy until a reset."""
        self.busy = True
        if path.rpartition("/")[2] == TR_BASIC_ITEM:
            self.deaf = True
            return
        program = PROGRAMS.get(path.rpartition("/")[2])
        if program is not None:
            self.program = program(self)
            self.program.start()
            return
        if path.lower().endswith("idread.crt"):
            self.idread = IdRead(time.monotonic())
        name = path.lower().rpartition("/")[2]
        if "reuloop" in name or "cpuwatch" in name:
            self.c64loop = C64Loop(time.monotonic(), needs_reu="reuloop" in name,
                                   collapse_ns=self.faults.get("reu_collapse_ns"))
            self.memory[0xC104] = 0x5A
        if path.lower().endswith("hello.prg"):
            self.memory[SCREEN_RAM_DEFAULT:SCREEN_RAM_DEFAULT + SCREEN_BYTES] = b"\x20" * SCREEN_BYTES
            self.memory[0xD018] = 0x17
            text = codes_for_text("treadmill hello")
            at = SCREEN_RAM_DEFAULT + 12 * 40 + 12
            self.memory[at:at + len(text)] = text
            self.heartbeat = True

    debug_defines: set = field(default_factory=set)   # the Dbg_* defines this build carries
    letters: list[str] = field(default_factory=list) # every letter command received
    knobs: dict = field(default_factory=lambda: dict(KNOB_DEFAULTS))

    # -- the manufacturing test's programs and hands --------------------------

    def say(self, text: str) -> None:
        """Unprompted serial text, from any thread."""
        if self.end is not None:
            self.end.write(text.encode("latin-1"))

    def show(self, rows: list[str], reverse: set[int] = frozenset()) -> None:
        """A program's screen: these rows, the rest blank, lower/upper case."""
        self.memory[0xD018] = 0x17
        codes = bytearray()
        for r in range(SCREEN_ROWS):
            row = codes_for_text((rows[r] if r < len(rows) else "").ljust(SCREEN_COLUMNS)[:SCREEN_COLUMNS])
            codes += bytes(c | REVERSE_VIDEO for c in row) if r in reverse else row
        self.memory[SCREEN_RAM_DEFAULT:SCREEN_RAM_DEFAULT + SCREEN_BYTES] = codes

    def hot_key(self, number: int) -> None:
        """The menu's 1-5: launch the path the settings hold for it."""
        path = self.faults.get(f"hot_key_{number}", HOT_KEYS.get(number))
        if path is None:
            return
        folder, _, name = path.rpartition("/")
        kb = self.faults.get("reu_kb", 512)
        handler = f"Loading IO handler: {kb}KB REU\nREU Size: {kb}K bytes\n" if name in PROGRAMS and \
            PROGRAMS[name] is ReuChecker else "Loading IO handler: TeensyROM\n"
        self.say(f"Remote Launch:\nP: {folder or '/'}\nF: {name}\n{handler}")
        self.run(path)

    def cold_start(self) -> None:
        """What the menu cartridge's cold start leaves that a test can see: no
        key waiting, GETIN through the KERNAL (it clears $0002-$03FF and runs
        RESTOR)."""
        self.memory[KEYCOUNT] = 0
        self.memory[IGETIN:IGETIN + 2] = KERNAL_GETIN

    def getin_live(self) -> bool:
        """A program's GETIN takes keys from the buffer: IGETIN is the KERNAL's."""
        return bytes(self.memory[IGETIN:IGETIN + 2]) == KERNAL_GETIN

    def type_keys(self, text: str) -> None:
        """Keys the operator presses, as the letters on them."""
        self.press_keys(text.upper().encode("latin-1"))

    def press_keys(self, codes: bytes) -> None:
        """Keys pressed on the C64's keyboard, as the GETIN codes they give:
        the IRQ's scan queues each one (ten at most), and a GETIN loop takes
        it at once."""
        with self.lock:
            for code in codes:
                if self.getin_live() and (self.program is not None or not self.busy):
                    (self.program or self.menu).key(code)
                elif self.memory[KEYCOUNT] < KEYBUF_SIZE:
                    self.memory[KEYBUF + self.memory[KEYCOUNT]] = code
                    self.memory[KEYCOUNT] += 1

    def watch_led_questions(self) -> None:
        """The operator reads the LED questions from now on, answering each as
        ``faults["leds"]`` says (Y to both by default); past its end, nothing."""
        self.led_answers = list(self.faults.get("leds", ["y", "y"]))

    def return_to_menu(self, sensed: bool = True) -> None:
        """What BtnPressed does in loop(): the menu's handler back, the C64
        reset. Not ``sensed``: the C64 reset by its own button with the TR+
        none the wiser, so nothing on serial."""
        if sensed:
            self.say("Button detected\nLoading IO handler: TeensyROM\nResetting C64\n")
        self.flooding = False
        with self.lock:                  # a program drawing from its own thread stops before the menu shows
            self.program = None
            self.busy = False
            self.deaf = False
            self.heartbeat = False
            self.idread = None
            self.c64loop = None
            self.booting_for = self.boot_polls
            self.cold_start()
            self.menu.reset()

    def press_buttons(self) -> None:
        """The operator's MENU then ALT, for the External Ports test; pressed
        before the test is up, they count once it is. They take
        ``faults["buttons_after_s"]`` to get to them."""
        delay = self.faults.get("buttons_after_s")
        if delay:
            timer = threading.Timer(delay, self._press_buttons)
            timer.daemon = True
            timer.start()
        else:
            self._press_buttons()

    def _press_buttons(self) -> None:
        with self.lock:
            if isinstance(self.program, ExtPorts) and self.program.waiting:
                self.program.buttons()
            else:
                self.buttons_pending = True

    def set_video(self, standard: str) -> None:
        """The C64 now runs ``standard`` (a Kawari reloaded): the banner's
        video line says so from the next version reply on."""
        line = {"PAL": "PAL Vid  50 Hz", "NTSC": "NTSC Vid  60 Hz"}[standard]
        self.banner = re.sub(r"(PAL|NTSC) Vid  \d\d Hz", line, self.banner)

    def external_reset(self) -> None:
        """A reset the board did not make: sensed unless the fault says not.
        The C64 resets either way, so its screen goes."""
        self.return_to_menu(sensed=self.faults.get("reset_sense", True))

    def flood(self, line: str) -> None:
        """The firmware printing ``line`` in a loop, as PollingHndlr does for a
        status the C64 keeps writing, until the C64 is reset or powered off."""
        self.flooding = True

        def run():
            while self.flooding and self.powered:
                self.say(line)
                time.sleep(0.003)
        threading.Thread(target=run, name="fake-board-flood", daemon=True).start()

    def power_off(self) -> None:
        self.powered = False
        self.unblocked.set()
        if self.end is not None:
            self.end.close()

    def outside(self, path: str) -> int:
        """How far, in ns, the sample point lies outside ``path``'s clean window; 0 inside it."""
        low, high = self.windows[path]
        point = max(self.knobs["p"], self.knobs["r"])
        worst = max(low - point, point - high, 0)
        for letter, (lo, hi) in self.knob_windows.items():
            if letter != "s" or path == "io":
                worst = max(worst, lo - self.knobs[letter], self.knobs[letter] - hi)
        return worst

    def vic_glitches(self) -> int:
        """How many screen cells the capture shows wrong in a frame."""
        return max(0, self.vic_noise_below - self.knobs["e"])

    def dma_outside(self) -> int:
        """How far, in ns, the DMA knobs lie outside their windows; 0 inside all."""
        return max(0, *(max(lo - self.knobs[k], self.knobs[k] - hi) for k, (lo, hi) in self.dma_windows.items()))

    def _z_report(self, pages: int) -> str:
        """z###'s serial report (StatusFunctions.c TestDMAPattern): the
        control pattern stays clean, the other four go wrong outside the
        DMA windows."""
        k = self.knobs
        total = pages * 256
        bad = int(total * min(1.0, self.dma_outside() * IDREAD_WRONG_PER_NS))
        lines = [f"\nDMA pattern test: DMADataHold={k['w']} DMADataSetup={k['y']} DMASetup={k['e']} "
                 f"Passes={pages}\n  (overwrites C64 $c000-$c0ff)\n"]
        for i, (head, prior) in enumerate((("$ff", "ff"), ("$ff", "00"), ("$00", "ff"), ("$00/$ff alt", "00"),
                                           ("$55/$aa alt", "ff"))):
            n = 0 if i == 0 else bad
            lines.append(f"{head} over ${prior} @ $c000: {n} bad of {total} bytes" + ("  CLEAN\n" if n == 0 else "\n"))
        return "".join(lines)

    def accrue(self) -> None:
        """idread's and the REU client's counters up to now, at the knobs as
        they have been since the last read or change."""
        if self.idread is not None:
            self.idread.advance(time.monotonic(), self.outside("rom"), self.outside("io"))
        if self.c64loop is not None:
            self.c64loop.advance(time.monotonic(), self.special_io in REU_NAMES, self.dma_outside())

    def serve(self, end: PipeEnd) -> None:
        self.end = end
        while True:
            self.unblocked.wait()
            if not self.powered:
                break
            first = end.read(1, 60.0)
            if len(first) < 1:
                break
            if self.silent or self.deaf:
                continue
            if first != b"d":                       # 0x64: a token follows; anything else is a letter
                self._letter(end, first.decode("latin-1"))
                continue
            second = end.read(1, WAIT_S)
            if len(second) < 1:
                break
            value = int.from_bytes(first + second, "big")
            self.received.append(value)
            if self.image == "minimal" and value not in {FW_CHECK, VERSION_INFO, RESET_C64, LAUNCH_FILE}:
                self._refuse(end, "Busy!")
                continue
            if self.busy and value not in ALWAYS:
                self._refuse(end, "Busy!")
                continue
            handler = self._handlers().get(value)
            if self.startup_sid:
                # GoodSIDToken goes out on its own when the menu loads its startup SID, so the first
                # command after a reset can find it in front of its reply (seen on the bench).
                self.startup_sid = False
                end.write(board_reply(GOOD_SID))
            if handler is None:
                end.write(f"Unk cmd: 0x{value:04x}\n".encode("latin-1"))
                continue
            handler(end)
            if not self.busy and value in MENU_ONLY:
                end.write(STATS_TRAILER)

    # -- letter commands (SerUSBIO.ino's single-letter debug set) -------------

    def _letter(self, end: PipeEnd, letter: str) -> None:
        """A letter is read only when its define is compiled in; otherwise the
        firmware drops it and reads what follows as new commands. 'e' and 'b'
        are always compiled in."""
        from treadmill.link.dbgcmds import LETTERS, TIMING_KNOBS
        need = LETTERS.get(letter)
        if letter == "e":
            self.letters.append("e")
            end.write(b"--> Setting EEPROM to defaults\nApplied upon reboot\n")
            return
        if letter == "b":
            self.letters.append("b")
            end.write(b"Bus analysis: A0-15 D0-7 R/W counts\n")
            return
        if need is None or need not in self.debug_defines:
            return                                   # dropped, like the firmware's switch
        if letter == "t":
            sub = end.read(1, LETTER_ARG_S).decode("latin-1")
            self.accrue()
            if sub in self._knob_letters():
                digits = end.read(TIMING_KNOBS[sub], WAIT_S).decode("latin-1")
                self.knobs[sub] = int(digits)
                self.letters.append(f"t{sub}{digits}")
                head = f"\nVal Set to: {int(digits)}\n\n"          # GetDigits()
                if sub == "x":
                    head += "Kernal HIRAM sense mode set: takes effect at the next Kernal Replace launch\n"
                self.menu.render()                                 # the marks follow the margin at once
            elif sub == "d":
                self.restore_knobs()
                self.letters.append("td")
                banner = parse_banner(self.banner)
                head = ("Defaults set\n" if self.image == "minimal"
                        else f"Defaults set ({banner.video or 'PAL'} {banner.machine or 'C64'})\n")
                self.menu.render()
            else:
                self.letters.append(f"t{sub}")
                head = "No changes\n"
            listing = self._minimal_knob_listing() if self.image == "minimal" else self._knob_listing()
            end.write(("-----\n" + head + listing).encode("latin-1"))
            return
        if letter == "z":
            digits = end.read(3, WAIT_S).decode("latin-1")
            self.letters.append(f"z{digits}")
            if self.faults.get("z_stall"):
                end.write(b"\nDMA pattern test: ...\n\n  no pattern write on pass 3: C64 bus not clocking, or DMA timed out\n")
            elif self.dma:
                end.write(self._z_report(int(digits or 0) or 64).encode("latin-1"))
            return
        self.letters.append(letter)
        end.write(f"ok {letter}\n".encode("latin-1"))

    def _knob_letters(self) -> set[str]:
        """The letters this build's 't' takes: MinimalBoot's six, or main's
        thirteen and, on a TR+ build, the Kernal Replace two."""
        if self.image == "minimal":
            return {k.letter for k in KNOBS.values() if "minimal" in k.images}
        return {k.letter for k in KNOBS.values() if self.dma or not k.tr_plus_only}

    def restore_knobs(self) -> None:
        """'td': the knobs to the defaults for the standard the banner shows.
        Main's leaves KSenseMode alone; MinimalBoot's restores its five
        rise/fall knobs, and nS_MaxAdj and the DMA set through
        SetVideoStdDMATiming, as Min_SerUSBIO.ino does."""
        banner = parse_banner(self.banner)
        standard = standard_of(banner.machine, banner.video)
        restored = {k.letter for k in KNOBS.values() if k.restored_by_td}
        if self.image == "minimal":
            restored = self._knob_letters() | {"a", "b", "e", "w", "y"}
        for letter in restored:
            self.knobs[letter] = KNOBS[letter].default(standard)

    def _knob_listing(self) -> str:
        """The block the firmware prints after every 't' command, as
        SerUSBIO.ino formats it; the Kernal Replace lines on a TR+ build only."""
        k = self.knobs
        return ("Current:    Variable  Val (Command)\n"
                f"\t nS_MaxAdj   {k['m']:04d} (tm####)\n"
                f"\t nS_RWnReady  {k['r']:03d} (tr###)\n"
                f"\t nS_PLAprop   {k['p']:03d} (tp###)\n"
                f"\t nS_DataSetup {k['s']:03d} (ts###)\n"
                f"\t nS_DataHold  {k['h']:03d} (th###)\n"
                f"\t nS_VICStart  {k['v']:03d} (tv###)\n"
                f"\t nS_VICDHold  {k['i']:03d} (ti###)\n"
                f"\t nS_DMAAssert {k['a']:03d} (ta###)\n"
                f"\t nS_DMABAWait {k['b']:03d} (tb###)\n"
                f"\t nS_DMASetup  {k['e']:03d} (te###)\n"
                f"\t nS_DMADataHold  {k['w']:03d} (tw###)\n"
                f"\t nS_DMADataSetup {k['y']:03d} (ty###)\n"
                f"\t Cyc_KernProp  {k['k']:02d} (tk##)\n"
                + (f"\t KSenseMode    {k['x']}, in effect {self._ksense_in_effect()} (tx#: 0 Skoe, 2 DMA, 9 auto)\n"
                   f"\t nS_KSenseSample {k['n']:03d} (tn###)\n" if self.dma else "")
                + "\tSet Defaults      (td)\n"
                "\tList current vals (t)\n-----\n")

    def _ksense_in_effect(self) -> int:
        """KSenseEff as a launch sets it: auto is DMA on a C128, Skoe on a C64."""
        mode = self.knobs["x"]
        if mode == 9:
            return 2 if parse_banner(self.banner).machine == "C128" else 0
        return 2 if mode == 2 else 0

    def _minimal_knob_listing(self) -> str:
        """Min_SerUSBIO.ino's block: six knobs, its own spacing."""
        k = self.knobs
        return ("Current:    Variable  Val (Command)\n"
                f"\t   nS_MaxAdj {k['m']:04d} (tm####)\n"
                f"\t nS_RWnReady  {k['r']:03d} (tr###)\n"
                f"\t  nS_PLAprop  {k['p']:03d} (tp###)\n"
                f"\tnS_DataSetup  {k['s']:03d} (ts###)\n"
                f"\t nS_DataHold  {k['h']:03d} (th###)\n"
                f"\t nS_VICStart  {k['v']:03d} (tv###)\n"
                "\tSet Defaults      (td)\n"
                "\tList current vals (t)\n-----\n")

    # -- helpers -----------------------------------------------------------

    def _reply(self, end: PipeEnd, token: int) -> None:
        end.write(board_reply(token))

    def _refuse(self, end: PipeEnd, text: str) -> None:
        end.write(board_reply(FAIL) + f"{text}\n".encode("latin-1"))

    @staticmethod
    def _path(end: PipeEnd) -> str:
        return end.read_until(b"\0", WAIT_S)[:-1].decode("latin-1")

    @staticmethod
    def _number(end: PipeEnd, width: int) -> int:
        return int.from_bytes(end.read(width, WAIT_S), "big")

    # -- commands ----------------------------------------------------------

    def _handlers(self):
        return {
            FW_CHECK: lambda e: self._reply(e, FW_FULL if self.image == "main" else FW_MINIMAL),
            VERSION_INFO: self._version,
            PING: lambda e: e.write(READY_LINE.encode("latin-1")),
            RESET_C64: self._reset,
            C64_PAUSE_ON: lambda e: self._reply(e, ACK),
            C64_PAUSE_OFF: lambda e: self._reply(e, ACK),
            READ_C64_MEM: self._read_memory,
            WRITE_C64_MEM: self._write_memory,
            LAUNCH_FILE: self._launch,
            DELETE_FILE: self._delete,
            POST_FILE: self._post,
            GET_FILE: self._get,
            GET_DIR_NDJSON: self._list,
        }

    def _version(self, end: PipeEnd) -> None:
        booting = self.booting_for > 0
        self.booting_for -= 1
        self._reply(end, ACK)
        text = self.banner.replace("complete", "in progress") if booting else self.banner
        end.write(text.encode("latin-1"))

    def _reset(self, end: PipeEnd) -> None:
        self.flooding = False            # the C64's reset ends what it was looping on
        self.booting_for = self.boot_polls
        self.startup_sid = True
        self.busy = False
        self.heartbeat = False
        self.idread = None
        self.c64loop = None
        with self.lock:
            self.program = None
            self.memory[0xD018] = 0x17
            self.cold_start()
            self.menu.reset()
        end.write(b"Reset cmd received\nLoading IO handler: TeensyROM\n")

    def _read_memory(self, end: PipeEnd) -> None:
        if not self.dma:
            # A plain TR has no such command: the bytes that follow are read as
            # new commands. Modelled as an unknown-command line per byte pair.
            end.write(b"Unk cmd\n")
            return
        address = self._number(end, 2)
        length = self._number(end, 2)
        self.accrue()
        if self.idread is not None:
            self.memory[IDREAD_RESULTS:IDREAD_RESULTS + 8] = self.idread.bytes()
        if self.c64loop is not None:
            self.memory[0xC110:0xC114] = self.c64loop.bytes()
        if address == VIC_MEMPTR and self.key_on_screen_read:
            keys, self.key_on_screen_read = self.key_on_screen_read, ""
            self.type_keys(keys)
        if address == VIC_MEMPTR and self.program is None:
            self.menu.drawn_further()
        if address <= JIFFY_CLOCK + 2 and address + length > JIFFY_CLOCK and not self.faults.get("jiffy_stopped"):
            ticks = int(time.monotonic() * 60) & 0xFFFFFF          # the KERNAL's IRQ, 60 a second
            self.memory[JIFFY_CLOCK:JIFFY_CLOCK + 3] = ticks.to_bytes(3, "big")
        data = bytearray(self.memory[address:address + length])
        if self.dma_bit_once and address == SCREEN_RAM_DEFAULT:
            for at in self.dma_bit_once:                # bit 1 low on this read, not in memory
                data[at] &= ~0x02
            self.dma_bit_once = ()
        self._reply(end, ACK)
        end.write(bytes(data))

    def _write_memory(self, end: PipeEnd) -> None:
        if not self.dma:
            end.write(b"Unk cmd\n")
            return
        address = self._number(end, 2)
        length = self._number(end, 2)
        data = end.read(length, WAIT_S)
        self.memory[address:address + len(data)] = data
        self._reply(end, ACK)
        if address <= KEYCOUNT < address + len(data) and self.memory[KEYCOUNT] and self.getin_live() and (
                self.program is not None or not self.busy):
            # The menu's GETIN loop, or a launched program's, takes the keys as soon as they are in the buffer.
            count = self.memory[KEYCOUNT]
            self.memory[KEYCOUNT] = 0
            for code in bytes(self.memory[KEYBUF:KEYBUF + count]):
                (self.program or self.menu).key(code)
        if address == LED_QUESTION_ROW and self.led_answers and \
                LED_QUESTION_MARK in bytes(b & ~REVERSE_VIDEO for b in data):
            # faults["led_early"]: keys the operator presses as each question goes up, too soon to have read it.
            self.type_keys(self.faults.get("led_early", ""))
            self.led_asked = True
        elif address == KEYCOUNT and self.led_asked and self.led_answers:
            self.led_asked = False
            self.type_keys(self.led_answers.pop(0))

    def _launch(self, end: PipeEnd) -> None:
        self._reply(end, ACK)
        drive = end.read(1, WAIT_S)[0]
        path = self._path(end)
        self.launched.append((drive, path))
        self._reply(end, ACK)
        folder, _, name = path.rpartition("/")
        end.write(f"Remote Launch:\nP: {folder or '/'}\nF: {name}\n".encode("latin-1"))
        chatter = "Loading IO handler: TR_BASIC\n" if name == TR_BASIC_ITEM else self.chatter_on_launch
        if chatter:
            end.write(chatter.encode("latin-1"))
        self.run(path)

    def _delete(self, end: PipeEnd) -> None:
        self._reply(end, ACK)
        end.read(1, WAIT_S)
        path = self._path(end)
        if self.files.pop(path, None) is None:
            self._refuse(end, "File not found")
            return
        self._reply(end, ACK)

    def _post(self, end: PipeEnd) -> None:
        self._reply(end, ACK)
        length = self._number(end, 4)
        total = self._number(end, 2)
        end.read(1, WAIT_S)
        path = self._path(end)
        if path in self.files:
            self._refuse(end, "File exists")
            return
        self._ensure_directories(path)
        self._reply(end, ACK)
        data = self._receive(end, length)
        if len(data) < length:                       # nothing kept: the firmware removes its partial file
            self._refuse(end, "Timeout!")
            self._refuse(end, f"Rec {len(data)} of {length} bytes")
            self._discard(end, length - len(data))
            return
        if checksum(data) != total:
            self._refuse(end, "Checksum mismatch")
            return
        self.files[path] = bytes(data)
        self._reply(end, ACK)

    @staticmethod
    def _receive(end: PipeEnd, length: int) -> bytes:
        """ReceiveFileData: bytes until the count is reached, given up when
        none arrive for half a second (SerialAvailabeTimeout), however slowly
        they come."""
        data = bytearray()
        while len(data) < length:
            part = end.read(min(length - len(data), 4096), 0.5)
            if not part:
                break
            data += part
        return bytes(data)

    @staticmethod
    def _discard(end: PipeEnd, owed: int) -> None:
        """DiscardCmdInput: what the sender still owed is dropped until it has
        all come or a second passes with nothing arriving, so a command sent
        inside that second is dropped with it."""
        while owed > 0:
            part = end.read(min(owed, 4096), DISCARD_QUIET_S)
            if not part:
                return
            owed -= len(part)

    def _ensure_directories(self, path: str) -> None:
        """EnsureDirectory: every folder on the way to a posted file exists afterwards."""
        folder = path.rpartition("/")[0]
        while folder and folder not in self.directories:
            self.directories.append(folder)
            folder = folder.rpartition("/")[0]

    def _get(self, end: PipeEnd) -> None:
        self._reply(end, ACK)
        end.read(1, WAIT_S)
        path = self._path(end)
        data = self.files.get(path)
        if data is None:
            self._refuse(end, "File not found")
            return
        self._reply(end, ACK)
        end.write(len(data).to_bytes(4, "little") + checksum(data).to_bytes(4, "little") + data)
        self._reply(end, ACK)

    def _list(self, end: PipeEnd) -> None:
        self._reply(end, ACK)
        drive = end.read(1, WAIT_S)[0]
        skip = self._number(end, 2)
        take = self._number(end, 2)
        path = self._path(end)
        if drive == DRIVE_TEENSY:
            # SendTRDirectory: the whole tree, whatever the path, skip and take say.
            self._reply(end, ACK)
            self._reply(end, DIR_START)
            end.write("".join(json.dumps(e) + "\r\n" for e in self._teensy_listing()).encode("latin-1"))
            self._reply(end, DIR_END)
            return
        # faults["usb_ready_after_s"]: the thumb drive mounts that long after power-on, None never; until then
        # its volume does not open, as GetDirectoryCommand finds it.
        usb_after = self.faults.get("usb_ready_after_s", 0.0)
        usb_down = drive == DRIVE_USB and (usb_after is None or time.monotonic() - self.born < usb_after)
        if usb_down or path not in self.directories:
            self._refuse(end, "Directory not found. (Error 5)")
            return
        self._reply(end, ACK)
        self._reply(end, DIR_START)
        entries = self.listing or self._card_listing(path)
        lines = "".join(json.dumps(e) + "\r\n" for e in entries[skip:skip + take])
        end.write(lines.encode("latin-1"))
        self._reply(end, DIR_END)

    def _card_listing(self, path: str) -> list[dict]:
        def parent(p: str) -> str:
            return p.rpartition("/")[0] or "/"
        out = [{"type": "dir", "name": d.rpartition("/")[2]}
               for d in self.directories if d != "/" and parent(d) == path]
        out += [{"type": "file", "name": p.rpartition("/")[2], "size": len(data)}
                for p, data in self.files.items() if parent(p) == path]
        return out

    @staticmethod
    def _teensy_listing() -> list[dict]:
        out = []
        for folder in TREE["/"]:
            out.append({"type": "dir", "name": folder.name})
            out += [{"type": "file", "name": e.name, "size": 0}
                    for e in TREE.get(folder.name, []) if e.name != UP_DIR]
        return out


def start_fake_board(**overrides):
    """A fake board on its own thread and the host-side ByteStream to it."""
    board = FakeBoard(**overrides)
    stream, far_end = open_pipe("fake-board")
    thread = threading.Thread(target=board.serve, args=(far_end,), name="fake-board", daemon=True)
    thread.start()
    return board, stream
