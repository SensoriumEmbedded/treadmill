"""The built-in programs the manufacturing test runs, as the fake board plays
them: the External Ports test (blocked until the buttons are pressed), the
Expansion Port test (a pass per launch or ``o``) and the REU checker. Their
screens and serial lines carry the strings ``treadmill/mfg.py`` reads, taken
from TRExtPortCheck.asm, ExpansionPortTest.asm and StatusFunctions.c; the
REU checker's is a sketch of the real one's layout and colors, which mfg
reads by color, since the real one is a packed binary.

``faults`` (per board, from the fake bench) breaks one thing at a time:
``epc_fail`` ("ethernet", "sd", "usb"), ``clock_offset_s``, ``xpt_fail_pass``,
``reu_fail``, ``reu_kb``; ``leds`` lists what the operator types for each LED
question (``FakeBoard.watch_led_questions``).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from treadmill.c64.screen import SCREEN_BYTES, SCREEN_COLUMNS, SCREEN_RAM_DEFAULT, SCREEN_ROWS, codes_for_text

WHITE, RED, GREEN, YELLOW, DARK_GREY, MEDIUM_GREY, LIGHT_GREEN = 1, 2, 5, 7, 11, 12, 13
SOLID, CHECKER, CROSS = 0xA0, 0x66, 0x5B     # a reverse space, the checkerboard and a cross: the map's cells

EPC_HEAD = ["TeensyROM+ v0.9 Test", "", " TeensyROM External Port Checks ", "Ethernet connected",
            "1st SD file: APPS", "1st USB file: validation"]
EPC_FAIL_LINES = {"ethernet": ["Ethernet init via DHCP *Failed!*", "Could not init Ethernet"],
                  "sd": ["Ethernet connected", "Could not init SD"],
                  "usb": ["1st SD file: APPS", "Could not init USB"]}
EPC_BUTTONS = ["Press and release the TR Menu Button", "  Waiting for press...detected",
               "  Waiting for release..detected", "Press and release the Alt Button",
               "  Waiting for press..detected", "  Waiting for release..detected"]
EPC_VERIFY = ["  Verify the following:", "   * Ethernet Port Green LED is on",
              "   * TR main LED (by Menu button) is on", '   * Serial output "Hello from TR at.."',
              "   * Check time shown above.", "   * RTC batt: cycle power & recheck", "Press any key to return"]

XPT_PATTERNS = [("$ff over $ff", 4096), ("$ff over $00", 16384), ("$00 over $ff", 16384),
                ("$00/$ff alt over $00", 16384), ("$55/$aa alt over $ff", 16384)]
XPT_SCREEN_PATTERNS = [" $ff: 0/4096 OK", " $ff: 0/16384 OK", " $00: 0/16384 OK", " $00/$ff: 0/16384 OK",
                       " $55/$aa: 0/16384 OK"]
XPT_TEENSY = ["Walking ones address Test OK", "Cascading ones address Test OK", "DMA Page Read/Write Tests OK",
              "DMA Bit Transition Tests"]
XPT_C64 = ["IRQ Test OK", "NMI Test OK", "IO1 Read Test OK", "IO1 Wr, IO2 Rd Test OK", "IO2 Wr, IO1 Rd Test OK",
           "Game, ExROM, ROMH, ROML Tests:", " No Cart ROM Test OK", " 8k Cart ROM Test OK",
           " 16k Cart ROM Test OK", " Ultimax Cart ROM Test OK"]
XPT_PROMPT = [" Apply External Reset to test Rst Input", " l:Loop, o:Once, any other key to exit"]

KEY_O = 0x4F


def hello_clock(offset_s: float = 0.0) -> str:
    """The board's clock as ExtPortCheck prints it: UTC at the default
    timezone, 12-hour, hour 00 for twelve o'clock."""
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_s)
    return f"{t.hour % 12:02d}:{t.minute:02d}:{t.second:02d} {'p' if t.hour >= 12 else 'a'}m"


class ExtPorts:
    def __init__(self, board):
        self.board = board
        self.fail = board.faults.get("epc_fail")
        self.waiting = False

    def start(self) -> None:
        b = self.board
        if self.fail:
            rows = EPC_HEAD[:3] + EPC_FAIL_LINES[self.fail] + ["", " External Port Check Failed!", "",
                                                                "Press any key to return"]
            b.show(rows, reverse={0, 2, rows.index(" External Port Check Failed!")})
            return
        b.show(EPC_HEAD + EPC_BUTTONS[:2], reverse={0, 2})
        with b.lock:
            if b.buttons_pending:
                b.buttons_pending = False
                self.buttons()
            else:
                self.waiting = True
                b.unblocked.clear()          # ExtPortCheck services nothing until the buttons

    def buttons(self) -> None:
        self.waiting = False
        clock = hello_clock(self.board.faults.get("clock_offset_s", 0.0))
        self.board.say(f"\r\nHello from TR at: {clock}\n\r\n")
        rows = EPC_HEAD + EPC_BUTTONS + ["Sent to USB Serial:", f"   Hello from TR at: {clock}", "",
                                         " External Port Check Passed.", ""] + EPC_VERIFY
        self.board.show(rows, reverse={0, 2, rows.index(" External Port Check Passed.")})
        self.board.unblocked.set()

    def key(self, code: int) -> None:
        self.board.return_to_menu()


class ExpPort:
    def __init__(self, board):
        self.board = board
        self.passes = 0

    def start(self) -> None:
        self.run_pass()

    def run_pass(self) -> None:
        self.passes += 1
        bad = self.board.faults.get("xpt_fail_pass") == self.passes
        if bad:
            label, size = XPT_PATTERNS[0]
            self.board.say(f"{label} @ $c000: 3 bad of {size} bytes\n"
                           "  worst page: 2, unchanged: 0, xor mask $04, 1->0: 3, 0->1: 0\n")
            rows = ["TeensyROM+ v0.9 Test", ""] + XPT_TEENSY + [
                " $ff: 3/4096 Failed", " Failed, additional details on serial", "",
                " Expansion Port Test Failed! "] + XPT_PROMPT
            self.board.show(rows, reverse={0, rows.index(" Expansion Port Test Failed! ")})
            return
        for label, size in XPT_PATTERNS:
            self.board.say(f"{label} @ $c000: 0 bad of {size} bytes  CLEAN\n")
        rows = ["TeensyROM+ v0.9 Test"] + XPT_TEENSY + XPT_SCREEN_PATTERNS + XPT_C64 + [
            " Expansion Port Test Passed. "] + XPT_PROMPT
        self.board.show(rows, reverse={0, rows.index(" Expansion Port Test Passed. ")})

    def key(self, code: int) -> None:
        if code == KEY_O:
            self.run_pass()
        else:
            self.board.return_to_menu()


def checker_screen(kb: int, done: int, testing: int | None = None, error_bank: int | None = None,
                   working_ink: int = LIGHT_GREEN) -> tuple[bytes, bytes]:
    """REU-Checker v1.0's screen as codes and colors, laid out where mfg reads
    it (``REU_*`` in treadmill/mfg.py): yellow text on dark grey, the WORKING
    line in ``working_ink``, and the bank map, one cell per 64 KB: the first
    ``done`` banks tested (green, red at ``error_bank``), ``testing`` under
    test, the rest missing."""
    from treadmill.mfg import REU_CELL_KB, REU_MAP_COL, REU_MAP_COLS, REU_MAP_ROW, REU_MAP_ROWS, REU_WORKING_ROW
    rows = [""] * SCREEN_ROWS
    rows[1] = "      REU-CHECKER 128KB-16MB V1.0"
    rows[3] = f"              FOUND: {kb}KB"
    rows[REU_WORKING_ROW] = "     OK   THE REU IS WORKING!   OK"
    codes = bytearray(b"".join(codes_for_text(r.ljust(SCREEN_COLUMNS)[:SCREEN_COLUMNS]) for r in rows))
    colors = bytearray([YELLOW]) * SCREEN_BYTES
    line = REU_WORKING_ROW * SCREEN_COLUMNS
    colors[line:line + SCREEN_COLUMNS] = bytes([working_ink]) * SCREEN_COLUMNS
    for i in range(REU_MAP_ROWS * REU_MAP_COLS):
        at = (REU_MAP_ROW + i // REU_MAP_COLS) * SCREEN_COLUMNS + REU_MAP_COL + i % REU_MAP_COLS
        if i == testing and i < kb // REU_CELL_KB:
            codes[at], colors[at] = CROSS, WHITE
        elif i < min(done, kb // REU_CELL_KB):
            codes[at], colors[at] = SOLID, RED if i == error_bank else GREEN
        else:
            codes[at], colors[at] = CHECKER, MEDIUM_GREY
    return bytes(codes), bytes(colors)


class ReuChecker:
    """Like the real checker it tests its banks one after another, then
    holds its screen until a reset takes the board back to the menu. It
    draws in its own colors, which the fake's video shows (``look``)."""

    def __init__(self, board):
        self.board = board
        self.kb = board.faults.get("reu_kb", 512)
        self.error_bank = 3 if board.faults.get("reu_fail") else None
        self.colors = bytes(SCREEN_BYTES)

    def start(self) -> None:
        self.draw(0)
        threading.Thread(target=self.run, daemon=True).start()

    def draw(self, n: int) -> None:
        codes, self.colors = checker_screen(self.kb, n, n, self.error_bank)
        self.board.memory[0xD018] = 0x17
        self.board.memory[SCREEN_RAM_DEFAULT:SCREEN_RAM_DEFAULT + SCREEN_BYTES] = codes

    def look(self) -> tuple[bytes, int, int]:
        """Color RAM, background and border, for the fake's video."""
        return self.colors, DARK_GREY, GREEN

    def run(self) -> None:
        for n in range(1, self.kb // 64 + 1):
            time.sleep(0.15)
            with self.board.lock:
                if self.board.program is not self:
                    return
                self.draw(n)

    def key(self, code: int) -> None:
        pass
