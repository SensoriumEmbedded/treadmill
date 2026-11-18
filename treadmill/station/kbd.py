"""The C64-USBKeyboard board: its host protocol over USB serial.

The board sits in one machine, on the C64's keyboard connector, and takes
text commands (``Docs/Host_Protocol.md`` in its repo, ``proto=1``): keys by
matrix position or typed as text, RESTORE, the C64's /RESET, the Kawari's
video standard, the TeensyROM's buttons and the CIAs' TOD clock. Every
command line gets one ``OK``/``ERR`` reply. A timed action replies once it
has finished, so a command can still be pending while another is sent, and
each reply goes to the oldest pending command of its name. Lines starting
``EV `` arrive on their own.

The board is found by its USB serial string, ``C64USBKeyboard_<chip id>``,
without opening any port. It is never sent TeensyROM tokens, and never
opened at 134 baud, which would reboot its Teensy into the bootloader.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Iterable

from treadmill.errors import EXIT_NO_STATION, TreadmillError
from treadmill.link.transport import ByteStream, TransportError, list_teensyrom_ports, open_serial

PROTO = 1
SERIAL_PREFIX = "C64USBKEYBOARD_"     # as Windows reports it; the board says C64USBKeyboard_
MAX_LINE = 120
COMMANDS = frozenset({"?", "TIME", "K", "P", "S", "ST", "N", "A", "R", "B", "T", "V", "D", "X", "E", "F"})
# A reply comes within this of the end of its action; a flash write for F can take 400 ms.
REPLY_S = 2.0
# The longest action of each kind the board accepts: S types for up to 60 s, X runs up to 10 s.
S_LIMIT_S = 60.0
X_LIMIT_S = 10.0
V_RESTART_MS = 1000
CRESET_PULSE_MS = 1
FIND_POLL_S = 0.5


class KbdError(TreadmillError):
    """The keyboard board did not answer as its protocol says, or went away."""


class KbdNotFound(KbdError):
    code = EXIT_NO_STATION


class KbdRefused(KbdError):
    """The board answered ``ERR``."""

    def __init__(self, reply: "Reply"):
        super().__init__(f"keyboard board refused {reply.line!r}: {reply.raw}")
        self.reply = reply


@dataclass(frozen=True)
class Reply:
    ok: bool
    cmd: str                   # the command name as the board echoes it, or "*"
    reason: str | None         # syntax | range | busy | notfitted | aborted, on ERR
    fields: dict[str, str]
    raw: str
    t_host: float              # time.monotonic() when the line arrived
    line: str = ""             # the command it answers

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.fields.get(key, default)

    def __getitem__(self, key: str) -> str:
        return self.fields[key]


def parse_reply(text: str, t_host: float = 0.0) -> Reply | None:
    """An ``OK``/``ERR`` line as a Reply; None for anything else."""
    words = text.split()
    if len(words) < 2 or words[0] not in ("OK", "ERR"):
        return None
    ok = words[0] == "OK"
    rest = words[2:]
    reason = None
    if not ok and rest:
        reason, rest = rest[0], rest[1:]
    fields = dict(w.split("=", 1) for w in rest if "=" in w)
    return Reply(ok, words[1], reason, fields, text, t_host)


def expected_cmd(line: str) -> str | None:
    """The command name the board's reply to ``line`` will carry: ``*`` for a
    line it can't read (too long, a byte outside printable ASCII, an unknown
    command), else the first word upper-cased. None for a blank line, which
    gets no reply."""
    data = line.encode("utf-8")
    if len(data) > MAX_LINE or any(b < 0x20 or b > 0x7E for b in data):
        return "*"
    words = line.split()
    if not words:
        return None
    name = words[0].upper()
    return name if name in COMMANDS else "*"


def _num(args: list[str], i: int, default: int) -> int:
    try:
        return int(args[i])
    except (IndexError, ValueError):
        return default


def action_seconds(line: str) -> float:
    """How long the action ``line`` asks for runs on the board, from its own
    arguments and the protocol's defaults: what its reply waits on."""
    words = line.split()
    if not words:
        return 0.0
    cmd, args = words[0].upper(), words[1:]
    if cmd in ("S", "ST"):
        return S_LIMIT_S
    if cmd == "X":
        return X_LIMIT_S
    if cmd == "V" and args and args[0] != "?":
        return (CRESET_PULSE_MS + _num(args, 1, V_RESTART_MS)) / 1000
    if cmd == "R" and not (args and args[0].lower() in ("hold", "release")):
        return _num(args, 0, 50000) / 1e6
    if cmd == "P":
        return (_num(args, 1, 50) + _num(args, 2, 50)) / 1000
    if cmd == "N":
        return _num(args, 0, 50) / 1000
    if cmd == "B":
        return _num(args, 1, 50) / 1000
    return 0.0


def keys_text(keys: str | Iterable[str]) -> str:
    """Keys as the protocol writes them: ``"17+12"``, from a string or a list."""
    return keys if isinstance(keys, str) else "+".join(keys)


# -- finding the board -------------------------------------------------------------

@dataclass(frozen=True)
class KbdPort:
    port: str
    serial: str      # the USB serial string as the OS reports it
    chip: str


def keyboard_chip(serial_string: str | None) -> str | None:
    """The chip id in a keyboard board's USB serial string, or None for any
    other device. Windows reports the string in capitals."""
    if not serial_string or not serial_string.upper().startswith(SERIAL_PREFIX):
        return None
    chip = serial_string[len(SERIAL_PREFIX):]
    return chip if chip.isdigit() else None


def find_keyboards() -> list[KbdPort]:
    """Every keyboard board on USB, by serial string; no port is opened."""
    out = []
    for port, serial_string in list_teensyrom_ports():
        chip = keyboard_chip(serial_string)
        if chip:
            out.append(KbdPort(port, serial_string, chip))
    return out


def find_keyboard(chip: str | None = None, port_hint: str | None = None, timeout: float = 0.0) -> KbdPort:
    """The keyboard board with chip id ``chip``, or the only one on USB when
    ``chip`` is None. With a ``timeout`` the search repeats, for a board that
    comes up with its machine."""
    deadline = time.monotonic() + timeout
    while True:
        boards = find_keyboards()
        match = [b for b in boards if chip is None or b.chip == str(chip)]
        if len(match) > 1 and port_hint:
            match = [b for b in match if b.port == port_hint] or match
        if len(match) == 1:
            return match[0]
        if len(match) > 1:
            listed = ", ".join(f"{b.port} (chip {b.chip})" for b in match)
            raise KbdError(f"{len(match)} keyboard boards on USB ({listed}); name one by its chip id")
        if time.monotonic() >= deadline:
            seen = ", ".join(f"{b.port} (chip {b.chip})" for b in boards) or "none"
            want = f"with chip id {chip}" if chip else "at all"
            raise KbdNotFound(f"no keyboard board {want} on USB; keyboard boards present: {seen}")
        time.sleep(FIND_POLL_S)


# -- the open board ----------------------------------------------------------------

@dataclass(eq=False)
class Pending:
    """A command sent and not yet answered."""
    line: str
    cmd: str
    timeout: float
    t_sent: float
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    _reply: Reply | None = field(default=None, repr=False)
    _error: str | None = field(default=None, repr=False)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def _finish(self, reply: Reply | None, error: str | None = None) -> None:
        self._reply, self._error = reply, error
        self._done.set()

    def wait(self, timeout: float | None = None) -> Reply:
        """The reply. ``timeout`` counts from now; without one, from when the
        line was sent, for as long as its action plus the reply margin."""
        left = timeout if timeout is not None else self.t_sent + self.timeout - time.monotonic()
        if not self._done.wait(max(left, 0.0)):
            raise KbdError(f"no reply to {self.line!r} from the keyboard board within "
                           f"{(timeout if timeout is not None else self.timeout):g} s")
        if self._reply is None:
            raise KbdError(f"no reply to {self.line!r}: {self._error}")
        return self._reply


class KeyboardBoard:
    """An open keyboard board. ``open()`` finds it by chip id; ``attach()``
    takes a stream already open (a fake board in the self-tests)."""

    def __init__(self, stream: ByteStream, *, port: str | None = None, usb_serial: str | None = None):
        self.stream = stream
        self.port = port or stream.name
        self.usb_serial = usb_serial
        self.identity: dict[str, str] = {}
        self.events: list[tuple[float, str]] = []
        self.lines: list[tuple[float, str]] = []
        self.stray: list[Reply] = []            # replies no pending command claimed
        self._pending: list[Pending] = []
        self._cv = threading.Condition()
        self._write_lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._read, name=f"treadmill-kbd:{self.port}", daemon=True)
        self._thread.start()

    # -- opening and closing -------------------------------------------------

    @classmethod
    def open(cls, chip: str | None = None, port_hint: str | None = None, *, timeout: float = 0.0) -> "KeyboardBoard":
        found = find_keyboard(chip, port_hint, timeout=timeout)
        try:
            stream = open_serial(found.port)
        except TransportError as e:
            raise KbdError(f"keyboard board chip {found.chip}: {e}") from None
        return cls.attach(stream, port=found.port, usb_serial=found.serial)

    @classmethod
    def attach(cls, stream: ByteStream, *, port: str | None = None,
               usb_serial: str | None = None) -> "KeyboardBoard":
        board = cls(stream, port=port, usb_serial=usb_serial)
        try:
            board._handshake()
        except BaseException:
            board.close()
            raise
        return board

    def _handshake(self) -> None:
        """A bare LF ends whatever a previous host left half sent; ``?`` must
        speak this protocol; then the key and debug lines go quiet."""
        self.write_raw(b"\n")
        reply = self.command("?")
        if not reply.ok:
            raise KbdError(f"{self.port} is not a keyboard board that answers '?': {reply.raw}")
        proto = reply.get("proto")
        if proto != str(PROTO):
            raise KbdError(f"the keyboard board on {self.port} speaks protocol {proto}; treadmill speaks {PROTO}")
        self.identity = dict(reply.fields)
        self.ok("E 0")

    def close(self) -> None:
        with self._cv:
            if self._closed:
                return
            self._closed = True
        self.stream.close()
        self._thread.join(timeout=1.0)

    def __enter__(self) -> "KeyboardBoard":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def chip(self) -> str | None:
        return self.identity.get("chip") or keyboard_chip(self.usb_serial)

    @property
    def fitted(self) -> frozenset[str]:
        """The off-PCB outputs marked fitted when the board was opened."""
        return _listed(self.identity.get("fitted"))

    # -- the line reader -----------------------------------------------------

    def _read(self) -> None:
        while True:
            chunk = self.stream.read_until(b"\n", 0.2)
            if chunk is None:
                if self.stream.ended:
                    break
                continue
            self._dispatch(time.monotonic(), chunk.decode("ascii", "replace").rstrip("\r\n"))
        with self._cv:
            pending, self._pending = self._pending, []
            self._cv.notify_all()
        for p in pending:
            p._finish(None, "the keyboard board's port closed")

    def _dispatch(self, t: float, text: str) -> None:
        with self._cv:
            self.lines.append((t, text))
            if text.startswith("EV "):
                self.events.append((t, text))
            else:
                reply = parse_reply(text, t)
                if reply is not None:
                    for p in self._pending:
                        if p.cmd == reply.cmd:
                            self._pending.remove(p)
                            p._finish(replace(reply, line=p.line))
                            break
                    else:
                        self.stray.append(reply)
            self._cv.notify_all()

    # -- sending -------------------------------------------------------------

    def write_raw(self, data: bytes) -> None:
        """Bytes as they are, no LF added and no reply waited for."""
        if self._closed:
            raise KbdError(f"the keyboard board on {self.port} is closed")
        with self._write_lock:
            try:
                self.stream.write(data)
            except TransportError as e:
                raise KbdError(str(e)) from None

    def start(self, line: str, timeout: float | None = None) -> Pending:
        """Send one command line and return at once; ``wait()`` on the result
        for its reply. Other commands, ``abort()`` and ``close()`` may follow
        while it is pending."""
        if "\n" in line or "\r" in line:
            raise ValueError("one command per line; send line breaks with write_raw()")
        cmd = expected_cmd(line)
        if cmd is None:
            raise ValueError("a blank line gets no reply; send it with write_raw()")
        pending = Pending(line, cmd, timeout if timeout is not None else action_seconds(line) + REPLY_S,
                          time.monotonic())
        with self._cv:
            if self._closed:
                raise KbdError(f"the keyboard board on {self.port} is closed")
            self._pending.append(pending)
        try:
            self.write_raw(line.encode("utf-8") + b"\n")
        except KbdError:
            with self._cv:
                if pending in self._pending:
                    self._pending.remove(pending)
            raise
        return pending

    def command(self, line: str, timeout: float | None = None) -> Reply:
        """One command line and its reply, ``OK`` or ``ERR`` alike."""
        return self.start(line, timeout).wait()

    def ok(self, line: str, timeout: float | None = None) -> Reply:
        """One command line, refused with KbdRefused unless the board says ``OK``."""
        reply = self.command(line, timeout)
        if not reply.ok:
            raise KbdRefused(reply)
        return reply

    # -- listening -----------------------------------------------------------

    def read_lines(self, seconds: float) -> list[str]:
        """Every line that arrives in the next ``seconds``, events and replies alike."""
        with self._cv:
            start = len(self.lines)
        time.sleep(seconds)
        with self._cv:
            return [text for _, text in self.lines[start:]]

    def wait_event(self, pattern: str, timeout: float, since: float | None = None) -> tuple[float, str] | None:
        """The first ``EV`` line matching the regex ``pattern`` that arrived at
        or after ``since`` (default: now), waiting up to ``timeout``."""
        rx = re.compile(pattern)
        since = time.monotonic() if since is None else since
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for t, text in self.events:
                    if t >= since and rx.search(text):
                        return t, text
                left = deadline - time.monotonic()
                if left <= 0 or self.stream.ended:
                    return None
                self._cv.wait(left)

    # -- the commands ----------------------------------------------------------

    def status(self) -> dict[str, str]:
        """``?`` now: identity and the state of every output."""
        return dict(self.ok("?").fields)

    def time_us(self) -> int:
        return int(self.ok("TIME")["us"])

    def hold(self, keys: str | Iterable[str], max_ms: int | None = None) -> Reply:
        return self.ok(_join("K", keys_text(keys), "d", max_ms))

    def release(self, keys: str | Iterable[str]) -> Reply:
        return self.ok(f"K {keys_text(keys)} u")

    def press(self, keys: str | Iterable[str], hold_ms: int | None = None, gap_ms: int | None = None) -> Reply:
        if gap_ms is not None and hold_ms is None:
            hold_ms = 50
        return self.ok(_join("P", keys_text(keys), hold_ms, gap_ms))

    def type(self, text: str, hold_ms: int | None = None, gap_ms: int | None = None) -> Reply:
        """Text as the protocol's ``S`` types it: ``{return}`` and friends in braces."""
        if hold_ms is None and gap_ms is None:
            return self.ok(f"S {text}")
        return self.ok(f"ST {hold_ms or 50} {gap_ms or 50} {text}")

    def restore(self, ms: int | None = None) -> Reply:
        return self.ok(_join("N", ms))

    def reset(self, us: int | None = None) -> Reply:
        """A /RESET pulse; ``t1 - t0`` of the reply is its width."""
        return self.ok(_join("R", us))

    def reset_hold(self, max_ms: int | None = None) -> Reply:
        return self.ok(_join("R hold", max_ms))

    def reset_release(self) -> Reply:
        return self.ok("R release")

    def button(self, which: str, ms: int | None = None) -> Reply:
        return self.ok(_join("B", which, ms))

    def tod(self, rate: int | str | None = None) -> str:
        """Set the TOD clock (50, 60 or "off") and return what it is now;
        with no rate, only read it."""
        return self.ok("T ?" if rate is None else f"T {rate}")["tod"]

    def video(self, setting: str, restart_ms: int | None = None) -> Reply:
        """The Kawari's standard: ``open`` gives its saved model, ``closed`` the other."""
        return self.ok(_join("V", setting, restart_ms))

    def video_state(self) -> dict[str, str]:
        return dict(self.ok("V ?").fields)

    def sequence(self, steps: Iterable[str]) -> Reply:
        return self.ok("X " + ";".join(steps))

    def loopback(self, on: bool) -> Reply:
        return self.ok(f"D {'on' if on else 'off'}")

    def abort(self) -> list[Reply]:
        """``A``: release every host hold and stop a running action. Returns
        the replies it caused in arrival order: the aborted action's ``ERR``,
        then ``OK A``."""
        with self._cv:
            running = list(self._pending)
        ok_a = self.ok("A", timeout=max([p.timeout for p in running], default=0.0) + REPLY_S)
        caused = [p._reply for p in running if p.done and p._reply is not None
                  and p._reply.reason == "aborted"]
        return sorted(caused + [ok_a], key=lambda r: r.t_host)


def _join(*parts: object) -> str:
    return " ".join(str(p) for p in parts if p is not None)


def _listed(value: str | None) -> frozenset[str]:
    if not value or value == "-":
        return frozenset()
    return frozenset(v for v in value.split(",") if v)
