"""Soaks without hardware: 16-bit counters unwrapped across their wraps, a
hang counted and relaunched, a probe without counters sampled an interval
at a time, and idread soaked on the fake board."""

from pathlib import Path

import pytest

import treadmill.link.dbgcmds as dbgcmds
from selftest.fake_board import start_fake_board
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from treadmill.board import Board
from treadmill.link import Link
from treadmill.probes import IdReadProbe
from treadmill.soaks import soak
from treadmill.station import load_station
from treadmill.sweeps import Sample, SweepError


class Clock:
    """The soak's clock, moved only by its probe's interval."""

    def __init__(self, monkeypatch):
        self.now = 0.0
        monkeypatch.setattr("treadmill.soaks.time.monotonic", lambda: self.now)


class Counting:
    """A REU-loop-like probe whose 16-bit round counter goes up by ``step``
    an interval and wraps; ``bad`` adds bad rounds on the intervals named;
    ``hang_at`` stops it once (its rounds stand still) until it is set up again."""

    name = "counting"

    def __init__(self, clock: Clock, step: int, bad: dict[int, int] | None = None, hang_at: int | None = None):
        self.clock, self.step, self.bad, self.hang_at = clock, step, bad or {}, hang_at
        self.rounds = 0xFFF0
        self.errors = 0xFFFE
        self.reads = 0
        self.setups = self.teardowns = 0
        self.dwell = None

    def setup(self):
        self.setups += 1

    def teardown(self):
        self.teardowns += 1

    def workload(self):
        self.clock.now += self.dwell
        self.reads += 1
        if self.reads == self.hang_at:
            return
        self.rounds = (self.rounds + self.step) & 0xFFFF
        self.errors = (self.errors + self.bad.get(self.reads, 0)) & 0xFFFF

    def counters(self):
        return self.rounds, self.errors

    def delta(self, before, after):
        good, bad = (after[0] - before[0]) & 0xFFFF, (after[1] - before[1]) & 0xFFFF
        if good + bad == 0:
            return Sample(1, 1, "stopped", hung=True)
        return Sample(bad, good, None)


def test_a_soak_unwraps_16_bit_counters_and_keeps_every_count(monkeypatch):
    probe = Counting(Clock(monkeypatch), 40_000, bad={3: 5})                 # wraps every interval or two
    result = soak(probe, 50, 10, log=lambda m: None)
    assert [i.samples for i in result.intervals] == [40_000] * 5, "each interval's rounds, wrap or not"
    assert result.failures == 5 and result.samples == 200_000 and not result.clean
    assert probe.dwell == 10 and (probe.setups, probe.teardowns) == (1, 1)
    assert "5 wrong of 200000" in result.describe() and "at    30.0 s: 5 / 40000" in result.describe()


def test_a_hang_counts_and_the_soak_goes_on(monkeypatch):
    probe = Counting(Clock(monkeypatch), 100, hang_at=2)
    result = soak(probe, 40, 10, log=lambda m: None)
    assert [i.hung for i in result.intervals] == [False, True, False, False]
    assert result.hangs == 1 and result.failures == 0 and not result.clean
    assert (probe.setups, probe.teardowns) == (2, 2), "relaunched once, torn down at the end"
    assert result.to_dict()["intervals"][1]["hung"]


def test_counters_that_do_not_read_count_as_a_hang_even_right_after_a_relaunch(monkeypatch):
    """Counters read off the screen can fail to read whole: in the loop, and
    on the first read after a relaunch, that is a hang, not the soak's end."""
    clock = Clock(monkeypatch)

    class Unreadable(Counting):
        misses = {2, 3}                          # the read after interval 1, and the first after the relaunch

        def __init__(self):
            super().__init__(clock, 100)
            self.calls = 0

        def counters(self):
            self.calls += 1
            if self.calls in self.misses:
                raise SweepError("the top row does not read whole")
            return super().counters()

    probe = Unreadable()
    result = soak(probe, 40, 10, log=lambda m: None)
    assert [i.hung for i in result.intervals][:2] == [True, True] and "do not read" in result.intervals[0].note
    assert result.hangs == 2 and probe.setups == 3 and probe.teardowns == 3 and result.samples > 0


def test_a_setup_that_fails_is_torn_down(monkeypatch):
    probe = Counting(Clock(monkeypatch), 100)

    def launched_then_failed():
        probe.setups += 1
        raise SweepError("bad at the defaults")
    probe.setup = launched_then_failed
    with pytest.raises(SweepError):
        soak(probe, 30, 10, log=lambda m: None)
    assert probe.teardowns == 1, "the program the setup launched is not left running"


def test_a_probe_without_counters_is_sampled_an_interval_at_a_time(monkeypatch):
    clock = Clock(monkeypatch)

    class Picture:
        name = "picture"
        dwell = None
        labels: list[str] = []

        def setup(self):
            pass

        def teardown(self):
            pass

        def sample(self, label, setting=None):
            clock.now += self.dwell
            self.labels.append(label)
            return Sample(0, 1)

    probe = Picture()
    result = soak(probe, 30, 10, log=lambda m: None)
    assert probe.labels == ["soak 1", "soak 2", "soak 3"] and result.clean
    with pytest.raises(ValueError):
        soak(probe, 30, 0)


def test_idread_soaks_on_the_fake_board(lab, monkeypatch, tmp_path: Path):  # noqa: F811
    monkeypatch.setattr(dbgcmds, "TEXT_REPLY", (3.0, 0.05, 20.0))
    fake, stream = start_fake_board()
    board = Board.open(load_station("s1"), link_factory=lambda: Link(stream))
    crt = tmp_path / "idread.crt"
    crt.write_bytes(b"C64 CARTRIDGE   " + bytes(64))
    try:
        clean = soak(IdReadProbe(board, crt, log=lambda m: None), 1.0, 0.3, log=lambda m: None)
        assert clean.clean and len(clean.intervals) >= 3 and clean.samples > 1000
        fake.windows["io"] = (160, 210)                  # the default nS_PLAprop now misses IO's selects
        broken = soak(IdReadProbe(board, crt, log=lambda m: None), 1.0, 0.3, log=lambda m: None)
        assert broken.failures > 0 and not broken.hangs and "io2" in broken.intervals[0].note
    finally:
        board.close()


def test_regread_jobs_soaks_on_the_fake_board(lab, monkeypatch, tmp_path: Path):  # noqa: F811
    """The REU's registers read back beside the host's jobs, each interval's
    counts added; the jobs' cost shows as wrong reads (BACKLOG 21)."""
    from treadmill.probes.regread import RegReadProbe
    monkeypatch.setattr(dbgcmds, "TEXT_REPLY", (3.0, 0.05, 20.0))
    fake, stream = start_fake_board(special_io="512KB REU")
    board = Board.open(load_station("s1"), link_factory=lambda: Link(stream))
    prg = tmp_path / "regread.prg"
    prg.write_bytes(bytes([0x01, 0x08]) + bytes(16))
    try:
        clean = soak(RegReadProbe(board, prg, jobs=True, log=lambda m: None), 1.0, 0.3, log=lambda m: None)
        assert clean.clean and len(clean.intervals) >= 3 and clean.samples > 10000
        fake.faults["reg_wrong_per_job"] = 1
        costly = soak(RegReadProbe(board, prg, jobs=True, log=lambda m: None), 1.0, 0.3, log=lambda m: None)
        assert costly.failures > 0 and not costly.hangs and "host jobs" in costly.intervals[0].note
    finally:
        board.close()
