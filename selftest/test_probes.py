"""The DMA-path probes on the fake board: the firmware's z### pattern test,
the C64's own REU loop with the Settings page that turns the REU on, the
C64's own writes beside z###, screen noise beside a workload, the VIC's
fetches from an Ultimax cartridge, and what each makes of a stall, a crash or
a hang."""

import time
from pathlib import Path

import pytest

import treadmill.link.dbgcmds as dbgcmds
from selftest.fake_board import KNOB_DEFAULTS, start_fake_board
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from treadmill.board import Board
from treadmill.c64.keys import KernalKeys
from treadmill.c64.settings import REU, Settings, SettingsError
from treadmill.link import DebugCommands, Link
from treadmill.observe.channels import ScreenChannel
from treadmill.probes.cpuwatch import CpuWatchProbe
from treadmill.probes.krread import ksense_mode, probe_kernal
from treadmill.probes.noise import NoiseProbe
from treadmill.probes.reuloop import ReuLoopProbe
from treadmill.probes.vicrom import VicRomProbe
from treadmill.probes.zpattern import ZPatternProbe
from treadmill.station import load_station
from treadmill.sweeps import KNOBS, Sample, SearchSpec, Sweep, SweepError

DEFINES = {"Dbg_SerTimChg", "Dbg_SerDMA"}


@pytest.fixture
def fake_station(lab, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dbgcmds, "TEXT_REPLY", (3.0, 0.05, 20.0))
    monkeypatch.setattr(dbgcmds, "LONG_REPLY_GAP_S", 0.2)
    fake, stream = start_fake_board(debug_defines=set(DEFINES))
    board = Board.open(load_station("s1"), link_factory=lambda: Link(stream), fw_defines=tuple(DEFINES))
    try:
        yield fake, board, DebugCommands(board.link, DEFINES)
    finally:
        board.close()


def test_zpattern_finds_the_dmasetup_window_with_the_sums_held(fake_station):
    fake, board, dbg = fake_station
    probe = ZPatternProbe(board, dbg, pages=4, log=lambda m: None)
    margin = Sweep(dbg, KNOBS["e"], probe, SearchSpec(both=True, step=20, fine=5), log=lambda m: None).run()
    down, up = margin.edges["down"], margin.edges["up"]
    assert (down.clean_to, down.first_fail, up.clean_to, up.first_fail) == (380, 375, 480, 485)
    assert up.probes == ["zpattern"] and fake.letters.count("z004") == len(margin.points)
    bad = next(p for p in margin.points if p.failed)
    assert bad.samples == 2 * 5 * 4 * 256 and "pre-fill 0" in bad.notes[0] and "$ff over $ff" not in bad.notes[0]
    assert fake.knobs == KNOB_DEFAULTS and margin.restored
    assert fake.letters[-1] == "td", "the defaults come back before the reset to the menu"


def test_zpattern_counts_a_stall_as_a_hang(fake_station):
    fake, board, dbg = fake_station
    fake.faults["z_stall"] = True
    got = ZPatternProbe(board, dbg, pages=4).sample("e430")
    assert got.hung and got.failures == 1 and "no pattern write on pass 3" in got.note


def test_zpattern_needs_its_define_and_a_sane_page_count(fake_station):
    fake, board, dbg = fake_station
    with pytest.raises(SweepError) as e:
        ZPatternProbe(board, DebugCommands(board.link, {"Dbg_SerTimChg"})).setup()
    assert "Dbg_SerDMA" in str(e.value)
    with pytest.raises(SweepError):
        ZPatternProbe(board, dbg, pages=1000)


def _settings(board) -> Settings:
    screen = ScreenChannel(board.link, None, None, None, dma_allowed=lambda: True)
    return Settings(screen, KernalKeys(board), timeout=10.0, log=lambda m: None)


def test_the_settings_page_sets_special_io_and_puts_it_back(fake_station):
    fake, board, dbg = fake_station
    settings = _settings(board)
    assert settings.special_io_now() == "None"
    with settings.special_io(REU) as was:
        assert was == "None" and fake.special_io == REU
        assert fake.menu.settings_page is None, "back at the menu inside the block"
    assert fake.special_io == "None" and fake.menu.settings_page is None
    with pytest.raises(SettingsError):
        settings.set_special_io("Turbo Chameleon")


def _reu_program(tmp_path: Path) -> Path:
    prg = tmp_path / "reuloop.prg"
    prg.write_bytes(bytes([0x01, 0x08]) + bytes(16))           # the fake runs the name, not the bytes
    return prg


def test_reuloop_finds_the_dmasetup_window_reading_back_at_the_defaults(fake_station, tmp_path: Path):
    fake, board, dbg = fake_station
    fake.special_io = REU
    probe = ReuLoopProbe(board, _reu_program(tmp_path), dwell=0.5, log=lambda m: None)
    margin = Sweep(dbg, KNOBS["e"], probe, SearchSpec(both=True, step=20, fine=5), log=lambda m: None).run()
    down, up = margin.edges["down"], margin.edges["up"]
    assert (down.clean_to, down.first_fail, up.clean_to, up.first_fail) == (380, 375, 480, 485)
    bad = next(p for p in margin.points if p.failed)
    assert bad.samples > 20 and "bad of" in bad.notes[0]
    assert fake.c64loop is None and margin.restored, "the C64 is reset to the menu after the sweep"


def test_reuloop_wants_an_reu_at_special_io(fake_station, tmp_path: Path):
    fake, board, dbg = fake_station
    with pytest.raises(SweepError) as e:
        ReuLoopProbe(board, _reu_program(tmp_path), log=lambda m: None).setup()
    assert "is Special IO set to an REU" in str(e.value)


def test_a_crashed_reu_client_is_relaunched_at_the_defaults(fake_station, tmp_path: Path):
    fake, board, dbg = fake_station
    fake.special_io = REU
    fake.faults["reu_collapse_ns"] = 40
    probe = ReuLoopProbe(board, _reu_program(tmp_path), dwell=0.3, log=lambda m: None)
    margin = Sweep(dbg, KNOBS["e"], probe, SearchSpec(scan=(330, 430), step=20), log=lambda m: None).run()
    first, second = margin.points[0], margin.points[1]
    assert (first.value, first.hung) == (330, True) and "stopped" in first.notes[0]
    assert (second.value, second.hung, second.failed) == (350, False, True), "relaunched, then counting again"
    assert [p.failed for p in margin.points[3:]] == [False, False, False]
    assert margin.edges["down"].clean_to == 390 and margin.restored


class _Workload:
    """A probe that only takes time, clean, for a companion to run beside."""

    name = "workload"

    def setup(self) -> None:
        pass

    def sample(self, label: str, setting=None) -> Sample:
        time.sleep(0.3)
        return Sample(0, 1)

    def teardown(self) -> None:
        pass


def test_noise_finds_the_edge_the_picture_shows_first(fake_station):
    fake, board, dbg = fake_station
    from selftest.synth import calibrated, fake_frame_source
    work = _Workload()
    noise = NoiseProbe(fake_frame_source(fake), calibrated(), work, log=lambda m: None)
    margin = Sweep(dbg, KNOBS["e"], [noise, work], SearchSpec(both=True, step=20, fine=5), log=lambda m: None).run()
    down, up = margin.edges["down"], margin.edges["up"]
    assert (down.clean_to, down.first_fail, down.probes) == (400, 395, ["noise"])
    assert (up.clean_to, up.first_fail) == (520, None)
    info = margin.probe_info["noise"]
    assert info["baseline"] is not None and info["closing_baseline"] is not None and info["limit"] >= 100
    bad = next(p for p in margin.points if p.failed)
    assert bad.by_probe["workload"] == [0, 1] and "over the limit" in bad.notes[0]


def test_noise_needs_the_picture(fake_station):
    fake, board, dbg = fake_station
    with pytest.raises(SweepError) as e:
        NoiseProbe(None, None, _Workload()).setup()
    assert "video" in str(e.value)


def test_cpuwatch_finds_where_a_late_dma_assert_breaks_the_c64_s_writes(fake_station, tmp_path: Path):
    fake, board, dbg = fake_station
    prg = tmp_path / "cpuwatch.prg"
    prg.write_bytes(bytes([0x01, 0x08]) + bytes(16))
    probe = CpuWatchProbe(board, dbg, prg, pages=4, log=lambda m: None)
    margin = Sweep(dbg, KNOBS["a"], probe, SearchSpec(stop=420, step=40, fine=10), log=lambda m: None).run()
    assert (margin.last_clean, margin.first_fail) == (300, 310)
    assert fake.letters.count("z004") >= len(margin.points), "a z### for every sample"
    with pytest.raises(SweepError) as e:
        CpuWatchProbe(board, DebugCommands(board.link, {"Dbg_SerTimChg"}), prg).setup()
    assert "Dbg_SerDMA" in str(e.value)


def test_sweep_command_with_the_dma_probes(lab: Path, capsys):  # noqa: F811
    from treadmill.cli import EXIT_OK, EXIT_USAGE, main
    import json
    base = ["sweep", "e", "--station", "s1", "--reports", str(lab / "reports"), "--fw-defines",
            "Dbg_SerTimChg,Dbg_SerDMA", "--json"]
    code = main(base + ["--probe", "zpattern", "--z-pages", "4", "--both", "--step", "20", "--fine", "5"])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    payload = json.loads(out)
    assert payload["probes"] == ["zpattern"] and payload["held"] == {"release": 854, "latch": 834}
    assert (payload["edges"]["down"]["clean_to"], payload["edges"]["up"]["clean_to"]) == (380, 480)
    assert main(base + ["--probe", "noise"]) == EXIT_USAGE
    assert "runs beside another" in json.loads(capsys.readouterr().err)["error"]


def test_a_grid_maps_dmasetup_by_dmadatahold_with_the_latch_held(fake_station):
    fake, board, dbg = fake_station
    fake.dma_windows["w"] = (400, 450)
    probe = ZPatternProbe(board, dbg, pages=2, log=lambda m: None)
    margin = Sweep(dbg, KNOBS["e"], probe, SearchSpec(scan=(360, 480), grid=(380, 460), step=20),
                   log=lambda m: None).run()
    cells = {(p.value, p.held["w"]): p.failed for p in margin.points}
    assert all(p.held["y"] == 834 - p.value for p in margin.points), "the latch stays where the defaults put it"
    assert not cells[(420, 420)] and cells[(360, 420)] and cells[(420, 380)] and cells[(420, 460)]
    assert (480, 440) not in cells and (460, 460) not in cells, "past the release limit: not probed"
    rows = [[p.held["w"] for p in margin.points if p.value == e] for e in (360, 380)]
    assert rows == [[380, 400, 420, 440, 460], [460, 440, 420, 400, 380]], "rows snake"
    assert margin.edges == {} and margin.held == {"latch": 834} and margin.to_dict()["spec"]["grid"] == [380, 460]
    text = margin.describe()
    assert r"nS_DMASetup \ nS_DMADataHold 380 400 420 440 460" in text
    assert fake.knobs == KNOB_DEFAULTS
    with pytest.raises(SweepError) as e:
        Sweep(dbg, KNOBS["p"], probe, SearchSpec(scan=(140, 160), grid=(1, 2)))
    assert "a grid is nS_DMASetup" in str(e.value)


def _vicrom(fake, board, tmp_path: Path, **kw) -> "VicRomProbe":
    from selftest.synth import calibrated, fake_frame_source
    crt = tmp_path / "vicrom.crt"
    crt.write_bytes(b"C64 CARTRIDGE   " + bytes(64))
    return VicRomProbe(board, fake_frame_source(fake), calibrated(), crt, dwell=0.3, log=lambda m: None, **kw)


def test_vicrom_finds_the_vic_start_window_from_the_picture(fake_station, tmp_path: Path):
    fake, board, dbg = fake_station
    probe = _vicrom(fake, board, tmp_path)
    margin = Sweep(dbg, KNOBS["v"], probe, SearchSpec(both=True, step=20, fine=5), log=lambda m: None).run()
    down, up = margin.edges["down"], margin.edges["up"]
    assert (down.clean_to, down.first_fail, up.clean_to, up.first_fail) == (150, 145, 290, 295)
    info = margin.probe_info["vicrom"]
    assert info["baseline"] is not None and info["limit"] >= 100
    bad = next(p for p in margin.points if p.failed)
    assert "off the reference" in bad.notes[0]
    assert fake.ultimax_since is None and fake.knobs == KNOB_DEFAULTS, "back at the menu with the defaults"


def test_a_hung_vicrom_is_relaunched(fake_station, tmp_path: Path):
    fake, board, dbg = fake_station
    fake.faults["vic_hang_ns"] = 30
    probe = _vicrom(fake, board, tmp_path)
    margin = Sweep(dbg, KNOBS["i"], probe, SearchSpec(scan=(250, 365), step=40), log=lambda m: None).run()
    first, second = margin.points[0], margin.points[1]
    assert (first.value, first.hung) == (250, True) and "stopped" in first.notes[0]
    assert (second.value, second.hung, second.failed) == (290, False, True), "relaunched, counting, glyphs wrong"
    assert [(p.value, p.failed) for p in margin.points[2:]] == [(330, False), (365, False)]


def test_the_probe_kernal_inverts_the_longest_filler_run():
    kernal = bytearray(range(256)) * 32
    kernal[0x04B7:0x04D3] = b"\xaa" * 28                     # the stock KERNAL's filler at $E4B7
    image, defines = probe_kernal(bytes(kernal))
    assert defines == {"MARK": 0xE4B7, "LEN": 28, "FILL": 0xAA, "PAT": 0x0F}
    assert image[0x04B7:0x04D3] == b"\x55" * 28 and image[:0x04B7] == bytes(kernal[:0x04B7])
    with pytest.raises(SweepError):
        probe_kernal(bytes(range(256)) * 32)                  # no run long enough
    _, defines = probe_kernal(b"\x0f" * 0x2000)
    assert defines["LEN"] == 64 and defines["PAT"] == 0x3C, "the RAM pattern steers clear of the filler"


def test_ksense_mode_comes_back_after_the_block(fake_station):
    fake, board, dbg = fake_station
    with ksense_mode(dbg, 0) as was:
        assert was == 9 and fake.knobs["x"] == 0
        dbg.restore_defaults()
        assert fake.knobs["x"] == 0, "td leaves it"
    assert fake.knobs["x"] == 9


def test_the_kernal_file_is_chosen_with_k_and_put_back(fake_station):
    fake, board, dbg = fake_station
    from treadmill.c64.menu import MenuDriver
    screen = ScreenChannel(board.link, None, None, None, dma_allowed=lambda: True)
    keys = KernalKeys(board)
    menu = MenuDriver(screen, keys, board)
    logged: list[str] = []
    settings = Settings(screen, keys, timeout=10.0, log=logged.append)
    fake.files["/treadmill/krprobe.bin"] = bytes(0x2000)
    fake.files["/kernals/jiffy.bin"] = bytes(0x2000)
    fake.directories += ["/treadmill", "/kernals"]
    fake.kernal_file = "SD://kernals/jiffy.bin"
    assert settings.kernal_file_now() == "SD://kernals/jiffy.bin"
    with settings.kernal_file(menu, "SD:/treadmill/krprobe.bin") as was:
        assert was == "SD://kernals/jiffy.bin" and fake.kernal_file == "SD://treadmill/krprobe.bin"
    assert fake.kernal_file == "SD://kernals/jiffy.bin"
    settings.set_kernal_file(menu, "SD:/treadmill/krprobe.bin")
    logged.clear()
    with settings.kernal_file(menu, "SD:/treadmill/krprobe.bin"):
        pass
    assert len([m for m in logged if "Kernal file" in m]) == 1, f"the same file is not put back: {logged}"
    fake.kernal_file = "SD:<.>mos-Support/KERNAL/jaffydos.bin"         # as the page shows a long path
    with settings.kernal_file(menu, "SD:/treadmill/krprobe.bin"):
        pass
    assert fake.kernal_file == "SD://treadmill/krprobe.bin" and "not a path to put back" in logged[-1]
    with pytest.raises(SettingsError):
        settings.set_kernal_file(menu, "TR:/whatever.bin")


def _pet(char: str) -> int:
    """ACME's ``!convtab pet``: lower case to unshifted letters, upper case to shifted ones."""
    c = ord(char)
    return c - 0x20 if "a" <= char <= "z" else c + 0x80 if "A" <= char <= "Z" else c


@pytest.mark.parametrize("top, source, comment, code", [
    ("MainMenuCRT/source/MainMenu.asm", "MainMenuCRT/source/MainMenu.asm", "Select Kernal File", "SET_KERNAL_FILE"),
    ("SettingsMenu/source/SettingsMenu.asm", "SettingsMenu/source/Pg_TRSettings.asm", "Special IO Increment",
     "NEXT_IO"),
])
def test_the_settings_keys_are_the_menus(top, source, comment, code):
    import os
    import re
    import treadmill.c64.settings as settings
    root = os.environ.get("TREADMILL_FW_REPO")
    if not root:
        pytest.skip("TREADMILL_FW_REPO not set")
    c64 = Path(root) / "Source" / "C64"
    assert re.search(r"^\s*!convtab pet\b", (c64 / top).read_text(encoding="latin-1"), re.M), top
    text = (c64 / source).read_text(encoding="latin-1")
    char, = re.findall(rf"cmp #'(.)'\s*;\s*{comment}", text)
    assert getattr(settings, code) == _pet(char), f"{source} compares '{char}', {_pet(char):#04x}"


def test_sweep_command_with_vicrom_and_krread(lab: Path, capsys):  # noqa: F811
    from treadmill.cli import EXIT_OK, main
    import json
    base = ["--station", "s1", "--reports", str(lab / "reports"), "--fw-defines", "Dbg_SerTimChg", "--json",
            "--dwell", "0.3", "--both", "--step", "20", "--fine", "10"]
    assert main(["sweep", "v", "--probe", "vicrom", *base]) == EXIT_OK, capsys.readouterr().err
    edges = json.loads(capsys.readouterr().out)["edges"]
    assert (edges["down"]["clean_to"], edges["up"]["clean_to"]) == (150, 290)
    assert main(["sweep", "n", "--probe", "krread", *base]) == EXIT_OK, capsys.readouterr().err
    payload = json.loads(capsys.readouterr().out)
    assert payload["probes"] == ["krread"] and payload["edges"]["up"]["clean_to"] == 330


def test_sweep_command_under_minimalboot(lab: Path, capsys):  # noqa: F811
    from treadmill.cli import EXIT_OK, EXIT_USAGE, main
    import json
    base = ["sweep", "p", "--station", "s1", "--reports", str(lab / "reports"), "--fw-defines", "Dbg_SerTimChg",
            "--json", "--image", "minimal", "--dwell", "0.2", "--both", "--step", "20", "--fine", "10"]
    assert main(base) == EXIT_OK, capsys.readouterr().err
    payload = json.loads(capsys.readouterr().out)
    assert payload["probes"] == ["idread-minimal"] and payload["firmware"]["image"] == "minimal"
    assert (payload["edges"]["down"]["clean_to"], payload["edges"]["up"]["clean_to"]) == (142, 210)
    assert main(base + ["--probe", "menu"]) == EXIT_USAGE
    assert "idread, read by video" in json.loads(capsys.readouterr().err)["error"]
