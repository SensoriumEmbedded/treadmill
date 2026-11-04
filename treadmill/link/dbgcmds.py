"""The single-letter debug commands, gated on the define that compiles each in.

``Source/Teensy/SerUSBIO.ino`` reads letters only in builds with the matching
``Dbg_*`` define; in any other build the letter is dropped and its argument
bytes are read as top-level commands, and ``e`` (settings reset) is always
compiled in. So a letter goes out only when the flashed build carries the
define, and ``e`` only on a station that allows it.
"""

from __future__ import annotations

from treadmill.link.trlink import Link, NotAllowed

# letter -> the define that compiles it in (None: always available)
LETTERS: dict[str, str | None] = {
    "t": "Dbg_SerTimChg",   # timing knobs: t<m|r|p|s|h|v|i|a|b|e|w|y|k><digits>, td restores
    "u": "Dbg_SerDMA", "v": "Dbg_SerDMA", "w": "Dbg_SerDMA", "z": "Dbg_SerDMA", "y": "Dbg_SerDMA",
    "f": "Dbg_SerMem", "x": "Dbg_SerMem",
    "o": "Dbg_SerSD",
    "l": "Dbg_SerLog", "c": "Dbg_SerLog",
    "q": "Dbg_SerASID", "a": "Dbg_SerASID", "i": "Dbg_SerASID",
    "m": "Dbg_SerSwift", "p": "Dbg_SerSwift", "r": "Dbg_SerSwift", "k": "Dbg_SerSwift", "s": "Dbg_SerSwift",
    "b": None,              # bus analysis dump
    "e": None,              # settings to defaults: destructive
}

# The timing knobs behind 't' and how many digits GetDigits() reads for each.
TIMING_KNOBS = {"m": 4, "r": 3, "p": 3, "s": 3, "h": 3}
TIMING_NAMES = {"m": "nS_MaxAdj", "r": "nS_RWnReady", "p": "nS_PLAprop",
                "s": "nS_DataSetup", "h": "nS_DataHold"}

TEXT_REPLY = (3.0, 0.3, 20.0)


class DebugCommands:
    def __init__(self, link: Link, defines: set[str] | frozenset[str],
                 allow_eeprom_reset: bool = False):
        self._link = link
        self.defines = set(defines)
        self.allow_eeprom_reset = allow_eeprom_reset

    def available(self, letter: str) -> bool:
        need = LETTERS.get(letter)
        if letter not in LETTERS:
            return False
        if letter == "e":
            return self.allow_eeprom_reset
        return need is None or need in self.defines

    def send(self, command: str) -> str:
        """One letter command with its argument bytes, written in a single
        write so the firmware reads them together; returns the text reply."""
        letter = command[:1]
        if letter not in LETTERS:
            raise NotAllowed(f"{command!r}: not a debug command")
        if not self.available(letter):
            why = ("the station does not allow a settings reset" if letter == "e"
                   else f"the flashed build lacks {LETTERS[letter]}")
            raise NotAllowed(f"{command!r} not sent: {why}")
        self._link._send(command.encode("latin-1"))
        return self._link.stream.read_idle(*TEXT_REPLY).decode("latin-1")

    def set_knob(self, knob: str, value: int) -> str:
        """``tp210`` and friends: a timing knob to a value, zero-padded to the
        digit count the firmware reads."""
        if knob not in TIMING_KNOBS:
            raise NotAllowed(f"timing knob {knob!r}: use {', '.join(TIMING_KNOBS)}")
        width = TIMING_KNOBS[knob]
        if not 0 <= value < 10 ** width:
            raise NotAllowed(f"{TIMING_NAMES[knob]} = {value}: {width} digits at most")
        return self.send(f"t{knob}{value:0{width}d}")

    def restore_defaults(self) -> str:
        return self.send("td")

    def listing(self) -> str:
        """The bare 't' reply: the current knob values."""
        return self.send("t")
