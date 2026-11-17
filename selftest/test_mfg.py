"""The manufacturing test without hardware: its checks one by one, the board
log, and whole sessions against fake boards, passing and halting."""

import csv
import io
import json
import socket
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import numpy as np

from selftest.fake_bench import SD_ROOT, FakeBench
from selftest.fake_board import BANNER as FAKE_BANNER
from selftest.fake_programs import DARK_GREY, GREEN, LIGHT_GREEN, YELLOW, ExpPort, ExtPorts, checker_screen
from selftest.fake_programs import RED as REU_RED
from selftest.synth import calibrated, frame_of
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from treadmill.c64.screen import (
    CHARSET_LOWER_UPPER, COLOR_RAM, SCREEN_BYTES, SCREEN_RAM_DEFAULT, VIC_BORDER, screen_rows,
)
from treadmill.cli import EXIT_FAILED, EXIT_OK, main
from treadmill.firmware.hexinfo import MAIN_BASE, encode_hex, inspect_hex
from treadmill import mfg
from treadmill.link.banner import parse_banner
from treadmill.link.transport import ByteStream
from treadmill.link.trlink import DRAIN_S, Link
from treadmill.mfg import (
    CIA1_ICR, EPC_ENDED, FAIL, HELLO, IGETIN, INSTRUCTIONS, LAUNCHED, NO_KEY, NOT_RUN, PASS, RED, SET_STANDARD,
    SID, BoardLog, BoardRecord, Console, Ctx, MfgProfile, RealBench, StepFailed, hold_epc_prompt,
    _ntp_stamp, arrival, clock_delta, clock_problem, ntp_offset, pc_clock_offset, utc_at,
    ReuView, compare_sd, epc_reason, firmware_problems, reu_verdict, reu_view, run_session, sd_snapshot,
    xpt_reason, xpt_serial,
)
from treadmill.observe.capture import CaptureTimeout
from treadmill.observe.channels import ScreenChannel
from treadmill.report.artifacts import RunDir
from treadmill.station import load_station

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
    path = folder / "trplus.toml"
    path.write_text('factory_hex = "factory.hex"\nepc_timeout_s = 30\nxpt_pass_timeout_s = 30\n'
                    'reu_timeout_s = 30\nntp_server = ""\n' + extra, encoding="utf-8")
    return path


def _console() -> tuple[Console, io.StringIO]:
    out = io.StringIO()
    return Console(out=out, color=False), out


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


def test_the_hello_line_arrival():
    chunks = [(1.0, b"menu\r\n"), (2.0, b"\r\nHello from"), (3.0, b" TR at: 10:49:13 pm\n")]
    text = b"".join(c for _, c in chunks).decode("latin-1")
    assert arrival(chunks, HELLO.search(text).start()) == 2.0      # split across two chunks: its first byte's
    assert arrival(chunks, 5) == 1.0 and arrival(chunks, 6) == 2.0
    with pytest.raises(ValueError):
        arrival(chunks, len(text))
    before = datetime.now(timezone.utc)
    assert abs((utc_at(time.monotonic() - 2.0, 10.0) - before).total_seconds() - 8.0) < 0.5
    assert abs((utc_at(time.monotonic(), None) - datetime.now(timezone.utc)).total_seconds()) < 0.5


def test_the_clock_problem_named():
    assert clock_problem(-1.8, 5, False, True) is None
    assert "the time fetch" in clock_problem(-6.0, 5, False, True)
    assert clock_problem(3601, 5, False, True) == ("the board's clock reads UTC+1:00, +1 s off: "
                                                  "its timezone setting is not UTC")
    assert "reads UTC+0:30, -2 s off" in clock_problem(1798, 5, None, True)
    assert "reads UTC-5:00, -1 s off" in clock_problem(-18001, 5, False, True)
    assert "reads UTC-11:00 or UTC+13:00," in clock_problem(-39601, 5, False, True)     # no date on the line
    assert "reads UTC-12:00 or UTC+12:00," in clock_problem(-43200, 5, False, True)
    assert "the time fetch" in clock_problem(3600, 5, True, True)      # settings at defaults: no timezone
    assert "the time fetch" in clock_problem(600, 5, False, True)
    assert clock_problem(600, 5, False, False).endswith("; this PC's clock was not checked against NTP")
    # The screen showed the time fetch fail: the clock was never set, so no timezone reading of it either.
    for delta in (17455, 3601):
        assert clock_problem(delta, 5, False, True, "NTP Response timeout!") == (
            f"the time fetch over Ethernet failed (the screen showed 'NTP Response timeout!'): the board's clock "
            f"is {delta:+d} s from UTC")
    assert clock_problem(-1.8, 5, False, True, "RTC not updated") is None


class FakeNtp:
    """An SNTP server on localhost whose clock is ``offset_s`` ahead of this
    PC's, or one that fails: ``silent``, ``stale`` (a reply to some other
    request) or ``kod`` (a kiss-of-death, stratum 0)."""

    def __init__(self, offset_s: float = 0.0, fault: str | None = None):
        self.offset_s, self.fault = offset_s, fault
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(512)
            except OSError:
                continue
            self.requests += 1
            if self.fault == "silent":
                continue
            now = _ntp_stamp(time.time() + self.offset_s)
            origin = bytes(8) if self.fault == "stale" else data[40:48]
            stratum = 0 if self.fault == "kod" else 2
            self.sock.sendto(bytes([0x24, stratum]) + bytes(22) + origin + now + now, addr)

    def __enter__(self) -> "FakeNtp":
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join()
        self.sock.close()


def test_this_pcs_clock_against_ntp():
    with FakeNtp(10.0) as server:
        assert ntp_offset("127.0.0.1", server.port) == pytest.approx(10.0, abs=0.05)
        assert server.requests == 3
    for fault in ("silent", "stale", "kod"):
        with FakeNtp(10.0, fault) as server:
            assert ntp_offset("127.0.0.1", server.port, samples=2, timeout=0.2) is None, fault
            assert server.requests == 2


def test_the_session_says_what_it_knows_of_this_pcs_clock(monkeypatch):
    console, out = _console()
    assert pc_clock_offset("", console) is None and "not checked against NTP" in out.getvalue()
    monkeypatch.setattr(mfg, "ntp_offset", lambda server: None)
    assert pc_clock_offset("pool.ntp.org", console) is None and "No time from pool.ntp.org" in out.getvalue()
    monkeypatch.setattr(mfg, "ntp_offset", lambda server: 0.4)
    assert pc_clock_offset("pool.ntp.org", console) == 0.4
    assert "This PC's clock is 0.4 s behind pool.ntp.org." in out.getvalue()
    monkeypatch.setattr(mfg, "ntp_offset", lambda server: -12.5)
    assert pc_clock_offset("pool.ntp.org", console) == -12.5
    assert "12.5 s ahead of pool.ntp.org; boards are checked against NTP's time" in out.getvalue()


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
    # A line is read once its newline is in: "  CLEAN" is a printf of its own, and a read can end before it.
    assert xpt_serial("$ff over $ff @ $c000: 0 bad of 4096 bytes")[0] == []
    assert xpt_serial("$ff over $ff @ $c000: 0 bad of 4096 bytes  CLEAN\n")[0] == [
        ("$ff over $ff @ $c000: 0 bad of 4096 bytes", "CLEAN")]
    assert LAUNCHED.search("Remote Launch:\nP: /Test+Diags\nF: REU-Chec") is None
    assert LAUNCHED.search("F: REU-Checker v1.0\r\n").group(1) == "REU-Checker v1.0"
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


def _bench_on(monkeypatch, states: list[list[tuple[str, str]]]) -> tuple[RealBench, io.StringIO]:
    """A RealBench whose USB shows ``states`` in turn, one per look."""
    seq, now = iter(states), []

    def ports():
        now[:] = next(seq, now)
        return list(now)

    monkeypatch.setattr(mfg, "list_teensyrom_ports", ports)
    monkeypatch.setattr(mfg, "BOARD_POLL_S", 0.0)
    console, out = _console()
    return RealBench(console), out


def test_a_teensyrom_on_usb_before_the_session_is_never_taken(monkeypatch):
    """Backlog 27: the boards on USB at the start are known by chip id, so
    one that leaves USB and comes back, on its port or another (its machine
    power-cycled, a treadmill run reflashing it), is not taken for the next
    board. A board under test switched on early stays skipped too, as the
    warning says. A PJRC device that names no chip id is never a board."""
    other, early, new = (("COM3", "TEENSYROM-SERIAL-11111111"), ("COM4", "TEENSYROM-SERIAL-22222222"),
                         ("COM5", "TEENSYROM-SERIAL-33333333"))
    keyboard = ("COM6", "12345678")
    bench, out = _bench_on(monkeypatch, [[other, early, keyboard], [other, early, keyboard], [keyboard],
                                         [("COM7", other[1]), keyboard], [("COM7", other[1]), early, keyboard, new]])
    assert bench.wait_for_board() == ("COM5", "33333333")
    assert "COM3 (chip id 11111111), COM4 (chip id 22222222), COM6 (chip id ?)" in out.getvalue()
    assert "skipped for this session" in out.getvalue() and "start again" in out.getvalue()


# -- whole sessions against fake boards ----------------------------------------------------------------

def test_a_passing_board_through_the_cli(lab: Path, capsys):  # noqa: F811
    profile = _profile(lab / "mfg")
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
    assert {"open", "menu", "epc_launch", "epc_prompt", "epc_hello", "epc_verdict", "led_ethernet", "led_tr",
            "reset_press", "reset_follow", "reset_menu", "reset_listing", "xpt_launch", "xpt1_lines",
            "xpt1_prompt", "xpt1_verdict", "xpt3_key", "xpt_menu", "sd_list", "reu_launch", "reu_up",
            "reu_last_change", "reu_checker", "reu_menu", "finish"} <= set(result["waits"])
    assert (folder / "serial.log").is_file() and (folder / "expansion_port_3.screen.txt").is_file()
    assert "Hello from TR at:" in (folder / "serial.log").read_text(encoding="latin-1")
    assert "#### " in out and "Board 1 PASSED" in out and "1 board(s): 1 passed, 0 failed" in out
    assert "TR+ manufacturing test: PASS" not in out          # that summary goes to the C64, not the console
    run = json.loads((folder.parent.parent / "run.json").read_text(encoding="utf-8"))
    assert run["mode"] == "mfg" and run["counts"]["PASS"] == 1
    assert run["pc_clock"] == {"ntp_server": "", "offset_s": None}


def test_the_reu_checker_read_by_its_colors():
    cal = calibrated()

    def view(kb=512, done=8, testing=None, error_bank=None, working_ink=LIGHT_GREEN, border=GREEN):
        codes, colors = checker_screen(kb, done, testing, error_bank, working_ink)
        image = frame_of(np.frombuffer(codes, np.uint8), np.frombuffer(colors, np.uint8), CHARSET_LOWER_UPPER,
                         background=DARK_GREY, border=border)
        return reu_view(image, cal)

    v = view()
    assert v.cells.startswith("G" * 8 + ".") and v.cells.count("G") == 8 and v.working and v.border_green
    assert reu_verdict(v, 8) is None
    v = view(error_bank=3)
    assert v.cells.startswith("GGGRGGGG.") and "R" not in v.cells[8:]
    assert reu_verdict(v, 8) == "the REU checker shows an error in the bank at 192K"
    v = view(kb=256, done=2, testing=2)
    assert v.cells.startswith("GG..") and v.cells.count("G") == 2
    assert reu_verdict(view(kb=256, done=4), 8) == "the REU checker found 256K, expected 512K"
    assert not view(working_ink=YELLOW).working and not view(border=REU_RED).border_green
    assert reu_verdict(view(working_ink=YELLOW), 8) == "the REU checker's 'THE REU IS WORKING!' line is not green"
    rest = "." * (256 - 9)
    assert reu_verdict(ReuView("GG.GGGGG." + rest, True, True), 8).endswith("(wrong at 128K)")
    assert reu_verdict(ReuView("G" * 9 + rest, True, True), 8) == "the REU checker found 576K, expected 512K"


def test_failures_halt_and_mark_the_screen(lab: Path):  # noqa: F811
    """Four boards: one fails the Expansion Port test's second pass, one has
    no SD card, one's clock is an hour off, one's REU shows an error. Each
    stops at its step with the later steps not run, its screen marked (the
    REU's on the menu, after a reset), and a row in the log."""
    profile = MfgProfile.load(_profile(lab / "mfg"))
    console, out = _console()
    bench = FakeBench(console, boards=[{"xpt_fail_pass": 2}, {"epc_fail": "sd"}, {"clock_offset_s": 600},
                                       {"reu_fail": True}])
    seen = []
    power_on = bench._power_on

    def keep():
        power_on()
        seen.append(bench.fake)
    bench._power_on = keep
    video = bench.video_kit()
    try:
        summary = run_session(load_station("s1"), profile, bench, console, video, RunDir(lab / "reports"),
                              boards=4, log=lambda m: None)
    finally:
        video.close()
    assert summary["counts"] == {PASS: 0, FAIL: 4, "ABORTED": 0}
    rows = _rows(profile.log)
    assert [r["failed_step"] for r in rows] == ["Expansion Port", "External Ports", "External Ports", "REU"]
    assert rows[0]["reason"] == "pass 2: $ff: 3/4096 Failed"
    assert rows[1]["reason"] == "Could not init SD"
    assert "s from UTC: the time fetch" in rows[2]["reason"] and rows[2]["led_ethernet"] == ""
    assert rows[3]["reason"] == "the REU checker shows an error in the bank at 192K" and rows[3]["sd_root"] == PASS
    for row in rows:
        assert row["result"] == FAIL and row["c64_marked"] == "yes"
    for row in rows[:3]:
        assert row["reu"] == NOT_RUN and row["sd_root"] == NOT_RUN
    assert rows[0]["external_ports"] == PASS and rows[1]["external_reset"] == NOT_RUN
    bank_map = (Path(rows[3]["evidence"]) / "reu.colors.txt").read_text(encoding="utf-8").splitlines()[1]
    assert bank_map[3] == "R" and "R" not in bank_map[4:]
    for fake, step in zip(seen, ("FAIL 5 Expansion Port", "FAIL 3 External Ports", "FAIL 3 External Ports",
                                 "FAIL 7 REU")):
        assert fake.memory[VIC_BORDER] == RED and fake.memory[VIC_BORDER + 1] == RED
        assert fake.memory[CIA1_ICR] == 0x01 and not any(fake.memory[SID:SID + 0x19])   # chords played, silent
        screen = bytes(fake.memory[SCREEN_RAM_DEFAULT:SCREEN_RAM_DEFAULT + SCREEN_BYTES])
        assert screen_rows(screen)[24].startswith(step) and screen[0] & 0x80 == 0     # the banner turned back
        assert all(c >= 0x80 for c in screen[24 * 40:])
        assert fake.memory[COLOR_RAM + 24 * 40] == 7
    assert out.getvalue().count("#####") >= 4 and "Could not init SD" in out.getvalue()
    assert "Board 4 FAILED" in out.getvalue()


def test_the_led_answers_come_from_the_c64(lab: Path):  # noqa: F811
    """Three boards' operators answer on the C64's keyboard: a stray key then
    N for the Ethernet LED; Y then N; Y then nothing. While the questions are
    up GETIN goes through the no-key stub, so no key ends the test, whose exit
    would log "Button detected" as an external reset does."""
    profile = MfgProfile.load(_profile(lab / "mfg", "led_timeout_s = 1\n"))
    console, out = _console()
    bench = FakeBench(console, boards=[{"leds": ["qn"]}, {"leds": ["y", "n"]}, {"leds": ["y"]}])
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
    assert [r["failed_step"] for r in rows] == ["External Ports"] * 3
    assert [r["reason"] for r in rows] == ["the Ethernet green LED is off (operator)", "the TR LED is off (operator)",
                                           "no Y or N for the TR LED on the C64's keyboard within 1 s"]
    assert [(r["led_ethernet"], r["led_tr"]) for r in rows] == [("n", ""), ("y", "n"), ("y", "")]
    for fake, row in zip(seen, rows):
        assert row["c64_marked"] == "yes"
        assert fake.memory[IGETIN:IGETIN + 2] == b"\x3e\x03"
        assert fake.memory[0x033E:0x033E + len(NO_KEY)] == NO_KEY
        assert "Button detected" not in (Path(row["evidence"]) / "serial.log").read_text(encoding="latin-1")
    assert "answer the questions at the bottom of the C64's screen" in out.getvalue()


def test_the_getin_vector_changes_by_one_byte():
    """The stub goes at the vector's own low byte, so only the high byte is
    written: a 6510 between the two fetches of JMP ($032A) sees GETIN or the
    stub, never a third address. A low byte that puts no stub in the cassette
    buffer is refused before anything is written."""
    class Link:
        def __init__(self, vector: bytes):
            self.vector, self.writes = vector, []

        def read_memory(self, address, length, drain=None):
            assert (address, length) == (IGETIN, 2)
            return self.vector

        def write_memory(self, address, data, drain=None):
            self.writes.append((address, bytes(data)))

    c = SimpleNamespace(link=Link(b"\x3e\xf1"))
    hold_epc_prompt(c)
    assert c.link.writes == [(0x033E, NO_KEY), (IGETIN + 1, b"\x03")]
    for vector in (b"\x10\xf1", b"\xfa\xf1"):
        c.link = Link(vector)
        with pytest.raises(StepFailed, match="GETIN vector"):
            hold_epc_prompt(c)
        assert c.link.writes == []


def test_keys_lost_frames_and_an_early_expansion_port_fail(lab: Path, monkeypatch):  # noqa: F811
    """What an operator and a capture do to the tests.
    1. A key on the C64 as mfg first reads the verdict waits in the buffer:
       GETIN is held as soon as the Hello line is in, and each question
       empties the buffer once it shows.
    2. A key as the prompt shows, before anything sent can land, ends the
       test, and the step says so.
    3. A key pressed as a question goes up does not answer it.
    4. Lost frames while the buttons are awaited are waited out.
    5. An Expansion Port pass that stops at its address test, with no serial
       line, fails on its screen's reason without waiting for serial.
    Boards 1, 3 and 4 say N to the TR LED, so each stops at that question."""
    monkeypatch.setattr(mfg, "VERDICT_S", 2.0)
    profile = MfgProfile.load(_profile(lab / "mfg"))
    console, out = _console()
    bench = FakeBench(console, boards=[{"epc_key": "verdict", "leds": ["y", "n"]}, {"epc_key": "prompt"},
                                       {"leds": ["y", "n"], "led_early": "y"},
                                       {"buttons_after_s": 1.0, "leds": ["y", "n"]}, {"xpt_early_fail_pass": 1}])
    lost = []
    read_video = ScreenChannel.read_video

    def flaky(self, frame=None):
        program = bench.fake.program
        if isinstance(program, ExtPorts) and program.waiting:
            lost.append(1)
            raise CaptureTimeout("no frame from the capture")
        return read_video(self, frame)
    monkeypatch.setattr(ScreenChannel, "read_video", flaky)
    video = bench.video_kit()
    try:
        run_session(load_station("s1"), profile, bench, console, video, RunDir(lab / "reports"),
                    boards=5, log=lambda m: None)
    finally:
        video.close()
    rows = _rows(profile.log)
    led_off = "the TR LED is off (operator)"
    assert [r["reason"] for r in rows] == [led_off, EPC_ENDED, led_off, led_off,
                                           "pass 1: Miscompare at $0010: Exp $04, Rd $00"]
    assert [r["failed_step"] for r in rows] == ["External Ports"] * 4 + ["Expansion Port"]
    assert [(r["led_ethernet"], r["led_tr"]) for r in rows[:4]] == [("y", "n"), ("", ""), ("y", "n"), ("y", "n")]
    assert lost and all(r["c64_marked"] == "yes" for r in rows)


def test_this_pcs_clock_is_measured_for_each_board(lab: Path, monkeypatch):  # noqa: F811
    """The PC's offset from NTP is measured again before each board, so a
    clock set mid-session (as the warning says to) is not corrected twice.
    Here NTP's second answer puts the fake board's exact clock 100 s fast."""
    offsets = [0.0, -100.0]
    monkeypatch.setattr(mfg, "ntp_offset", lambda server: offsets.pop(0))
    profile = replace(MfgProfile.load(_profile(lab / "mfg")), ntp_server="pool.ntp.org")
    console, out = _console()
    bench = FakeBench(console, boards=[{"leds": ["y", "n"]}, {"leds": ["y", "n"]}])
    video = bench.video_kit()
    try:
        run_session(load_station("s1"), profile, bench, console, video, RunDir(lab / "reports"),
                    boards=2, log=lambda m: None)
    finally:
        video.close()
    rows = _rows(profile.log)
    assert rows[0]["reason"] == "the TR LED is off (operator)"
    assert offsets == [] and 98 <= float(rows[1]["epc_time_delta_s"]) <= 100   # whole seconds: 0 to 2 s behind
    assert rows[1]["reason"].endswith("s from UTC: the time fetch over Ethernet, or the RTC")
    assert "This PC's clock is 100.0 s ahead of pool.ntp.org" in out.getvalue()


def test_a_passing_board_waits_only_for_stray_bytes(lab: Path, monkeypatch):  # noqa: F811
    """The link's fixed drain before every command was about a third of a
    passing board's time. With drain_when_pending a command waits only when
    the board sent something unasked first: the Hello line, the lines after a
    reset, a pass's pattern lines, the REU launch. The menu here draws its
    listings an item at a time, as the bench's does, so a launch read with no
    drain before it lands mid-draw."""
    switch: list[bool] = []          # each command's drain_when_pending
    stray: list[bytes] = []          # what each settle found waiting
    send, settle = Link._send, ByteStream.settle

    def counted_send(self, data, drain=DRAIN_S):
        switch.append(self.drain_when_pending)
        return send(self, data, drain)

    def counted_settle(self, quiet, limit):
        stale, quiet_ok = settle(self, quiet, limit)
        stray.append(stale)
        return stale, quiet_ok
    monkeypatch.setattr(Link, "_send", counted_send)
    monkeypatch.setattr(ByteStream, "settle", counted_settle)
    profile = MfgProfile.load(_profile(lab / "mfg"))
    console, _ = _console()
    bench = FakeBench(console, boards=[{"menu_draws_by_item": True}])
    video = bench.video_kit()
    try:
        summary = run_session(load_station("s1"), profile, bench, console, video, RunDir(lab / "reports"),
                              boards=1, log=lambda m: None)
    finally:
        video.close()
    assert summary["counts"][PASS] == 1, summary["boards"][0]["reason"]
    after = switch[switch.index(True):]           # Board.open's probe comes before the switch
    assert all(after) and len(after) > 100
    assert len(stray) == len(after) and sum(1 for stale in stray if stale) <= 10


def test_serial_since_extends_its_last_answer_and_starts_over_for_a_new_mark_or_link():
    chunks = [(0.0, b"one\n"), (0.0, b"two\n")]
    c = Ctx(*([None] * 10))
    c.board = SimpleNamespace(link=SimpleNamespace(stream=SimpleNamespace(tap=SimpleNamespace(chunks=chunks))))
    assert c.serial_since(0) == "one\ntwo\n"
    chunks.append((0.0, b"three\n"))
    assert c.serial_since(0) == "one\ntwo\nthree\n"
    assert c.serial_since(1) == "two\nthree\n"
    replaced = [*chunks, (0.0, b"four\n")]          # a reconnect: a new list, the old chunks first
    c.board.link.stream.tap.chunks = replaced
    assert c.serial_since(1) == "two\nthree\nfour\n"


def test_a_lost_frame_while_a_pass_shows_its_prompt_is_waited_out(lab: Path, monkeypatch):  # noqa: F811
    """Backlog 28: after a pass's serial lines, the wait for its prompt reads
    video only. A lost frame there failed a good board with the capture's
    message, and marked its screen by DMA in the middle of the bus test."""
    profile = MfgProfile.load(_profile(lab / "mfg"))
    console, _ = _console()
    bench = FakeBench(console, boards=[{}])
    lost = []
    read_video = ScreenChannel.read_video

    def flaky(self, frame=None):
        if isinstance(bench.fake.program, ExpPort) and bench.fake.program.passes and not lost:
            lost.append(1)
            raise CaptureTimeout("no frame from the capture")
        return read_video(self, frame)
    monkeypatch.setattr(ScreenChannel, "read_video", flaky)
    video = bench.video_kit()
    try:
        summary = run_session(load_station("s1"), profile, bench, console, video, RunDir(lab / "reports"),
                              boards=1, log=lambda m: None)
    finally:
        video.close()
    assert lost and summary["counts"][PASS] == 1, summary["boards"][0]["reason"]


def test_a_c64_in_the_other_standard_is_set_right_and_its_board_starts_over(lab: Path):  # noqa: F811
    """The station tests in NTSC. A Kawari C64 left in PAL by other work on
    the bench made every video check misread, and its REU checker fail a good
    board. The board is not failed or logged: the operator sets the C64, and
    the same board starts over under its number, here after a second PAL
    power-on."""
    profile = MfgProfile.load(_profile(lab / "mfg"))
    console, out = _console()
    pal = FAKE_BANNER.replace("NTSC Vid  60 Hz", "PAL Vid  50 Hz")
    bench = FakeBench(console, boards=[{"banner": pal}, {"banner": pal}, {}])
    video = bench.video_kit()
    try:
        summary = run_session(load_station("s1"), profile, bench, console, video, RunDir(lab / "reports"),
                              boards=1, log=lambda m: None)
    finally:
        video.close()
    rows = _rows(profile.log)
    assert [r["result"] for r in rows] == [PASS] and summary["counts"] == {PASS: 1, FAIL: 0, "ABORTED": 0}
    wrong = "the C64 runs PAL, and this station tests in NTSC"
    result = json.loads((Path(rows[0]["evidence"]) / "result.json").read_text(encoding="utf-8"))
    assert result["n"] == 1 and result["restarts"] == [wrong, wrong]
    text = out.getvalue()
    assert text.count("The C64 runs PAL, and this station tests in NTSC.") == 2
    assert text.count(INSTRUCTIONS[SET_STANDARD]) == 2 and "Board 1 PASSED" in text and "FAIL" not in text


def test_a_failed_time_fetch_is_named_in_the_clock_failure(lab: Path):  # noqa: F811
    """The bench has no RTC battery, so a board whose time fetch over
    Ethernet failed reads the RTC's count since power-on (00:00:14 am on the
    bench, +17455 s). The screen says the fetch failed, and so does the reason."""
    profile = MfgProfile.load(_profile(lab / "mfg"))
    console, _ = _console()
    bench = FakeBench(console, boards=[{"time_fetch": "timeout", "clock_offset_s": 17455}])
    video = bench.video_kit()
    try:
        run_session(load_station("s1"), profile, bench, console, video, RunDir(lab / "reports"),
                    boards=1, log=lambda m: None)
    finally:
        video.close()
    row = _rows(profile.log)[0]
    assert row["failed_step"] == "External Ports" and row["reason"].startswith(
        "the time fetch over Ethernet failed (the screen showed 'NTP Response timeout!'): the board's clock is +17")
