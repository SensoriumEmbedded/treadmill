"""The command line without hardware: stations, offline calibration, usage errors."""

import json
from pathlib import Path

import cv2
import pytest

from selftest.synth import GEOMETRY, frame_of, menu_like_screen
from treadmill.cli import EXIT_FAILED, EXIT_NO_STATION, EXIT_OK, EXIT_USAGE, main


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


def test_hardware_errors_keep_the_json_contract(monkeypatch, capsys):
    import treadmill.session
    from treadmill.link.discover import NotFound
    from treadmill.observe.capture import CaptureTimeout

    def no_board(*args, **kwargs):
        raise NotFound("no TeensyROM with chip id 1 on USB")
    monkeypatch.setattr(treadmill.session, "open_station", no_board)
    assert main(["screen", "--station", "s1", "--json"]) == EXIT_NO_STATION
    assert "chip id 1" in json.loads(capsys.readouterr().err)["error"]

    def feed_stopped(*args, **kwargs):
        raise CaptureTimeout("no new frame within 10.0 s from CvCapture")
    monkeypatch.setattr(treadmill.session, "open_station", feed_stopped)
    assert main(["frame", "--station", "s1", "--out", "x.png", "--json"]) == EXIT_FAILED
    assert "no new frame" in json.loads(capsys.readouterr().err)["error"]


def test_score_a_saved_frame_against_its_dma_read(tmp_path: Path, capsys):
    import numpy as np

    from selftest.synth import calibrated, harvested_bank

    codes, colors = menu_like_screen(4)
    cal = calibrated()
    bank = harvested_bank(cal)
    cv2.imwrite(str(tmp_path / "frame.png"), frame_of(codes, colors, seed=4))
    np.save(tmp_path / "dma.npy", codes)
    bank.save(tmp_path / "bank.npz")
    cal.save(tmp_path / "cal.json")
    assert main(["score", "--frame", str(tmp_path / "frame.png"), "--dma", str(tmp_path / "dma.npy"),
                 "--charset", "lower_upper", "--bank", str(tmp_path / "bank.npz"),
                 "--calib", str(tmp_path / "cal.json"), "--json"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["dma_refined"]["disagreement"] == 0 and out["stored"]["disagreement"] == 0
    assert out["stored"]["charset"] == "lower_upper"
