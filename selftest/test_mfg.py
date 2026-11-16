"""The manufacturing test without hardware: its checks one by one, the board
log, and whole sessions against fake boards, passing and halting."""

import builtins
import csv
import io
import json
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import numpy as np

from selftest.fake_bench import SD_ROOT, FakeBench
from selftest.fake_programs import DARK_GREY, GREEN, LIGHT_GREEN, YELLOW, checker_screen
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
from treadmill.link.discover import Found, NotFound, teensyrom_uid
from treadmill.mfg import (
    CIA1_ICR, FAIL, HELLO, NOT_RUN, PASS, RED, SID, BoardLog, BoardRecord, Console, MfgProfile, RealBench,
    _ntp_stamp, arrival, clock_delta, clock_problem, ntp_offset, pc_clock_offset, utc_at,
    ReuView, compare_sd, epc_reason, firmware_problems, reu_verdict, reu_view, run_session, sd_snapshot,
    xpt_reason, xpt_serial,
)
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

    def wait(exclude=(), timeout=None):
        for port, serial_string in now:
            if port not in exclude:
                return Found(port, teensyrom_uid(serial_string), "serial-string")
        raise NotFound("none")

    monkeypatch.setattr(mfg, "list_teensyrom_ports", ports)
    monkeypatch.setattr(mfg, "wait_for_teensyrom", wait)
    console, out = _console()
    return RealBench(console), out


def test_a_board_on_usb_before_the_session_is_taken_only_once_it_comes_back(monkeypatch):
    other, early, new = (("COM3", "TEENSYROM-SERIAL-11111111"), ("COM4", "TEENSYROM-SERIAL-22222222"),
                         ("COM5", "TEENSYROM-SERIAL-33333333"))
    bench, out = _bench_on(monkeypatch, [[other], [other], [other, new]])
    assert "COM3 (chip id 11111111)" in out.getvalue()
    assert bench.wait_for_board() == ("COM5", "33333333")
    bench, _ = _bench_on(monkeypatch, [[other, early], [other, early], [other], [other, early]])
    assert bench.wait_for_board() == ("COM4", "22222222")


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
