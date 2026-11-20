"""Margin records without hardware: floors per direction, the shrink check
against an earlier build, the floors a station's runs suggest, the table and
the CSV."""

import json
from pathlib import Path

from treadmill import margins
from treadmill.margins import Floor


def _record(station="s1", knob="nS_PLAprop", commit="aaa", when="2026-11-19T10:00:00+00:00", down=(140, 139),
            up=(200, 205), probes=("idread",), step=10, fine=5, default=150, drifting=False) -> dict:
    def edge(way, ends):
        if ends is None:
            return None
        return {"way": way, "clean_to": ends[0], "first_fail": ends[1], "drifting": drifting}
    return {"schema": 1, "when": when, "station": station, "board": "tr-plus",
            "machine": {"id": "C64-14", "model": "C64", "standard": "NTSC", "c128": False},
            "firmware": {"commit": commit, "dirty": False, "version": "TeensyROM+ v0.9", "image": "main"},
            "knob": {"name": knob, "letter": "p", "unit": "ns"}, "mode": "both", "passes": 1,
            "probes": list(probes), "default": default, "spec": {"step": step, "fine": fine},
            "edges": {"down": edge("down", down), "up": edge("up", up)}}


def test_floor_checks_each_way():
    r = _record()
    assert margins.floor_problems(r, Floor(up=200, down=140)) == []
    problems = margins.floor_problems(r, Floor(up=210, down=135))
    assert len(problems) == 2 and "floor is 135" in problems[0] and "up: clean only to 200" in problems[1]
    assert margins.floor_problems(r, None) == [] and margins.floor_problems(r, Floor(up=180)) == []


def test_a_shrink_against_another_build_is_a_warning():
    old = _record(commit="old", when="2026-11-18T10:00:00+00:00", up=(230, 235), down=(140, 139))
    same_build = _record(commit="new", when="2026-11-18T11:00:00+00:00", up=(260, 265))
    other_probe = _record(commit="old2", when="2026-11-18T12:00:00+00:00", up=(260, 265), probes=("menu",))
    now = _record(commit="new", up=(205, 210), down=(141, 140))
    warnings = margins.shrinks(now, [old, same_build, other_probe])
    assert warnings == [f"nS_PLAprop up: clean to 205, 25 ns inside 230 on old ({old['when']})"]
    assert margins.shrinks(_record(commit="new", up=(225, 230)), [old]) == [], "5 ns is inside the 10 ns grain"
    assert margins.shrinks(now, []) == []


def test_suggest_takes_a_step_inside_the_worst_edge_and_never_past_the_default():
    rs = [_record(up=(230, 235), down=(140, 139)), _record(up=(210, 215), down=(135, 134)),
          _record(knob="nS_DataHold", up=(155, 160), down=None, default=150), _record(station="s2", up=(100, 101))]
    floors = margins.suggest(rs, "s1")
    assert floors == {"nS_DataHold": Floor(up=150, down=None), "nS_PLAprop": Floor(up=200, down=150)}
    toml = margins.floors_toml(floors)
    assert toml == "[margins]\nnS_DataHold = { up = 150 }\nnS_PLAprop = { up = 200, down = 150 }\n"


def test_records_round_trip_and_tabulate(tmp_path: Path):
    margins.write(tmp_path / "run1" / "t1", _record(when="2026-11-19T10:00:00+00:00"))
    margins.write(tmp_path / "run2" / "t1", _record(commit="bbb", when="2026-11-18T10:00:00+00:00", drifting=True))
    (tmp_path / "run3").mkdir()
    (tmp_path / "run3" / "margins.json").write_text(json.dumps({"schema": 99}), encoding="utf-8")
    (tmp_path / "run4").mkdir()
    (tmp_path / "run4" / "margins.json").write_text("not json", encoding="utf-8")
    loaded = margins.load(tmp_path)
    assert [margins.build_id(r) for r in loaded] == ["bbb", "aaa"], "oldest first; other schemas and junk skipped"
    text = margins.table(loaded)
    assert text.splitlines()[0].split()[:3] == ["station", "machine", "standard"]
    assert "drifting" in text and "down,up" in text
    lines = margins.to_csv(loaded).splitlines()
    assert lines[0].startswith("when,station") and len(lines) == 3
    assert margins.table([]) == "no margin records"
