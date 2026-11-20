"""The TeensyROM menu's Settings pages, driven by keys and read back from the screen.

F8 opens Settings from the main menu (``Source/C64/SettingsMenu``); a digit
picks a page; on page 2, the TeensyROM page, ``a`` steps Special IO to the
next IO handler, its name drawn at row 5 from column 19
(``Pg_TRSettings.asm``), wrapping after the last; SPACE goes back to the
menu. Special IO is a saved setting: it applies from the next launch and
survives a power cycle, so a test that changes it restores it in a finally
(``special_io``). Keys are typed through whatever the station has: the
KERNAL key buffer by DMA on a TR+, else the keyboard board; the page is
read by DMA where the board has it, the names being exact text.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from treadmill.c64.keys import F8, SPACE, Keys
from treadmill.c64.menu import LEGEND_ROW1
from treadmill.errors import TreadmillError
from treadmill.observe.channels import DEFAULT_TIMEOUT, ScreenChannel

SPECIAL_IO_LABEL = "Special IO:"
TR_PAGE = b"2"
NEXT_IO = 0x41                    # PETSCII 'a' (unshifted): Special IO increment
# The manually selectable IO handlers in enumIOHandlers order (Menu_Regs.h) on a TR+ build, as
# the page names them: 'a' steps through these and wraps.
SPECIAL_IO = ("None", "Swift-Turbo/Modem", "MIDI:Datel/Siel", "MIDI:Sequential", "MIDI:Passport/Sent",
              "MIDI:Namesoft IRQ", "512KB REU", "Kernal Replace", "Debug", "REU+USB Mouse/Disk", "GeoRAM",
              "USB Mouse")
REU = "512KB REU"
KERNAL_REPLACE = "Kernal Replace"


class SettingsError(TreadmillError):
    pass


class Settings:
    def __init__(self, screen: ScreenChannel, keys: Keys | None, *, timeout: float = DEFAULT_TIMEOUT, log=print):
        self.screen = screen
        self.keys = keys
        self.timeout = timeout
        self.log = log

    def _press(self, codes: bytes) -> None:
        if self.keys is None:
            raise SettingsError("no way to press keys on this station (keys_kernal or keys_hw)")
        self.keys.press(codes)

    @staticmethod
    def _special_io(rows: list[str]) -> str | None:
        for row in rows:
            if SPECIAL_IO_LABEL in row:
                return row.split(SPECIAL_IO_LABEL, 1)[1].strip()
        return None

    def open(self) -> str:
        """From the main menu to the TeensyROM page; Special IO as it shows."""
        self._press(bytes([F8]))
        time.sleep(0.5)
        self._press(TR_PAGE)
        read = self.screen.wait(lambda r: self._special_io(r.rows()) is not None, self.timeout, exact=True,
                                what="the Settings page with Special IO")
        return self._special_io(read.rows())

    def close(self) -> None:
        self._press(bytes([SPACE]))
        self.screen.wait_text(LEGEND_ROW1.strip(), timeout=self.timeout, exact=True)

    def special_io_now(self) -> str:
        """Special IO as the TeensyROM page shows it, the menu left as it was."""
        try:
            return self.open()
        finally:
            self.close()

    def set_special_io(self, want: str) -> str:
        """Special IO stepped round to ``want``; returns what it was."""
        if want not in SPECIAL_IO:
            raise SettingsError(f"Special IO {want!r}: one of {', '.join(SPECIAL_IO)}")
        was = self.open()
        try:
            now = was
            for _ in range(len(SPECIAL_IO)):
                if now == want:
                    break
                self._press(bytes([NEXT_IO]))
                before = now
                read = self.screen.wait(lambda r: self._special_io(r.rows()) not in (None, before), self.timeout,
                                        exact=True, what=f"Special IO to move on from {before!r}")
                now = self._special_io(read.rows())
            if now != want:
                raise SettingsError(f"Special IO never showed {want!r}; last {now!r}")
            self.log(f"[settings] Special IO {was!r} -> {now!r}")
        finally:
            self.close()
        return was

    @contextmanager
    def special_io(self, want: str) -> Iterator[str]:
        """Special IO set to ``want`` for the block and put back after it,
        whatever happened; yields what it was."""
        was = self.set_special_io(want)
        try:
            yield was
        finally:
            if was != want:
                self.set_special_io(was)
