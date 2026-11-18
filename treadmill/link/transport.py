"""Byte streams to a TeensyROM: its USB serial port, its TCP listener, or an
in-process pipe for the self-tests.

A ``ByteStream`` reads on a background thread into a buffer, so nothing the
board sends between commands is lost: every chunk is also appended to ``tap``
with a timestamp, which is the raw material of the per-test serial log and
the unprompted-event classifier.
"""

from __future__ import annotations

import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from treadmill.errors import EXIT_NO_STATION, TreadmillError

TCP_PORT = 2112
CONNECT_TIMEOUT_S = 5.0
# The board drops what arrives while its USB serial is still settling after
# an open; the bench scripts wait the same 400 ms.
SERIAL_SETTLE_S = 0.4
SERIAL_BAUD = 115200
# PJRC's USB vendor id; a TeensyROM's device serial string carries its chip id.
PJRC_VID = 0x16C0
# The quiet periods are sized for a serial line that cannot say when the board
# is done. A fake board on an in-process pipe answers within a thread switch,
# so its pipe waits a tenth of each: 30 ms for the link's 0.3 s drain.
PIPE_QUIET_SCALE = 0.1


class TransportError(TreadmillError):
    """The port or connection could not be opened, or closed underneath us."""

    code = EXIT_NO_STATION


@dataclass
class Tap:
    """Everything received, in arrival order, for logs and event classification."""
    chunks: list[tuple[float, bytes]] = field(default_factory=list)

    def since(self, t0: float) -> bytes:
        return b"".join(c for t, c in self.chunks if t >= t0)

    def all(self) -> bytes:
        return b"".join(c for _, c in self.chunks)


class _Raw(Protocol):
    def read(self, n: int) -> bytes: ...
    def write(self, data: bytes) -> None: ...
    def close(self) -> None: ...


class ByteStream:
    def __init__(self, raw: _Raw, name: str, read_chunk: int = 4096, quiet_scale: float = 1.0):
        """``quiet_scale`` multiplies the quiet periods only (``drain``, and
        ``read_idle``'s ``idle``), never a deadline for the board's answer."""
        self._raw = raw
        self.name = name
        self.quiet_scale = quiet_scale
        self._buf = bytearray()
        self._cv = threading.Condition()
        self._ended = False
        self.tap = Tap()
        self._thread = threading.Thread(target=self._pump, args=(read_chunk,),
                                        name=f"treadmill-rx:{name}", daemon=True)
        self._thread.start()

    # -- reading -----------------------------------------------------------

    @property
    def ended(self) -> bool:
        return self._ended

    def _pump(self, chunk: int) -> None:
        try:
            while not self._ended:
                data = self._raw.read(chunk)
                if data is None:
                    break
                if data:
                    with self._cv:
                        self._buf += data
                        self.tap.chunks.append((time.monotonic(), bytes(data)))
                        self._cv.notify_all()
        except Exception:
            pass
        finally:
            with self._cv:
                self._ended = True
                self._cv.notify_all()

    def _take(self, count: int) -> bytes:
        out = bytes(self._buf[:count])
        del self._buf[:count]
        return out

    def read(self, count: int, timeout: float) -> bytes:
        """Up to ``count`` bytes; short when the deadline passes or the stream ends."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while len(self._buf) < count and not self._ended:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(left)
            return self._take(min(count, len(self._buf)))

    def read_until_any(self, markers: list[bytes], timeout: float) -> bytes | None:
        """Everything up to and including the first marker to appear, or None."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                end = self._end_of_first(markers)
                if end or self._ended:
                    return self._take(end) if end else None
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cv.wait(left)

    def read_until(self, marker: bytes, timeout: float) -> bytes | None:
        return self.read_until_any([marker], timeout)

    def read_idle(self, first: float, idle: float, total: float) -> bytes:
        """What the board sends next: waits ``first`` for the first byte, then
        until the stream has been quiet for ``idle``, never longer than ``total``."""
        limit = time.monotonic() + total
        deadline = time.monotonic() + first
        idle *= self.quiet_scale
        seen = 0
        with self._cv:
            while not self._ended:
                if len(self._buf) > seen:
                    seen = len(self._buf)
                    deadline = time.monotonic() + idle
                until = min(deadline, limit)
                left = until - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(left)
            return self._take(len(self._buf))

    def drain(self, quiet: float) -> bytes:
        """Wait ``quiet`` seconds and discard what arrived; returns it for the log."""
        time.sleep(quiet * self.quiet_scale)
        with self._cv:
            return self._take(len(self._buf))

    def settle(self, quiet: float, limit: float) -> tuple[bytes, bool]:
        """Discard what is waiting, and once anything was, what follows until
        nothing has arrived for ``quiet``; with nothing waiting, return at
        once. Returns the bytes for the log and whether the stream went quiet
        within ``limit``: a board that never stops sending is not."""
        quiet *= self.quiet_scale
        taken = bytearray()
        deadline = time.monotonic() + limit
        quiet_until = 0.0
        with self._cv:
            if not self._buf:
                return b"", True
            while True:
                now = time.monotonic()
                if self._buf:
                    taken += self._take(len(self._buf))
                    quiet_until = now + quiet
                elif self._ended or now >= quiet_until:
                    return bytes(taken), True
                if now >= deadline:
                    return bytes(taken), False
                self._cv.wait(min(quiet_until, deadline) - now)

    def _end_of_first(self, markers: list[bytes]) -> int:
        ends = []
        for m in markers:
            at = self._buf.find(m)
            if at >= 0:
                ends.append(at + len(m))
        return min(ends) if ends else 0

    # -- writing and closing ---------------------------------------------

    def write(self, data: bytes) -> None:
        """A write the port refuses or times out on (pyserial's 'Write timeout'
        when the board stopped reading, or has gone) is a TransportError."""
        if self._ended:
            raise TransportError(f"{self.name}: the connection to the board closed")
        try:
            self._raw.write(data)
        except OSError as e:
            raise TransportError(f"{self.name}: {e}") from e

    def close(self) -> None:
        with self._cv:
            self._ended = True
            self._cv.notify_all()
        try:
            self._raw.close()
        except Exception:
            pass


# -- USB serial ------------------------------------------------------------

class _SerialRaw:
    def __init__(self, port: str):
        import serial  # pyserial, imported here so the self-tests need no port
        try:
            self._ser = serial.Serial(port, SERIAL_BAUD, timeout=0.1, write_timeout=5)
        except serial.SerialException as e:
            raise TransportError(f"{port}: {e}") from e

    def read(self, n: int) -> bytes:
        waiting = self._ser.in_waiting
        return self._ser.read(max(1, min(n, waiting)) if waiting else 1)

    def write(self, data: bytes) -> None:
        self._ser.write(data)
        self._ser.flush()

    def close(self) -> None:
        self._ser.close()


def open_serial(port: str) -> ByteStream:
    """The board's USB serial port, settled. Refuses a port another program
    holds rather than retrying: that is the one rule the bench insists on."""
    stream = ByteStream(_SerialRaw(port), port)
    time.sleep(SERIAL_SETTLE_S)
    stream.drain(0)
    return stream


def list_teensyrom_ports() -> list[tuple[str, str | None]]:
    """(port, device serial string) for every PJRC device present. A TeensyROM's
    serial string names its chip id, so a station is found without opening
    any port."""
    from serial.tools import list_ports
    out = []
    for p in list_ports.comports():
        if p.vid == PJRC_VID:
            out.append((p.device, p.serial_number))
    return out


# -- TCP -------------------------------------------------------------------

class _SocketRaw:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._sock.settimeout(0.2)

    def read(self, n: int) -> bytes | None:
        try:
            data = self._sock.recv(n)
        except socket.timeout:
            return b""
        except OSError:
            return None
        return data if data else None   # b"" from recv means the peer closed

    def write(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()


def open_tcp(host: str, port: int = TCP_PORT) -> ByteStream:
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
    except OSError as e:
        raise TransportError(f"no answer from {host}:{port}: {e}") from e
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return ByteStream(_SocketRaw(sock), f"{host}:{port}")


def parse_host(text: str) -> tuple[str, int]:
    host, _, port = text.partition(":")
    return host, int(port) if port else TCP_PORT


# -- in-process pipe, for the self-tests --------------------------------------

class _QueueRaw:
    """One end of a pipe: reads from ``inbox``, writes to ``outbox``."""

    def __init__(self, inbox: "queue.Queue[bytes | None]", outbox: "queue.Queue[bytes | None]"):
        self._in, self._out = inbox, outbox

    def read(self, n: int) -> bytes | None:
        try:
            item = self._in.get(timeout=0.1)
        except queue.Empty:
            return b""
        return item   # None = the other end closed

    def write(self, data: bytes) -> None:
        self._out.put(bytes(data))

    def close(self) -> None:
        self._out.put(None)


def open_pipe(name: str = "pipe") -> tuple[ByteStream, "PipeEnd"]:
    """A ByteStream and the matching far end a fake board reads and writes."""
    to_board: queue.Queue = queue.Queue()
    to_host: queue.Queue = queue.Queue()
    stream = ByteStream(_QueueRaw(to_host, to_board), name, quiet_scale=PIPE_QUIET_SCALE)
    return stream, PipeEnd(to_board, to_host)


class PipeEnd:
    """The board's side of an in-process pipe."""

    def __init__(self, inbox: queue.Queue, outbox: queue.Queue):
        self._in, self._out = inbox, outbox
        self._pending = bytearray()
        self.closed = False          # the host closed its end: a port closing, DTR dropping

    def read(self, count: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self._pending) < count:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                item = self._in.get(timeout=left)
            except queue.Empty:
                break
            if item is None:
                self.closed = True
                break
            self._pending += item
        out = bytes(self._pending[:count])
        del self._pending[:count]
        return out

    def read_until(self, marker: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while marker not in self._pending:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                item = self._in.get(timeout=left)
            except queue.Empty:
                break
            if item is None:
                self.closed = True
                break
            self._pending += item
        at = self._pending.find(marker)
        end = at + len(marker) if at >= 0 else len(self._pending)
        out = bytes(self._pending[:end])
        del self._pending[:end]
        return out

    def write(self, data: bytes) -> None:
        self._out.put(bytes(data))

    def close(self) -> None:
        self._out.put(None)
