"""The menu layout against a screen the bench read by DMA, the driver against
the fake board's menu, the type set and its expected listing, and the slow
and stalled ways to post a file."""

import pytest

from selftest.fake_board import start_fake_board
from selftest.fake_menu import GAMES, ROOT
from treadmill.c64.menu import (
    EMPTY, MARK_DIR, MARK_NONE, UP_DIR, Entry, MenuDriver, MenuError, mark_for, parse_listing,
    render_listing,
)
from treadmill.fixtures.sdcard import TYPES_DIR, expected_listing, menu_entries, provision, type_set
from treadmill.link import Link, Refused
from treadmill.observe.channels import ScreenChannel
from treadmill.observe.golden import Golden, compare

# The Teensy Mem root as a TR+ in a C128 showed it, read by DMA on the bench (firmware 0.9).
REAL_ROOT = [
    "          TeensyROM+ v0.9-pr85",
    "Src: Teensy Mem               1:35:25am",
    "/",
    " /Games                             Dir",
    " /MIDI + ASID                       Dir",
    " /Featured SIDs                     Dir",
    " /SID Cover Tunes                   Dir",
    " /Pictures                          Dir",
    " /Multimedia                        Dir",
    " /Test+Diags                        Dir",
    " /Utilities                         Dir",
    " /Text + PETSCII + Docs             Dir",
    " /TeensyROM Specific                Dir",
    "", "", "", "", "", "", "", "", "", "",
    "F1 Teensy Mem  F5 USB Drive  Pg 1/1",
    "F3 SD Card     F7 Help",
]


def test_render_reproduces_the_screen_the_bench_read():
    real = [row.ljust(40) for row in REAL_ROOT]
    assert render_listing("teensy", "/", ROOT, clock=" 1:35:25am", banner="TeensyROM+ v0.9-pr85") == real
    listing = parse_listing(real)
    assert listing.source == "Teensy Mem" and listing.path == "/" and listing.page == (1, 1)
    assert listing.names == [e.name for e in ROOT] and all(e.mark == MARK_DIR for e in listing.entries)
    assert listing.is_root


def test_parse_sees_handlers_marks_and_the_end_of_a_listing():
    rows = render_listing("sd", "/x", [Entry(UP_DIR, MARK_DIR), Entry("a.prg", "Prg", True), Entry(EMPTY, MARK_NONE)],
                          page=(2, 3))
    listing = parse_listing(rows)
    assert listing.page == (2, 3) and not listing.is_root
    assert listing.entries[1] == Entry("a.prg", "Prg", True) and rows[4][35:39] == "+Prg"
    assert listing.entries[2] == Entry(EMPTY, MARK_NONE) and len(listing.entries) == 3
    assert parse_listing(["" for _ in range(25)]) is None
    with pytest.raises(MenuError):
        listing.index("missing")


def test_marks_follow_the_firmware_extension_table():
    assert [mark_for(n) for n in ("A.PRG", "x.crt", "k.koa", "p.pic", "t.nfo", "s.seq", "d.d81", "r.reu")] == \
        ["Prg", "Crt", "Kla", "Art", "Txt", "Seq", "D81", "REU"]
    assert mark_for("noext") == "Unk" and mark_for("odd.xyz") == "Unk" and mark_for("/a/b/c.sid") == "SID"


def test_type_set_covers_the_marks_in_menu_order():
    files = type_set()
    assert len({f.name for f in files}) == len(files)
    assert files == type_set()                                        # the same bytes every time
    marks = {f.mark for f in files}
    assert {"Prg", "Crt", "Hex", "P00", "SID", "Kla", "Art", "Txt", "Seq", "D64", "Unk"} <= marks
    entries = menu_entries(files)
    assert entries[0] == Entry(UP_DIR, MARK_DIR) and entries[1] == Entry("/sub", MARK_DIR)
    assert [e.name for e in entries[2:]] == sorted(e.name for e in entries[2:])
    rows = expected_listing(files)
    assert rows[1].startswith("Src: SD Card") and rows[2] == "?" * 40 and len(parse_listing(rows).entries) == 13


@pytest.fixture
def fake():
    board, stream = start_fake_board()
    link = Link(stream)
    link.probe()
    yield board, link
    link.close()


class _Press:
    def __init__(self, link: Link):
        self.link = link

    def press(self, codes) -> None:
        self.link.keys(bytes(codes))


def test_driver_walks_the_fake_menu(fake):
    board, link = fake
    screen = ScreenChannel(link, None, None, None, dma_allowed=lambda: True)
    menu = MenuDriver(screen, _Press(link))
    root = menu.select_source("teensy")
    assert root.names == [e.name for e in ROOT]
    games = menu.open("/Games")
    assert games.entries == tuple(GAMES) and games.entries[1].handler
    assert menu.up().entries == root.entries
    with pytest.raises(MenuError):
        menu.open("/Nowhere")
    menu.open("/Games")
    with pytest.raises(MenuError):
        menu.open("Joust!")                                           # a file, not a directory
    menu.up()
    assert menu.select_source("usb").entries == (Entry(EMPTY, MARK_NONE),)
    # The card: the type set, provisioned, then listed by the menu as the goldens expect it.
    manifest = provision(link, type_set())
    assert len(manifest) == len(type_set()) and set(board.files) >= {f"{TYPES_DIR}/prg.prg", f"{TYPES_DIR}/sub/inner.prg"}
    sd = menu.select_source("sd")
    assert sd.names == ["/treadmill"]
    inside = menu.open("/treadmill")
    assert inside.names == [UP_DIR, "/types"]
    types = menu.open("/types")
    assert not compare(screen.read(exact=True).rows(), Golden.parse("\n".join(f"|{r}|" for r in expected_listing(type_set()))))
    assert [e.mark for e in types.entries][:3] == [MARK_DIR, MARK_DIR, "Art"]
    # Provisioning again posts nothing and removes a stray.
    board.files[f"{TYPES_DIR}/stray.prg"] = b"x"
    provision(link, type_set())
    assert f"{TYPES_DIR}/stray.prg" not in board.files
    # Paging: an entry on the second page is reached through HOME and a page key.
    for i in range(25):
        board.files[f"/treadmill/many/f{i:02d}.prg"] = b"\x01\x08"
    board.directories.append("/treadmill/many")
    menu.up()
    many = menu.open("/many")
    assert many.page == (1, 2) and len(many.entries) == 19
    page, index = menu.find("f20.prg")
    assert page.page == (2, 2) and page.entries[index].name == "f20.prg"
    assert menu.up().page == (1, 1)


def test_post_file_paced_and_truncated(fake):
    board, link = fake
    data = bytes(range(256)) * 8
    link.post_file(data, "/T/slow.bin", pace=(256, 0.01))
    assert board.files["/T/slow.bin"] == data and "/T" in board.directories
    with pytest.raises(Refused) as e:
        link.post_file(data, "/T/cut.bin", truncate=100)
    assert "Timeout" in e.value.text and "/T/cut.bin" not in board.files
    link.post_file(data, "/T/cut.bin")                      # the retry a host makes after a stall
    assert link.get_file("/T/cut.bin") == data
    assert link.ping().endswith("ready!")                   # the trailers after the refusal were consumed
