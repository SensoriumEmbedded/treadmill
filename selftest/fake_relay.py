"""A mains relay at the far end of an in-process pipe, speaking its host
protocol (``docs/Host_Protocol.md`` in the mains-relay repository, proto 1)
closely enough for the power backend and the recovery ladder to be tested
with no hardware: one reply per line, the minimum off time, ``CYCLE``
replying once power is back, ``OFF``'s limit as an ``EV`` line, ``busy``,
a closed port restoring power, and ``OFF <ch> LATCH`` (fw 1.1.0), which
nothing but ``ON`` ends. ``fw="1.0.0"`` makes a relay that predates it. Its clock runs ``1 / time_scale`` times
as fast as the host's, so a 5 s cycle takes 50 ms.

Each ``connect()`` is a host opening the port; closing the stream it
returns is the host closing it (a DTR drop). ``on_power(channel, on)`` lets
a fake machine see its mains go and come back.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from treadmill.link.transport import PIPE_QUIET_SCALE, ByteStream, PipeEnd, _QueueRaw

MIN_OFF_MS = 2000
OFF_DEFAULT_MS, OFF_MAX_MS = 600000, 3600000
CYCLE_DEFAULT_MS, CYCLE_MAX_MS = 5000, 60000


@dataclass
class _Chan:
    off: bool = False
    kind: str = "held"             # held | cycling | releasing | latched
    off_at: float = 0.0
    restore_at: float = 0.0
    ms: int = 0
    cycle_owed: bool = False
    early: bool = False
    on_owed: int = 0


@dataclass
class FakeRelay:
    chip: str = "21474836"
    channels: int = 1
    proto: int = 1
    fw: str = "1.1.0"
    time_scale: float = 0.01
    on_power: Callable[[int, bool], None] | None = None
    # What happened, for the self-tests to check.
    received: list[str] = field(default_factory=list)
    switched: list[tuple[int, bool]] = field(default_factory=list)   # (channel, on), every relay change
    cuts_ms: list[int] = field(default_factory=list)                 # how long each cut lasted, in relay ms

    def __post_init__(self) -> None:
        self.ch = [_Chan() for _ in range(self.channels)]
        self.lock = threading.RLock()
        self.end: PipeEnd | None = None
        self._ticking = False

    # -- connecting --------------------------------------------------------

    def connect(self, name: str = "fake-relay") -> ByteStream:
        """A host opening the relay's port. One host at a time, like a COM port."""
        to_relay: queue.Queue = queue.Queue()
        to_host: queue.Queue = queue.Queue()
        stream = ByteStream(_QueueRaw(to_host, to_relay), name, quiet_scale=PIPE_QUIET_SCALE)
        end = PipeEnd(to_relay, to_host)
        with self.lock:
            self.end = end
        threading.Thread(target=self._serve, args=(end,), name="fake-relay", daemon=True).start()
        return stream

    def power(self, channel: int = 1) -> bool:
        with self.lock:
            return not self.ch[channel - 1].off

    def _now(self) -> float:
        return time.monotonic() / self.time_scale     # in relay seconds

    def _say(self, text: str) -> None:
        end = self.end
        if end is not None and not end.closed:
            end.write((text + "\n").encode("ascii"))

    def _set(self, i: int, on: bool) -> None:
        self.switched.append((i + 1, on))
        if self.on_power:
            self.on_power(i + 1, on)

    # -- the serve loop and the clock ----------------------------------------

    def _serve(self, end: PipeEnd) -> None:
        line = bytearray()
        while True:
            b = end.read(1, 0.01)
            if not b:
                if end.closed:
                    self._dtr_drop(end)
                    return
                continue
            if b == b"\n":
                raw, line = bytes(line), bytearray()
                with self.lock:
                    if end is self.end:
                        self._line(raw)
            elif b != b"\r":
                line += b

    def _ticker(self) -> None:
        """Ends cuts on the relay's clock; runs only while a channel is cut,
        so an idle fake costs no thread."""
        while True:
            time.sleep(0.002)
            with self.lock:
                self._poll()
                if not any(c.off and (c.kind != "latched" or c.early) for c in self.ch):
                    self._ticking = False
                    return

    def _poll(self) -> None:
        now = self._now()
        for i, c in enumerate(self.ch):
            if c.off and now >= c.restore_at:
                self._restore(i)

    def _restore(self, i: int) -> None:
        c = self.ch[i]
        self.ch[i] = _Chan()
        self.cuts_ms.append(round((self._now() - c.off_at) * 1000))
        self._set(i, True)
        if c.cycle_owed:
            self._say("ERR CYCLE aborted" if c.early else f"OK CYCLE ch={i + 1} off_ms={c.ms}")
        for _ in range(c.on_owed):
            self._say(f"OK ON ch={i + 1} power=on")
        if c.kind == "held" and not c.early:
            self._say(f"EV OFF {i + 1} timeout")

    def _end_early(self, i: int) -> None:
        c = self.ch[i]
        c.restore_at = min(c.restore_at, max(self._now(), c.off_at + MIN_OFF_MS / 1000))
        c.early = True

    def _dtr_drop(self, end: PipeEnd) -> None:
        with self.lock:
            if end is not self.end:
                return
            self.end = None
            for i, c in enumerate(self.ch):
                if c.off and c.kind != "latched":
                    self._end_early(i)
                    c.kind, c.cycle_owed, c.on_owed = "releasing", False, 0
            self._poll()

    def _cut(self, i: int, kind: str, ms: int) -> None:
        now = self._now()
        self.ch[i] = _Chan(off=True, kind=kind, off_at=now, restore_at=now + ms / 1000, ms=ms)
        self._set(i, False)
        self._tick()

    def _tick(self) -> None:
        if not self._ticking:
            self._ticking = True
            threading.Thread(target=self._ticker, name="fake-relay-tick", daemon=True).start()

    # -- commands ----------------------------------------------------------

    def _line(self, raw: bytes) -> None:
        self._poll()
        text = raw.decode("ascii", "replace")
        self.received.append(text)
        words = text.split()
        if not words:
            return
        cmd, args = words[0].upper(), words[1:]
        handler = {"?": self._identify, "STATE": self._state, "ON": self._on, "OFF": self._off,
                   "CYCLE": self._cycle}.get(cmd)
        if handler is None:
            self._say("ERR * syntax")
        else:
            handler(cmd, args)

    def _channel(self, cmd: str, arg: str) -> int | None:
        if not arg.isdigit():
            self._say(f"ERR {cmd} syntax")
            return None
        if not 1 <= int(arg) <= self.channels:
            self._say(f"ERR {cmd} range")
            return None
        return int(arg) - 1

    def _millis(self, cmd: str, args: list[str], default: int, hi: int) -> int | None:
        if len(args) < 2:
            return default
        if not args[1].isdigit():
            self._say(f"ERR {cmd} syntax")
            return None
        if not MIN_OFF_MS <= int(args[1]) <= hi:
            self._say(f"ERR {cmd} range")
            return None
        return int(args[1])

    def _word(self, i: int) -> str:
        c = self.ch[i]
        return "on" if not c.off else ("cycling" if c.kind == "cycling" else "off")

    def _identify(self, cmd: str, args: list[str]) -> None:
        if args:
            self._say("ERR ? syntax")
            return
        state = ",".join(self._word(i) for i in range(self.channels))
        self._say(f"OK ? proto={self.proto} name=MainsRelay fw={self.fw} build=2026-11-22T12:00:00 chip={self.chip} "
                  f"mcu=teensy31 ch={self.channels} state={state} min_off_ms={MIN_OFF_MS} "
                  f"max_off_ms={OFF_DEFAULT_MS}")

    def _state(self, cmd: str, args: list[str]) -> None:
        if len(args) != 1:
            self._say("ERR STATE syntax")
            return
        i = self._channel(cmd, args[0])
        if i is None:
            return
        c = self.ch[i]
        latched = c.off and c.kind == "latched"
        left = max(0, round((c.restore_at - self._now()) * 1000)) if c.off and (c.early or not latched) else 0
        self._say(f"OK STATE ch={i + 1} power={self._word(i)} left_ms={left}" + (" latch=1" if latched else ""))

    def _on(self, cmd: str, args: list[str]) -> None:
        if len(args) != 1:
            self._say("ERR ON syntax")
            return
        i = self._channel(cmd, args[0])
        if i is None:
            return
        if not self.ch[i].off:
            self._say(f"OK ON ch={i + 1} power=on")
            return
        self.ch[i].on_owed += 1
        self._end_early(i)
        self._poll()
        self._tick()                   # a latch stopped the clock

    def _off(self, cmd: str, args: list[str]) -> None:
        if not 1 <= len(args) <= 2:
            self._say("ERR OFF syntax")
            return
        i = self._channel(cmd, args[0])
        if i is None:
            return
        latch = len(args) == 2 and args[1].upper() == "LATCH" and self.fw >= "1.1.0"
        ms = 0 if latch else self._millis(cmd, args, OFF_DEFAULT_MS, OFF_MAX_MS)
        if ms is None:
            return
        c = self.ch[i]
        if c.off and (c.kind == "cycling" or c.on_owed):
            self._say("ERR OFF busy")
            return
        if c.off and c.kind == "latched":
            latch, ms = True, 0            # only ON ends a latch
        if not c.off:
            self._cut(i, "held", ms)
        c = self.ch[i]
        c.kind, c.early, c.ms = ("latched" if latch else "held"), False, ms
        c.restore_at = float("inf") if latch else self._now() + ms / 1000
        self._say(f"OK OFF ch={i + 1} power=off max_ms={ms}" + (" latch=1" if latch else ""))

    def _cycle(self, cmd: str, args: list[str]) -> None:
        if not 1 <= len(args) <= 2:
            self._say("ERR CYCLE syntax")
            return
        i = self._channel(cmd, args[0])
        if i is None:
            return
        ms = self._millis(cmd, args, CYCLE_DEFAULT_MS, CYCLE_MAX_MS)
        if ms is None:
            return
        if self.ch[i].off:
            self._say("ERR CYCLE busy")
            return
        self._cut(i, "cycling", ms)
        self.ch[i].cycle_owed = True
