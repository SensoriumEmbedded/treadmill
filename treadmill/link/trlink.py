"""``Link``: the commands a TeensyROM answers, over a ``ByteStream``.

The main image answers everything here. The minimal image answers reset,
launch, version and the firmware check and fails the rest with ``Busy!``.
The extension image runs with USB off and answers nothing. Reading and
writing C64 memory also needs a TR+ running its main image, and the link
refuses those commands until a probe has shown that: a plain TR's firmware
would read their address and length bytes as further commands.

Ported from the TeensyROM MCP server's ``tools/mcp/lib/trlink.mjs`` with the
bench library's extras (keys, screen, pause, get file).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from treadmill.c64.screen import (
    C128_RESET, CIA2_PORT_A, KEYBUF, KEYBUF_128, KEYBUF_SIZE, KEYCOUNT, KEYCOUNT_128, RESET_VECTOR,
    SCREEN_BYTES, VIC_MEMPTR, charset_in, screen_address, screen_rows,
)
from treadmill.errors import TreadmillError
from treadmill.link.banner import TR_PLUS, Banner, parse_banner
from treadmill.link.protocol import (
    ACK, BAD_SID, C64_PAUSE_OFF, C64_PAUSE_ON, DELETE_FILE, DIR_END, DIR_START, DRIVES, FAIL, FW_CHECK,
    GET_DIR_NDJSON, GET_FILE, GOOD_SID, IMAGES, LAUNCH_FILE, MENU_ONLY, PING, POST_FILE, READ_C64_MEM,
    RESET_C64, STATS_TRAILER_BYTES, VERSION_INFO, WRITE_C64_MEM, board_reply, checksum,
    from_board, path_field, to_board,
)
from treadmill.link.transport import ByteStream

REPLY_BYTES = 2
REPLY_S = 5.0
DRAIN_S = 0.3
FW_CHECK_S = 3.0
POST_DATA_S = 180.0
GET_DATA_S = 180.0
LISTING_S = 120.0
TRAILER_S = 0.3
TEXT_REPLY = (5.0, 0.3, 30.0)    # first byte, idle, total
FAIL_TEXT = (1.0, 0.1, 2.0)
KEYBUF_DRAIN_S = 5.0
# After a failed transfer the board discards what the sender still owed until a second passes with
# nothing arriving (ControlComms.md, under Write C64 Memory Spans; PostFile does the same), and a
# command sent inside that second is discarded with it.
DISCARD_QUIET_S = 1.2
# Sent on their own whenever a SID's header is parsed, the menu's startup SID partway through a boot
# included (ControlComms.md): never the reply a command is waiting for.
ASYNC_TOKENS = (GOOD_SID, BAD_SID)


class LinkError(TreadmillError):
    """The board did not do what was asked."""


class Refused(LinkError):
    """The board answered FailToken; ``text`` is its own explanation."""

    def __init__(self, what: str, text: str):
        super().__init__(f"{what}: the board refused it" + (f": {text}" if text else ""))
        self.text = text


class NoReply(LinkError):
    pass


class NotAllowed(LinkError):
    """A command the link refuses to send to this board (DMA on a plain TR)."""


def drive_number(name: str | int) -> int:
    if isinstance(name, int):
        return name
    if name in DRIVES:
        return DRIVES[name]
    raise LinkError(f"drive {name!r}: use {', '.join(DRIVES)}")


@dataclass(frozen=True)
class ScreenDump:
    address: int
    charset: str
    codes: bytes
    rows: list[str]


@dataclass(frozen=True)
class Probe:
    image: str | None          # main | minimal | None (nothing answered)
    banner: Banner | None

    @property
    def dma(self) -> bool:
        return self.image == "main" and self.banner is not None and self.banner.kind == TR_PLUS


class Link:
    def __init__(self, stream: ByteStream, *, dma_allowed: bool = False,
                 on_unsolicited: Callable[[bytes], None] | None = None):
        self._s = stream
        self.dma_allowed = dma_allowed
        self.c128 = False
        self._on_unsolicited = on_unsolicited or (lambda data: None)

    # -- lifecycle ----------------------------------------------------------

    @property
    def stream(self) -> ByteStream:
        return self._s

    def close(self) -> None:
        self._s.close()

    def __enter__(self) -> "Link":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- identity --------------------------------------------------------

    def fw_check(self) -> str | None:
        """'main', 'minimal', or None when nothing answered (an extension image,
        or no board). Reads past anything the board says unprompted."""
        self._send(to_board(FW_CHECK))
        replies = [board_reply(t) for t in IMAGES]
        seen = self._s.read_until_any(replies, FW_CHECK_S)
        if seen is None:
            return None
        if len(seen) > REPLY_BYTES:
            self._on_unsolicited(seen[:-REPLY_BYTES])
        return IMAGES[from_board(seen[-REPLY_BYTES:])]

    def version(self) -> str:
        self._send(to_board(VERSION_INFO))
        self._ack("version")
        return self._s.read_idle(*TEXT_REPLY).decode("latin-1")

    def banner(self) -> Banner:
        return parse_banner(self.version())

    def probe(self) -> Probe:
        """Which image and board this is; afterwards DMA commands are allowed
        exactly when the board is a TR+ running its main image."""
        image = self.fw_check()
        banner = self.banner() if image else None
        probe = Probe(image, banner)
        self.dma_allowed = probe.dma
        if banner and banner.machine:
            self.c128 = banner.machine == "C128"
        return probe

    def ping(self) -> str:
        """The ready line ('TeensyROM+ v0.9 ready!'); raises Refused('Busy!')
        once a launched program's handler has replaced the menu's."""
        self._send(to_board(PING))
        head = self._s.read(REPLY_BYTES, REPLY_S)
        if len(head) < REPLY_BYTES:
            raise NoReply("ping: no reply from the board")
        if from_board(head) == FAIL:
            raise Refused("ping", self._fail_text())
        rest = self._s.read_idle(*TEXT_REPLY)
        return (head + rest).decode("latin-1").strip()

    # -- control ---------------------------------------------------------

    def reset(self) -> str:
        """Reset the C64 to the menu. Answers with text lines, not an Ack; from
        the minimal image this restarts the board, so the port may drop."""
        self._send(to_board(RESET_C64))
        return self._s.read_idle(*TEXT_REPLY).decode("latin-1").strip()

    def pause(self, on: bool) -> None:
        self._send(to_board(C64_PAUSE_ON if on else C64_PAUSE_OFF))
        self._ack("pause" if on else "resume")

    def launch(self, path: str, drive: str | int = "sd") -> None:
        target = bytes([drive_number(drive)]) + path_field(path)
        self._send(to_board(LAUNCH_FILE))
        self._ack("launch")
        self._s.write(target)
        self._ack(f"launch {path}")

    # -- memory (TR+ main image only) --------------------------------------

    def _require_dma(self, what: str) -> None:
        if not self.dma_allowed:
            raise NotAllowed(f"{what}: DMA commands need a TeensyROM+ running its main image; "
                             "probe() first, and never send them to a plain TR")

    def read_memory(self, address: int, length: int, *, drain: float = DRAIN_S) -> bytes:
        """``drain``: how long to wait for stray bytes before the command. A
        caller that knows nothing is in flight can read many times faster."""
        self._require_dma(f"read of ${address:04X}")
        self._send(to_board(READ_C64_MEM) + to_board(address) + to_board(length), drain)
        self._ack(f"read of ${address:04X}")
        data = self._s.read(length, REPLY_S)
        if len(data) != length:
            raise LinkError(f"read of ${address:04X}: got {len(data)} of {length} bytes")
        return data

    def write_memory(self, address: int, data: bytes) -> None:
        self._require_dma(f"write to ${address:04X}")
        data = bytes(data)
        self._send(to_board(WRITE_C64_MEM) + to_board(address) + to_board(len(data)) + data)
        self._ack(f"write to ${address:04X}")

    def read_screen(self) -> ScreenDump:
        """The text screen wherever the VIC is pointed, decoded for the charset
        it is showing."""
        memptr = self.read_memory(VIC_MEMPTR, 1)[0]
        cia2 = self.read_memory(CIA2_PORT_A, 1)[0]
        address = screen_address(memptr, cia2)
        charset = charset_in(memptr, cia2)
        codes = self.read_memory(address, SCREEN_BYTES)
        return ScreenDump(address, charset, codes, screen_rows(codes, charset))

    def _key_buffer(self) -> tuple[int, int]:
        """The keyboard buffer and count the running KERNAL's GETIN reads. A
        C128 runs the TR menu and every C64 program in 64 mode, with the C64
        KERNAL's pair; it uses its own pair only in 128 mode, which its reset
        vector shows."""
        if self.c128 and int.from_bytes(self.read_memory(RESET_VECTOR, 2), "little") == C128_RESET:
            return KEYBUF_128, KEYCOUNT_128
        return KEYBUF, KEYCOUNT

    def key(self, code: int) -> None:
        """One key into the KERNAL keyboard buffer, so the next GETIN returns it.
        Programs that scan the keyboard matrix themselves never see it."""
        buf, count = self._key_buffer()
        self.write_memory(buf, bytes([code]))
        self.write_memory(count, bytes([1]))

    def keys(self, codes: bytes | list[int]) -> None:
        """Several keys, at most the buffer's ten at a time, waiting for the
        KERNAL to drain the buffer between chunks."""
        buf, count = self._key_buffer()
        codes = bytes(codes)
        for i in range(0, len(codes), KEYBUF_SIZE):
            chunk = codes[i:i + KEYBUF_SIZE]
            deadline = time.monotonic() + KEYBUF_DRAIN_S
            while self.read_memory(count, 1)[0] != 0:
                if time.monotonic() > deadline:
                    raise LinkError("the keyboard buffer did not drain; is a program reading keys?")
                time.sleep(0.05)
            self.write_memory(buf, chunk)
            self.write_memory(count, bytes([len(chunk)]))

    # -- files (main image, at the menu) ---------------------------------------

    def delete_file(self, path: str, drive: str | int = "sd") -> None:
        self._send(to_board(DELETE_FILE))
        self._ack("delete")
        self._s.write(bytes([drive_number(drive)]) + path_field(path))
        self._ack(f"delete {path}")
        self._trailer()

    def post_file(self, data: bytes, path: str, drive: str | int = "sd", *,
                  pace: tuple[int, float] | None = None, truncate: int | None = None) -> None:
        """Copy ``data`` to the board. The firmware refuses to overwrite, so a
        caller that wants replacement deletes first. ``pace`` sends the data
        as a slow host would, ``(bytes per write, seconds between writes)``;
        ``truncate`` sends only that many bytes and then waits for the board
        to give the transfer up, which it reports as a refusal, and for it
        to stop discarding the bytes it was still owed."""
        data = bytes(data)
        header = (to_board(len(data), 4) + to_board(checksum(data))
                  + bytes([drive_number(drive)]) + path_field(path))
        self._send(to_board(POST_FILE))
        self._ack("post")
        self._s.write(header)
        self._ack(f"post {path}")
        body = data if truncate is None else data[:truncate]
        if pace is None:
            self._s.write(body)
        else:
            chunk, pause = pace
            for i in range(0, len(body), chunk):
                self._s.write(body[i:i + chunk])
                time.sleep(pause)
        try:
            self._ack(f"post {path} data", POST_DATA_S)
        except Refused:
            if len(body) < len(data):
                time.sleep(DISCARD_QUIET_S)
            raise
        self._trailer()

    def push_file(self, data: bytes, path: str, drive: str | int = "sd") -> None:
        """Post, replacing any file at that path."""
        try:
            self.delete_file(path, drive)
        except Refused:
            pass
        self.post_file(data, path, drive)

    def get_file(self, path: str, drive: str | int = "sd") -> bytes:
        """Copy a file from the board. The board sends its length and checksum
        as 32-bit values, least significant byte first, then the data."""
        self._send(to_board(GET_FILE))
        self._ack("get")
        self._s.write(bytes([drive_number(drive)]) + path_field(path))
        self._ack(f"get {path}")
        head = self._s.read(8, REPLY_S)
        if len(head) < 8:
            raise NoReply(f"get {path}: no length from the board")
        length = int.from_bytes(head[:4], "little")
        expected = int.from_bytes(head[4:8], "little") & 0xFFFF
        data = self._s.read(length, GET_DATA_S)
        if len(data) != length:
            raise LinkError(f"get {path}: got {len(data)} of {length} bytes")
        self._ack(f"get {path} done")
        self._trailer()
        if checksum(data) != expected:
            raise LinkError(f"get {path}: checksum {checksum(data):04x} != {expected:04x}")
        return data

    def list_directory(self, path: str = "/", drive: str | int = "sd", *,
                       skip: int = 0, take: int = 1000) -> list[dict]:
        """One dict per entry as the board's NDJSON listing gives them."""
        request = bytes([drive_number(drive)]) + to_board(skip) + to_board(take) + path_field(path)
        self._send(to_board(GET_DIR_NDJSON))
        self._ack("directory")
        self._s.write(request)
        self._ack(f"directory {path}")
        self._expect(DIR_START, f"directory {path}")
        listing = self._s.read_until(board_reply(DIR_END), LISTING_S)
        if listing is None:
            raise LinkError(f"the listing of {path} stopped before its end marker")
        self._trailer()
        text = listing[:-REPLY_BYTES].decode("latin-1")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    # -- framing -----------------------------------------------------------

    def _send(self, data: bytes, drain: float = DRAIN_S) -> None:
        stale = self._s.drain(drain)
        if stale:
            self._on_unsolicited(stale)
        self._s.write(data)

    def _ack(self, what: str, timeout: float = REPLY_S) -> None:
        self._expect(ACK, what, timeout)

    def _expect(self, token: int, what: str, timeout: float = REPLY_S) -> None:
        deadline = time.monotonic() + timeout
        while True:
            reply = self._s.read(REPLY_BYTES, max(deadline - time.monotonic(), 0.0))
            if len(reply) < REPLY_BYTES:
                raise NoReply(f"{what}: no reply from the board")
            value = from_board(reply)
            if value not in ASYNC_TOKENS:
                break
            self._on_unsolicited(reply)
        if value == token:
            return
        if value == FAIL:
            raise Refused(what, self._fail_text())
        raise LinkError(f"{what}: unexpected reply 0x{value:04x}")

    def _fail_text(self) -> str:
        """The line after a FailToken. At the menu, the stats trailer follows
        it; the first line is the board's own words."""
        raw = self._s.read_idle(*FAIL_TEXT).decode("latin-1")
        return raw.strip().splitlines()[0] if raw.strip() else ""

    def _trailer(self) -> None:
        """Consume the two 32-bit stats the main image appends after a
        menu-only command, so they never masquerade as the next reply."""
        trailer = self._s.read(STATS_TRAILER_BYTES, TRAILER_S)
        if trailer and len(trailer) < STATS_TRAILER_BYTES:
            self._on_unsolicited(trailer)
