from treadmill.c64.screen import (
    CHARSET_CUSTOM, CHARSET_LOWER_UPPER, CHARSET_UPPER_GRAPHICS, charset_in, code_as_text,
    codes_for_text, screen_address, screen_rows,
)
from treadmill.link.banner import parse_banner

BANNER = ("\n  FW: TeensyROM+ v0.9-dma.tim*\r\n      Nov  4 2026, 09:12:33\r\n"
          "  Teensy: 816MHz  55.0C  UID: 19277260\r\n  C128  PAL Vid  50 Hz\n  Boot: in progress\n")


def test_banner_fields():
    b = parse_banner(BANNER)
    assert b.version == "TeensyROM+ v0.9-dma.tim*"
    assert b.kind == "tr-plus" and b.tag == "dma.tim" and b.dirty
    assert b.built == "Nov  4 2026, 09:12:33"
    assert b.uid == "19277260" and b.mhz == 816 and b.temp_c == 55.0
    assert b.machine == "C128" and b.video == "PAL" and b.hz == 50
    assert b.boot == "in progress" and not b.minimal


def test_banner_plain_tr_minimal_and_old_firmware():
    b = parse_banner("  FW: TeensyROM v0.8.0.11(minimal)\r\n      Sep 25 2026, 03:03:51\r\n"
                     "  Teensy: 816MHz  62.1C  UID: 14470230\r\n")
    assert b.kind == "tr" and b.minimal and b.tag is None and not b.dirty
    assert b.machine is None and b.boot is None
    assert parse_banner("garbage").version is None and parse_banner("garbage").kind is None


def test_screen_code_table():
    assert code_as_text(1) == "a" and code_as_text(1, CHARSET_UPPER_GRAPHICS) == "A"
    assert code_as_text(65) == "A" and code_as_text(65, CHARSET_UPPER_GRAPHICS) == "."
    assert code_as_text(0) == "@" and code_as_text(0x81) == "a"          # reverse bit ignored
    assert code_as_text(27) == "[" and code_as_text(28) == "£" and code_as_text(31) == "←"
    assert code_as_text(0x30) == "0" and code_as_text(0x3f) == "?"
    assert code_as_text(0x60) == "."                                      # graphics
    # The menu shows a file name's underscore as the bottom-line graphic, in either set.
    assert code_as_text(0x64) == "_" and code_as_text(0xE4) == "_" and code_as_text(0x64, CHARSET_UPPER_GRAPHICS) == "_"
    assert codes_for_text("a_b") == bytes([1, 0x64, 2])
    rows = screen_rows(bytes([0x14, 0x05, 0x18, 0x14] + [0x20] * 996))
    assert rows[0].startswith("text ") and len(rows) == 25 and len(rows[0]) == 40


def test_vic_pointers():
    # $D018 = $17 with CIA2 bank 0: screen at $0400, lower/upper charset.
    assert screen_address(0x17, 0x97) == 0x0400
    assert charset_in(0x17, 0x97) == CHARSET_LOWER_UPPER
    assert charset_in(0x15, 0x97) == CHARSET_UPPER_GRAPHICS
    assert charset_in(0x17, 0x96) == CHARSET_CUSTOM          # bank 1 sees RAM, not the ROM
    assert screen_address(0x17, 0x96) == 0x4400
