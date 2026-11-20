"""The TeensyROM main menu as the C64 shows it: the listing's layout, the
item-type marks, and a driver that moves through it with keys and screen
waits.

The layout is ``Source/C64/MainMenuCRT/source/MainMenu.asm``'s: row 0 the
version banner in reverse video, row 1 ``Src:`` and the clock, row 2 the
current path, rows 3-21 up to nineteen items (the name from column 1, ``+``
at column 35 for an item with an IO handler, a three-character type mark at
column 36), row 23 the F-key legend with the page count, row 24 its second
line. The marks are ``TblItemType`` in ``StringsMsgs.s``, in ``regItemTypes``
order; a card's files get theirs from the extension table in
``DriveDirLoad.h``. The cursor moves an item at a time, pages forward from
the last item of a page and lands on the first item of the new page; HOME is
the top of the directory.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from treadmill.c64.keys import CRSR_DOWN, CRSR_RIGHT, F1, F3, F5, HOME, RETURN, UP_ARROW, Keys
from treadmill.c64.screen import SCREEN_COLUMNS, SCREEN_ROWS
from treadmill.errors import TreadmillError
from treadmill.observe.channels import DEFAULT_TIMEOUT, POLL_S, ScreenChannel

# How long read() lets two reads disagree before taking the last: a listing draws in well under
# a second, and only video's misreads keep two reads of a still screen apart.
SETTLE_S = 2.0

SOURCES = {"teensy": "Teensy Mem", "sd": "SD Card", "usb": "USB Drive"}
SOURCE_KEYS = {"teensy": F1, "sd": F3, "usb": F5}
UP_DIR = "/.. <Up Dir>"              # UpDirString in Common_Defs.h
EMPTY = "<Empty>"                    # what an empty or missing drive lists

# regItemTypes order: index = the type the board reports for the item.
MARKS = ("   ", "Unk", "Dir", "D64", "D71", "D81", "Prg", "Crt", "Hex", "P00", "SID", "Kla", "Art",
         "Txt", "Seq", "16k", "8Hi", "8Lo", "128", "REU", "TRH", "GEO")
MARK_NONE, MARK_UNKNOWN, MARK_DIR = MARKS[0], MARKS[1], MARKS[2]
EXTENSION_MARKS = {
    "prg": "Prg", "crt": "Crt", "hex": "Hex", "p00": "P00", "sid": "SID",
    "kla": "Kla", "koa": "Kla", "ocp": "Art", "pic": "Art", "art": "Art", "aas": "Art", "hpi": "Art",
    "txt": "Txt", "nfo": "Txt", "md": "Txt", "seq": "Seq",
    "d64": "D64", "d71": "D71", "d81": "D81", "reu": "REU", "trh": "TRH",
}

ROW_BANNER, ROW_SOURCE, ROW_PATH, FIRST_ITEM_ROW = 0, 1, 2, 3
ITEMS_PER_PAGE = 19                  # MaxItemsPerPage
ROW_LEGEND1, ROW_LEGEND2 = 23, 24
NAME_COLUMN, HANDLER_COLUMN, MARK_COLUMN, MARK_WIDTH = 1, 35, 36, 3
NAME_WIDTH = HANDLER_COLUMN - NAME_COLUMN       # MaxItemDispLength less its terminator
CLOCK_COLUMN, CLOCK_WIDTH = 29, 10
CLOCK_ANY = "?" * CLOCK_WIDTH
LEGEND_ROW1 = "F1 Teensy Mem  F5 USB Drive  Pg "
LEGEND_ROW2 = "F3 SD Card     F7 Help"
ANY_ROW = "?" * SCREEN_COLUMNS


class MenuError(TreadmillError):
    pass


def mark_for(filename: str) -> str:
    """The mark the menu shows for a file on a card, from its extension."""
    name = filename.rsplit("/", 1)[-1]
    if "." not in name:
        return MARK_UNKNOWN
    return EXTENSION_MARKS.get(name.rsplit(".", 1)[-1].lower(), MARK_UNKNOWN)


def shown_path(path: str) -> str:
    """The path row for a directory: the firmware appends each listed
    directory name, its leading slash included, to a path that starts as
    "/", so every path off the root begins with two (DriveDirLoad.ino)."""
    return path if path == "/" else "/" + path


def shown_name(filename: str, show_extensions: bool = False) -> str:
    """The name a card or USB listing shows for a file: one of a type the
    menu knows loses its extension unless Settings shows extensions, which
    the firmware's default does not (``rsstItemName`` in IOH_TeensyROM.c);
    a file of an unknown type keeps its whole name. Either is then cut to
    the columns before the handler mark. The extension goes first, so a dot
    inside a long name's cut is not taken for it (``MenuItemDisplayName``,
    firmware PR #102; earlier firmware cut first and dropped from the cut's
    last dot)."""
    if not show_extensions and mark_for(filename) != MARK_UNKNOWN:
        filename = filename.rsplit(".", 1)[0]
    return filename[:NAME_WIDTH]


@dataclass(frozen=True)
class Entry:
    name: str
    mark: str
    handler: bool = False            # the '+' of an item with an IO handler (Teensy Mem only)


@dataclass(frozen=True)
class Listing:
    source: str                      # as row 1 shows it: "Teensy Mem", "SD Card", "USB Drive"
    path: str
    entries: tuple[Entry, ...]       # this page's
    page: tuple[int, int]            # (this page, pages)

    @property
    def names(self) -> list[str]:
        return [e.name for e in self.entries]

    @property
    def is_root(self) -> bool:
        """A root listing: its first page has no up-dir entry. Past page 1
        the screen cannot tell, so this is False there."""
        return bool(self.entries) and self.page[0] <= 1 and self.entries[0].name != UP_DIR

    def index(self, name: str) -> int:
        try:
            return self.names.index(name)
        except ValueError:
            raise MenuError(f"{name!r} is not on page {self.page[0]} of {self.path}: {self.names}") from None


def parse_listing(rows: list[str]) -> Listing | None:
    """The listing a screen shows, or None when the screen is not the menu's listing."""
    if len(rows) < SCREEN_ROWS:
        return None
    if not rows[ROW_LEGEND1].startswith(LEGEND_ROW1) or not rows[ROW_LEGEND2].startswith(LEGEND_ROW2):
        return None
    if not rows[ROW_SOURCE].startswith("Src: "):
        return None
    source = rows[ROW_SOURCE][len("Src: "):CLOCK_COLUMN].rstrip()
    path = rows[ROW_PATH].rstrip()
    pages = rows[ROW_LEGEND1][len(LEGEND_ROW1):].strip().split("/")
    page = (int(pages[0]), int(pages[1])) if len(pages) == 2 and all(p.isdigit() for p in pages) else (0, 0)
    entries = []
    for r in range(FIRST_ITEM_ROW, FIRST_ITEM_ROW + ITEMS_PER_PAGE):
        row = rows[r].ljust(SCREEN_COLUMNS)
        name = row[NAME_COLUMN:HANDLER_COLUMN].rstrip()
        mark = row[MARK_COLUMN:MARK_COLUMN + MARK_WIDTH]
        if not name and not mark.strip():
            break
        entries.append(Entry(name, mark, row[HANDLER_COLUMN] == "+"))
    return Listing(source, path, tuple(entries), page)


def render_listing(source: str, path: str, entries, page: tuple[int, int] = (1, 1),
                   clock: str = CLOCK_ANY, banner: str | None = None) -> list[str]:
    """The 25 rows the menu shows for one page of ``entries``. ``source`` is
    a drive key or the name row 1 shows; ``banner`` is the version string of
    row 0, left as a wildcard row when not given, as is the clock."""
    source = SOURCES.get(source, source)
    rows = [banner.center(SCREEN_COLUMNS) if banner else ANY_ROW]
    rows.append(f"Src: {source}".ljust(CLOCK_COLUMN)[:CLOCK_COLUMN] + clock[:CLOCK_WIDTH].rjust(CLOCK_WIDTH) + " ")
    rows.append(path)
    for e in list(entries)[:ITEMS_PER_PAGE]:
        line = list((" " + e.name).ljust(SCREEN_COLUMNS)[:SCREEN_COLUMNS])
        if e.handler:
            line[HANDLER_COLUMN] = "+"
        line[MARK_COLUMN:MARK_COLUMN + MARK_WIDTH] = e.mark.ljust(MARK_WIDTH)[:MARK_WIDTH]
        rows.append("".join(line))
    while len(rows) < ROW_LEGEND1:
        rows.append("")
    rows.append(f"{LEGEND_ROW1}{page[0]}/{page[1]}")
    rows.append(LEGEND_ROW2)
    return [r.ljust(SCREEN_COLUMNS)[:SCREEN_COLUMNS] for r in rows]


class MenuDriver:
    """The menu through a screen channel, moved with keys when the station
    can press any: F1/F3/F5 for the sources, HOME, cursor and page keys to
    reach an item, RETURN to open it, the up arrow for the parent."""

    def __init__(self, screen: ScreenChannel, keys: Keys | None = None, board=None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.screen = screen
        self.keys = keys
        self.board = board
        self.timeout = timeout

    @property
    def can_navigate(self) -> bool:
        return self.keys is not None

    def press(self, *codes: int) -> None:
        if self.keys is None:
            raise MenuError("no way to press keys on this station (keys_kernal or keys_hw)")
        self.keys.press(bytes(codes))

    def read(self, exact: bool = False) -> Listing:
        """The listing on screen once two reads in a row parse the same. The
        menu draws a listing an item at a time, its legend first, so a read
        while it draws parses as a shorter listing. Reads that never agree,
        as video's misreads can make, give the last one after SETTLE_S."""
        deadline = time.monotonic() + SETTLE_S
        previous = None
        while True:
            rows = self.screen.read(exact).rows()
            listing = parse_listing(rows)
            if listing is None:
                raise MenuError("the screen is not the menu listing:\n" + "\n".join(rows))
            if listing == previous or time.monotonic() >= deadline:
                return listing
            previous = listing
            time.sleep(POLL_S)

    def wait(self, predicate: Callable[[Listing], bool] = lambda listing: True, what: str = "the menu listing",
             timeout: float | None = None, exact: bool = False) -> Listing:
        """The first listing ``predicate`` accepts, on two reads in a row that
        parse the same, as ``read`` takes it."""
        found: list[Listing | None] = []

        def accept(read) -> bool:
            listing = parse_listing(read.rows())
            agreed = listing is not None and bool(found) and listing == found[-1]
            found.append(listing)
            return agreed and predicate(listing)
        self.screen.wait(accept, self.timeout if timeout is None else timeout, exact, what=what)
        return found[-1]

    def select_source(self, drive: str) -> Listing:
        if drive not in SOURCES:
            raise MenuError(f"source {drive!r}: one of {', '.join(SOURCES)}")
        name = SOURCES[drive]
        self.press(SOURCE_KEYS[drive])
        return self.wait(lambda listing: listing.source == name and listing.is_root and listing.page[0] <= 1,
                         what=f"the {name} listing")

    def find(self, name: str) -> tuple[Listing, int]:
        """The page that lists ``name`` and its index there, paging forward
        from the current page when it is not on it."""
        listing = self.read()
        for _ in range(max(listing.page[1], 1)):
            if name in listing.names:
                return listing, listing.names.index(name)
            if listing.page[1] <= 1:
                break
            following = listing.page[0] % listing.page[1] + 1
            self.press(CRSR_RIGHT)
            listing = self.wait(lambda l: l.page[0] == following, what=f"page {following} of {listing.path}")
        raise MenuError(f"{name!r} is not in {listing.path} ({listing.page[1]} page(s))")

    def _reach(self, listing: Listing, index: int) -> None:
        """From wherever the cursor is to item ``index`` of ``listing``'s page."""
        self.press(HOME, *([CRSR_RIGHT] * (listing.page[0] - 1)), *([CRSR_DOWN] * index))

    def open(self, name: str) -> Listing:
        """Into the directory ``name`` of the current listing."""
        listing, index = self.find(name)
        entry = listing.entries[index]
        if entry.mark != MARK_DIR:
            raise MenuError(f"{name!r} is a {entry.mark.strip() or 'blank'} item, not a directory")
        self._reach(listing, index)
        self.press(RETURN)
        return self.wait(lambda l: l.entries != listing.entries, what=f"the listing of {name}")

    def up(self) -> Listing:
        """To the parent directory; the root stays the root. From a later
        page HOME comes first, since only page 1 shows whether this is the root."""
        listing = self.read()
        if listing.page[0] > 1:
            self.press(HOME)
            listing = self.wait(lambda l: l.page[0] <= 1, what=f"page 1 of {listing.path}")
        if listing.is_root:
            return listing
        self.press(UP_ARROW)
        return self.wait(lambda l: l.entries != listing.entries, what=f"the parent of {listing.path}")

    def select(self, name: str) -> None:
        """RETURN on ``name``: a file launches, so there is nothing to wait
        for here; the caller watches the screen or the link."""
        self.highlight(name)
        self.press(RETURN)

    def highlight(self, name: str) -> Listing:
        """The cursor on ``name``, for a key that acts on the highlighted item
        (``K`` takes it as the Kernal Replace file)."""
        listing, index = self.find(name)
        self._reach(listing, index)
        return listing

    def ensure_root(self, drive: str = "teensy") -> Listing:
        """At the root of ``drive``: by its F-key when the station presses
        keys, else by a reset, which lands on the Teensy Mem root."""
        name = SOURCES[drive]
        try:
            listing = self.read()
            if listing.source == name and listing.is_root and listing.page[0] <= 1:
                return listing
        except MenuError:
            pass
        if self.keys is not None:
            return self.select_source(drive)
        if drive != "teensy" or self.board is None:
            raise MenuError(f"no way to reach the {name} root without keys")
        self.board.reset()
        self.board.wait_for_menu()
        return self.wait(lambda l: l.source == name and l.is_root, what="the Teensy Mem root after a reset")
