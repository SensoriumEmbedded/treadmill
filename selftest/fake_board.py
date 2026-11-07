"""A TeensyROM main image at the far end of an in-process pipe, answering the
commands as ``docs/ControlComms.md`` describes them. The self-tests run the
link against it with no hardware; it is the Python counterpart of the MCP
server's ``tools/mcp/test/fake-board.mjs`` and models the same details,
including the two 32-bit stats the firmware appends after a menu-only
command."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

from selftest.fake_menu import TREE, UP_DIR, FakeMenu
from treadmill.c64.screen import KEYBUF, KEYCOUNT, SCREEN_BYTES, SCREEN_RAM_DEFAULT, codes_for_text
from treadmill.link.banner import parse_banner
from treadmill.link.protocol import (
    ACK, C64_PAUSE_OFF, C64_PAUSE_ON, DELETE_FILE, DIR_END, DIR_START, DRIVE_TEENSY, FAIL, FW_CHECK,
    FW_FULL, FW_MINIMAL, GET_DIR_NDJSON, GET_FILE, LAUNCH_FILE, MENU_ONLY, PING, POST_FILE,
    READ_C64_MEM, RESET_C64, VERSION_INFO, WRITE_C64_MEM, board_reply, checksum,
)
from treadmill.link.transport import PipeEnd, open_pipe

WAIT_S = 2.0
STATS_TRAILER = bytes([1, 0, 0, 0, 0x00, 0x2C, 0xA3, 0x30])
BANNER = ("\n  FW: TeensyROM+ v0.9\r\n      Oct 30 2026, 10:00:00\r\n"
          "  Teensy: 816MHz  55.0C  UID: 19277260\r\n  C64  NTSC Vid  60 Hz\n  Boot: complete\n")
BANNER_TR = BANNER.replace("TeensyROM+", "TeensyROM")
READY_LINE = "TeensyROM+ v0.9 ready!\n"
# Commands the firmware answers whatever else it is doing (ProcessCommand's first switch).
ALWAYS = {FW_CHECK, VERSION_INFO, RESET_C64, LAUNCH_FILE, C64_PAUSE_ON, C64_PAUSE_OFF,
          READ_C64_MEM, WRITE_C64_MEM}


@dataclass
class FakeBoard:
    image: str = "main"                  # main | minimal
    banner: str = BANNER
    busy: bool = False                   # a launched program's handler is active
    silent: bool = False                 # an extension image: answers nothing
    dma: bool = True                     # a TR+; a plain TR has no DMA commands at all
    memory: bytearray = field(default_factory=lambda: bytearray(b"\x20" * 0x10000))
    files: dict[str, bytes] = field(default_factory=dict)
    directories: list[str] = field(default_factory=lambda: ["/"])
    listing: list[dict] = field(default_factory=list)
    launched: list[tuple[int, str]] = field(default_factory=list)
    received: list[int] = field(default_factory=list)
    chatter_on_launch: str = "Loading IO handler: TeensyROM\n"
    boot_polls: int = 0                  # how many version polls say "in progress" after a reset
    booting_for: int = 0
    heartbeat: bool = False              # a launched hello.a is ticking its bottom-right cell
    menu: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # A C64 at the TeensyROM menu: screen at $0400, lower/upper charset, VIC bank 0, no key waiting.
        self.memory[0xD018] = 0x17
        self.memory[0xDD00] = 0x97
        self.memory[KEYCOUNT] = 0
        self.menu = FakeMenu(self)       # draws the Teensy Mem root into screen RAM

    def version_string(self) -> str:
        return parse_banner(self.banner).version or "TeensyROM"

    def run(self, path: str) -> None:
        """What a launched program does to the screen: hello.a paints its text
        and starts its heartbeat; anything else leaves the screen as the menu
        left it. Either way the board is busy until a reset."""
        self.busy = True
        if path.lower().endswith("hello.prg"):
            self.memory[SCREEN_RAM_DEFAULT:SCREEN_RAM_DEFAULT + SCREEN_BYTES] = b"\x20" * SCREEN_BYTES
            self.memory[0xD018] = 0x17
            text = codes_for_text("treadmill hello")
            at = SCREEN_RAM_DEFAULT + 12 * 40 + 12
            self.memory[at:at + len(text)] = text
            self.heartbeat = True

    debug_defines: set = field(default_factory=set)   # the Dbg_* defines this build carries
    letters: list[str] = field(default_factory=list) # every letter command received
    knobs: dict = field(default_factory=lambda: {"m": 996, "r": 225, "p": 150, "s": 90, "h": 410})

    def serve(self, end: PipeEnd) -> None:
        while True:
            first = end.read(1, 60.0)
            if len(first) < 1:
                break
            if self.silent:
                continue
            if first != b"d":                       # 0x64: a token follows; anything else is a letter
                self._letter(end, first.decode("latin-1"))
                continue
            second = end.read(1, WAIT_S)
            if len(second) < 1:
                break
            value = int.from_bytes(first + second, "big")
            self.received.append(value)
            if self.image == "minimal" and value not in {FW_CHECK, VERSION_INFO, RESET_C64, LAUNCH_FILE}:
                self._refuse(end, "Busy!")
                continue
            if self.busy and value not in ALWAYS:
                self._refuse(end, "Busy!")
                continue
            handler = self._handlers().get(value)
            if handler is None:
                end.write(f"Unk cmd: 0x{value:04x}\n".encode("latin-1"))
                continue
            handler(end)
            if not self.busy and value in MENU_ONLY:
                end.write(STATS_TRAILER)

    # -- letter commands (SerUSBIO.ino's single-letter debug set) -------------

    def _letter(self, end: PipeEnd, letter: str) -> None:
        """A letter is read only when its define is compiled in; otherwise the
        firmware drops it and reads what follows as new commands. 'e' and 'b'
        are always compiled in."""
        from treadmill.link.dbgcmds import LETTERS, TIMING_KNOBS
        need = LETTERS.get(letter)
        if letter == "e":
            self.letters.append("e")
            end.write(b"--> Setting EEPROM to defaults\nApplied upon reboot\n")
            return
        if letter == "b":
            self.letters.append("b")
            end.write(b"Bus analysis: A0-15 D0-7 R/W counts\n")
            return
        if need is None or need not in self.debug_defines:
            return                                   # dropped, like the firmware's switch
        if letter == "t":
            sub = end.read(1, WAIT_S).decode("latin-1")
            if sub in TIMING_KNOBS:
                digits = end.read(TIMING_KNOBS[sub], WAIT_S).decode("latin-1")
                self.knobs[sub] = int(digits)
                self.letters.append(f"t{sub}{digits}")
                head = f"{sub} set to {int(digits)}\n"
            elif sub == "d":
                self.knobs = {"m": 996, "r": 225, "p": 150, "s": 90, "h": 410}
                self.letters.append("td")
                head = "Timing defaults restored\nDefaults set (NTSC C64)\n"
            else:
                self.letters.append(f"t{sub}")
                head = "No changes\n"
            end.write(("-----\n" + head + self._knob_listing()).encode("latin-1"))
            return
        self.letters.append(letter)
        end.write(f"ok {letter}\n".encode("latin-1"))

    def _knob_listing(self) -> str:
        """The block the firmware prints after every 't' command."""
        k = self.knobs
        return ("Current:    Variable  Val (Command)\n"
                f"\t nS_MaxAdj   {k['m']:04d} (tm####)\n"
                f"\t nS_RWnReady  {k['r']:03d} (tr###)\n"
                f"\t nS_PLAprop   {k['p']:03d} (tp###)\n"
                f"\t nS_DataSetup {k['s']:03d} (ts###)\n"
                f"\t nS_DataHold  {k['h']:03d} (th###)\n"
                "\tSet Defaults      (td)\n"
                "\tList current vals (t)\n-----\n")

    # -- helpers -----------------------------------------------------------

    def _reply(self, end: PipeEnd, token: int) -> None:
        end.write(board_reply(token))

    def _refuse(self, end: PipeEnd, text: str) -> None:
        end.write(board_reply(FAIL) + f"{text}\n".encode("latin-1"))

    @staticmethod
    def _path(end: PipeEnd) -> str:
        return end.read_until(b"\0", WAIT_S)[:-1].decode("latin-1")

    @staticmethod
    def _number(end: PipeEnd, width: int) -> int:
        return int.from_bytes(end.read(width, WAIT_S), "big")

    # -- commands ----------------------------------------------------------

    def _handlers(self):
        return {
            FW_CHECK: lambda e: self._reply(e, FW_FULL if self.image == "main" else FW_MINIMAL),
            VERSION_INFO: self._version,
            PING: lambda e: e.write(READY_LINE.encode("latin-1")),
            RESET_C64: self._reset,
            C64_PAUSE_ON: lambda e: self._reply(e, ACK),
            C64_PAUSE_OFF: lambda e: self._reply(e, ACK),
            READ_C64_MEM: self._read_memory,
            WRITE_C64_MEM: self._write_memory,
            LAUNCH_FILE: self._launch,
            DELETE_FILE: self._delete,
            POST_FILE: self._post,
            GET_FILE: self._get,
            GET_DIR_NDJSON: self._list,
        }

    def _version(self, end: PipeEnd) -> None:
        booting = self.booting_for > 0
        self.booting_for -= 1
        self._reply(end, ACK)
        text = self.banner.replace("complete", "in progress") if booting else self.banner
        end.write(text.encode("latin-1"))

    def _reset(self, end: PipeEnd) -> None:
        self.booting_for = self.boot_polls
        self.busy = False
        self.heartbeat = False
        self.memory[0xD018] = 0x17
        self.menu.reset()
        end.write(b"Reset cmd received\nLoading IO handler: TeensyROM\n")

    def _read_memory(self, end: PipeEnd) -> None:
        if not self.dma:
            # A plain TR has no such command: the bytes that follow are read as
            # new commands. Modelled as an unknown-command line per byte pair.
            end.write(b"Unk cmd\n")
            return
        address = self._number(end, 2)
        length = self._number(end, 2)
        self._reply(end, ACK)
        end.write(bytes(self.memory[address:address + length]))

    def _write_memory(self, end: PipeEnd) -> None:
        if not self.dma:
            end.write(b"Unk cmd\n")
            return
        address = self._number(end, 2)
        length = self._number(end, 2)
        data = end.read(length, WAIT_S)
        self.memory[address:address + len(data)] = data
        self._reply(end, ACK)
        if address <= KEYCOUNT < address + len(data) and self.memory[KEYCOUNT] and not self.busy:
            # The menu's GETIN loop takes the keys as soon as they are in the buffer.
            count = self.memory[KEYCOUNT]
            for code in bytes(self.memory[KEYBUF:KEYBUF + count]):
                self.menu.key(code)
            self.memory[KEYCOUNT] = 0

    def _launch(self, end: PipeEnd) -> None:
        self._reply(end, ACK)
        drive = end.read(1, WAIT_S)[0]
        path = self._path(end)
        self.launched.append((drive, path))
        self._reply(end, ACK)
        folder, _, name = path.rpartition("/")
        end.write(f"Remote Launch:\nP: {folder or '/'}\nF: {name}\n".encode("latin-1"))
        if self.chatter_on_launch:
            end.write(self.chatter_on_launch.encode("latin-1"))
        self.run(path)

    def _delete(self, end: PipeEnd) -> None:
        self._reply(end, ACK)
        end.read(1, WAIT_S)
        path = self._path(end)
        if self.files.pop(path, None) is None:
            self._refuse(end, "File not found")
            return
        self._reply(end, ACK)

    def _post(self, end: PipeEnd) -> None:
        self._reply(end, ACK)
        length = self._number(end, 4)
        total = self._number(end, 2)
        end.read(1, WAIT_S)
        path = self._path(end)
        if path in self.files:
            self._refuse(end, "File exists")
            return
        self._ensure_directories(path)
        self._reply(end, ACK)
        data = self._receive(end, length)
        if len(data) < length:
            self._refuse(end, "Timeout waiting for file data")   # nothing kept: the firmware removes its partial file
            return
        if checksum(data) != total:
            self._refuse(end, "Checksum mismatch")
            return
        self.files[path] = bytes(data)
        self._reply(end, ACK)

    @staticmethod
    def _receive(end: PipeEnd, length: int) -> bytes:
        """ReceiveFileData: bytes until the count is reached, given up when
        none arrive for half a second (SerialAvailabeTimeout), however slowly
        they come."""
        data = bytearray()
        while len(data) < length:
            part = end.read(min(length - len(data), 4096), 0.5)
            if not part:
                break
            data += part
        return bytes(data)

    def _ensure_directories(self, path: str) -> None:
        """EnsureDirectory: every folder on the way to a posted file exists afterwards."""
        folder = path.rpartition("/")[0]
        while folder and folder not in self.directories:
            self.directories.append(folder)
            folder = folder.rpartition("/")[0]

    def _get(self, end: PipeEnd) -> None:
        self._reply(end, ACK)
        end.read(1, WAIT_S)
        path = self._path(end)
        data = self.files.get(path)
        if data is None:
            self._refuse(end, "File not found")
            return
        self._reply(end, ACK)
        end.write(len(data).to_bytes(4, "little") + checksum(data).to_bytes(4, "little") + data)
        self._reply(end, ACK)

    def _list(self, end: PipeEnd) -> None:
        self._reply(end, ACK)
        drive = end.read(1, WAIT_S)[0]
        skip = self._number(end, 2)
        take = self._number(end, 2)
        path = self._path(end)
        if drive == DRIVE_TEENSY:
            # SendTRDirectory: the whole tree, whatever the path, skip and take say.
            self._reply(end, ACK)
            self._reply(end, DIR_START)
            end.write("".join(json.dumps(e) + "\r\n" for e in self._teensy_listing()).encode("latin-1"))
            self._reply(end, DIR_END)
            return
        if path not in self.directories:
            self._refuse(end, "Directory not found. (Error 5)")
            return
        self._reply(end, ACK)
        self._reply(end, DIR_START)
        entries = self.listing or self._card_listing(path)
        lines = "".join(json.dumps(e) + "\r\n" for e in entries[skip:skip + take])
        end.write(lines.encode("latin-1"))
        self._reply(end, DIR_END)

    def _card_listing(self, path: str) -> list[dict]:
        def parent(p: str) -> str:
            return p.rpartition("/")[0] or "/"
        out = [{"type": "dir", "name": d.rpartition("/")[2]}
               for d in self.directories if d != "/" and parent(d) == path]
        out += [{"type": "file", "name": p.rpartition("/")[2], "size": len(data)}
                for p, data in self.files.items() if parent(p) == path]
        return out

    @staticmethod
    def _teensy_listing() -> list[dict]:
        out = []
        for folder in TREE["/"]:
            out.append({"type": "dir", "name": folder.name})
            out += [{"type": "file", "name": e.name, "size": 0}
                    for e in TREE.get(folder.name, []) if e.name != UP_DIR]
        return out


def start_fake_board(**overrides):
    """A fake board on its own thread and the host-side ByteStream to it."""
    board = FakeBoard(**overrides)
    stream, far_end = open_pipe("fake-board")
    thread = threading.Thread(target=board.serve, args=(far_end,), name="fake-board", daemon=True)
    thread.start()
    return board, stream
