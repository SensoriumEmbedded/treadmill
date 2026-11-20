"""Firmware specs, tags, hex inspection and the checkout record, without
building anything."""

import subprocess
from pathlib import Path

import pytest

from treadmill.firmware.checkout import CheckoutError, describe, worktree_for
from treadmill.firmware.hexinfo import MAIN_BASE, HexError, encode_hex, inspect_hex
from treadmill.firmware.spec import SpecError, cache_key, parse_spec
from treadmill.link.banner import parse_banner
from treadmill.firmware.flash import board_runs


def test_spec_parsing_and_tags():
    s = parse_spec("ref=main", target="tr-plus", defines=("Dbg_SerTimChg", "Dbg_SerDMA"))
    assert s.source_kind == "ref" and s.source == "main" and s.is_variant
    assert s.effective_tag() == "dma.tim"                       # display order, not given order
    assert parse_spec("ref=main").effective_tag() is None and not parse_spec("ref=main").is_variant
    assert parse_spec("ref=x", defines=("Dbg_SerMem",), tag="pr65").effective_tag() == "pr65"
    assert parse_spec("ref=x", defines=("Dbg_Unknown",)).effective_tag() == "x"
    assert parse_spec("none").source_kind == "none"
    assert parse_spec("hex=C:/x.hex").describe() == "hex=C:/x.hex target=auto"
    with pytest.raises(SpecError):
        parse_spec("main")
    with pytest.raises(SpecError):
        parse_spec("ref=main", defines=("bad-name",))
    with pytest.raises(SpecError):
        parse_spec("ref=main", tag="a b")
    with pytest.raises(SpecError):
        parse_spec("hex=x.hex", defines=("Dbg_SerDMA",))
    with pytest.raises(SpecError):
        parse_spec("none", tag="t")


def test_cache_key_changes_with_every_input():
    base = cache_key("abc", "", "tr-plus", (), None, "v1")
    assert base != cache_key("abd", "", "tr-plus", (), None, "v1")
    assert base != cache_key("abc", "d1", "tr-plus", (), None, "v1")
    assert base != cache_key("abc", "", "tr", (), None, "v1")
    assert base != cache_key("abc", "", "tr-plus", ("Dbg_SerDMA",), None, "v1")
    assert base != cache_key("abc", "", "tr-plus", (), "pr65", "v1")
    assert base == cache_key("abc", "", "tr-plus", (), None, "v1")


def _main_image(version: str, date: str = "Oct 30 2026", time_: str = "10:00:00") -> bytes:
    pad = b"\x00" * 8
    return (pad + version.encode() + b"\x00" + pad + date.encode() + b"\x00" + pad
            + time_.encode() + b"\x00" + pad)


def test_inspect_hex_reads_version_and_stamp(tmp_path: Path):
    hexfile = tmp_path / "TeensyROM+_0.9-dma.tim-dirty_full.hex"
    hexfile.write_text(encode_hex(MAIN_BASE + 0x1000, _main_image("TeensyROM+ v0.9-dma.tim*")))
    info = inspect_hex(hexfile)
    assert info.kind == "tr-plus" and info.version == "TeensyROM+ v0.9-dma.tim*"
    assert info.stamp == "Oct 30 2026, 10:00:00" and info.tag == "dma.tim" and info.dirty
    plain = tmp_path / "TeensyROM_0.9_full.hex"
    plain.write_text(encode_hex(MAIN_BASE, _main_image("TeensyROM v0.9")))
    p = inspect_hex(plain)
    assert p.kind == "tr" and p.tag is None and not p.dirty
    with pytest.raises(HexError):
        inspect_hex(tmp_path / "bad.hex") if (tmp_path / "bad.hex").write_text("nope") else None


def test_board_runs_compares_version_and_stamp(tmp_path: Path):
    hexfile = tmp_path / "x.hex"
    hexfile.write_text(encode_hex(MAIN_BASE, _main_image("TeensyROM+ v0.9")))
    info = inspect_hex(hexfile)
    same = parse_banner("  FW: TeensyROM+ v0.9\r\n      Oct 30 2026, 10:00:00\r\n  Teensy: 816MHz 50.0C  UID: 1\r\n")
    other = parse_banner("  FW: TeensyROM+ v0.9\r\n      Oct 31 2026, 10:00:00\r\n  Teensy: 816MHz 50.0C  UID: 1\r\n")
    assert board_runs(same, info) and not board_runs(other, info) and not board_runs(None, info)


def test_describe_records_dirty_state(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,  # noqa: E731
                                    stdin=subprocess.DEVNULL)
    run("init", "-q", "-b", "main")
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
    (repo / "a.txt").write_text("hello")
    run("add", "a.txt")
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "a")
    co = describe(repo)
    assert not co.dirty and co.diff_hash == "" and len(co.commit) == 40 and co.short == co.commit[:12]
    (repo / "a.txt").write_text("changed")
    dirty = describe(repo)
    assert dirty.dirty and dirty.diff_hash and dirty.describe.endswith("-dirty")
    with pytest.raises(CheckoutError):
        describe(tmp_path / "nowhere")


def test_worktree_for_puts_a_reused_worktree_back(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,  # noqa: E731
                                    stdin=subprocess.DEVNULL)
    run("init", "-q", "-b", "main")
    (repo / "TeensyROM.h").write_text("// #define Dbg_SerDMA\n")
    run("add", "TeensyROM.h")
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "h")
    wt = worktree_for(repo, "main", tmp_path / "work")
    assert wt == tmp_path / "work" / "wt" / describe(repo).short and not describe(wt).dirty
    (wt / "TeensyROM.h").write_text("#define Dbg_SerDMA\n")        # what a define build leaves behind
    (wt / "stray.txt").write_text("and what a build that died might")
    assert describe(wt).dirty
    assert worktree_for(repo, "main", tmp_path / "work") == wt
    assert not describe(wt).dirty and not (wt / "stray.txt").exists()


def test_a_define_build_patches_minimalboot_s_header_too(tmp_path: Path):
    from treadmill.firmware.build import MAIN_HEADER, MINIMAL_HEADER, enable_defines_in_header
    main, minimal = tmp_path / MAIN_HEADER, tmp_path / MINIMAL_HEADER
    minimal.parent.mkdir(parents=True)
    main.write_text("  // #define Dbg_SerDMA    //DMA\n  // #define Dbg_SerTimChg //timing\n", encoding="latin-1")
    minimal.write_text("  // #define Dbg_SerTimChg //timing\n  // #define Dbg_SerLog\n", encoding="latin-1")
    assert enable_defines_in_header(tmp_path, ("Dbg_SerTimChg", "Dbg_SerDMA")) == []
    assert main.read_text(encoding="latin-1") == "  #define Dbg_SerDMA    //DMA\n  #define Dbg_SerTimChg //timing\n"
    assert minimal.read_text(encoding="latin-1") == "  #define Dbg_SerTimChg //timing\n  // #define Dbg_SerLog\n"
    assert enable_defines_in_header(tmp_path, ("Dbg_Nope",)) == ["Dbg_Nope"]
