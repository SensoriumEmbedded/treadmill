"""The manufacturing test without hardware: its checks one by one, the board
log, and whole sessions against fake boards, passing and halting."""

import builtins
import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from selftest.fake_bench import SD_ROOT, FakeBench
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from treadmill.c64.screen import COLOR_RAM, SCREEN_BYTES, SCREEN_RAM_DEFAULT, VIC_BORDER, screen_rows
from treadmill.cli import EXIT_FAILED, EXIT_OK, main
from treadmill.firmware.hexinfo import MAIN_BASE, encode_hex, inspect_hex
from treadmill.link.banner import parse_banner
from treadmill.mfg import (
    FAIL, HELLO, NOT_RUN, PASS, RED, BoardLog, BoardRecord, Console, MfgProfile, clock_delta, compare_sd,
    epc_reason, firmware_problems, run_session, sd_snapshot, xpt_reason, xpt_serial,
)
from treadmill.observe.golden import Golden, save_golden
from treadmill.report.artifacts import RunDir
from treadmill.station import load_station

REU_ROWS = ["REU Checker v1.0", "", "REU size: 512K", "", "All tests PASSED"]
BANNER = "  FW: {v}\r\n      {d}, {t}\r\n  Teensy: 816MHz 50.0C  UID: 1\r\n  C64  NTSC Vid  60 Hz\n  Boot: complete\n"


def _hex(path: Path, version: str = "TeensyROM+ v0.9", date: str = "Oct 30 2026", time_: str = "10:00:00") -> Path:
    pad = b"\x00" * 8
    image = (pad + version.encode() + b"\x00" + pad + date.encode() + b"\x00" + pad + time_.encode() + b"\x00" + pad)
    path.write_text(encode_hex(MAIN_BASE, image))
    return path


def _profile(folder: Path, extra: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    _hex(folder / "factory.hex")
    (folder / "sd_root.json").write_text(json.dumps({"entries": SD_ROOT}), encoding="utf-8")
    save_golden(REU_ROWS, folder / "reu_checker.pass.screen.txt")
    path = folder / "trplus.toml"
    path.write_text('factory_hex = "factory.hex"\nepc_timeout_s = 30\nxpt_pass_timeout_s = 30\n'
                    'reu_timeout_s = 30\n' + extra, encoding="utf-8")
    return path


def _console(answers: list[str] | None = None) -> tuple[Console, io.StringIO]:
    out = io.StringIO()
    queue = list(answers or [])
    return Console(out=out, ask=lambda prompt: queue.pop(0) if queue else "y", color=False), out


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# -- checks that need no board ------------------------------------------------------------------

def test_firmware_is_held_to_the_factory_hex(tmp_path: Path):
    expected = inspect_hex(_hex(tmp_path / "f.hex"))
    good = parse_banner(BANNER.format(v="TeensyROM+ v0.9", d="Oct 30 2026", t="10:00:00"))
    assert firmware_problems("main", good, expected) == []
    tagged = parse_banner(BANNER.format(v="TeensyROM+ v0.9-tim*", d="Oct 30 2026", t="10:00:00"))
    problems = firmware_problems("main", tagged, expected)
    assert any("tagged" in p for p in problems) and any("uncommitted" in p for p in problems)
    assert any("version" in p for p in problems)
    assert firmware_problems("main", tagged, inspect_hex(_hex(tmp_path / "t.hex", "TeensyROM+ v0.9-tim*")),
                             require_release=False) == []
    rebuilt = parse_banner(BANNER.format(v="TeensyROM+ v0.9", d="Oct 31 2026", t="10:00:00"))
    assert firmware_problems("main", rebuilt, expected) == [
        "built Oct 31 2026, 10:00:00, expected Oct 30 2026, 10:00:00"]
    assert any("minimal" in p for p in firmware_problems("minimal", good, expected))
    plain = parse_banner(BANNER.format(v="TeensyROM v0.9", d="Oct 30 2026", t="10:00:00"))
    assert any("not a TR+" in p for p in firmware_problems("main", plain, expected))


def test_the_hello_clock_against_utc():
    noon = datetime(2026, 11, 11, 12, 5, 0, tzinfo=timezone.utc)
    assert clock_delta(HELLO.search("Hello from TR at: 00:05:00 pm"), noon) == 0      # 12 o'clock prints as 00
    assert clock_delta(HELLO.search("Hello from TR at: 00:05:00 am"), noon) == -43200
    midnight = datetime(2026, 11, 12, 0, 0, 1, tzinfo=timezone.utc)
    assert clock_delta(HELLO.search("Hello from TR at: 11:59:59 pm"), midnight) == -2  # across the day's end
    assert clock_delta(HELLO.search("hello from tr at: 03:07:09 PM"), datetime(2026, 1, 1, 15, 8, 9,
                                                                                tzinfo=timezone.utc)) == -60


def test_sd_root_compare():
    golden = [{"type": "dir", "name": "APPS"}, {"type": "file", "name": "MPE.TRH", "size": 10},
              {"type": "file", "name": "VMS", "size": 1}]
    card = [{"type": "dir", "name": "APPS"}, {"type": "file", "name": "MPE.TRH", "size": 9},
            {"type": "dir", "name": "VMS"}, {"type": "file", "name": "junk.prg", "size": 3},
            {"type": "dir", "name": "System Volume Information"}]
    assert compare_sd(card, golden, ("System Volume Information",)) == [
        "extra junk.prg", "MPE.TRH is 9 bytes, expected 10", "VMS is a dir, expected a file"]
    assert compare_sd(golden[:1], golden) == ["missing MPE.TRH", "missing VMS"]
    assert compare_sd(golden, golden) == []


def test_snapshot_skips_dotfiles_and_the_cli_writes_it(tmp_path: Path, capsys):
    master = tmp_path / "image"
    (master / "APPS").mkdir(parents=True)
    (master / "MPE.TRH").write_bytes(b"x" * 7)
    (master / ".hidden").write_text("no")
    assert sd_snapshot(master)["entries"] == [{"type": "dir", "name": "APPS"},
                                              {"type": "file", "name": "MPE.TRH", "size": 7}]
    out = tmp_path / "golden" / "sd_root.json"
    assert main(["mfg", "--snapshot-sd", str(master), "--out", str(out)]) == EXIT_OK
    assert json.loads(out.read_text(encoding="utf-8"))["entries"][1]["name"] == "MPE.TRH"
    assert "2 root entries" in capsys.readouterr().out


def test_test_program_parsers():
    text = ("$ff over $ff @ $c000: 0 bad of 4096 bytes  CLEAN\r\n"
            "$ff over $00 @ $c000: 3 bad of 16384 bytes\n  worst page: 2, unchanged: 0, xor mask $04\n"
            "$00/$ff alt over $00 @ $c000: 0 bad of 16384 bytes, pre-fill 2 bad\n"
            "  no read on pass 2: C64 bus not clocking\n")
    lines, stall = xpt_serial(text)
    assert lines == [("$ff over $ff @ $c000: 0 bad of 4096 bytes", "CLEAN"),
                     ("$ff over $00 @ $c000: 3 bad of 16384 bytes", "BAD"),
                     ("$00/$ff alt over $00 @ $c000: 0 bad of 16384 bytes, pre-fill 2 bad", "BAD")]
    assert stall == "no read on pass 2: C64 bus not clocking"
    assert xpt_reason(["TeensyROM+ v0.9 Test", "IRQ Test OK", "NMI Test NMI not detected",
                       " Expansion Port Test Failed! "]) == "NMI Test NMI not detected"
    assert epc_reason("Ethernet connected\nCould not init SD\n External Port Check Failed!") == "Could not init SD"


def test_profile_paths_and_refusals(tmp_path: Path):
    path = _profile(tmp_path / "mfg", 'sd_ignore = ["x"]\n')
    p = MfgProfile.load(path)
    assert p.factory_hex == tmp_path / "mfg" / "factory.hex" and p.log == tmp_path / "mfg" / "boards.csv"
    assert p.sd_ignore == ("x",) and p.xpt_passes == 3 and p.require_release
    path.write_text(path.read_text(encoding="utf-8") + "colour = 1\n", encoding="utf-8")
    with pytest.raises(Exception, match="unknown key"):
        MfgProfile.load(path)


def test_board_log_header_once_and_a_fallback_when_refused(tmp_path: Path):
    console, out = _console()
    record = BoardRecord(1, "COM9", "19300001", datetime(2026, 11, 11, 9, 30, 0), result=PASS)
    log = BoardLog(tmp_path / "boards.csv", "20261111-093000", console)
    log.append(record)
    log.append(record)
    assert (tmp_path / "boards.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    rows = _rows(tmp_path / "boards.csv")
    assert len(rows) == 2 and rows[0]["chip_id"] == "19300001" and rows[0]["start"] == "2026-11-11 09:30:00"
    assert rows[0]["power_up"] == NOT_RUN and rows[0]["result"] == PASS
    locked = tmp_path / "locked.csv"
    locked.mkdir()                              # a path that cannot be opened for writing, as Excel's lock does
    target = BoardLog(locked, "20261111-093000", console).append(record)
    assert target.name == "locked-20261111-093000.csv" and len(_rows(target)) == 1
    assert "could not be written" in out.getvalue()


# -- whole sessions against fake boards ----------------------------------------------------------------

def test_a_passing_board_through_the_cli(lab: Path, monkeypatch, capsys):  # noqa: F811
    profile = _profile(lab / "mfg")
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")
    code = main(["mfg", "--station", "s1", "--profile", str(profile), "--reports", str(lab / "reports"),
                 "--boards", "1"])
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    rows = _rows(lab / "mfg" / "boards.csv")
    assert len(rows) == 1
    row = rows[0]
    assert row["result"] == PASS and row["chip_id"] == "19300001" and row["c64_marked"] == "yes", row
    assert all(row[s] == PASS for s in ("power_up", "firmware", "external_ports", "external_reset",
                                        "expansion_port", "sd_root", "reu"))
    assert row["led_ethernet"] == "y" and row["led_tr"] == "y" and abs(float(row["epc_time_delta_s"])) < 5
    folder = Path(row["evidence"])
    result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    assert result["fw_version"] == "TeensyROM+ v0.9" and result["fresh_eeprom"] is False
    assert (folder / "serial.log").is_file() and (folder / "expansion_port_3.screen.txt").is_file()
    assert "Hello from TR at:" in (folder / "serial.log").read_text(encoding="latin-1")
    assert "#### " in out and "1 board(s): 1 passed, 0 failed" in out
    assert "TR+ manufacturing test: PASS" not in out          # that summary goes to the C64, not the console
    run = json.loads((folder.parent.parent / "run.json").read_text(encoding="utf-8"))
    assert run["mode"] == "mfg" and run["counts"]["PASS"] == 1


def test_with_no_reu_golden_the_checker_s_screen_is_kept_for_one(lab: Path):  # noqa: F811
    profile = MfgProfile.load(_profile(lab / "mfg"))
    profile.reu_golden.unlink()
    console, _ = _console()
    bench = FakeBench(console)
    video = bench.video_kit()
    try:
        run_session(load_station("s1"), profile, bench, console, video, RunDir(lab / "reports"), boards=1,
                    log=lambda m: None)
    finally:
        video.close()
    row = _rows(profile.log)[0]
    assert row["failed_step"] == "REU" and "no REU checker golden" in row["reason"], row
    assert row["c64_marked"] == "yes" and row["sd_root"] == PASS
    saved = Golden.load(Path(row["evidence"]) / "reu.screen.txt")
    assert [r.rstrip() for r in saved.rows[:5]] == REU_ROWS


def test_failures_halt_and_mark_the_screen(lab: Path):  # noqa: F811
    """Three boards: one fails the Expansion Port test's second pass, one has
    no SD card, one's clock is an hour off. Each stops at its step with the
    later steps not run, its screen marked, and a row in the log."""
    profile = MfgProfile.load(_profile(lab / "mfg"))
    console, out = _console()
    bench = FakeBench(console, boards=[{"xpt_fail_pass": 2}, {"epc_fail": "sd"}, {"clock_offset_s": 3600}])
    seen = []
    power_on = bench._power_on

    def keep():
        power_on()
        seen.append(bench.fake)
    bench._power_on = keep
    video = bench.video_kit()
    try:
        summary = run_session(load_station("s1"), profile, bench, console, video, RunDir(lab / "reports"),
                              boards=3, log=lambda m: None)
    finally:
        video.close()
    assert summary["counts"] == {PASS: 0, FAIL: 3, "ABORTED": 0}
    rows = _rows(profile.log)
    assert [r["failed_step"] for r in rows] == ["Expansion Port", "External Ports", "External Ports"]
    assert rows[0]["reason"] == "pass 2: $ff: 3/4096 Failed"
    assert rows[1]["reason"] == "Could not init SD"
    assert "from UTC" in rows[2]["reason"] and rows[2]["led_ethernet"] == ""
    for row in rows:
        assert row["result"] == FAIL and row["c64_marked"] == "yes"
        assert row["reu"] == NOT_RUN and row["sd_root"] == NOT_RUN
    assert rows[0]["external_ports"] == PASS and rows[1]["external_reset"] == NOT_RUN
    for fake, step in zip(seen, ("FAIL 5 Expansion Port", "FAIL 3 External Ports", "FAIL 3 External Ports")):
        assert fake.memory[VIC_BORDER] == RED and fake.memory[VIC_BORDER + 1] == RED
        screen = bytes(fake.memory[SCREEN_RAM_DEFAULT:SCREEN_RAM_DEFAULT + SCREEN_BYTES])
        assert screen_rows(screen)[24].startswith(step) and screen[0] & 0x80 == 0     # the banner turned back
        assert all(c >= 0x80 for c in screen[24 * 40:])
        assert fake.memory[COLOR_RAM + 24 * 40] == 7
    assert out.getvalue().count("#####") >= 3 and "Could not init SD" in out.getvalue()
