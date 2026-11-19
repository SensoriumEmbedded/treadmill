"""The manufacturing test's bench with fake boards: each "power on" starts a
new fake TR+ with its own chip id, the operator's hands act on it, and the
capture shows whichever board is on. ``boards`` lists each board's faults
(``fake_programs.py``) in the order they are plugged in; a board past the end
of the list has none. The C64 has a fake keyboard board, powered with it
like the real one: its resets are external resets of the TR+, and its ``V``
switches the Kawari, whose J1 open gives the standard the board powered on
in. A station that names no keyboard board never opens it."""

from __future__ import annotations

import re
import threading

from selftest.fake_board import BANNER, FakeBoard, start_fake_board
from selftest.fake_kbd import FakeKeyboard
from selftest.synth import calibrated, fake_frame_source, harvested_bank
from treadmill.board import Board
from treadmill.link import Link
from treadmill.link.transport import open_pipe
from treadmill.mfg import (
    BUTTONS, CONNECT, INSTRUCTIONS, LEDS, POWER_OFF, RESET_BUTTON, SET_STANDARD, Console, VideoKit,
)
from treadmill.station.kbd import KeyboardBoard

VIDEO_LINES = {"PAL": "PAL Vid  50 Hz", "NTSC": "NTSC Vid  60 Hz"}

FIRST_UID = 19300000
SD_ROOT = [{"type": "dir", "name": "APPS"}, {"type": "dir", "name": "VMS"},
           {"type": "file", "name": "MPE.TRH", "size": 227392},
           {"type": "file", "name": "autolaunch.txt", "size": 1383}]


class _Off:
    """What the capture shows with no board on: an empty screen."""
    heartbeat = False

    def __init__(self):
        self.memory = bytearray(b"\x20" * 0x10000)
        self.memory[0xD018], self.memory[0xDD00] = 0x17, 0x97


class _OnBench:
    """The board on the bench now, for the capture."""

    def __init__(self, bench: "FakeBench"):
        self._bench, self._off = bench, _Off()

    def __getattr__(self, name):
        fake = self._bench.fake
        return getattr(fake if fake is not None and fake.powered else self._off, name)


class FakeBench:
    def __init__(self, console: Console, boards: list[dict] | None = None, kbd_fitted: set[str] | None = None):
        self.console = console
        self.plans = list(boards or [])
        self.kbd_fitted = {"tod", "reset", "kawari"} if kbd_fitted is None else kbd_fitted
        self.fake: FakeBoard | None = None
        self.kbd: FakeKeyboard | None = None
        self.saved = "NTSC"                 # the Kawari's saved model: what J1 open gives
        self.stream = None
        self.uid = ""
        self.port = ""
        self.count = 0

    def do(self, action: str) -> None:
        self.console.instruct(INSTRUCTIONS[action])
        if action == CONNECT:
            self._power_on()
        elif action == BUTTONS:
            self.fake.press_buttons()
        elif action == LEDS:
            self.fake.watch_led_questions()
        elif action == RESET_BUTTON:
            self.fake.external_reset()
        elif action == POWER_OFF:
            self.fake.power_off()
        elif action == SET_STANDARD:
            # Off, the Kawari switched, on again: the next plan is the same board in the new standard.
            self.fake.power_off()
            self._power_on()

    def _power_on(self) -> None:
        faults = dict(self.plans.pop(0)) if self.plans else {}
        self.count += 1
        self.uid = str(FIRST_UID + self.count)
        banner = faults.pop("banner", BANNER).replace("19277260", self.uid)
        listing = faults.pop("sd_root", SD_ROOT)
        by_item = faults.pop("menu_draws_by_item", False)
        self.fake, self.stream = start_fake_board(banner=banner, faults=faults, listing=list(listing),
                                                  menu_draws_by_item=by_item)
        self.port = f"FAKE{self.count}"
        self.saved = "PAL" if VIDEO_LINES["PAL"] in banner else "NTSC"
        self.kbd = FakeKeyboard(fitted=set(self.kbd_fitted), on_reset=self.fake.external_reset,
                                on_video=self._video)

    def _video(self, setting: str) -> None:
        """The Kawari reconfigured with J1 ``setting``: the TR+ measures the
        new standard once the C64's reset is released."""
        other = {"PAL": "NTSC", "NTSC": "PAL"}
        standard = self.saved if setting == "open" else other[self.saved]
        self.fake.banner = re.sub(r"(PAL|NTSC) Vid  \d\d Hz", VIDEO_LINES[standard], self.fake.banner)

    def wait_for_board(self) -> tuple[str, str]:
        if self.fake is None or not self.fake.powered:
            raise RuntimeError("the fake bench has no board on: CONNECT first")
        return self.port, self.uid

    def open_board(self, station, timeout: float) -> Board:
        if self.stream.ended:                   # the same board opened again: it never left USB
            self.stream, far_end = open_pipe("fake-board")
            threading.Thread(target=self.fake.serve, args=(far_end,), name="fake-board", daemon=True).start()
        stream = self.stream
        factory = (lambda: self.open_keyboard(station)) if station.keyboard is not None else None
        return Board.open(station, link_factory=lambda: Link(stream), kbd_factory=factory)

    def open_keyboard(self, station) -> KeyboardBoard:
        return KeyboardBoard.attach(self.kbd.connect(), port="fake-keyboard")

    def gone(self, port: str) -> bool:
        return self.fake is None or not self.fake.powered or port != self.port

    def video_kit(self) -> VideoKit:
        cal = calibrated()
        return VideoKit(fake_frame_source(_OnBench(self)), cal, harvested_bank(cal), fitted=True)
