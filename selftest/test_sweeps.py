"""The sweep engine without hardware: the search on synthetic probes, a
sweep of the fake board's nS_PLAprop with both probes, and the command."""

import json
import random
from pathlib import Path

import pytest

from selftest.fake_board import KNOB_DEFAULTS, start_fake_board
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from treadmill.board import Board
from treadmill.cli import EXIT_OK, EXIT_USAGE, main
from treadmill.link import DebugCommands, Link
from treadmill.observe.channels import ScreenChannel
from treadmill.observe.golden import Golden
from treadmill.station import load_station
from treadmill.sweeps import KNOBS, IdReadProbe, MenuProbe, Point, SearchSpec, Sweep, SweepError, knob_named, search

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


def test_search_runs_downward_and_stops_at_fine():
    def low_fails(value: int) -> Point:            # a hold time that fails when too short
        return Point(value, 1 if value < 183 else 0, 1, 0.0)
    points, clean, fail = search(low_fails, 390, SearchSpec(stop=100, step=50, fine=5))
    assert fail < clean and 183 <= clean < 190 and 178 < fail < 183
    assert abs(clean - fail) <= 5


def test_a_spec_that_could_not_end_is_refused():
    for bad in ({"step": 0}, {"fine": 0}, {"repeat": 0}, {"step": -10}):
        with pytest.raises(SweepError) as e:
            SearchSpec(stop=300, **bad)
        assert "at least 1" in str(e.value) and e.value.code == EXIT_USAGE
    SearchSpec(stop=300, step=1, fine=1, repeat=1)


def test_knob_lookup():
    assert knob_named("p") is KNOBS["p"] and knob_named("tp") is KNOBS["p"] and knob_named("nS_PLAprop") is KNOBS["p"]
    assert KNOBS["k"].unit == "cycles" and KNOBS["w"].name == "nS_DMADataHold"
    with pytest.raises(SweepError):
        knob_named("q")


@pytest.fixture
def fake_station(lab):  # noqa: F811
    fake, stream = start_fake_board(debug_defines={"Dbg_SerTimChg"})
    board = Board.open(load_station("s1"), link_factory=lambda: Link(stream), fw_defines=("Dbg_SerTimChg",))
    try:
        yield fake, board
    finally:
        board.close()


def _idread_program(tmp_path: Path) -> Path:
    prg = tmp_path / "idread.prg"
    prg.write_bytes(b"\x01\x08" + b"\x00" * 64)        # the fake runs the name, not the bytes
    return prg


def test_sweep_of_the_fake_s_pla_margin_by_idread(fake_station, tmp_path: Path):
    fake, board = fake_station
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})
    probe = IdReadProbe(board, _idread_program(tmp_path), dwell=0.2, log=lambda m: None)
    sweep = Sweep(dbg, KNOBS["p"], probe, SearchSpec(stop=KNOB_DEFAULTS["p"] + 100, step=20, fine=5), log=lambda m: None)
    margin = sweep.run()
    assert margin.default == KNOB_DEFAULTS["p"] and margin.probe == "idread"
    assert margin.last_clean is not None and margin.first_fail is not None
    assert margin.last_clean <= fake.margins["p"] < margin.first_fail and margin.first_fail - margin.last_clean <= 5
    assert margin.restored and fake.knobs["p"] == KNOB_DEFAULTS["p"] and fake.letters[-1] == "td"
    assert margin.points[0].value == KNOB_DEFAULTS["p"] and not margin.points[0].failed
    bad = next(p for p in margin.points if p.failed)
    assert 0 < bad.rate <= 1 and bad.samples > 100
    assert not board.link.ping().endswith("Busy!"), "teardown left the board at the menu"
    text = margin.describe()
    assert "nS_PLAprop: clean up to" in text and "of margin" in text
    d = margin.to_dict()
    assert d["margin"] == margin.last_clean - KNOB_DEFAULTS["p"] and len(d["points"]) == len(margin.points)


def test_menu_probe_sees_the_misread_marks(fake_station, tmp_path: Path):
    fake, board = fake_station
    screen = ScreenChannel(board.link, None, None, None, dma_allowed=lambda: True)
    probe = MenuProbe(board, screen, Golden.load(ROOT_GOLDEN), tmp_path)
    probe.setup()
    assert probe.sample("p150") == (0, 1, None)
    fake.knobs["p"] = fake.margins["p"] + 40
    failures, samples, note = probe.sample("p250")
    assert (failures, samples) == (1, 1) and "differ from the golden" in note
    assert (tmp_path / "p250.screen.txt").is_file()
    fake.knobs["p"] = KNOB_DEFAULTS["p"]


def test_menu_probe_refuses_a_station_it_cannot_read(fake_station):
    fake, board = fake_station
    board.mask({"dma"})                                  # a plain TR on a rig whose capture did not open
    blind = ScreenChannel(board.link, None, None, None, dma_allowed=board.dma_allowed)
    with pytest.raises(SweepError) as e:
        MenuProbe(board, blind, Golden.load(ROOT_GOLDEN)).setup()
    assert "cannot read the screen" in str(e.value)


def test_a_value_the_board_does_not_take_is_an_error(fake_station, tmp_path: Path):
    fake, board = fake_station
    fake.debug_defines = set()                           # the build lost its define: letters are dropped
    dbg = DebugCommands(board.link, {"Dbg_SerTimChg"})   # the station still believes it has it
    sweep = Sweep(dbg, KNOBS["p"], IdReadProbe(board, _idread_program(tmp_path)), SearchSpec(stop=200))
    with pytest.raises(SweepError) as e:
        sweep.current()
    assert "no nS_PLAprop" in str(e.value)


def test_sweep_command_against_the_fake(lab: Path, capsys):  # noqa: F811
    code = main(["sweep", "p", "--station", "s1", "--reports", str(lab / "reports"), "--fw-defines", "Dbg_SerTimChg",
                 "--probe", "idread", "--to", "260", "--step", "20", "--fine", "5", "--dwell", "0.2", "--json"])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    payload = json.loads(out)
    assert payload["knob"] == "nS_PLAprop" and payload["last_clean"] <= 210 < payload["first_fail"]
    assert payload["restored"] and payload["default"] == 150
    folder = Path(payload["folder"])
    assert json.loads((folder / "sweep.json").read_text(encoding="utf-8"))["first_fail"] == payload["first_fail"]
    assert (folder / "serial.log").is_file()
    assert main(["sweep", "q", "--station", "s1", "--json"]) == EXIT_USAGE
    assert "no timing knob" in json.loads(capsys.readouterr().err)["error"]
    assert main(["sweep", "p", "--station", "s1", "--reports", str(lab / "reports"), "--json"]) == EXIT_USAGE
    assert "lacks Dbg_SerTimChg" in json.loads(capsys.readouterr().err)["error"]
    assert main(["sweep", "p", "--station", "s1", "--reports", str(lab / "reports"), "--fw-defines", "Dbg_SerTimChg",
                 "--fine", "0", "--json"]) == EXIT_USAGE
    assert "fine must be at least 1" in json.loads(capsys.readouterr().err)["error"]
