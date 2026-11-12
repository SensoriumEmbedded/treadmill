"""The built-in programs the manufacturing test runs, as the fake board plays
them: the External Ports test (blocked until the buttons are pressed), the
Expansion Port test (a pass per launch or ``o``) and the REU checker. Their
screens and serial lines carry the strings ``treadmill/mfg.py`` reads, taken
from TRExtPortCheck.asm, ExpansionPortTest.asm and StatusFunctions.c; the
REU checker's screen is made up, since the real one is a packed binary.

``faults`` (per board, from the fake bench) breaks one thing at a time:
``epc_fail`` ("ethernet", "sd", "usb"), ``clock_offset_s``, ``xpt_fail_pass``,
``reu_fail``, ``reu_kb``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


class ReuChecker:
    def __init__(self, board):
        self.board = board

    def start(self) -> None:
        verdict = "FAILED" if self.board.faults.get("reu_fail") else "PASSED"
        self.board.show(["REU Checker v1.0", "", f"REU size: {self.board.faults.get('reu_kb', 512)}K", "",
                         f"All tests {verdict}"])

    def key(self, code: int) -> None:
        pass
