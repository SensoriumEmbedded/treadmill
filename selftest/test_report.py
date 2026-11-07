"""The run record and the report rendered from it, with no hardware and no pytest session."""

import json
from pathlib import Path

from treadmill.report import RunDir, render_report


def test_run_dir_finish_counts_outcomes_and_finds_folders(tmp_path: Path):
    rd = RunDir(path=tmp_path / "20260101-000000")
    rd.tests["a::t1"] = {"nodeid": "a::t1", "outcome": "passed", "duration": 1.23456, "message": None}
    rd.tests["a::t2"] = {"nodeid": "a::t2", "outcome": "failed", "duration": 0.5, "message": "boom"}
    rd.tests["a::t3"] = {"nodeid": "a::t3", "outcome": "skipped", "duration": 0.0, "message": "lacks dma"}
    rd.for_test("a::t2")
    rd.finish(1)
    assert rd.meta["summary"] == {"passed": 1, "failed": 1, "skipped": 1} and rd.meta["exit_status"] == 1
    by_id = {t["nodeid"]: t for t in rd.meta["tests"]}
    assert by_id["a::t1"]["duration"] == 1.235 and by_id["a::t1"]["folder"] is None
    assert by_id["a::t2"]["folder"] == "a__t2"
    assert rd.stamp == "20260101-000000" and "finished" in rd.meta
    out = json.loads(rd.write_run().read_text(encoding="utf-8"))
    assert out["summary"]["failed"] == 1


def test_render_report_shows_failures_with_their_artifacts(tmp_path: Path):
    run = tmp_path / "20260101-000000"
    folder = run / "tests_x.py__test_a"
    folder.mkdir(parents=True)
    (folder / "serial.log").write_bytes(b"Reset cmd received\r\nLoading IO handler: TeensyROM\r\n")
    (folder / "menu-root.diff.txt").write_text(" 0 |a|  |b|\n   |^|\n", encoding="utf-8")
    (folder / "xcheck.json").write_text(json.dumps([{"disagreement": 0.034}]), encoding="utf-8")
    meta = {
        "stamp": "20260101-000000", "suite": "0.0.1", "suite_commit": "abc", "python": "3.14", "host": "bench",
        "station": {"id": "s1", "model": "C64", "region": "PAL", "rig": "rigA", "tr_kind": "tr-plus",
                    "caps": ["dma", "video"]},
        "board": {"version": "TeensyROM+ v0.9", "built": "Oct 30 2026, 10:00:00", "image": "main",
                  "machine_line": "C64 PAL 50"},
        "firmware": {"spec": "ref=main target=tr-plus defines=Dbg_SerTimChg", "version": "TeensyROM+ v0.9-tim*",
                     "stamp": "Nov 7 2026, 12:00:00", "describe": "v0.9-3-gabc-dirty", "dirty": True,
                     "flashed": True, "defines": ["Dbg_SerTimChg"]},
        "video": {"available": True, "kind": "CvCapture", "mode": "MJPG 1280x720@30"},
        "calibration": {"method": "dma-refined", "residual": 0.21},
        "exit_status": 1, "argv": ["tests", "--station", "s1"],
        "tests": [
            {"nodeid": "tests/x.py::test_a", "outcome": "failed", "duration": 1.5,
             "message": "AssertionError: <b>boom</b>", "folder": "tests_x.py__test_a"},
            {"nodeid": "tests/x.py::test_b", "outcome": "skipped", "duration": 0.0,
             "message": "station lacks power", "folder": None},
            {"nodeid": "tests/x.py::test_c", "outcome": "passed", "duration": 2.0, "message": None, "folder": None},
        ],
        "summary": {"failed": 1, "skipped": 1, "passed": 1},
    }
    (run / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    page = render_report(run).read_text(encoding="utf-8")
    assert "1 failed, 1 passed, 1 skipped" in page
    assert "Dbg_SerTimChg" in page and "TeensyROM+ v0.9-tim*" in page and "v0.9-3-gabc-dirty dirty" in page
    assert "station lacks power" in page
    assert "&lt;b&gt;boom&lt;/b&gt;" in page and "<b>boom</b>" not in page
    assert "Loading IO handler: TeensyROM" in page and "menu-root.diff.txt" in page and "|^|" in page
    assert "worst disagreement 3.4%" in page
    assert 'href="tests_x.py__test_a/serial.log"' in page
    assert page.count("<h2 id=") == 1, "only the failed test gets a details section"


def test_render_report_with_nothing_run(tmp_path: Path):
    run = tmp_path / "r"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"stamp": "r", "aborted": "station 'x': data/stations/x.toml does not exist",
                                              "exit_status": 2, "tests": [], "summary": {}}), encoding="utf-8")
    page = render_report(run).read_text(encoding="utf-8")
    assert "no tests ran" in page and "does not exist" in page
