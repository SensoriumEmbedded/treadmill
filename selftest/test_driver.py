"""The driver without hardware: ``run`` as a child pytest against the fake
board, ``exec`` and ``shell`` on the fake station, and the exit-code contract."""

import io
import json
import sys
import textwrap
from pathlib import Path

from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from treadmill.cli import EXIT_FAILED, EXIT_NO_STATION, EXIT_OK, EXIT_USAGE, main
from treadmill.driver import PYTEST_EXIT, FirmwareOptions, RunOptions, pytest_command

QUIET = ("-p", "no:anyio", "-p", "no:cacheprovider")


def test_run_the_identity_test_against_the_fake_board(lab: Path, capsys):  # noqa: F811
    code = main(["run", "--station", "s1", "--reports", str(lab / "reports"), "--json", "--",
                 "-k", "test_identity", *QUIET])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    payload = json.loads(out)
    assert payload["summary"] == {"passed": 1} and payload["exit_code"] == 0 and payload["aborted"] is None
    assert payload["tests"][0]["nodeid"].endswith("::test_identity") and payload["tests"][0]["folder"]
    assert payload["board"]["version"] == "TeensyROM+ v0.9" and payload["station"]["tr_kind"] == "tr-plus"
    assert "1 passed" in err, "pytest's own output belongs on stderr under --json"
    run = Path(payload["run_dir"])
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert meta["exit_status"] == 0 and meta["firmware"]["spec"] == "none target=auto"
    assert meta["argv"][-1] == QUIET[-1] and meta["suite"]
    assert "test_identity" in (run / "report.html").read_text(encoding="utf-8")


def test_run_with_nothing_selected_is_the_request_s_fault(lab: Path, capsys):  # noqa: F811
    code = main(["run", "--station", "s1", "--reports", str(lab / "reports"), "--json", "--",
                 "-k", "nothing_is_called_this", *QUIET])
    out, _ = capsys.readouterr()
    payload = json.loads(out)
    assert code == EXIT_USAGE and payload["pytest_status"] == 5
    assert payload["aborted"] == "no tests were selected" and payload["summary"] == {}


def test_run_checks_the_request_before_starting_pytest(lab: Path, capsys):  # noqa: F811
    assert main(["run", "--station", "s1", "--fw", "main", "--json"]) == EXIT_USAGE
    assert "ref=" in json.loads(capsys.readouterr().err)["error"]
    assert main(["run", "--station", "nowhere", "--json"]) == EXIT_USAGE
    assert "nowhere" in json.loads(capsys.readouterr().err)["error"]
    assert not (lab / "reports").exists() or not any((lab / "reports").iterdir()), "a refused request made a run folder"


def test_run_passes_the_plugin_s_own_exit_code_through(lab: Path, monkeypatch, capsys):  # noqa: F811
    """Another process holds the rig: the plugin ends the session with the
    suite's no-station code (4), which pytest itself would read as a usage error."""
    from treadmill.station.lock import HELD_ENV, RigLock

    holder = RigLock("rigA").acquire()
    monkeypatch.delenv(HELD_ENV, raising=False)        # the child is another run, not this process's own flasher
    try:
        code = main(["run", "--station", "s1", "--reports", str(lab / "reports"), "--json", "--",
                     "-k", "test_identity", *QUIET])
    finally:
        holder.release()
    out, _ = capsys.readouterr()
    payload = json.loads(out)
    assert code == EXIT_NO_STATION and payload["pytest_status"] == 4, payload
    assert "in use" in payload["aborted"] and payload["summary"] == {}


def test_exec_records_a_station_that_cannot_be_opened(lab: Path, tmp_path: Path, monkeypatch, capsys):  # noqa: F811
    import treadmill.driver as driver
    from treadmill.link.discover import NotFound

    def no_board(*args, **kwargs):
        raise NotFound("no TeensyROM with chip id 19277260 on USB")
    monkeypatch.setattr(driver, "open_station", no_board)
    script = tmp_path / "s.py"
    script.write_text("pass\n", encoding="utf-8")
    code = main(["exec", "--station", "s1", "--reports", str(lab / "reports"), "--json", str(script)])
    assert code == EXIT_NO_STATION
    assert "chip id" in json.loads(capsys.readouterr().err)["error"]
    run = next((lab / "reports").iterdir())
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert "chip id" in meta["aborted"] and meta["exit_status"] == EXIT_NO_STATION and meta["tests"] == []


def test_pytest_command_carries_every_option(tmp_path: Path):
    opts = RunOptions(station="s1", tests=("tests/smoke", "tests/menu/test_navigation.py::test_sources"),
                      caps_mask="dma,keys_kernal", soak=3, estimate=True, include_tolerant=True,
                      no_video=True, no_xcheck=True, golden_dir="g",
                      firmware=FirmwareOptions(fw="ref=main", target="tr-plus", defines=("Dbg_SerDMA",),
                                               tag="t", allow_dirty=True, no_flash=True),
                      pytest_args=("-x", "-k", "menu"))
    cmd = pytest_command(opts, tmp_path)
    assert cmd[:3] == [sys.executable, "-m", "pytest"]
    assert cmd[3].endswith("smoke") and cmd[4].endswith("test_navigation.py::test_sources")
    text = " ".join(cmd)
    for piece in ("--station s1", f"--run-dir {tmp_path}", "--fw ref=main", "--fw-target tr-plus",
                  "--define Dbg_SerDMA", "--tag t", "--allow-dirty", "--no-flash", "--caps-mask dma,keys_kernal",
                  "--soak 3", "--estimate", "--include-tolerant", "--no-video", "--no-xcheck", "--golden-dir g"):
        assert piece in text, piece
    assert cmd[-3:] == ["-x", "-k", "menu"]
    assert PYTEST_EXIT[1] == EXIT_FAILED and PYTEST_EXIT[5] == EXIT_USAGE and PYTEST_EXIT[0] == EXIT_OK


def test_exec_runs_a_script_with_the_station_as_globals(lab: Path, tmp_path: Path, capsys):  # noqa: F811
    script = tmp_path / "poke.py"
    script.write_text(textwrap.dedent("""
        import sys
        assert __name__ == "__main__" and sys.argv[1:] == ["a", "b"], sys.argv
        print("hello from the script")
        rows = screen.read().rows()
        (artifacts / "note.txt").write_text("x")
        result = {"ping": link.ping(), "legend": rows[23].strip(), "keys": keys is not None,
                  "menu": type(menu).__name__, "dbg": type(dbg).__name__, "station": station.id}
    """), encoding="utf-8")
    code = main(["exec", "--station", "s1", "--reports", str(lab / "reports"), "--json", str(script), "--", "a", "b"])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    payload = json.loads(out)
    assert payload["result"]["ping"].endswith("ready!") and payload["result"]["legend"].startswith("F1 Teensy Mem")
    assert payload["result"]["keys"] and payload["result"]["menu"] == "MenuDriver"
    assert payload["result"]["dbg"] == "DebugCommands" and payload["result"]["station"] == "s1"
    assert "hello from the script" in err, "under --json the script's prints leave stdout to the record"
    folder = Path(payload["folder"])
    assert (folder / "serial.log").read_bytes() and (folder / "events.json").is_file() and (folder / "note.txt").is_file()
    meta = json.loads((Path(payload["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    assert meta["command"] == "exec-poke" and meta["summary"] == {"passed": 1} and meta["exit_status"] == 0
    assert meta["board"]["version"] == "TeensyROM+ v0.9" and meta["argv"][0] == "treadmill"


def test_exec_reports_a_failing_script(lab: Path, tmp_path: Path, capsys):  # noqa: F811
    script = tmp_path / "boom.py"
    script.write_text("link.ping()\nraise ValueError('no good')\n", encoding="utf-8")
    code = main(["exec", "--station", "s1", "--reports", str(lab / "reports"), str(script)])
    out, err = capsys.readouterr()
    assert code == EXIT_FAILED
    assert "ValueError: no good" in err and "boom.py: ValueError: no good" in out
    run = next((lab / "reports").iterdir())
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert meta["tests"][0]["outcome"] == "failed" and "no good" in meta["tests"][0]["message"]
    assert meta["exit_status"] == 1 and "no good" in (run / "report.html").read_text(encoding="utf-8")


def test_exec_needs_a_script_that_exists(lab: Path, capsys):  # noqa: F811
    assert main(["exec", "--station", "s1", "--json", str(lab / "missing.py")]) == EXIT_USAGE
    assert "missing.py" in json.loads(capsys.readouterr().err)["error"]


def test_shell_reads_commands_from_stdin(lab: Path, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr("sys.stdin", io.StringIO("print(link.ping())\nprint('station is', station.id)\n"))
    code = main(["shell", "--station", "s1", "--reports", str(lab / "reports")])
    out, _ = capsys.readouterr()
    assert code == EXIT_OK and "ready!" in out and "station is s1" in out
    run = next((lab / "reports").iterdir())
    assert (run / "shell" / "serial.log").is_file()
