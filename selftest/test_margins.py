"""Margin records without hardware: floors per direction, the shrink check
against an earlier build, the floors a station's runs suggest, the table and
the CSV, PR #99's runs imported, the map and its Mermaid export."""

import json
from pathlib import Path

import pytest

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
          _record(knob="nS_DataHold", up=(155, 160), down=None, default=150), _record(station="s2", up=(100, 101)),
          _record(up=(160, 165), probes=("idread-usbload",))]                # under load: not a floor's
    floors = margins.suggest(rs, "s1")
    assert floors == {"nS_DataHold": Floor(up=150, down=None), "nS_PLAprop": Floor(up=200, down=150)}
    toml = margins.floors_toml(floors)
    assert toml == "[margins]\nnS_DataHold = { up = 150 }\nnS_PLAprop = { up = 200, down = 150 }\n"


def test_a_record_with_another_knob_set_stands_apart():
    """A sweep of nS_PLAprop with nS_RWnReady set low is no floor's and is
    compared only with records that set the same."""
    from treadmill.report import margin_map
    plain = _record(commit="old", when="2026-11-18T10:00:00+00:00", down=(140, 139))
    low_r = {**_record(commit="new", down=(110, 109), up=(160, 165)), "set": {"nS_RWnReady": 60}}
    assert margins.shrinks(low_r, [plain]) == [] and margins.shrinks(plain, [low_r]) == []
    assert margins.suggest([plain, low_r], "s1") == {"nS_PLAprop": Floor(up=190, down=150)}
    assert [row["set"] for row in margins.rows([plain, low_r])] == ["", "nS_RWnReady=60"]
    assert margin_map.label(low_r).endswith("idread, nS_RWnReady 60, new")
    assert len(margin_map._latest([plain, low_r])["C64 C64-14 NTSC, tr-plus"]) == 2


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


PR99_TEXT = """defaults {'e': 430, 'w': 424, 'y': 404} sums [854, 834] baseline noise {'pairs': 10, 'median': 0, 'max': 5}
z   {'e': 380, 'w': 474, 'y': 454, 'bad': 0, 'bytes': 1000, 'prefill_bad': 0, 'noise': {'pairs': 9, 'median': 9, 'max': 300000}}
z   {'e': 400, 'w': 454, 'y': 434, 'bad': 0, 'bytes': 1000, 'prefill_bad': 0, 'noise': {'pairs': 9, 'median': 9, 'max': 3000}}
z   {'e': 430, 'w': 424, 'y': 404, 'bad': 0, 'bytes': 1000, 'prefill_bad': 0, 'noise': {'pairs': 9, 'median': 9, 'max': 3500}}
z   {'e': 460, 'w': 394, 'y': 374, 'bad': 0, 'bytes': 1000, 'prefill_bad': 0, 'noise': {'pairs': 9, 'median': 9, 'max': 3000}}
z   {'e': 480, 'w': 374, 'y': 354, 'bad': 12, 'bytes': 1000, 'prefill_bad': 3, 'noise': {'pairs': 9, 'median': 9, 'max': 3200}}
reu {'e': 400, 'rounds': 140, 'bad_pages': 0, 'noise': {'pairs': 9, 'median': 0, 'max': 5}}
reu {'e': 430, 'rounds': 140, 'bad_pages': 0, 'noise': {'pairs': 9, 'median': 0, 'max': 5}}
"""


def _pr99_run(tmp_path: Path) -> Path:
    run = tmp_path / "20261112-134944"
    (run / "exec-2026-11-12-dmasetup-sweep").mkdir(parents=True)
    (run / "exec-2026-11-12-dmasetup-sweep" / "dmasetup-sweep.txt").write_text(PR99_TEXT, encoding="utf-8")
    meta = {"started": "2026-11-12T13:49:44",
            "station": {"id": "st", "machine": "002b", "model": "C64", "tr_kind": "tr-plus"},
            "board": {"image": "main", "version": "TeensyROM+ v0.9-dma.tim*", "tag": "dma.tim", "dirty": True,
                      "machine_line": "C64 NTSC 60"},
            "firmware": {"commit": "44960c417a0494f419efa5100ae13f837a725a0a", "dirty": False,
                         "defines": ["Dbg_SerTimChg", "Dbg_SerDMA"]}}
    (run / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    return run


def test_a_pr99_run_imports_with_its_noise_judged_as_the_probe_judges_it(tmp_path: Path):
    z, reu = margins.import_pr99(_pr99_run(tmp_path))
    assert z["probes"] == ["zpattern", "noise"] and reu["probes"] == ["reuloop", "noise"]
    assert (z["edges"]["down"]["clean_to"], z["edges"]["down"]["first_fail"]) == (400, 380)
    assert z["edges"]["down"]["probes"] == ["noise"], "the picture breaks first; the data stayed clean at 380"
    assert (z["edges"]["up"]["clean_to"], z["edges"]["up"]["first_fail"]) == (460, 480)
    assert z["held"] == {"release": 854, "latch": 834} and z["machine"]["standard"] == "NTSC"
    assert z["probe_info"]["noise"]["baseline"] == 3500 and z["points"][-1]["by_probe"]["zpattern"] == [15, 2000]
    assert margins.build_id(z) == "44960c417a0494f419efa5100ae13f837a725a0a*", "a define-patched build is dirty"
    with pytest.raises(ValueError):
        margins.import_pr99(tmp_path)


def test_the_map_and_the_mermaid_export(tmp_path: Path):
    from treadmill.report import margin_map
    z, reu = margins.import_pr99(_pr99_run(tmp_path))
    grid = {**z, "mode": "grid", "edges": {}, "points": [
        {"pass": 1, "value": e, "failures": int(w > 450), "samples": 1, "hung": False, "held": {"w": w, "y": 834 - e},
         "by_probe": {}, "notes": []} for e in (400, 420) for w in (430, 460)]}
    out = margin_map.render([z, reu, grid], tmp_path / "map.html", {"nS_DMASetup": [414]})
    page = out.read_text(encoding="utf-8")
    assert "nS_DMASetup (ns)" in page and "C64 002b NTSC, tr-plus, zpattern+noise, 44960c41*" in page
    assert "under review: 414" in page and "nS_DMASetup grid" in page and "400 x 460: 1/1 failed" in page
    text = margin_map.mermaid([z, reu], {"nS_DMASetup": [414]})
    assert "dateFormat X" in text and "zpattern+noise 44960c41* 400-460 :400, 460" in text
    assert "under review 414 :milestone, 414, 0s" in text and "section C64 002b NTSC" in text
    assert margin_map.render([], tmp_path / "none.html").read_text(encoding="utf-8").count("No margin records") == 1


def test_the_static_svgs_draw_alone_and_name_no_station_or_registry_id(tmp_path: Path, capsys):
    import re
    from treadmill.cli import EXIT_OK, EXIT_USAGE, main
    from treadmill.report import margin_map

    def rec(commit, default, down, up, machine="C64-14", model="C64", standard="NTSC", station="s1"):
        r = _record(station=station, commit=commit, default=default, down=down, up=up)
        r["machine"] = {"id": machine, "model": model, "standard": standard, "c128": False}
        return r

    rs = [rec("88f5c805aa", 135, (60, None), (200, 205)), rec("d024038fbb", 143, (60, None), (210, 215)),
          rec("88f5c805aa", 135, (60, None), (190, 195), machine="C64-07", station="s2"),
          rec("88f5c805aa", 135, (60, None), (230, 235), machine="C64C-24", model="C64C", standard="PAL",
              station="s3"),
          {**rec("88f5c805aa", 220, (100, None), (460, 465)), "knob": {"name": "nS_DataSetup", "unit": "ns"}}]
    paths = margin_map.export_svgs(rs, tmp_path / "images", {"nS_PLAprop": [148]},
                                   builds={"88f5c805": "main", "d024038f": "fine wait"})
    assert [p.name for p in paths] == ["margin-nS_DataSetup.svg", "margin-nS_PLAprop.svg"]
    svg = paths[1].read_text(encoding="utf-8")
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"') and "<title>" not in svg and "var(--" not in svg
    labels = re.findall(r'<text x="0" y="\d+" fill="#222" font-size="12">([^<]*)</text>', svg)
    assert labels == ["C64 NTSC (1), main", "C64 NTSC (1), fine wait", "C64 NTSC (2), main", "C64C PAL, main"]
    assert not any(s in svg for s in ("C64-14", "C64-07", "s1", "s2", "tr-plus")), "no registry id or station"
    assert 'stroke="#3366cc" stroke-dasharray="4 3"' in svg and "dashed: 148" in svg
    assert margin_map.public_labels(rs[:1]) == ["C64 NTSC"], "one row: model and standard alone"
    reports = tmp_path / "reports"
    for i, r in enumerate(rs):
        margins.write(reports / f"run{i}" / "t", r)
    assert main(["margins", "svg", "--reports", str(reports), "--json"]) == EXIT_USAGE
    assert "needs --out" in capsys.readouterr().err
    assert main(["margins", "svg", "--reports", str(reports), "--out", str(tmp_path / "out"), "--knob", "p",
                 "--build", "88f5c805=main", "--mark", "nS_PLAprop=148", "--json"]) == EXIT_OK
    assert [Path(p).name for p in json.loads(capsys.readouterr().out)["svgs"]] == ["margin-nS_PLAprop.svg"]


def test_the_map_opens_with_an_overview_of_each_machine(tmp_path: Path):
    from treadmill.report import margin_map

    def rec(knob, group, default, down, up, when, probes=("idread",), unit="ns", image="main"):
        r = _record(knob=knob, default=default, down=down, up=up, when=when, probes=probes)
        r["knob"].update(group=group, unit=unit)
        r["firmware"]["image"] = image
        r["edges"] = {w: e for w, e in r["edges"].items() if e}
        return r

    older = rec("nS_PLAprop", "cpu", 150, (135, None), (200, 205), "2026-11-19T09:00:00+00:00")
    newer = rec("nS_PLAprop", "cpu", 150, (135, None), (235, 240), "2026-11-19T10:00:00+00:00")
    loaded = rec("nS_MaxAdj", "cpu", 993, (883, 878), (1200, None), "2026-11-19T10:00:00+00:00",
                 probes=("idread-usbload",))
    plain = rec("nS_MaxAdj", "cpu", 993, (888, 883), (1200, None), "2026-11-19T10:00:00+00:00")
    down_only = rec("nS_VICDHold", "vic", 365, (250, None), None, "2026-11-19T10:00:00+00:00", probes=("vicrom",))
    cycles = rec("Cyc_KernProp", "kernal", 35, (10, 9), (99, None), "2026-11-19T10:00:00+00:00",
                 probes=("krread",), unit="cycles")
    broken = rec("nS_DataHold", "cpu", 390, (None, 390), (None, 390), "2026-11-19T10:00:00+00:00",
                 image="minimal")
    page = margin_map.render([older, newer, loaded, plain, down_only, cycles, broken],
                             tmp_path / "map.html").read_text(encoding="utf-8")
    overview = page[page.index("<h2>Overview: C64 C64-14 NTSC, tr-plus</h2>"):page.index("<h2>In the cycle")]
    assert "235 ✕ 240" not in overview and "<tspan>235</tspan>" in overview and "✕ 240" in overview
    assert "✕ 205" not in overview and "fails at 205" not in overview, "the newer record only"
    assert overview.count('class="fail"') == 4 and overview.count('class="limit"') == 5
    assert overview.index("nS_MaxAdj</text>") < overview.index("nS_MaxAdj · USB load"), "under load after"
    assert "35 cycles" in overview and "9 ✕ </tspan><tspan>10" in overview
    assert "CPU cycle, MinimalBoot" in overview and "fails at the default" in overview
    assert "VIC cycle" in overview and "Kernal Replace HIRAM sense" in overview and "DMA</text>" not in overview
    cycle = page[page.index("<h2>In the cycle: C64 C64-14 NTSC, tr-plus, 978 ns</h2>"):page.index("<details>")]
    assert "idread-usbload" not in cycle and "fails at the default" not in cycle, "main image, no USB load"
    # a section per routine; inside one, time order from the rise (nS_PLAprop 150 before nS_MaxAdj's 993)
    order = ["CPU cycle<tspan", ">nS_PLAprop<tspan", ">nS_MaxAdj<tspan", "Kernal Replace, Skoe sense<tspan",
             ">Cyc_KernProp<tspan", "VIC cycle<tspan", ">nS_VICDHold<tspan"]
    assert [cycle.index(t) for t in order] == sorted(cycle.index(t) for t in order)
    assert "DMA<tspan" not in cycle, "a routine with no records has no section"
    assert "from this interrupt" in cycle and "Phi2 fall</text>" in cycle and "next rise</text>" in cycle
    assert page.index("<details><summary>Every record, a chart per knob</summary>") < page.index("<h2>nS_PLAprop (ns)")


def test_margins_import_and_map_commands(tmp_path: Path, capsys):
    from treadmill.cli import EXIT_OK, EXIT_USAGE, main
    run = _pr99_run(tmp_path)
    assert main(["margins", "import", str(run), "--json"]) == EXIT_OK
    written = json.loads(capsys.readouterr().out)["written"]
    assert [Path(w["path"]).parent.name for w in written] == ["import-zpattern", "import-reuloop"]
    assert main(["margins", "map", "--reports", str(tmp_path), "--mermaid"]) == EXIT_OK
    assert "400-460" in capsys.readouterr().out
    assert main(["margins", "map", "--reports", str(tmp_path), "--mark", "e=414"]) == EXIT_OK
    assert (tmp_path / "margin-map.html").is_file()
    assert main(["margins", "map", "--reports", str(tmp_path), "--mark", "e414", "--json"]) == EXIT_USAGE
    assert main(["margins", "table", str(run), "--json"]) == EXIT_USAGE
    assert main(["margins", "import", "--json"]) == EXIT_USAGE
