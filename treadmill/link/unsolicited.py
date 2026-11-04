"""What the board says when nobody asked.

The firmware prints lines on its USB serial port whichever channel carried the
last command: the boot banner and a crash report after a reboot, ``Loading IO
handler: X`` on every handler swap, ``Remote Launch:`` lines, ``PHI2 timed``,
``Menu reported``, ``Reset cmd received`` and the like, plus two binary SID
tokens when a SID header is parsed. The link hands every such chunk here; the
classifier turns it into events a test can assert on, and the per-test serial
log keeps the raw text.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from treadmill.link.protocol import BAD_SID, GOOD_SID, board_reply

_SID_GOOD = board_reply(GOOD_SID)
_SID_BAD = board_reply(BAD_SID)

# (kind, pattern): the first pattern that matches a line names its kind.
_LINE_KINDS: list[tuple[str, re.Pattern[str]]] = [
    ("handler", re.compile(r"^Loading IO handler: (?P<name>.+?)\s*$")),
    ("launch", re.compile(r"^Remote Launch:")),
    ("reset", re.compile(r"^(Reset cmd received|Button detected|Resetting C64)")),
    ("boot_banner", re.compile(r"TeensyROM.* (?:is )?on-?line|^\s*FW: TeensyROM", re.I)),
    ("crash_report", re.compile(r"^CrashReport:")),
    ("phi2", re.compile(r"^PHI2 (?:timed|now)")),
    ("menu_reported", re.compile(r"^Menu reported")),
    ("eeprom_reset", re.compile(r"^--> Setting EEPROM to defaults")),
    ("unknown_command", re.compile(r"^Unk cmd")),
    ("timeout", re.compile(r"Timeout!")),
]


@dataclass(frozen=True)
class Event:
    kind: str          # handler | launch | reset | boot_banner | crash_report | phi2 |
                       # menu_reported | eeprom_reset | unknown_command | timeout | sid | text
    text: str
    at: float
    name: str | None = None   # the handler's name for 'handler'; good/bad for 'sid'


def classify(data: bytes, at: float | None = None) -> list[Event]:
    """Events for one chunk of unprompted bytes. Lines are classified one by
    one; the SID tokens are found as bytes, since they are not text."""
    at = time.monotonic() if at is None else at
    events: list[Event] = []
    for token, name in ((_SID_GOOD, "good"), (_SID_BAD, "bad")):
        count = data.count(token)
        events.extend(Event("sid", f"SID header {name}", at, name) for _ in range(count))
        data = data.replace(token, b"")
    text = data.decode("latin-1", "replace")
    for line in text.splitlines():
        line = line.strip("\r\n")
        if not line.strip():
            continue
        for kind, pattern in _LINE_KINDS:
            m = pattern.search(line)
            if m:
                events.append(Event(kind, line, at, m.groupdict().get("name")))
                break
        else:
            events.append(Event("text", line, at))
    return events


@dataclass
class EventLog:
    """Every unprompted event of a session, with the raw bytes behind them."""
    events: list[Event] = field(default_factory=list)
    raw: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes) -> list[Event]:
        self.raw += data
        new = classify(data)
        self.events.extend(new)
        return new

    def since(self, t0: float) -> list[Event]:
        return [e for e in self.events if e.at >= t0]

    def kinds(self, since: float = 0.0) -> set[str]:
        return {e.kind for e in self.events if e.at >= since}

    def last(self, kind: str) -> Event | None:
        for e in reversed(self.events):
            if e.kind == kind:
                return e
        return None

    def current_handler(self) -> str | None:
        e = self.last("handler")
        return e.name if e else None
