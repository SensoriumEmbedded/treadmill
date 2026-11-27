"""The DMA-path probes on the fake board: the firmware's z### pattern test,
the C64's own REU loop with the Settings page that turns the REU on, the
C64's own writes beside z###, screen noise beside a workload, the VIC's
fetches from an Ultimax cartridge, and what each makes of a stall, a crash or
a hang."""

import functools
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
from treadmill.probes.krread import KrReadProbe, ksense_mode, probe_kernal
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
    assert up.probes == ["zpattern"] and fake.letters.count("z004") == len(margin.points) + 1, \
        "a z004 a point, and one at the default after the walk"
    assert margin.after == {"clean": True, "restarted": False, "notes": []}
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


def test_vicrom_high_also_shows_the_cpu_cycle_knobs(fake_station, tmp_path: Path):
    """With its screen and sprites in ROMH, the VIC reads the cartridge while
    Phi2 is high too, through the CPU cycle's path: nS_PLAprop's upper edge
    shows in the picture, where plain vicrom stays clean."""
    fake, board, dbg = fake_station
    spec = SearchSpec(stop=260, step=20, fine=5)
    plain = Sweep(dbg, KNOBS["p"], _vicrom(fake, board, tmp_path), spec, log=lambda m: None).run()
    assert plain.first_fail is None
    fake.vic_windows["i"] = (250, 430)   # vicrom-high holds nS_VICDHold at 280; the machines are clean down to 250
    high = Sweep(dbg, KNOBS["p"], _vicrom(fake, board, tmp_path, name="vicrom-high"), spec, log=lambda m: None).run()
    assert high.probe == "vicrom-high" and high.last_clean <= fake.windows["rom"][1] < high.first_fail
    assert "/treadmill/vicrom-high.crt" in fake.files and fake.knobs == KNOB_DEFAULTS


def test_vicrom_high_holds_vicdhold_where_a_tr_plus_still_answers(fake_station, tmp_path: Path):
    """A TR+ running vicrom-high goes silent past an nS_VICDHold threshold
    (BACKLOG 59), so a sweep holds that knob lower from the probe's launch
    to its teardown, through every relaunch, and sends td only once the
    probe has stopped; the record says what was held."""
    fake, board, dbg = fake_station
    fake.faults.update(vicrom_high_silent_above=320, vic_hang_ns=10)   # a hang past the edge: relaunches
    fake.vic_windows["i"] = (250, 430)                                 # the machines are clean down to 250
    probe = _vicrom(fake, board, tmp_path, name="vicrom-high", cpu=True)
    margin = Sweep(dbg, KNOBS["p"], probe, SearchSpec(stop=260, step=20, fine=5), log=lambda m: None).run()
    assert not fake.silent and any(p.hung for p in margin.points), "relaunched, and the board still answers"
    assert margin.last_clean <= fake.windows["rom"][1] < margin.first_fail
    assert margin.to_dict()["set"] == {"nS_VICDHold": 280} and fake.knobs == KNOB_DEFAULTS
    with pytest.raises(SweepError) as e:
        Sweep(dbg, KNOBS["i"], _vicrom(fake, board, tmp_path, name="vicrom-high"), SearchSpec(stop=300))
    assert "holds nS_VICDHold at 280" in str(e.value)


def test_vicrom_cpu_counts_the_cpus_own_reads_of_romh(fake_station, tmp_path: Path):
    """The C64 counts its reads of ROMH, so the CPU's path fails as a count
    while the picture is still clean, and a VIC-cycle drive held into the
    CPU's half shows as wrong reads."""
    fake, board, dbg = fake_station
    probe = _vicrom(fake, board, tmp_path, name="vicrom-cpu", cpu=True)
    margin = Sweep(dbg, KNOBS["p"], probe, SearchSpec(stop=260, step=20, fine=5), log=lambda m: None).run()
    assert margin.last_clean <= fake.windows["rom"][1] < margin.first_fail
    assert margin.points[0].samples > 1 and margin.points[0].notes[0].startswith("cpu 0/")
    bad = next(p for p in margin.points if p.failed)
    assert bad.notes[0].startswith("cpu ") and "glyphs" not in bad.notes[0], bad.notes
    assert margin.probe_info["vicrom-cpu"]["cpu_counted"]
    probe.setup()
    try:
        fake.knobs["i"] = fake.vic_windows["i"][1] + 20
        held = probe.sample("i450")
    finally:
        fake.knobs["i"] = KNOB_DEFAULTS["i"]
        probe.teardown()
    assert held.failures > 1 and "cpu " in held.note


def _game(fake, board, tmp_path: Path, **kw) -> "GameProbe":
    from selftest.synth import calibrated, fake_frame_source
    from treadmill.probes.game import GameProbe
    crt = tmp_path / "game.crt"
    crt.write_bytes(b"C64 CARTRIDGE   " + bytes(64))
    return GameProbe(board, fake_frame_source(fake), calibrated(), str(crt), settle=0.2, learn=1.0, dwell=0.3,
                     log=lambda m: None, **kw)


def test_the_game_probe_masks_what_moves_on_its_own(fake_station, tmp_path: Path):
    """The fake's game moves one cell by itself: learned at the defaults and
    masked, it fails no point, and the VIC knobs' edges show as vicrom's do."""
    fake, board, dbg = fake_station
    probe = _game(fake, board, tmp_path)
    margin = Sweep(dbg, KNOBS["v"], probe, SearchSpec(both=True, step=20, fine=5), log=lambda m: None).run()
    down, up = margin.edges["down"], margin.edges["up"]
    assert (down.clean_to, down.first_fail, up.clean_to, up.first_fail) == (150, 145, 290, 295)
    info = margin.probe_info["game"]
    assert 0 < info["masked"] < 0.05 and info["motion"] >= 50 and info["game"] == "game.crt"
    bad = next(p for p in margin.points if p.failed)
    assert "still pixels off the reference" in bad.notes[0]
    assert fake.ultimax_since is None and fake.knobs == KNOB_DEFAULTS


def test_a_game_that_stops_moving_is_relaunched_and_a_hang_if_it_stays_still(fake_station, tmp_path: Path):
    from treadmill.sweeps import Setting
    fake, board, dbg = fake_station
    probe = _game(fake, board, tmp_path)
    sweep = Sweep(dbg, KNOBS["v"], probe, SearchSpec(stop=150), log=lambda m: None)
    sweep.prepare()
    reset, at_reset = board.reset, []
    board.reset = lambda *a, **kw: (at_reset.append(fake.knobs["v"]), reset(*a, **kw))[1]
    probe.setup()
    try:
        sweep.apply({"v": 200})
        fake.game = False                        # its cell stops: it hung at some earlier value
        got = probe.sample("tv200", Setting(sweep, {"v": 200}, bracket=False))
        assert tuple(got) == (0, 1, None, False) and fake.game, "relaunched, moving, clean"
        assert at_reset == [KNOB_DEFAULTS["v"]] and fake.knobs["v"] == 200, "relaunched at the defaults, then back"
        sweep.restore()
        probe.remote = "/treadmill/still.crt"    # a relaunch that never moves
        fake.game = False
        hung = probe.sample("tv215")
        assert hung.hung and "stopped moving" in hung.note
    finally:
        probe.teardown()


def test_the_game_probe_takes_a_file_or_a_card_path(fake_station, tmp_path: Path):
    from treadmill.probes.game import game_source
    assert game_source("sd:/Games/Lander.crt") == (None, "sd", "/Games/Lander.crt")
    assert game_source("USB:/x.crt")[1:] == ("usb", "/x.crt")
    local, drive, remote = game_source(str(tmp_path / "g.crt"))
    assert local == tmp_path / "g.crt" and (drive, remote) == ("sd", "/treadmill/game.crt")
    fake, board, dbg = fake_station
    with pytest.raises(SweepError) as e:
        Sweep(dbg, KNOBS["e"], _game(fake, board, tmp_path), SearchSpec(stop=440))
    assert "times DMA" in str(e.value)
    with pytest.raises(SweepError) as e:
        _game(fake, board, tmp_path, keys="{f1}").setup()
    assert "keyboard board" in str(e.value)


def test_a_hung_vicrom_is_relaunched(fake_station, tmp_path: Path):
    fake, board, dbg = fake_station
    fake.faults["vic_hang_ns"] = 30
    probe = _vicrom(fake, board, tmp_path)
    reset, at_reset = board.reset, []
    board.reset = lambda *a, **kw: (at_reset.append(fake.knobs["i"]), reset(*a, **kw))[1]
    margin = Sweep(dbg, KNOBS["i"], probe, SearchSpec(scan=(250, 370), step=40), log=lambda m: None).run()
    assert at_reset[0] == KNOB_DEFAULTS["i"], "relaunched with the defaults back: a knob past its edge may not boot"
    first, second = margin.points[0], margin.points[1]
    assert (first.value, first.hung) == (250, True) and "stopped" in first.notes[0]
    assert (second.value, second.hung, second.failed) == (290, False, True), "relaunched, counting, glyphs wrong"
    assert [(p.value, p.failed) for p in margin.points[2:]] == [(330, False), (370, False)]


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


@functools.lru_cache(maxsize=1)
def _video_kit():
    from selftest.synth import calibrated, harvested_bank
    calib = calibrated()
    return calib, harvested_bank(calib)


def _krread(fake, board, tmp_path: Path) -> KrReadProbe:
    """krread with a screen that reads the fake's picture, as a station's capture does."""
    from selftest.synth import fake_frame_source
    calib, bank = _video_kit()
    screen = ScreenChannel(board.link, fake_frame_source(fake), calib, bank, dma_allowed=lambda: True)
    program = tmp_path / "krread.prg"
    program.write_bytes(bytes(16))
    return KrReadProbe(board, screen, program, 28, dwell=0.3, log=lambda m: None)


def test_krread_counts_a_failing_default_as_the_sweep_s_result(fake_station, tmp_path: Path):
    """Kernal Replace not serving the probe KERNAL: krread runs, and the
    default's point fails with the reads it missed instead of the setup
    refusing."""
    fake, board, dbg = fake_station
    probe = _krread(fake, board, tmp_path)
    margin = Sweep(dbg, KNOBS["k"], probe, SearchSpec(both=True, step=20, fine=10), log=lambda m: None).run()
    assert len(margin.points) == 1 and margin.points[0].failed
    assert "without the replacement" in margin.points[0].notes[0]
    assert fake.krloop is None and fake.knobs == KNOB_DEFAULTS, "torn down, at the menu, at the defaults"


def test_krread_keeps_the_first_read_apart_from_the_verdict(fake_station, tmp_path: Path):
    """Every pass's first read with HIRAM set missing (002b's Skoe sense on
    NTSC) leaves the points clean, so the verdict's edges are found, and the
    misses are kept per value."""
    fake, board, dbg = fake_station
    fake.faults["kr_first_read_miss"] = True
    fake.special_io, fake.kernal_file = "Kernal Replace", "SD://treadmill/krprobe.bin"
    fake.knobs["x"] = 0
    probe = _krread(fake, board, tmp_path)
    margin = Sweep(dbg, KNOBS["k"], probe, SearchSpec(both=True, step=10, fine=5), log=lambda m: None).run()
    assert (margin.edges["down"].clean_to, margin.edges["up"].clean_to) == (20, 60)
    rom_first, ram_first, passes = probe.first_reads["k=35"]
    assert passes > 0 and rom_first == passes and ram_first == 0
    assert margin.probe_info["krread"]["first_reads"]["k=35"] == [rom_first, ram_first, passes]
    assert "the first read after the write" in next(n for p in margin.points for n in p.notes if "first" in n)


def test_krread_touches_no_memory_while_it_runs(fake_station, tmp_path: Path):
    """Host DMA while a program runs under Kernal Replace skips the DMA
    sense and corrupts the program (BACKLOG 40): from the launch to the
    teardown's reset, the sweep reads its counts off the screen and sends no
    DMA read or write."""
    fake, board, dbg = fake_station
    fake.special_io, fake.kernal_file = "Kernal Replace", "SD://treadmill/krprobe.bin"
    fake.knobs["x"] = 2
    dma = []

    def watched(name):
        real = getattr(board.link, name)

        def call(*a, **k):
            if fake.krloop is not None:          # krread running: from its launch to the reset
                dma.append(name)
            return real(*a, **k)
        return call

    for name in ("read_memory", "write_memory", "read_screen"):
        setattr(board.link, name, watched(name))
    probe = _krread(fake, board, tmp_path)
    margin = Sweep(dbg, KNOBS["n"], probe, SearchSpec(both=True, step=40, fine=20), log=lambda m: None).run()
    assert not margin.points[0].failed and margin.edges["up"].first_fail is not None
    assert dma == [], f"DMA while krread ran: {dma[:5]}"


class _Row:
    """A screen whose top row shows the counts given, one frame per read."""

    def __init__(self, *frames):
        self.frames = list(frames)
        self.has_video = True

    def read_video(self):
        from types import SimpleNamespace
        import numpy as np
        codes, confidence = np.full(1000, 0x20, np.uint8), np.ones(1000, np.float32)
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        if frame is not None:
            for cell, ch in enumerate(" ".join(f"{v:04x}" for v in frame)):
                codes[cell] = 0x20 if ch == " " else 0x30 + int(ch) if ch.isdigit() else 0x01 + ord(ch) - ord("a")
        return SimpleNamespace(codes=codes, confidence=confidence)


def test_krread_counts_need_two_frames_that_agree(tmp_path: Path):
    """One frame with a misread miss count is not taken as wrong reads; a
    row that never reads whole is a hang, not an error."""
    misread = _Row((0x100, 0, 0x8, 0, 0), (0x180, 0, 0, 0, 0), (0x200, 0, 0, 0, 0))
    probe = KrReadProbe(None, misread, tmp_path / "krread.prg", 28, dwell=0, log=lambda m: None)
    assert probe.counters() == (0x200, 0, 0, 0, 0)
    probe.screen = _Row(*[(0x100 + 0x80 * i, i + 1, 0, 0, 0) for i in range(10)])
    assert probe.counters() == (0x480, 8, 0, 0, 0), "miss counts that keep moving: the last frame read"
    probe.screen = _Row(None)
    got = probe.sample("n250")
    assert got.hung and "do not read whole" in got.note


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


def test_sweep_command_with_vicrom_krread_and_a_game(lab: Path, capsys):  # noqa: F811
    from treadmill.cli import EXIT_OK, EXIT_USAGE, main
    import json
    base = ["--station", "s1", "--reports", str(lab / "reports"), "--fw-defines", "Dbg_SerTimChg", "--json",
            "--dwell", "0.3", "--both", "--step", "20", "--fine", "10"]
    assert main(["sweep", "v", "--probe", "vicrom", *base]) == EXIT_OK, capsys.readouterr().err
    edges = json.loads(capsys.readouterr().out)["edges"]
    assert (edges["down"]["clean_to"], edges["up"]["clean_to"]) == (155, 285)
    assert main(["sweep", "n", "--probe", "krread", *base]) == EXIT_OK, capsys.readouterr().err
    payload = json.loads(capsys.readouterr().out)
    assert payload["probes"] == ["krread"] and payload["edges"]["up"]["clean_to"] == 330
    assert main(["sweep", "v", "--probe", "game", *base]) == EXIT_USAGE
    assert "needs --game" in json.loads(capsys.readouterr().err)["error"]
    game = lab / "game.crt"
    game.write_bytes(b"C64 CARTRIDGE   " + bytes(64))
    assert main(["sweep", "v", "--probe", "game", "--game", str(game), "--game-settle", "0.2", "--game-learn", "1",
                 *base]) == EXIT_OK, capsys.readouterr().err
    payload = json.loads(capsys.readouterr().out)
    assert payload["probes"] == ["game"] and payload["probe_info"]["game"]["masked"] > 0


def test_sweep_command_under_minimalboot(lab: Path, capsys):  # noqa: F811
    from treadmill.cli import EXIT_OK, EXIT_USAGE, main
    import json
    base = ["sweep", "p", "--station", "s1", "--reports", str(lab / "reports"), "--fw-defines", "Dbg_SerTimChg",
            "--json", "--image", "minimal", "--dwell", "0.2", "--both", "--step", "20", "--fine", "10"]
    assert main(base) == EXIT_OK, capsys.readouterr().err
    payload = json.loads(capsys.readouterr().out)
    assert payload["probes"] == ["idread-minimal"] and payload["firmware"]["image"] == "minimal"
    assert (payload["edges"]["down"]["clean_to"], payload["edges"]["up"]["clean_to"]) == (150, 210)
    assert main(base + ["--probe", "menu"]) == EXIT_USAGE
    assert "idread, read by video" in json.loads(capsys.readouterr().err)["error"]


def test_minimalboot_leaving_for_the_main_image_fails_that_value_and_the_walk_goes_on(fake_station, tmp_path: Path):
    """Below nS_MaxAdj's lower edge MinimalBoot starts the main image on its
    own: that value fails, the probe hands off to MinimalBoot again, and the
    walk bisects the edge instead of losing the board."""
    from selftest.synth import fake_frame_source
    from treadmill.probes.idread import MinimalIdReadProbe
    fake, board, _ = fake_station
    fake.faults["minimal_leaves_below"] = {"m": 960}
    board.reconnect_hook = lambda: Link(fake.host_stream)
    dbg = DebugCommands(lambda: board.link, DEFINES)       # the letters go to the image that answers
    calib, bank = _video_kit()
    screen = ScreenChannel(board.link, fake_frame_source(fake), calib, bank, dma_allowed=lambda: False)
    program = tmp_path / "idread-show.crt"
    program.write_bytes(b"C64 CARTRIDGE   " + bytes(48))
    probe = MinimalIdReadProbe(board, screen, program, dwell=0.3, log=lambda m: None)
    margin = Sweep(dbg, KNOBS["m"], probe, SearchSpec(stop=900, step=20, fine=5), standard="NTSC",
                   log=lambda m: None).run()
    edge = margin.edges["down"]
    assert (edge.clean_to, edge.first_fail) == (963, 958), margin.describe()
    assert not margin.stopped and margin.restored
    left = [p for p in margin.points if p.failed]
    assert left and all("MinimalBoot left for the main image" in p.notes[0] for p in left), margin.describe()
    assert fake.image == "main", "torn down to the main image"
