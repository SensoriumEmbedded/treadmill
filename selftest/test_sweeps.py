"""The sweep engine without hardware: the search on synthetic probes, a
sweep of the fake board's nS_PLAprop with both probes, both edges, passes
and scans, nS_DMASetup with the sums held, and the command."""

import json
import random
from pathlib import Path

import pytest

import treadmill.link.dbgcmds as dbgcmds
from selftest.fake_board import KNOB_DEFAULTS, start_fake_board
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from selftest.test_probes import _error_json
from treadmill.board import Board
from treadmill.cli import EXIT_OK, EXIT_USAGE, main
from treadmill.link import DebugCommands, Link, LinkError
from treadmill.observe.channels import ScreenChannel
from treadmill.observe.golden import Golden
from treadmill.probes import IdReadProbe, MenuProbe
from treadmill.station import load_station
from treadmill.sweeps import KNOBS, Point, Sample, SearchSpec, Sweep, SweepError, knob_named, search

REPO = Path(__file__).resolve().parents[1]
ROOT_GOLDEN = REPO / "tests" / "golden" / "menu" / "root.screen.txt"


def _probe_at(fails_above: int, flaky: float = 0.0, seed: int = 1):
    rng = random.Random(seed)
    probed = []

    def probe_at(value: int) -> Point:
        probed.append(value)
        bad = value > fails_above
        if flaky and bad:
            bad = rng.random() < flaky
        return Point(value, 1 if bad else 0, 1, 0.0)
    probe_at.probed = probed
    return probe_at


def test_search_walks_then_bisects_to_the_edge():
    probe_at = _probe_at(207)
    points, clean, fail = search(probe_at, 150, SearchSpec(stop=300, step=10, fine=1))
    assert (clean, fail) == (207, 208)
    assert probe_at.probed[:7] == [150, 160, 170, 180, 190, 200, 210]
    assert len(points) <= 7 + 4 and all(p.value == v for p, v in zip(points, probe_at.probed))


def test_search_without_an_edge_and_with_a_failing_start():
    points, clean, fail = search(_probe_at(1000), 150, SearchSpec(stop=250, step=20, fine=1))
    assert (clean, fail) == (250, None) and [p.value for p in points] == [150, 170, 190, 210, 230, 250]
    points, clean, fail = search(_probe_at(100), 150, SearchSpec(stop=250, step=20, fine=1))
    assert (clean, fail) == (None, 150) and len(points) == 1
    points, clean, fail = search(_probe_at(1000), 150, SearchSpec(stop=245, step=20, fine=1))
    assert (clean, fail) == (245, None) and [p.value for p in points][-2:] == [230, 245]   # the stop is probed
    points, clean, fail = search(_probe_at(1000), 150, SearchSpec(stop=150, step=20, fine=1))
    assert (clean, fail) == (150, None) and len(points) == 1


def test_search_from_a_start_already_probed():
    probe_at = _probe_at(207)
    points, clean, fail = search(probe_at, 150, SearchSpec(stop=300, step=10, fine=1), probe_start=False)
    assert (clean, fail) == (207, 208) and probe_at.probed[0] == 160
    points, clean, fail = search(_probe_at(1000), 150, SearchSpec(stop=150), probe_start=False)
    assert (points, clean, fail) == ([], 150, None)


def test_search_runs_downward_and_stops_at_fine():
    def low_fails(value: int) -> Point:            # a hold time that fails when too short
        return Point(value, 1 if value < 183 else 0, 1, 0.0)
    points, clean, fail = search(low_fails, 390, SearchSpec(stop=100, step=50, fine=5))
    assert fail < clean and 183 <= clean < 190 and 178 < fail < 183
    assert abs(clean - fail) <= 5


def test_a_spec_that_could_not_end_is_refused():
    for bad in ({"step": 0}, {"fine": 0}, {"repeat": 0}, {"step": -10}, {"passes": 0}):
        with pytest.raises(SweepError) as e:
            SearchSpec(stop=300, **bad)
        assert "at least 1" in str(e.value) and e.value.code == EXIT_USAGE
    SearchSpec(stop=300, step=1, fine=1, repeat=1)
    assert [SearchSpec().mode, SearchSpec(both=True).mode, SearchSpec(scan=(1, 2)).mode] == ["one", "both", "scan"]
    with pytest.raises(SweepError) as e:
        SearchSpec(scan=(300, 200))
    assert "low end comes first" in str(e.value)


def test_knob_lookup():
    assert knob_named("p") is KNOBS["p"] and knob_named("tp") is KNOBS["p"] and knob_named("nS_PLAprop") is KNOBS["p"]
    assert KNOBS["k"].unit == "cycles" and KNOBS["w"].name == "nS_DMADataHold"
    with pytest.raises(SweepError):
        knob_named("q")


@pytest.fixture
def quick_replies(monkeypatch):
    """The fake writes each reply in one piece, so a short quiet gap ends it."""
    monkeypatch.setattr(dbgcmds, "TEXT_REPLY", (3.0, 0.05, 20.0))


@pytest.fixture
def fake_station(lab, quick_replies):  # noqa: F811
    fake, stream = start_fake_board(debug_defines={"Dbg_SerTimChg"})
    board = Board.open(load_station("s1"), link_factory=lambda: Link(stream), fw_defines=("Dbg_SerTimChg",))
    try:
        yield fake, board
    finally:
        board.close()


def _idread_program(tmp_path: Path) -> Path:
    crt = tmp_path / "idread.crt"
    crt.write_bytes(b"C64 CARTRIDGE   " + bytes(64))      # the fake runs the name, not the bytes
    return crt


class Window:
    """A probe on the fake's knobs alone: a point fails outside ``window``
    (per knob letter); ``moves`` shifts the window each time the default
    comes round again, for a machine that drifts between passes; ``hangs``
    is a value from which the workload collapses."""

    def __init__(self, fake, letter: str, window: tuple[int, int], *, moves: int = 0, hangs: int | None = None,
                 name: str = "window"):
        self.fake, self.letter, self.window, self.moves, self.hangs = fake, letter, window, moves, hangs
        self.name = name
        self.seen_default = 0
        self.inside_defaults: list[dict] = []

    def setup(self) -> None:
        pass

    def sample(self, label: str, setting=None) -> Sample:
        value = self.fake.knobs[self.letter]
        if value == KNOB_DEFAULTS[self.letter]:
            self.seen_default += 1
        if setting is not None and setting.bracket:
            with setting.at_defaults():
                self.inside_defaults.append(dict(self.fake.knobs))
        shift = self.moves * (self.seen_default - 1)
        low, high = self.window[0] + shift, self.window[1] + shift
        if self.hangs is not None and value >= self.hangs:
            return Sample(1, 1, "hung", hung=True)
        return Sample(0 if low <= value <= high else 1, 1)

    def teardown(self) -> None:
        pass


def test_sweep_of_the_fake_s_pla_margin_by_idread(fake_station, tmp_path: Path):
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    probe = IdReadProbe(board, _idread_program(tmp_path), dwell=0.2, log=lambda m: None)
    sweep = Sweep(dbg, KNOBS["p"], probe, SearchSpec(stop=KNOB_DEFAULTS["p"] + 100, step=20, fine=5), log=lambda m: None)
    margin = sweep.run()
    assert margin.default == KNOB_DEFAULTS["p"] and margin.probe == "idread" and margin.standard == "NTSC"
    assert margin.last_clean is not None and margin.first_fail is not None
    io_high = fake.windows["io"][1]
    assert margin.last_clean <= io_high < margin.first_fail and margin.first_fail - margin.last_clean <= 5
    assert margin.restored and fake.knobs["p"] == KNOB_DEFAULTS["p"] and fake.letters[-1] == "td"
    assert margin.points[0].value == KNOB_DEFAULTS["p"] and not margin.points[0].failed
    bad = next(p for p in margin.points if p.failed)
    assert 0 < bad.rate <= 1 and bad.samples > 100 and bad.failed_by == ["idread"]
    assert bad.notes[0].startswith("rom 0/") and ", io2 0/" not in bad.notes[0], bad.notes   # IO goes first
    assert not board.link.ping().endswith("Busy!"), "teardown left the board at the menu"
    text = margin.describe()
    assert "nS_PLAprop: clean up to" in text and "of margin" in text
    d = margin.to_dict()
    assert d["edges"]["up"]["margin"] == margin.last_clean - KNOB_DEFAULTS["p"]
    assert len(d["points"]) == len(margin.points)
    assert d["edges"]["up"]["probes"] == ["idread"] and d["limits"] == [143, 330]


def test_sweep_down_finds_the_select_edge_and_stops_at_rwn_ready(fake_station, tmp_path: Path):
    """Lowered, nS_PLAprop samples the select lines before IO's have asserted;
    below nS_RWnReady it changes nothing, since both waits count from the
    cycle's start, so the knob table stops the walk there."""
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    probe = IdReadProbe(board, _idread_program(tmp_path), dwell=0.2, log=lambda m: None)
    io_low = fake.windows["io"][0]
    margin = Sweep(dbg, KNOBS["p"], probe, SearchSpec(stop=KNOB_DEFAULTS["r"], step=5, fine=1), log=lambda m: None).run()
    assert margin.direction == -1 and (margin.last_clean, margin.first_fail) == (io_low, io_low - 1)
    assert "nS_PLAprop: clean down to" in margin.describe() and margin.restored
    with pytest.raises(SweepError) as e:
        Sweep(dbg, KNOBS["p"], probe, SearchSpec(stop=KNOB_DEFAULTS["r"] - 30), log=lambda m: None).run()
    assert e.value.code == EXIT_USAGE and "past the sweep limits 143..330" in str(e.value)
    fake.windows["io"] = (100, 210)                                  # selects in time at any reachable value
    clean = Sweep(dbg, KNOBS["p"], probe, SearchSpec(stop=KNOB_DEFAULTS["r"], step=20, fine=1),
                  log=lambda m: None).run()
    assert clean.first_fail is None and [p.value for p in clean.points] == [150, 143]


def test_both_edges_from_one_probe_of_the_default(fake_station):
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    probe = Window(fake, "p", (146, 210))
    margin = Sweep(dbg, KNOBS["p"], probe, SearchSpec(both=True, step=10, fine=1), log=lambda m: None).run()
    values = [p.value for p in margin.points]
    assert values.count(150) == 1 and values[:3] == [150, 143, 146], "the default once, then down first"
    down, up = margin.edges["down"], margin.edges["up"]
    assert (down.clean_to, down.first_fail, up.clean_to, up.first_fail) == (146, 145, 210, 211)
    assert down.probes == ["window"] and not down.drifting and margin.restored
    text = margin.describe()
    assert "clean down to 146, fails from 145" in text and "clean up to 210, fails from 211" in text


def test_passes_alternate_and_mark_a_drifting_edge(fake_station):
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    probe = Window(fake, "p", (146, 210), moves=-10)                  # pass 2: clean 136..200
    margin = Sweep(dbg, KNOBS["p"], probe, SearchSpec(both=True, step=10, fine=1, passes=2),
                   log=lambda m: None).run()
    second = [p.value for p in margin.points if p.pass_no == 2]
    assert second[:2] == [150, 160], "pass 2 goes up first"
    up, down = margin.edges["up"], margin.edges["down"]
    assert (up.clean_to, up.first_fail) == (200, 201), "a value is clean only if clean in every pass"
    assert up.drifting and [x["first_fail"] for x in up.per_pass] == [211, 201]
    assert (down.clean_to, down.first_fail) == (146, 145) and down.drifting      # pass 2 found no edge down
    assert "drifting between passes" in margin.describe()


def test_a_scan_alternates_and_stops_past_two_collapses(fake_station):
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    probe = Window(fake, "p", (143, 270), hangs=240)
    margin = Sweep(dbg, KNOBS["p"], probe, SearchSpec(scan=(143, 270), step=15, passes=2), log=lambda m: None).run()
    first = [p.value for p in margin.points if p.pass_no == 1]
    second = [p.value for p in margin.points if p.pass_no == 2]
    assert first == [143, 158, 173, 188, 203, 218, 233, 248, 263], "stops after two collapses past the window"
    assert second == [270, 263, 248, 233, 218, 203, 188, 173, 158, 143], "descending; no stop before the window"
    up, down = margin.edges["up"], margin.edges["down"]
    assert (up.clean_to, up.first_fail, down.clean_to, down.first_fail) == (233, 248, 143, None)
    with pytest.raises(SweepError) as e:
        Sweep(dbg, KNOBS["p"], probe, SearchSpec(scan=(160, 200)), log=lambda m: None).run()
    assert "must take in the default 150" in str(e.value)


def test_dmasetup_holds_the_release_and_the_latch_and_reads_at_the_defaults(fake_station):
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    probe = Window(fake, "e", (390, 470))
    seen: list[Point] = []
    margin = Sweep(dbg, KNOBS["e"], probe, SearchSpec(both=True, step=20, fine=5), log=lambda m: None,
                   on_point=seen.append).run()
    assert margin.held == {"release": 854, "latch": 834} and margin.limits == (330, 520)
    for p in seen:
        assert p.held == {"w": 854 - p.value, "y": 834 - p.value}
    assert all(k["e"] == 430 and k["w"] == 424 and k["y"] == 404 for k in probe.inside_defaults), \
        "a DMA knob's readback runs at the defaults"
    assert (margin.edges["down"].clean_to, margin.edges["up"].clean_to) == (390, 470)
    assert fake.knobs == KNOB_DEFAULTS and margin.restored
    free = Sweep(dbg, KNOBS["e"], probe, SearchSpec(stop=440), hold=False, log=lambda m: None).run()
    assert free.held is None and free.points[-1].held == {}
    with pytest.raises(SweepError) as e:
        Sweep(dbg, KNOBS["p"], probe, SearchSpec(stop=160), hold=True)
    assert "only nS_DMASetup" in str(e.value)


def test_another_knob_set_for_the_sweep_moves_the_limits(fake_station):
    """nS_PLAprop's floor is nS_RWnReady: with nS_RWnReady set low, the walk
    reaches a select edge below the default nS_RWnReady."""
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    window = Window(fake, "p", (0, 999))
    seen: list[dict] = []

    class Watching(Window):
        def sample(self, label, setting=None):
            seen.append(dict(self.fake.knobs))
            return Sample(0 if max(self.fake.knobs["p"], self.fake.knobs["r"]) >= 130 else 1, 1)

    plain = Sweep(dbg, KNOBS["p"], window, SearchSpec(stop=KNOB_DEFAULTS["r"]), log=lambda m: None).run()
    assert plain.limits == (143, 330) and plain.to_dict()["set"] == {}
    margin = Sweep(dbg, KNOBS["p"], Watching(fake, "p", (0, 999)), SearchSpec(stop=100, step=10, fine=1),
                   fixed={"r": 100}, log=lambda m: None).run()
    assert margin.limits == (100, 330) and (margin.last_clean, margin.first_fail) == (130, 129)
    assert all(k["r"] == 100 for k in seen[:-1]) and all(p.held == {"r": 100} for p in margin.points)
    assert seen[-1]["r"] == KNOB_DEFAULTS["r"], "the check after a failed walk runs at the true defaults"
    assert margin.to_dict()["set"] == {"nS_RWnReady": 100} and "nS_RWnReady set to 100" in margin.describe()
    assert fake.knobs == KNOB_DEFAULTS and margin.restored, "the set knob goes back with the defaults"
    for fixed, spec, message in (({"r": 200}, SearchSpec(stop=160), "nS_RWnReady 200 is past its limits 0..150"),
                                 ({"p": 160}, SearchSpec(stop=160), "it cannot also be set"),
                                 ({"x": 2}, SearchSpec(stop=160), "cannot be set to 2")):
        with pytest.raises(SweepError) as e:
            Sweep(dbg, KNOBS["p"], window, spec, fixed=fixed, log=lambda m: None).run()
        assert message in str(e.value) and e.value.code == EXIT_USAGE
    with pytest.raises(SweepError) as e:
        Sweep(dbg, KNOBS["e"], window, SearchSpec(stop=440), fixed={"w": 400})
    assert "nS_DMADataHold moves with the sweep of nS_DMASetup" in str(e.value)
    screen = ScreenChannel(board.link, None, None, None, dma_allowed=lambda: True)
    with pytest.raises(SweepError) as e:
        Sweep(dbg, KNOBS["p"], MenuProbe(board, screen, Golden.load(ROOT_GOLDEN)), SearchSpec(stop=160),
              fixed={"a": 20})
    assert "nS_DMAAssert times DMA" in str(e.value)


def test_a_set_dma_knob_is_read_back_at_the_defaults(fake_station):
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    probe = Window(fake, "p", (100, 300))
    margin = Sweep(dbg, KNOBS["p"], probe, SearchSpec(stop=160, step=10), fixed={"a": 20}, log=lambda m: None).run()
    assert probe.inside_defaults and all(k["a"] == KNOB_DEFAULTS["a"] for k in probe.inside_defaults)
    assert all(p.held == {"a": 20} for p in margin.points) and fake.knobs == KNOB_DEFAULTS


def test_a_default_that_fails_after_the_walk_restarts_the_teensy(fake_station):
    """Backlog 53: past an IO1 edge the menu stayed garbled at the defaults
    until the Teensy restarted. A walk that failed is followed by one point
    at the default; failing there, the Teensy is restarted, the probes set
    up again, and the default checked once more."""
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})

    class Garbling(Window):
        broken = False
        setups = 0

        def setup(self):
            self.setups += 1

        def sample(self, label, setting=None):
            if self.fake.knobs["p"] > 200:
                self.broken = True                       # the TeensyROM's own state, until a restart
            return Sample(1, 1, "garbled") if self.broken else super().sample(label, setting)

    probe = Garbling(fake, "p", (100, 300))
    restarts = []

    def restart():
        restarts.append(dict(fake.knobs))
        probe.broken = False

    spec = SearchSpec(stop=260, step=20, fine=5)
    margin = Sweep(dbg, KNOBS["p"], probe, spec, restart=restart, log=lambda m: None).run()
    assert len(restarts) == 1 and restarts[0] == KNOB_DEFAULTS and probe.setups == 2
    assert margin.after == {"clean": True, "restarted": True, "notes": ["garbled"]}
    assert "restarted and the default ran clean" in margin.describe() and margin.to_dict()["after_walk"]["clean"]
    assert all(p.pass_no == 1 for p in margin.points), "the checks after the walk are not edge points"
    probe.broken = False
    stuck = Sweep(dbg, KNOBS["p"], probe, spec, log=lambda m: None).run()
    assert stuck.after == {"clean": False, "restarted": False, "notes": ["garbled"]}
    assert "no restart was at hand" in stuck.describe()
    probe.broken = False

    def failed_restart():
        raise LinkError("no board in the bootloader")

    lost = Sweep(dbg, KNOBS["p"], probe, spec, restart=failed_restart, log=lambda m: None).run()
    assert lost.after["clean"] is False and "the restart failed: no board" in lost.after["notes"][-1]
    assert lost.edges["up"].first_fail is not None, "the walk's record stands"
    probe.broken = False
    clean = Sweep(dbg, KNOBS["p"], Window(fake, "p", (100, 210)), spec, restart=restart, log=lambda m: None).run()
    assert clean.after == {"clean": True, "restarted": False, "notes": []} and len(restarts) == 1
    walked = Sweep(dbg, KNOBS["p"], Window(fake, "p", (100, 300)), spec, log=lambda m: None).run()
    assert not any(p.failed for p in walked.points)
    assert walked.after == {"clean": True, "restarted": False, "notes": []}, "a clean walk is checked as well"


def test_a_dma_knob_is_never_probed_by_resetting_the_c64(fake_station):
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    screen = ScreenChannel(board.link, None, None, None, dma_allowed=lambda: True)
    with pytest.raises(SweepError) as e:
        Sweep(dbg, KNOBS["w"], MenuProbe(board, screen, Golden.load(ROOT_GOLDEN)), SearchSpec(stop=440))
    assert "times DMA" in str(e.value) and e.value.code == EXIT_USAGE


def test_a_value_the_firmware_moves_mid_sample_fails_the_point(fake_station):
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})

    class Resetting(Window):
        def sample(self, label, setting=None):
            got = super().sample(label, setting)
            self.fake.knobs["p"] = KNOB_DEFAULTS["p"]           # as a standard change re-applies the defaults
            return got

    margin = Sweep(dbg, KNOBS["p"], Resetting(fake, "p", (100, 300)), SearchSpec(stop=170, step=20, fine=20),
                   log=lambda m: None).run()
    assert [p.failed for p in margin.points] == [False, True]
    assert "the board moved {'p': 150}" in margin.points[1].notes[0]


def test_several_probes_at_a_point(fake_station):
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    wide, narrow = Window(fake, "p", (100, 260), name="wide"), Window(fake, "p", (140, 200), name="narrow")
    margin = Sweep(dbg, KNOBS["p"], [wide, narrow], SearchSpec(stop=250, step=20, fine=5), log=lambda m: None).run()
    edge = margin.edges["up"]
    assert edge.probes == ["narrow"] and edge.clean_to <= 200 < edge.first_fail and margin.probe == "wide+narrow"
    bad = next(p for p in margin.points if p.failed)
    assert bad.by_probe["wide"] == [0, 1] and bad.by_probe["narrow"] == [1, 1]


def test_a_probe_whose_setup_fails_is_torn_down(fake_station):
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})

    class LaunchedThenFailed(Window):
        torn_down = 0

        def setup(self) -> None:
            raise SweepError("its program is running, but it is not looping")

        def teardown(self) -> None:
            self.torn_down += 1

    first, failing = Window(fake, "p", (100, 260), name="first"), LaunchedThenFailed(fake, "p", (0, 999))
    with pytest.raises(SweepError, match="not looping"):
        Sweep(dbg, KNOBS["p"], [first, failing], SearchSpec(stop=250, step=20), log=lambda m: None).run()
    assert failing.torn_down == 1 and fake.knobs == KNOB_DEFAULTS


def test_menu_probe_sees_the_misread_marks(fake_station, tmp_path: Path):
    fake, board = fake_station
    screen = ScreenChannel(board.link, None, None, None, dma_allowed=lambda: True)
    probe = MenuProbe(board, screen, Golden.load(ROOT_GOLDEN), tmp_path)
    probe.setup()
    assert probe.sample("p150")[:3] == (0, 1, None)
    fake.knobs["p"] = fake.windows["io"][1] + 40
    got = probe.sample("p250")
    assert (got.failures, got.samples) == (1, 1) and "differ from the golden" in got.note
    assert (tmp_path / "p250.screen.txt").is_file()
    fake.knobs["p"] = KNOB_DEFAULTS["p"]


def test_menu_probe_refuses_a_station_it_cannot_read(fake_station):
    fake, board = fake_station
    board.mask({"dma"})                                  # a plain TR on a rig whose capture did not open
    blind = ScreenChannel(board.link, None, None, None, dma_allowed=board.dma_allowed)
    with pytest.raises(SweepError) as e:
        MenuProbe(board, blind, Golden.load(ROOT_GOLDEN)).setup()
    assert "cannot read the screen" in str(e.value)


class _Counters:
    """A board whose counters at $C000 read as ``reads`` in turn, the last repeating."""

    def __init__(self, reads: list[bytes]):
        self.reads = reads
        self.link = self

    def dma_allowed(self) -> bool:
        return True

    def ensure_menu(self) -> None:
        pass

    def push_file(self, *args) -> None:
        pass

    def launch(self, *args) -> None:
        pass

    def read_memory(self, address: int, length: int) -> bytes:
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]


def test_idread_setup_wants_the_loop_running_not_just_the_counters_zeroed(tmp_path: Path, monkeypatch):
    import treadmill.probes.idread as idread
    monkeypatch.setattr(idread, "IDREAD_START_S", 2.5)
    stale, zeroed = bytes([0x20] * 8), bytes(8)
    dead = IdReadProbe(_Counters([stale, zeroed]), _idread_program(tmp_path), log=lambda m: None)
    with pytest.raises(SweepError) as e:
        dead.setup()                                       # zeroed, then nothing: the cartridge died
    assert "not running" in str(e.value)
    running = [stale, zeroed] + [bytes([n, 0, 0, 0, 0, 0, 0, 0]) for n in range(1, 10)]
    IdReadProbe(_Counters(running), _idread_program(tmp_path), log=lambda m: None).setup()


def test_a_value_the_board_does_not_take_is_an_error(fake_station, tmp_path: Path):
    fake, board = fake_station
    fake.debug_defines = set()                           # the build lost its define: letters are dropped
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})   # the station still believes it has it
    sweep = Sweep(dbg, KNOBS["p"], IdReadProbe(board, _idread_program(tmp_path)), SearchSpec(stop=200))
    with pytest.raises(SweepError) as e:
        sweep.current()
    assert "no nS_PLAprop" in str(e.value)
    with pytest.raises(SweepError) as e:
        sweep.run()
    assert "td not confirmed" in str(e.value)


def test_sweep_command_against_the_fake(lab: Path, capsys):  # noqa: F811
    code = main(["sweep", "p", "--station", "s1", "--reports", str(lab / "reports"), "--fw-defines", "Dbg_SerTimChg",
                 "--probe", "idread", "--to", "260", "--step", "20", "--fine", "5", "--dwell", "0.2", "--json",
                 "--assume-warm"])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    payload = json.loads(out)
    up = payload["edges"]["up"]
    assert payload["knob"]["name"] == "nS_PLAprop" and up["clean_to"] <= 210 < up["first_fail"]
    assert payload["restored"] and payload["default"] == 150 and payload["schema"] == 1
    assert payload["station"] == "s1" and payload["firmware"]["image"] == "main"
    folder = Path(payload["folder"])
    record = json.loads((folder / "margins.json").read_text(encoding="utf-8"))
    assert record["edges"]["up"]["first_fail"] == up["first_fail"]
    assert (folder / "serial.log").is_file()
    assert main(["sweep", "q", "--station", "s1", "--json"]) == EXIT_USAGE
    assert "no timing knob" in json.loads(capsys.readouterr().err)["error"]
    assert main(["sweep", "x", "--station", "s1", "--json"]) == EXIT_USAGE
    assert "a mode" in json.loads(capsys.readouterr().err)["error"]
    assert main(["sweep", "p", "--station", "s1", "--reports", str(lab / "reports"), "--json",
                 "--assume-warm"]) == EXIT_USAGE
    assert "lacks Dbg_SerTimChg" in _error_json(capsys.readouterr().err)["error"]
    assert main(["sweep", "p", "--station", "s1", "--reports", str(lab / "reports"), "--fw-defines", "Dbg_SerTimChg",
                 "--fine", "0", "--json"]) == EXIT_USAGE
    assert "fine must be at least 1" in json.loads(capsys.readouterr().err)["error"]
    assert main(["sweep", "p", "--station", "s1", "--reports", str(lab / "reports"), "--fw-defines", "Dbg_SerTimChg",
                 "--to", "400", "--json", "--assume-warm"]) == EXIT_USAGE
    assert "past the sweep limits" in _error_json(capsys.readouterr().err)["error"]
    assert main(["sweep", "p", "--station", "s1", "--both", "--to", "200", "--json"]) == EXIT_USAGE
    assert "one of --to, --both and --scan" in json.loads(capsys.readouterr().err)["error"]
    assert main(["sweep", "p", "--station", "s1", "--set", "r", "--json"]) == EXIT_USAGE
    assert "give KNOB=VALUE" in json.loads(capsys.readouterr().err)["error"]
    assert main(["sweep", "p", "--station", "s1", "--set", "r=60", "--set", "tr=70", "--json"]) == EXIT_USAGE
    assert "nS_RWnReady twice" in json.loads(capsys.readouterr().err)["error"]
    assert main(["sweep", "p", "--station", "s1", "--set", "x=2", "--json"]) == EXIT_USAGE
    assert "KSenseMode is a mode" in json.loads(capsys.readouterr().err)["error"], "the reason, not the syntax"
    assert main(["sweep", "p", "--station", "s1", "--reports", str(lab / "reports"), "--fw-defines", "Dbg_SerTimChg",
                 "--probe", "idread", "--to", "130", "--set", "nS_RWnReady=120", "--step", "20", "--fine", "5",
                 "--dwell", "0.2", "--json", "--assume-warm"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["set"] == {"nS_RWnReady": 120} and payload["limits"] == [120, 330]
    assert payload["edges"]["down"]["first_fail"] is not None
    # The stored records, read back.
    assert main(["margins", "table", "--reports", str(lab / "reports")]) == EXIT_OK
    out = capsys.readouterr().out
    assert "nS_PLAprop" in out and "s1" in out
    assert main(["margins", "suggest", "--station", "s1", "--reports", str(lab / "reports")]) == EXIT_OK
    assert "[margins]\nnS_PLAprop = { up = " in capsys.readouterr().out


def test_sweep_command_with_the_menu_probe_both_ways(lab: Path, capsys):  # noqa: F811
    code = main(["sweep", "p", "--station", "s1", "--reports", str(lab / "reports"), "--fw-defines", "Dbg_SerTimChg",
                 "--probe", "menu", "--both", "--step", "20", "--fine", "5", "--json", "--assume-warm"])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    payload = json.loads(out)
    up, down = payload["edges"]["up"], payload["edges"]["down"]
    assert payload["probes"] == ["menu"] and up["clean_to"] <= 210 < up["first_fail"] and payload["restored"]
    assert down["first_fail"] is not None and down["clean_to"] >= 146


def test_a_walk_that_stops_keeps_its_points_and_marks_the_unfinished_side_open(fake_station):
    """A probe that cannot get its program back raises: the walk ends there,
    and the record keeps what it found, the side it was on marked open
    (backlog 43). A probe that raises before any point leaves no record."""
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})

    class Breaks(Window):
        def sample(self, label, setting=None):
            if self.fake.knobs["p"] >= self.breaks_at:
                raise LinkError("the menu was not ready after 60 s (last: booting)")
            return super().sample(label, setting)

    probe = Breaks(fake, "p", (100, 210))
    probe.breaks_at = 230
    margin = Sweep(dbg, KNOBS["p"], probe, SearchSpec(both=True, step=20, fine=5), log=lambda m: None).run()
    down, up = margin.edges["down"], margin.edges["up"]
    assert "menu was not ready" in margin.stopped and margin.restored and fake.knobs["p"] == KNOB_DEFAULTS["p"]
    assert (down.clean_to, down.first_fail, down.open) == (143, None, False), "clean through its stop: finished"
    assert (up.clean_to, up.first_fail, up.open) == (210, None, True)
    assert [p.value for p in margin.points] == [150, 143, 170, 190, 210]
    record = margin.to_dict()
    assert record["stopped"] == margin.stopped and record["edges"]["up"]["open"]
    assert "THE WALK STOPPED EARLY" in margin.describe() and "up not finished: clean to 210" in margin.describe()
    from treadmill import margins
    assert margins.rows([record])[0]["open"] == "up"
    probe.breaks_at = 0
    with pytest.raises(LinkError):
        Sweep(dbg, KNOBS["p"], probe, SearchSpec(both=True, step=20, fine=5), log=lambda m: None).run()
    assert fake.knobs["p"] == KNOB_DEFAULTS["p"]


def test_a_stopped_idread_is_relaunched_at_the_defaults(tmp_path: Path):
    """A CPU knob past its edge can keep the menu from booting, so idread's
    relaunch after a hang goes through the defaults whatever the knob times
    (backlog 43: it relaunched at the failing nS_PLAprop and hung again)."""
    from contextlib import contextmanager
    seen = []

    class Handle:
        bracket = False

        @contextmanager
        def at_defaults(self, always=False):
            seen.append(f"defaults={always}")
            yield

    running = [bytes([n, 0, 0, 0, 0, 0, 0, 0]) for n in range(1, 4)]
    probe = IdReadProbe(_Counters(running), _idread_program(tmp_path), dwell=0.0, log=lambda m: None)
    probe._stalled = True
    probe.setup = lambda: seen.append("setup")
    got = probe.sample("tp150", Handle())
    assert seen[:2] == ["defaults=True", "setup"] and not got.hung


def test_a_relaunch_whose_menu_is_late_goes_on_to_the_station_s_recovery():
    """An NTSC C128's menu was still booting 60 s after the relaunch's
    reset: that is left to the probe's setup and its recovery ladder, not
    raised (backlog 43)."""
    from treadmill.sweeps import relaunch_reset
    calls, logs = [], []

    class Late:
        def reset(self):
            calls.append("reset")

        def wait_for_menu(self):
            raise LinkError("the menu was not ready after 60 s (last: booting)")

    relaunch_reset(Late(), "reuloop", logs.append)
    assert calls == ["reset"] and "recovery is next" in logs[0]


def test_a_side_clean_to_its_limit_has_no_margin(fake_station):
    """nS_RWnReady's up side stops at nS_PLAprop, 7 ns away, and nS_VICDHold's
    at its default: neither found an edge, and the record says the limit
    instead of a margin. A side whose limit is the default is recorded too."""
    from treadmill import margins
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    rw = Sweep(dbg, KNOBS["r"], Window(fake, "r", (100, 300)), SearchSpec(both=True, step=20, fine=5),
               log=lambda m: None).run()
    up, down = rw.edges["up"].to_dict(rw.default), rw.edges["down"].to_dict(rw.default)
    assert (up["clean_to"], up["first_fail"], up["limit"], up["margin"]) == (150, None, 150, None)
    assert down["limit"] is None and down["margin"] == rw.default - down["clean_to"]
    assert "nS_RWnReady: up ends at its limit 150, clean to there: no edge found, so no margin" in rw.describe()
    hold = Sweep(dbg, KNOBS["i"], Window(fake, "i", (300, 430)), SearchSpec(both=True, step=20, fine=5),
                 log=lambda m: None).run()
    assert hold.edges["up"].limit == 370 and hold.edges["up"].clean_to == 370
    assert "(the limit is the start: not walked)" in hold.describe()
    records = [margins.build(m.to_dict(), {"station": "s1", "machine": {"id": "14"}}) for m in (rw, hold)]
    assert [r["limit"] for r in margins.rows(records)] == ["up=150", "up=370"]
    old = {**records[0], "edges": {"up": {k: v for k, v in up.items() if k != "limit"}}}
    assert margins.rows([old])[0]["limit"] == "up=150", "an older record's limit is read from its stop"
    assert margins.suggest(records, "s1")["nS_VICDHold"].up is None, "a side not walked gives no floor"


class Garbling(Window):
    """The menu after a read past an IO1 edge (backlog 53): a value in
    ``garbles`` leaves every later sample failed until the Teensy restarts."""
    leaves_state = True
    broken = False

    def __init__(self, fake, letter, window, garbles):
        super().__init__(fake, letter, window)
        self.garbles = garbles

    def sample(self, label, setting=None):
        if self.garbles(self.fake.knobs[self.letter]):
            self.broken = True
        return Sample(1, 1, "garbled") if self.broken else super().sample(label, setting)

    def restart(self):
        self.broken = False


def test_a_probe_that_leaves_state_has_the_default_checked_after_a_failed_point(fake_station):
    """Backlog 53 again: the menu acts on what it reads over IO1, so a read
    past its edge can leave the TeensyROM's state bad, and every value
    probed after it would fail. Such a probe has the default checked, and
    the Teensy restarted, before each point that follows a failure, so the
    edge stands where it is; the record keeps the checks."""
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    probe = Garbling(fake, "p", (100, 300), lambda v: v > 200)
    margin = Sweep(dbg, KNOBS["p"], probe, SearchSpec(stop=260, step=20, fine=5), restart=probe.restart,
                   log=lambda m: None).run()
    assert (margin.last_clean, margin.first_fail) == (200, 205), "bisected on a board the restart put right"
    assert margin.checks == [{"before": 200, "after": 210, "clean": True, "restarted": True, "notes": ["garbled"]}]
    assert margin.to_dict()["default_checks"] == margin.checks
    assert ("before probing 200 after 210 failed, the default failed (garbled): the Teensy was restarted"
            in margin.describe())
    plain = Window(fake, "p", (100, 200))
    quiet = Sweep(dbg, KNOBS["p"], plain, SearchSpec(stop=260, step=20, fine=5), log=lambda m: None).run()
    assert quiet.checks == [] and (quiet.last_clean, quiet.first_fail) == (200, 205), "no check without the flag"


def test_every_point_after_a_garbling_failure_is_checked_both_ways(fake_station):
    """A bisection point that fails garbles the menu again, and the other
    side's walk starts right after the last one: each is checked, so both
    edges stand where the window puts them. A point after a clean one is
    not checked."""
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    probe = Garbling(fake, "s", (100, 400), lambda v: v < 100)
    margin = Sweep(dbg, KNOBS["s"], probe, SearchSpec(both=True, step=40, fine=5), restart=probe.restart,
                   log=lambda m: None).run()
    down, up = margin.edges["down"], margin.edges["up"]
    assert (down.clean_to, down.first_fail) == (101, 96)
    assert (up.clean_to, up.first_fail) == (396, 401), "the up side ran on a board put right after 96"
    assert [(c["before"], c["after"], c["restarted"]) for c in margin.checks] == [
        (81, 61, True), (91, 81, True), (96, 91, True), (261, 96, True), (401, 421, False), (391, 401, False)]
    assert all(c["clean"] for c in margin.checks)


def test_a_restart_that_fails_partway_stops_the_walk(fake_station):
    """A check whose restart fails leaves the probes torn down: the walk
    stops there with its points, rather than sampling probes not set up."""
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    probe = Garbling(fake, "p", (100, 300), lambda v: v > 200)

    def restart():
        raise SweepError("the Teensy did not come back")

    margin = Sweep(dbg, KNOBS["p"], probe, SearchSpec(stop=260, step=20, fine=5), restart=restart,
                   log=lambda m: None).run()
    assert margin.stopped and "not set up again after the restart before 200" in margin.stopped
    assert margin.edges["up"].open and [p.value for p in margin.points] == [150, 170, 190, 210]


def test_a_pass_that_failed_at_the_default_is_found(fake_station):
    """A later pass of a drifting machine can fail at the default while the
    check after the walk runs clean: margin_check fails the test on it."""
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})

    class SecondTime(Window):
        def sample(self, label, setting=None):
            got = super().sample(label, setting)
            at_default = self.fake.knobs[self.letter] == KNOB_DEFAULTS[self.letter]
            return Sample(1, 1, "flaked") if at_default and self.seen_default == 2 else got

    margin = Sweep(dbg, KNOBS["p"], SecondTime(fake, "p", (100, 200)), SearchSpec(both=True, step=20, fine=5,
                                                                                    passes=2),
                   log=lambda m: None).run()
    assert margin.after["clean"], "the check after the walk is the third time at the default"
    assert [(p.pass_no, p.value, p.notes) for p in margin.failed_at_default] == [(2, 150, ["flaked"])]
