"""The fake board's menu: the Teensy Mem tree as firmware 0.9 lists it
(transcribed from ``Source/Teensy/MainMenuItems.h``), the card's files from
the fake's store, and ``MainMenu.asm``'s key handling in miniature, rendered
into the fake's screen RAM through the layout the goldens use.

``python -m selftest.fake_menu <folder>`` writes the Teensy Mem goldens
from the tree, for ``tests/golden/menu``.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from treadmill.c64.keys import (
    CRSR_DOWN, CRSR_LEFT, CRSR_RIGHT, CRSR_UP, F1, F3, F5, F6, HOME, RETURN, SPACE, UP_ARROW,
)
from treadmill.c64.menu import (
    ANY_ROW, EMPTY, FIRST_ITEM_ROW, ITEMS_PER_PAGE, MARK_DIR, MARK_NONE, MARK_UNKNOWN, SCREEN_COLUMNS, UP_DIR,
    Entry, mark_for, render_listing, shown_name, shown_path,
)
from treadmill.c64.screen import REVERSE_VIDEO, SCREEN_BYTES, SCREEN_RAM_DEFAULT, codes_for_text
from treadmill.observe.golden import save_golden

_UP = Entry(UP_DIR, MARK_DIR)
ROOT = [Entry(name, MARK_DIR) for name in (
    "/Games", "/MIDI + ASID", "/Featured SIDs", "/SID Cover Tunes", "/Pictures", "/Multimedia",
    "/Test+Diags", "/Utilities", "/Text + PETSCII + Docs", "/TeensyROM Specific",
)]
GAMES = [
    _UP,
    Entry("Retromate Internet Chess v1.04", "Prg", True),
    Entry("Beach Head II         256k MC", "Crt"),
    Entry("Dig Dug", "16k"),
    Entry("Donkey Kong", "16k"),
    Entry("Gorf!", "Crt"),
    Entry("Joust!", "Prg"),
    Entry("Jupiter Lander", "8Hi"),
    Entry("Jupiter Lander (Cracked)", "Prg"),
    Entry("Mario Brothers", "Prg"),
    Entry("Minesweeper", "Prg"),
    Entry("Ms. Pac-Man", "Crt"),
    Entry("Robocop 2             224k EZF", "Crt"),
    Entry("Super Mario Brothers   48k PRG", "Prg"),
    Entry("Tetris", "Prg"),
]
PICTURES = [
    _UP,
    Entry("TS: TeensyROM", "Kla"),
    Entry("TS: BlackHole", "Kla"),
    Entry("TS: Ember Head", "Kla"),
    Entry("TS: Sensorium Heatmap", "Kla"),
    Entry("HA: Bluejay", "Kla"),
    Entry("HA: Hummingbird", "Kla"),
    Entry("HA: Sugar Skull", "Kla"),
    Entry("LP: Boston", "Kla"),
    Entry("LP: ELP Tarkus", "Kla"),
    Entry("LP: Zappa Sleep Dirt", "Kla"),
    Entry("LP: Zappa Apostrophe (')", "Kla"),
    Entry("Pie2 Hi-Res", "Art"),
    Entry("KP: Bird", "Kla"),
    Entry("KP: Burger", "Kla"),
    Entry("KP: Koala", "Kla"),
    Entry("KP: Tiger", "Kla"),
]
TEENSYROM_SPECIFIC = [
    _UP,
    Entry("TeensyROM Settings Menu", "Prg", True),
    Entry("TeensyROM Help Pages", "Prg", True),
    Entry("Text Viewer Usage Document", "Seq"),
    Entry("BASIC with TeensyROM Commands", "Prg", True),
    Entry("BASIC TR Command IO access only", "Prg", True),
    Entry("TeensyROM External Ports Test", "Prg", True),
    Entry("TR+ C64 Expansion Port Test", "Prg", True),
    Entry("TeensyROM ASID Player    +TR ASID", "Prg", True),
    Entry("MIDI2SID          +TeensyROM MIDI", "Prg", True),
    Entry("TS: TeensyROM", "Kla"),
]
TREE = {"/": ROOT, "/Games": GAMES, "/Pictures": PICTURES, "/TeensyROM Specific": TEENSYROM_SPECIFIC}
GOLDENS = {"root": ("/", ROOT), "games": ("/Games", GAMES), "pictures": ("/Pictures", PICTURES)}

_SOURCE_BY_KEY = {F1: "teensy", F3: "sd", F5: "usb"}
CLOCK = "12:00:00am"
HOT_KEY_FIRST, HOT_KEY_LAST = 0x31, 0x35          # '1'-'5' (NumHotKeys in Menu_Regs.h)


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


def card_entries(board, path: str) -> list[Entry]:
    """What the menu lists for ``path`` on the fake's card: the up-dir entry
    off the root, subdirectories as ``/name``, files with their marks, all
    sorted together the way ``LoadDirectory`` sorts them."""
    dirs = {d for d in board.directories if d != "/" and _parent(d) == path}
    files = [p for p in board.files if _parent(p) == path]
    entries = [] if path == "/" else [_UP]
    entries += [Entry("/" + d.rsplit("/", 1)[-1], MARK_DIR) for d in dirs]
    named = [(e.name, e) for e in entries]
    named += [(p.rsplit("/", 1)[-1], Entry(shown_name(p.rsplit("/", 1)[-1]), mark_for(p))) for p in files]
    entries = [e for _, e in sorted(named, key=lambda pair: pair[0].lower().encode("latin-1", "replace"))]
    return entries or [Entry(EMPTY, MARK_NONE)]


class FakeMenu:
    def __init__(self, board):
        self.board = board
        # The real menu draws a listing an item at a time, its legend first and the clock last, and a
        # read with no drain before it lands in the middle. Set, each screen read after a redraw sees
        # one more item, the way the bench's reads did.
        self.draw_by_item = False
        self._drawing: list[bytes] = []
        self.reset()

    def reset(self) -> None:
        self.source, self.path, self.page, self.cursor = "teensy", "/", 1, 0
        self.info_page = False
        self.render()

    # -- what is listed ----------------------------------------------------------

    def entries(self) -> list[Entry]:
        if self.source == "teensy":
            return list(TREE.get(self.path, [_UP]))
        if self.source == "usb":
            return [Entry(EMPTY, MARK_NONE)]
        return card_entries(self.board, self.path)

    def pages(self) -> int:
        return max(1, math.ceil(len(self.entries()) / ITEMS_PER_PAGE))

    def page_entries(self) -> list[Entry]:
        return self.entries()[(self.page - 1) * ITEMS_PER_PAGE:self.page * ITEMS_PER_PAGE]

    def rows(self) -> list[str]:
        banner = self.board.version_string()
        if self.info_page:
            rows = [banner.center(SCREEN_COLUMNS), "", "SID Info Page:", "", " File Information for", "",
                    " This Machine: C64 NTSC 60Hz TOD"]
            rows += [""] * (22 - len(rows)) + ["   Space Back to Main menu", "", ""]
            return [r.ljust(SCREEN_COLUMNS)[:SCREEN_COLUMNS] for r in rows[:25]]
        entries = self.page_entries()
        if self.board.outside("io") > 0:
            # The NTSC item-type misread: outside the IO read window every other item's type comes back wrong.
            entries = [Entry(e.name, MARK_UNKNOWN if i % 2 == 0 and e.mark != MARK_NONE else e.mark, e.handler)
                       for i, e in enumerate(entries)]
        return render_listing(self.source, shown_path(self.path), entries, (self.page, self.pages()), clock=CLOCK, banner=banner)

    def render(self) -> None:
        codes = self._codes(self.rows())
        if not self.info_page:
            row = FIRST_ITEM_ROW + self.cursor
            for i in range(row * SCREEN_COLUMNS, (row + 1) * SCREEN_COLUMNS):
                codes[i] |= REVERSE_VIDEO
        frames = [codes]
        if self.draw_by_item and not self.info_page:
            entries = self.page_entries()
            frames = [self._codes(render_listing(self.source, shown_path(self.path), entries[:k],
                                                 (self.page, self.pages()), clock="",
                                                 banner=self.board.version_string()))
                      for k in range(len(entries))] + frames
        self._drawing = frames[1:]
        self.board.memory[SCREEN_RAM_DEFAULT:SCREEN_RAM_DEFAULT + SCREEN_BYTES] = frames[0][:SCREEN_BYTES]

    def drawn_further(self) -> None:
        """A screen read came: the menu has drawn one more item since the last."""
        if self._drawing:
            self.board.memory[SCREEN_RAM_DEFAULT:SCREEN_RAM_DEFAULT + SCREEN_BYTES] = self._drawing.pop(0)[:SCREEN_BYTES]

    @staticmethod
    def _codes(rows: list[str]) -> bytearray:
        codes = bytearray()
        for row in rows:
            codes += codes_for_text(row)
        for i in range(SCREEN_COLUMNS):
            codes[i] |= REVERSE_VIDEO
        return codes

    # -- keys ----------------------------------------------------------------------

    def key(self, code: int) -> None:
        if not self.info_page and HOT_KEY_FIRST <= code <= HOT_KEY_LAST:
            self.board.hot_key(code - HOT_KEY_FIRST + 1)      # the launched program draws its own screen
            return
        if self.info_page:
            if code == SPACE:
                self.info_page = False
        elif code in _SOURCE_BY_KEY:
            self.source, self.path, self.page, self.cursor = _SOURCE_BY_KEY[code], "/", 1, 0
        elif code == F6:
            self.info_page = True
        elif code == HOME:
            self.page, self.cursor = 1, 0
        elif code == CRSR_DOWN:
            if self.cursor + 1 < len(self.page_entries()):
                self.cursor += 1
            else:
                self._page_down()
        elif code == CRSR_UP:
            if self.cursor > 0:
                self.cursor -= 1
            else:
                self._page_up()
                self.cursor = len(self.page_entries()) - 1
        elif code == CRSR_RIGHT:
            self._page_down()
        elif code == CRSR_LEFT:
            self._page_up()
            self.cursor = 0
        elif code == UP_ARROW:
            self._up()
        elif code == RETURN:
            self._select()
            if self.board.program is not None:
                return                   # the launched program draws its own screen
        self.render()

    def _page_down(self) -> None:
        self.page = self.page % self.pages() + 1
        self.cursor = 0

    def _page_up(self) -> None:
        self.page = self.pages() if self.page == 1 else self.page - 1

    def _up(self) -> None:
        if self.path != "/":
            self.path = "/" if self.source == "teensy" else _parent(self.path)
            self.page, self.cursor = 1, 0

    def _select(self) -> None:
        entry = self.page_entries()[self.cursor]
        if entry.mark == MARK_DIR:
            if entry.name == UP_DIR:
                self._up()
            else:
                self.path = entry.name if self.source == "teensy" else f"{self.path.rstrip('/')}/{entry.name[1:]}"
                self.page, self.cursor = 1, 0
        elif entry.mark != MARK_NONE:
            self.board.run(f"{self.path.rstrip('/')}/{entry.name}")


def write_goldens(folder: Path) -> list[Path]:
    """The Teensy Mem goldens: the root, /Games and /Pictures listings, the
    banner and the clock left open."""
    folder = Path(folder)
    out = []
    for name, (path, entries) in GOLDENS.items():
        rows = ["*" if r == ANY_ROW else r for r in render_listing("teensy", shown_path(path), entries)]
        header = (f"% TeensyROM menu, Teensy Mem {path}: firmware 0.9, MainMenuItems.h",
                  "% Row 0 is the version banner, the clock on row 1 is a wildcard.")
        out.append(save_golden(rows, folder / f"{name}.screen.txt", header))
    return out


if __name__ == "__main__":
    for p in write_goldens(Path(sys.argv[1])):
        print(p)
