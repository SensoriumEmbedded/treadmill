"""The manufacturing test's bench with fake boards: each "power on" starts a
new fake TR+ with its own chip id, the operator's hands act on it, and the
capture shows whichever board is on. ``boards`` lists each board's faults
(``fake_programs.py``) in the order they are plugged in; a board past the end
of the list has none."""

from __future__ import annotations

from selftest.fake_board import BANNER, FakeBoard, start_fake_board
from selftest.synth import calibrated, fake_frame_source, harvested_bank
from treadmill.board import Board
from treadmill.link import Link
from treadmill.mfg import BUTTONS, CONNECT, INSTRUCTIONS, LEDS, POWER_OFF, RESET_BUTTON, Console, VideoKit

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
    def __init__(self, console: Console, boards: list[dict] | None = None):
        self.console = console
        self.plans = list(boards or [])
        self.fake: FakeBoard | None = None
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

    def wait_for_board(self) -> tuple[str, str]:
        if self.fake is None or not self.fake.powered:
            raise RuntimeError("the fake bench has no board on: CONNECT first")
        return self.port, self.uid

    def open_board(self, station, timeout: float) -> Board:
        stream = self.stream
        return Board.open(station, link_factory=lambda: Link(stream))

    def gone(self, port: str) -> bool:
        return self.fake is None or not self.fake.powered

    def video_kit(self) -> VideoKit:
        cal = calibrated()
        return VideoKit(fake_frame_source(_OnBench(self)), cal, harvested_bank(cal), fitted=True)
