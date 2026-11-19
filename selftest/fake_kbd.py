"""A C64-USBKeyboard board at the far end of an in-process pipe, speaking
its host protocol (``Docs/Host_Protocol.md`` in the board's repo, proto 1)
closely enough for the client and the suite to be tested with no hardware:
one reply per line, the reasons, busy while a timed action runs, ``A``
answering after the action it stopped, hold timeouts as ``EV`` lines,
partial lines dropped after 500 ms, and a closed port releasing everything.

Each ``connect()`` is a host opening the port; closing the stream it
returns is the host closing it (a DTR drop). What the host did to the C64
is kept in lists, and the ``on_*`` hooks let a fake TeensyROM see a reset
or a standard switch.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from treadmill.link.transport import PIPE_QUIET_SCALE, ByteStream, PipeEnd, _QueueRaw
from treadmill.station.kbd import COMMANDS

OUTPUTS = ("tod", "menu", "alt", "reset", "kawari")
PARTIAL_S = 0.5
MAX_HOLD_MS = 10000
NAMES = {"return", "home", "clr", "del", "inst", "down", "up", "right", "left", "f1", "f2", "f3", "f4",
         "f5", "f6", "f7", "f8", "stop", "run", "restore"}
TYPABLE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 +-@*:;=,./\\^_!\"#$%&'()[]<>?")


class Refuse(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def text_keys(text: str) -> list[str]:
    """The keys ``S`` would type for ``text``, one entry each; Refuse('range')
    for a character with no C64 key or an unknown name."""
    keys, i = [], 0
    while i < len(text):
        c = text[i]
        if c == "{":
            end = text.find("}", i)
            if end < 0:
                raise Refuse("range")
            name = text[i + 1:end].lower()
            for mod in ("ctrl-", "c=-", "shift-"):
                if name.startswith(mod) and len(name) > len(mod):
                    rest = name[len(mod):]
                    if len(rest) == 1 and rest in TYPABLE or rest in NAMES:
                        break
                    raise Refuse("range")
            else:
                if name not in NAMES:
                    raise Refuse("range")
            keys.append("{" + name + "}")
            i = end + 1
            continue
        if c not in TYPABLE:
            raise Refuse("range")
        keys.append(c)
        i += 1
    return keys


# What the KERNAL's decode tables ($EB81 plain, $EBC2 with SHIFT) give GETIN
# for each name; RESTORE and the CTRL and C= forms give none here.
NAME_CODES = {"return": 0x0D, "shift-return": 0x8D, "home": 0x13, "clr": 0x93, "del": 0x14, "inst": 0x94,
              "down": 0x11, "up": 0x91, "right": 0x1D, "left": 0x9D, "stop": 0x03, "run": 0x83,
              "f1": 0x85, "f3": 0x86, "f5": 0x87, "f7": 0x88, "f2": 0x89, "f4": 0x8A, "f6": 0x8B, "f8": 0x8C}
CHAR_CODES = {"\\": 0x5C, "^": 0x5E, "_": 0x5F}


def getin_codes(text: str) -> bytes:
    """What a program's GETIN reads once ``S`` has typed ``text``: a plain
    letter is $41-$5A and a shifted one $C1-$DA in the C64's default set."""
    out = bytearray()
    for key in text_keys(text):
        if key.startswith("{"):
            code = NAME_CODES.get(key[1:-1])
            if code is not None:
                out.append(code)
        elif key.isalpha():
            out.append(ord(key.upper()) + (0x80 if key.isupper() else 0))
        else:
            out.append(CHAR_CODES.get(key, ord(key)))
    return bytes(out)


def valid_keys(text: str) -> list[str]:
    keys = text.split("+")
    if not 1 <= len(keys) <= 16 or any(len(k) != 2 or not k.isdigit() or k[0] > "7" or k[1] > "7" for k in keys):
        raise Refuse("range")
    return keys


def _int(text: str, low: int, high: int) -> int:
    try:
        value = int(text)
    except ValueError:
        raise Refuse("syntax") from None
    if not low <= value <= high:
        raise Refuse("range")
    return value


@dataclass
class Action:
    cmd: str
    seconds: float
    fields: dict
    abort: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    unstoppable_after: float | None = None    # V: once its CRESET_N pulse starts
    early_abort: Callable[[], None] | None = None


@dataclass
class FakeKeyboard:
    chip: str = "14470230"
    fitted: set[str] = field(default_factory=lambda: {"tod", "reset", "kawari"})
    mcu: str = "teensy36"
    fw: str = "1.0.0"
    proto: int = 1
    partial_s: float = PARTIAL_S
    # What the host did, for the self-tests to check.
    typed: list[str] = field(default_factory=list)
    pressed: list[str] = field(default_factory=list)
    restores: int = 0
    resets: list[int] = field(default_factory=list)       # pulse widths in µs
    switches: list[str] = field(default_factory=list)     # V open|closed, each that ran to the end
    buttons: list[str] = field(default_factory=list)
    received: list[str] = field(default_factory=list)     # every complete line
    # A fake TeensyROM or C64 watching: called after a reset pulse ends, a V ends, text is typed, keys pressed.
    on_reset: Callable[[], None] | None = None
    on_video: Callable[[str], None] | None = None
    on_type: Callable[[str], None] | None = None
    on_press: Callable[[list[str]], None] | None = None
    born: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tod = "50" if "tod" in self.fitted else "off"
        self.j1 = "open"
        self.loop = False
        self.e = 1
        self.holds: list[tuple[str, set[str], float]] = []    # (keys as sent, keys still held, deadline)
        self.reset_until: float | None = None
        self.action: Action | None = None
        self.lock = threading.RLock()
        self.end: PipeEnd | None = None

    # -- connecting --------------------------------------------------------

    def connect(self, name: str = "fake-kbd") -> ByteStream:
        """A host opening the board's port. One host at a time, like a COM port."""
        to_board: queue.Queue = queue.Queue()
        to_host: queue.Queue = queue.Queue()
        stream = ByteStream(_QueueRaw(to_host, to_board), name, quiet_scale=PIPE_QUIET_SCALE)
        end = PipeEnd(to_board, to_host)
        with self.lock:
            self.end = end
            self.e = 1
        threading.Thread(target=self._serve, args=(end,), name="fake-kbd", daemon=True).start()
        return stream

    def now_us(self) -> int:
        return int((time.monotonic() - self.born) * 1e6)

    def say(self, text: str, end: PipeEnd | None = None) -> None:
        with self.lock:
            end = end or self.end
            if end is not None and not end.closed and end is self.end:
                end.write((text + "\n").encode("ascii"))

    # -- the serve loop ----------------------------------------------------

    def _serve(self, end: PipeEnd) -> None:
        line = bytearray()
        last = time.monotonic()
        while True:
            b = end.read(1, 0.02)
            now = time.monotonic()
            if not b:
                if end.closed:
                    self._dtr_drop()
                    return
                if line and now - last >= self.partial_s:
                    line.clear()
                self._tick()
                continue
            last = now
            if b == b"\n":
                raw, line = bytes(line), bytearray()
                self._line(raw)
            elif b != b"\r":
                line += b

    def _tick(self) -> None:
        now = time.monotonic()
        with self.lock:
            for hold in list(self.holds):
                if now >= hold[2]:
                    self.holds.remove(hold)
                    self.say(f"EV K {hold[0]} timeout")
            if self.reset_until is not None and now >= self.reset_until:
                self.reset_until = None
                self.say("EV R timeout")
                self._reset_released()

    def _dtr_drop(self) -> None:
        with self.lock:
            self.end = None
            act = self.action
            if act is not None:
                act.abort.set()
        if act is not None:
            act.finished.wait(15)
        with self.lock:
            self._release_all()
            self.e = 1

    def _release_all(self) -> None:
        self.holds.clear()
        if self.reset_until is not None:
            self.reset_until = None
            self._reset_released()
        self.loop = False

    def _reset_released(self) -> None:
        if self.on_reset:
            self.on_reset()

    # -- one line ------------------------------------------------------------

    def _line(self, raw: bytes) -> None:
        if not raw:
            return
        if len(raw) > 120 or any(c < 0x20 or c > 0x7E for c in raw):
            self.say("ERR * syntax")
            return
        text = raw.decode("ascii")
        self.received.append(text)
        words = text.split()
        if not words:
            return
        cmd = words[0].upper()
        handler = getattr(self, "_cmd_" + {"?": "query"}.get(cmd, cmd.lower()), None) if cmd in COMMANDS else None
        if handler is None:
            self.say("ERR * syntax")
            return
        try:
            out = handler(words[1:], text)
        except Refuse as r:
            self.say(f"ERR {cmd} {r.reason}")
            return
        if out is not None:
            self.say(f"OK {cmd}" + (f" {out}" if out else ""))

    def _busy(self) -> None:
        with self.lock:
            if self.action is not None:
                raise Refuse("busy")

    def _need(self, *outputs: str) -> None:
        if not all(o in self.fitted for o in outputs):
            raise Refuse("notfitted")

    def _run(self, cmd: str, seconds: float, fields: dict, work: Callable[[], None] | None = None,
             unstoppable_after: float | None = None, early_abort: Callable[[], None] | None = None) -> None:
        """A timed action on its own thread: its reply comes when it ends."""
        act = Action(cmd, seconds, fields, unstoppable_after=unstoppable_after, early_abort=early_abort)
        with self.lock:
            self.action = act
        end = self.end
        threading.Thread(target=self._act, args=(act, work, end), daemon=True).start()

    def _act(self, act: Action, work, end) -> None:
        t0 = self.now_us()
        start = time.monotonic()
        ran = True
        if act.unstoppable_after is not None:
            if act.abort.wait(act.unstoppable_after):
                aborted, ran = True, False             # before the pulse: stop at once, nothing reloaded
                if act.early_abort:
                    act.early_abort()
            else:
                act.abort.wait(act.seconds - act.unstoppable_after)
                aborted = act.abort.is_set()           # after it: the reload ran, and /RESET is held to the end
                time.sleep(max(0.0, start + act.seconds - time.monotonic()))
        else:
            aborted = act.abort.wait(act.seconds)
            ran = not aborted
        t1 = self.now_us()
        if ran and work is not None:
            work()
        with self.lock:
            self.action = None
            fields = " ".join(f"{k}={v}" for k, v in act.fields.items())
            if aborted:
                self.say(f"ERR {act.cmd} aborted", end)
            else:
                self.say(f"OK {act.cmd} " + " ".join(p for p in (fields, f"t0={t0} t1={t1}") if p), end)
        act.finished.set()

    # -- the commands ----------------------------------------------------------

    def _cmd_query(self, args, text):
        if args:
            raise Refuse("syntax")
        held = "+".join(sorted({k for _, keys, _ in self.holds for k in keys})) or "-"
        fitted = ",".join(o for o in OUTPUTS if o in self.fitted) or "-"
        reset = ("1" if self.reset_until is not None else "0") if "reset" in self.fitted else "-"
        tod = self.tod if "tod" in self.fitted else "-"
        j1 = self.j1 if "kawari" in self.fitted else "-"
        return (f"proto={self.proto} name=C64USBKeyboard fw={self.fw} build=2026-11-17T12:00:00 chip={self.chip} "
                f"mcu={self.mcu} fitted={fitted} held={held} reset={reset} tod={tod} j1={j1} "
                f"loop={'on' if self.loop else 'off'} e={self.e}")

    def _cmd_time(self, args, text):
        return f"us={self.now_us()}"

    def _cmd_k(self, args, text):
        if len(args) < 2 or args[1].lower() not in ("d", "u"):
            raise Refuse("syntax")
        keys = valid_keys(args[0])
        self._busy()
        with self.lock:
            if args[1].lower() == "d":
                max_ms = _int(args[2], 1, MAX_HOLD_MS) if len(args) > 2 else MAX_HOLD_MS
                self.holds.append((args[0], set(keys), time.monotonic() + max_ms / 1000))
                self.pressed.append(args[0])
            else:
                for hold in list(self.holds):
                    hold[1].difference_update(keys)
                    if not hold[1]:
                        self.holds.remove(hold)
        return f"t={self.now_us()}"

    def _cmd_p(self, args, text):
        if not args:
            raise Refuse("syntax")
        keys = valid_keys(args[0])
        hold = _int(args[1], 1, 10000) if len(args) > 1 else 50
        gap = _int(args[2], 0, 10000) if len(args) > 2 else 50
        self._busy()

        def work():
            self.pressed.append("+".join(keys))
            if self.on_press:
                self.on_press(keys)
        self._run("P", (hold + gap) / 1000, {}, work)

    def _typing(self, cmd: str, text: str, hold: int, gap: int):
        keys = text_keys(text)
        seconds = len(keys) * (hold + gap) / 1000
        if seconds > 60:
            raise Refuse("range")
        self._busy()

        def work():
            self.typed.append(text)
            if self.on_type:
                self.on_type(text)
        self._run(cmd, seconds, {"n": len(keys)}, work)

    def _cmd_s(self, args, text):
        body = text.split(" ", 1)[1] if " " in text else ""
        if not body:
            raise Refuse("syntax")
        self._typing("S", body, 50, 50)

    def _cmd_st(self, args, text):
        parts = text.split(" ", 3)
        if len(parts) < 4:
            raise Refuse("syntax")
        hold, gap = _int(parts[1], 20, 10000), _int(parts[2], 20, 10000)
        self._typing("ST", parts[3], hold, gap)

    def _cmd_n(self, args, text):
        ms = _int(args[0], 1, 10000) if args else 50
        self._busy()

        def work():
            self.restores += 1
        self._run("N", ms / 1000, {}, work)

    def _cmd_a(self, args, text):
        with self.lock:
            act = self.action
            if act is not None:
                act.abort.set()
        if act is not None:
            act.finished.wait(15)
        with self.lock:
            self._release_all()
        return ""

    def _cmd_r(self, args, text):
        self._need("reset")
        word = args[0].lower() if args else ""
        if word == "hold":
            max_ms = _int(args[1], 1, MAX_HOLD_MS) if len(args) > 1 else MAX_HOLD_MS
            self._busy()
            with self.lock:
                self.reset_until = time.monotonic() + max_ms / 1000
            return f"t={self.now_us()}"
        if word == "release":
            self._busy()
            with self.lock:
                if self.reset_until is not None:
                    self.reset_until = None
                    self._reset_released()
            return f"t={self.now_us()}"
        us = _int(args[0], 10, 10_000_000) if args else 50000
        self._busy()

        def work():
            self.resets.append(us)
            self._reset_released()
        self._run("R", us / 1e6, {}, work)

    def _cmd_b(self, args, text):
        if not args or args[0].lower() not in ("menu", "alt"):
            raise Refuse("syntax")
        which = args[0].lower()
        ms = _int(args[1], 1, 10000) if len(args) > 1 else 50
        self._need(which)
        self._busy()

        def work():
            self.buttons.append(which)
        self._run("B", ms / 1000, {}, work)

    def _cmd_t(self, args, text):
        if len(args) != 1 or args[0].lower() not in ("50", "60", "off", "?"):
            raise Refuse("syntax")
        if args[0] == "?":
            return f"tod={self.tod if 'tod' in self.fitted else '-'}"
        self._need("tod")
        self._busy()
        self.tod = args[0].lower()
        return f"tod={self.tod}"

    def _cmd_v(self, args, text):
        if not args or args[0].lower() not in ("open", "closed", "?"):
            raise Refuse("syntax")
        if args[0] == "?":
            if "kawari" not in self.fitted:
                return "j1=- creset=-"
            return f"j1={self.j1} creset=released"
        setting = args[0].lower()
        restart = _int(args[1], 10, 10000) if len(args) > 1 else 1000
        self._need("kawari", "reset")
        self._busy()
        before = self.j1
        self.j1 = setting

        def work():
            self.switches.append(setting)
            if self.on_video:
                self.on_video(setting)
            self._reset_released()

        def put_back():
            self.j1 = before
        self._run("V", (1 + restart) / 1000, {"j1": setting}, work, unstoppable_after=0.001,
                  early_abort=put_back)

    def _cmd_d(self, args, text):
        if len(args) != 1 or args[0].lower() not in ("on", "off"):
            raise Refuse("syntax")
        self._busy()
        self.loop = args[0].lower() == "on"
        return f"loop={'on' if self.loop else 'off'}"

    def _cmd_x(self, args, text):
        body = text.split(" ", 1)[1] if " " in text else ""
        steps = [s.strip() for s in body.split(";") if s.strip()]
        if not steps:
            raise Refuse("syntax")
        if len(steps) > 16:
            raise Refuse("range")
        total = 0.0
        for step in steps:
            words = step.split()
            kind = words[0].lower()
            if kind in ("d", "u") and len(words) == 2:
                valid_keys(words[1])
            elif kind in ("w", "n") and len(words) == 2:
                total += _int(words[1], 0 if kind == "w" else 1, 10000) / 1000
            elif kind == "r" and len(words) == 2:
                self._need("reset")
                total += _int(words[1], 10, 10_000_000) / 1e6
            elif kind == "b" and len(words) == 3 and words[1].lower() in ("menu", "alt"):
                self._need(words[1].lower())
                total += _int(words[2], 1, 10000) / 1000
            else:
                raise Refuse("syntax")
        if total > 10:
            raise Refuse("range")
        self._busy()
        self._run("X", total, {"steps": len(steps)})

    def _cmd_e(self, args, text):
        if len(args) != 1 or args[0] not in ("0", "1"):
            raise Refuse("syntax")
        self.e = int(args[0])
        return f"e={self.e}"

    def _cmd_f(self, args, text):
        if args == ["?"]:
            return f"fitted={','.join(o for o in OUTPUTS if o in self.fitted) or '-'}"
        if len(args) != 2 or args[0].lower() not in OUTPUTS or args[1] not in ("0", "1"):
            raise Refuse("syntax")
        self._busy()
        (self.fitted.add if args[1] == "1" else self.fitted.discard)(args[0].lower())
        return f"fitted={','.join(o for o in OUTPUTS if o in self.fitted) or '-'}"
