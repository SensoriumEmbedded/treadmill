---
name: treadmill
description: Drive TReadmill, the hardware-in-the-loop validation suite for TeensyROM firmware, from a Claude Code session. Covers which `treadmill` command fits which ask (run the hardware tests against a station, test a branch or a debug build, prove the plain-TR tier, sweep a bus timing knob for its margin, run or draft an investigation script, poke a board from a shell, read a screen, calibrate or harvest a rig), how to read run.json and report.html, the --json and exit-code contract, and the go-ahead a board needs before it is flashed or driven. Use whenever the user wants to run hardware tests, validate firmware on a real C64 or C128, measure a timing margin, reproduce a bug on the bench, or asks about TReadmill, a station, a rig or a run.
---

# TReadmill from a session

TReadmill lives in its own repository (this one). The firmware checkout it
builds and flashes is the rig's `firmware_checkout` or `TREADMILL_FW_REPO`.
Every command takes `--json` (the shell excepted): data on stdout, everything
else on stderr; exit 0 good, 1 what ran is not right, 2 the request, 4 the
station could not be had.

## Before touching a board

Flashing or driving a board needs the person's go-ahead in the session: say
which station gets which image (`--fw ...`) and what will run, and wait. The
self-tests and anything with `TREADMILL_FAKE_BOARD=1` need no go-ahead.
Never write a port name, host, IP address or chip id into a committed file.

## Which command

| Ask | Command |
|---|---|
| What stations exist, what each can do | `treadmill stations --json` |
| Run the hardware tests on the firmware already on the board | `treadmill run --station <id> --json` |
| Test a branch, a PR branch or a commit | `treadmill run --station <id> --fw ref=<ref> --json` (built in a worktree, cached, flashed only if the board does not already run it) |
| Test a worktree with one-off edits | `--fw path=<dir> --allow-dirty` (the build carries the dirty `*`) |
| Test a prebuilt hex | `--fw hex=<file>` |
| A debug build (any `Dbg_*` define) | `--fw ref=main --define Dbg_SerTimChg [--define ...]`; the banner gets the define's mark (`v0.9-tim`) |
| Prove the plain-TR tier on a TR+ station | `--caps-mask dma,screen_dma,reset_sense,keys_kernal` |
| Some tests only | `--tests tests/smoke` (repeatable) or, after `--`, pytest's own `-k EXPR`, `-x`, `-v` |
| How long a selection takes | `--estimate` (runs nothing) |
| A timing knob's margin (how far `nS_PLAprop` can rise, or fall, before reads fail) | `treadmill sweep p --station <id> --fw ref=main --define Dbg_SerTimChg --json` (`--probe idread` on a TR+, counting ROM and IO2 reads apart; `--probe menu` on any TR; `--to` below the value walks down, no lower than `nS_RWnReady` for `p`; `--step`, `--fine`, `--repeat`, `--dwell`); the defaults are restored on exit; `sweep.json` in the run folder |
| The manufacturing test of new TR+ boards | `treadmill mfg --station <id>` at the operator's terminal: it prompts for the cables, the buttons, the LEDs, the reset and the power-off, and loops board after board (`--boards N` to stop); `--snapshot-sd <folder>` writes the SD golden; the log is `data/mfg/boards.csv`, the evidence `reports/<run>/boards/` (README, "Manufacturing test") |
| An investigation script under the harness | `treadmill exec --station <id> script.py [-- args] --json`; a `result` the script sets comes back |
| Poke the board interactively | `treadmill shell --station <id>` (`link.ping()`, `screen.read().rows()`, `menu.open("/Games")`, `keys.press(F1)`, `dbg.letter("t")`) |
| What is on the screen now | `treadmill screen --station <id> --json` (`--exact` prefers DMA, `--xcheck` compares both) |
| A rig's first setup | `treadmill calibrate --station <id>`, then `treadmill harvest --station <id>` |

`run` streams pytest's output, then prints the summary; under `--json` the
pytest output goes to stderr and stdout holds `run_dir`, `report`, `summary`,
`exit_code`, `tests` (nodeid, outcome, duration, message, folder), `station`,
`board`, `firmware`, `aborted`.

## Reading a run

`reports/<timestamp>/run.json` is the record: `station` (id, model, region,
caps, tr_kind), `board` (version, built, machine_line), `firmware` (spec,
commit, describe, dirty, defines, tag, version, stamp, flashed, cached,
build_log), `video` and `calibration`, `tests`, `summary`, `exit_status`,
`aborted` (why a run ended before its tests: a station or firmware problem),
`recoveries` (the ladder rungs the run needed). `report.html` renders it for
a person. Each test's folder holds `serial.log` (the board's raw output
during the test), `events.json`, the screens a golden check read
(`<name>.screen.txt`) with `<name>.diff.txt` when they differed, `frames.png`
(the last frames before a failure) and `xcheck.json`.

A failed golden check: read the diff; the screen read is on the left, the
golden on the right, `^` under the cells that differ. A video read that
disagrees with DMA above 1 % fails the test on its own (`xcheck.json`):
recalibrate or reharvest before blaming the firmware.

## Writing a script for exec

Globals: `session`, `station`, `board`, `link`, `screen`, `menu`, `keys`,
`dbg`, `video`, `firmware`, `artifacts` (a folder to write into), `run_dir`,
`log`. The board is at the menu when the script starts (not with `--as-is`).
Set `result` to what the caller should get back. `scripts/example.py` is the
pattern; keep one-off scripts under `scripts/`, dated in their docstring.

## Self-tests and the fake board

`TREADMILL_FW_REPO=<checkout> python -m pytest -q -p no:anyio selftest` runs
everything with no hardware (about six minutes); `selftest/test_hw_tests_offline.py`
runs the real `tests/` tree against the fake board. With `TREADMILL_FAKE_BOARD=1`
every command above runs against the fake, so a new test or script can be
tried before it meets a board.
