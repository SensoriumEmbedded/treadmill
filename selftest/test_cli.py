"""The command line without hardware: stations, offline calibration, usage errors."""

import json
from pathlib import Path

import cv2
import pytest

from selftest.synth import GEOMETRY, frame_of, menu_like_screen
from treadmill.cli import EXIT_OK, EXIT_USAGE, main


def test_stations_with_an_empty_lab(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / "stations").mkdir()
    (tmp_path / "rigs").mkdir()
    monkeypatch.setenv("TREADMILL_DATA", str(tmp_path))
    assert main(["stations", "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out) == {"stations": []}


def test_calibrate_from_a_saved_frame(tmp_path: Path, capsys):
    codes, colors = menu_like_screen(2)
    png = tmp_path / "menu.png"
    cv2.imwrite(str(png), frame_of(codes, colors, seed=2))
    assert main(["calibrate", "--frame", str(png), "--json", "--no-save"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    cal = out["calibration"]
    assert out["saved"] is None and cal["layout"] == "menu" and cal["method"] == "bounding-box"
    assert abs(cal["cell"][0] - GEOMETRY.cell_w) < 0.05 and abs(cal["cell"][1] - GEOMETRY.cell_h) < 0.05
    assert cal["frame"] == [1280, 720]


def test_calibrate_reports_an_unreadable_frame(tmp_path: Path, capsys):
    assert main(["calibrate", "--frame", str(tmp_path / "missing.png"), "--json"]) == EXIT_USAGE
    assert "error" in json.loads(capsys.readouterr().err)
    flat = tmp_path / "flat.png"                       # background and border the same color: no text area
    cv2.imwrite(str(flat), frame_of(menu_like_screen(1)[0], 15, seed=1, background=11, border=11))
    assert main(["calibrate", "--frame", str(flat), "--json"]) != EXIT_OK
    assert "alike" in json.loads(capsys.readouterr().err)["error"]


def test_usage_errors_exit_two():
    with pytest.raises(SystemExit) as e:
        main(["harvest"])                      # --station is required
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        main(["nope"])
