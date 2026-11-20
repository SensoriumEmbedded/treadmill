"""The single-letter debug commands, gated on the define that compiles each in.

``Source/Teensy/SerUSBIO.ino`` reads letters only in builds with the matching
``Dbg_*`` define; in any other build the letter is dropped and its argument
bytes are read as top-level commands, and ``e`` (settings reset) is always
compiled in. So a letter goes out only when the flashed build carries the
define, and ``e`` only on a station that allows it.
"""

from __future__ import annotations

import time
from typing import Callable

from treadmill.knobs import KNOBS, parse_listing
from treadmill.link.trlink import Link, NotAllowed

# letter -> the define that compiles it in (None: always available)
LETTERS: dict[str, str | None] = {
    "t": "Dbg_SerTimChg",   # timing knobs (treadmill.knobs): t<letter><digits>, td restores
    "u": "Dbg_SerDMA", "v": "Dbg_SerDMA", "w": "Dbg_SerDMA", "z": "Dbg_SerDMA", "y": "Dbg_SerDMA",
    "f": "Dbg_SerMem", "x": "Dbg_SerMem",
    "o": "Dbg_SerSD",
    "l": "Dbg_SerLog", "c": "Dbg_SerLog",
    "q": "Dbg_SerASID", "a": "Dbg_SerASID", "i": "Dbg_SerASID",
    "m": "Dbg_SerSwift", "p": "Dbg_SerSwift", "r": "Dbg_SerSwift", "k": "Dbg_SerSwift", "s": "Dbg_SerSwift",
    "b": None,              # bus analysis dump
    "e": None,              # settings to defaults: destructive
}

# The timing knobs behind 't', by letter: how many digits GetDigits() reads, and the
# variable the listing prints (treadmill.knobs has the rest).
TIMING_KNOBS = {k.letter: k.digits for k in KNOBS.values()}
TIMING_NAMES = {k.letter: k.name for k in KNOBS.values()}

TEXT_REPLY = (3.0, 0.3, 20.0)
LONG_REPLY_GAP_S = 2.0        # the most run() waits for a first byte before it checks again


class DebugCommands:
    def __init__(self, link: Link | Callable[[], Link], defines: set[str] | frozenset[str],
                 allow_eeprom_reset: bool = False):
        # A link, or what gives the link now: a board's changes when it reconnects (a hand-off to the
        # minimal image), and the letters must reach the image that answers.
        self._source = link if callable(link) else (lambda: link)
        self.defines = set(defines)
        self.allow_eeprom_reset = allow_eeprom_reset
        self._listed: set[str] | None = None     # the knob letters the last 't' reply listed
        self._listed_on: Link | None = None      # ... and the link it came over

    @property
    def _link(self) -> Link:
        return self._source()

    def available(self, letter: str) -> bool:
        need = LETTERS.get(letter)
        if letter not in LETTERS:
            return False
        if letter == "e":
            return self.allow_eeprom_reset
        return need is None or need in self.defines

    def _check(self, command: str) -> str:
        letter = command[:1]
        if letter not in LETTERS:
            raise NotAllowed(f"{command!r}: not a debug command")
        if not self.available(letter):
            why = ("the station does not allow a settings reset" if letter == "e"
                   else f"the flashed build lacks {LETTERS[letter]}")
            raise NotAllowed(f"{command!r} not sent: {why}")
        return letter

    def send(self, command: str) -> str:
        """One letter command with its argument bytes, written in a single
        write so the firmware reads them together; returns the text reply."""
        letter = self._check(command)
        self._link._send(command.encode("latin-1"))
        reply = self._link.stream.read_idle(*TEXT_REPLY).decode("latin-1")
        if letter == "t":
            self._listed, self._listed_on = set(parse_listing(reply)), self._link
        return reply

    def run(self, command: str, done: Callable[[str], bool], timeout: float) -> str:
        """A letter command whose reply comes over seconds, with pauses
        (``z###``): reads until ``done(text)`` holds or ``timeout`` passes,
        and returns what came."""
        self._check(command)
        self._link._send(command.encode("latin-1"))
        text, end = "", time.monotonic() + timeout
        while not done(text) and (left := end - time.monotonic()) > 0:
            text += self._link.stream.read_idle(min(LONG_REPLY_GAP_S, left), TEXT_REPLY[1],
                                                left).decode("latin-1", "replace")
        return text

    def set_knob(self, knob: str, value: int) -> str:
        """``tp210`` and friends: a timing knob to a value, zero-padded to the
        digit count the firmware reads."""
        if knob not in TIMING_KNOBS:
            raise NotAllowed(f"timing knob {knob!r}: use {', '.join(TIMING_KNOBS)}")
        spec = KNOBS[knob]
        if not spec.fits(value):
            raise NotAllowed(f"{spec.name} = {value}: {spec.digits} digits at most")
        if spec.tr_plus_only and knob not in self.listed():
            # Only a TR+ build compiles these letters in; elsewhere 'tx9' would
            # send the '9' on as a command of its own.
            raise NotAllowed(f"{spec.name} not sent: the board's 't' listing does not show it (a TR+ build has it)")
        return self.send(spec.format(value))

    def listed(self) -> set[str]:
        """The knob letters the board's last 't' reply listed, asking for a
        listing if nothing has been sent yet."""
        if self._listed is None or self._listed_on is not self._link:
            self.listing()
        return self._listed or set()

    def restore_defaults(self) -> str:
        return self.send("td")

    def listing(self) -> str:
        """The bare 't' reply: the current knob values."""
        return self.send("t")
