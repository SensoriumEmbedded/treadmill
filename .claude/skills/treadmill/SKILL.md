---
name: treadmill
description: Drive TReadmill, the hardware-in-the-loop validation suite for TeensyROM firmware, from a Claude Code session. Covers which `treadmill` command fits which ask (run the hardware tests against a station, test a branch or a debug build, prove the plain-TR tier, sweep a bus timing knob for its margin, run or draft an investigation script, poke a board from a shell, read a screen, calibrate or harvest a rig, run a Kawari station in PAL or NTSC, check the C64 USB keyboard board or drive a C64 through it without the TR), how to read run.json and report.html, the --json and exit-code contract, and the go-ahead a board needs before it is flashed or driven. Use whenever the user wants to run hardware tests, validate firmware on a real C64 or C128, measure a timing margin, reproduce a bug on the bench, or asks about TReadmill, a station, a rig or a run.
---

# TReadmill from a session

TReadmill lives in its own repository (this one). The firmware checkout it
builds and flashes is the rig's `firmware_checkout` or `TREADMILL_FW_REPO`.
Every command takes `--json` (the shell excepted): data on stdout, everything
else on stderr; exit 0 good, 1 what ran is not right, 2 the request, 4 the
station could not be had.

## Before touching a board

Flashing or driving a board needs the person's go-ahead in the session: say
which station gets which image (`--fw ...`) and what will run, and wait.
The keyboard board counts: a key, a reset or a standard switch drives the
machine. The self-tests and anything with `TREADMILL_FAKE_BOARD=1` need no
go-ahead.
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
| Prove the plain-TR tier on a TR+ station | `--caps-mask dma,screen_dma,reset_sense,keys_kernal`; with a keyboard board, leave out `keys_kernal` and the keys go through it, as on a plain TR |
| Run in one video standard, or both, on a Kawari station | `--region PAL\|NTSC\|both` (needs `region_switch`: the keyboard board's `V`); `both` is two runs, folders ending `-pal` and `-ntsc`, JSON `{"exit_code", "runs": [...]}` |
| Some tests only | `--tests tests/smoke` (repeatable) or, after `--`, pytest's own `-k EXPR`, `-x`, `-v` |
| How long a selection takes | `--estimate` (runs nothing) |
| A timing knob's margin, each way (how far `nS_PLAprop` can rise or fall before reads fail) | `treadmill sweep p --station <id> --fw ref=main --define Dbg_SerTimChg --both --json` (`--probe idread` on a TR+, counting ROM and IO2 reads apart; `--probe menu` on any TR, not for a knob that times DMA; for the DMA knobs, with `--define Dbg_SerDMA`: `zpattern` (`z###`, `--z-pages`), `reuloop` or `reuloop-sprites` (the C64's REU loop; Special IO is set to the REU and put back), `cpuwatch` (the C64's writes beside `z###`, for `nS_DMAAssert`), `noise` beside any of them (screen noise against a baseline at the defaults); `vicrom` for the VIC knobs (video, any TR), `krread` for Kernal Replace's sense knobs (Special IO, the Kernal file and `KSenseMode` set and put back); repeat `--probe` for several; `--image minimal` for MinimalBoot's knobs (idread by video, any TR with a capture), `--usb-load` for `nS_MaxAdj`'s upper edge; `--scan LO:HI --grid LO:HI` maps `nS_DMASetup` by `nS_DMADataHold`; `--to` for one edge; `--scan LO:HI` for every step of a range; `--passes N` for a drifting machine; `--free` moves `nS_DMASetup` without its held sums; `--step`, `--fine`, `--repeat`, `--dwell`); limits from `treadmill/knobs.py`, a spec past them is refused (exit 2); the defaults are restored on exit; `margins.json` in the run folder |
| Every knob's margin on a station (a margin run) | `treadmill run --station <id> --fw ref=main --define Dbg_SerTimChg --define Dbg_SerDMA --tests tests/sweep --json`, about 90 minutes on a TR+ (go-ahead first); `docs/knobs.md` says which test covers which knob; then `treadmill margins suggest` for its floors |
| The margins recorded so far, a chart of them, or a station's floors | `treadmill margins table` (or `csv`) `[--station <id>] [--knob p] --reports reports`; `treadmill margins map [--mark nS_DMASetup=414]` writes `reports/margin-map.html` (a chart per knob, a row per record), `--mermaid` prints gantt blocks for a PR; `treadmill margins import <run>...` makes records of the PR #99 script's runs; `treadmill margins suggest --station <id>` prints `[margins]` floors to copy into the private station file (the user does that) |
| The manufacturing test of new TR+ boards | `treadmill mfg --station <id>` at the operator's terminal: it prompts for the cables, the buttons, the LEDs and the power-off (and for the reset and a standard switch when the station's keyboard board cannot do them), and loops board after board (`--boards N` to stop); `--snapshot-sd <folder>` writes the SD golden; the log is `data/mfg/boards.csv`, the evidence `reports/<run>/boards/` (README, "Manufacturing test") |
| An investigation script under the harness | `treadmill exec --station <id> script.py [-- args] --json`; a `result` the script sets comes back |
| A machine whose TR does not answer on USB (an extension host), or no TR at all | `exec` or `shell` with `--no-link`: video and the keyboard board only; `board`, `link`, `menu`, `dbg` are None; no `--fw` |
| Check the keyboard board's own features | `treadmill exec --no-link --station <id> scripts/2026-11-19-keyboard-board-features.py --json` (about six minutes; restarts the C64 many times; `-- --skip tod,kawari` to leave outputs out) |
| Poke the board interactively | `treadmill shell --station <id>` (`link.ping()`, `screen.read().rows()`, `menu.open("/Games")`, `keys.press(F1)`, `dbg.letter("t")`, `kbd.reset()`) |
| What is on the screen now | `treadmill screen --station <id> --json` (`--exact` prefers DMA, `--xcheck` compares both) |
| A rig's first setup | `treadmill calibrate --station <id>`, then `treadmill harvest --station <id>` |

`run` streams pytest's output, then prints the summary; under `--json` the
pytest output goes to stderr and stdout holds `run_dir`, `report`, `summary`,
`exit_code`, `tests` (nodeid, outcome, duration, message, folder), `station`,
`board`, `firmware`, `aborted`.

## Reading a run

`reports/<timestamp>/run.json` is the record: `station` (id, model, region
from the registry, standard from the banner, caps, tr_kind, keyboard: open,
fitted, or the problem that kept it closed), `board` (version, built,
machine_line), `firmware` (spec,
commit, describe, dirty, defines, tag, version, stamp, flashed, cached,
build_log), `video` and `calibration`, `tests`, `summary`, `exit_status`,
`aborted` (why a run ended before its tests: a station or firmware problem),
`recoveries` (the ladder rungs the run needed: `reset`, `ext-reset`,
`usb-reboot`, `power-cycle`). `report.html` renders it for
a person. Each test's folder holds `serial.log` (the board's raw output
during the test), `events.json`, `kbd.log` (the keyboard board's lines both
ways), the screens a golden check read
(`<name>.screen.txt`) with `<name>.diff.txt` when they differed, `frames.png`
(the last frames before a failure) and `xcheck.json`.

A failed golden check: read the diff; the screen read is on the left, the
golden on the right, `^` under the cells that differ. A video read that
disagrees with DMA above 1 % fails the test on its own (`xcheck.json`):
recalibrate or reharvest before blaming the firmware.

## Writing a script for exec

Globals: `session`, `station`, `board`, `link`, `screen`, `menu`, `keys`,
`kbd`, `dbg`, `video`, `firmware`, `artifacts` (a folder to write into),
`run_dir`, `log`. The board is at the menu when the script starts (not with
`--as-is`); under `--no-link` the TR's names are None and the script checks
`kbd` before it relies on it.
Set `result` to what the caller should get back. `scripts/example.py` is the
pattern. Name a one-off script `scripts/YYYY-MM-DD-<topic>.py`, the day it
first ran on a bench, with the same date in its docstring's first paragraph;
README's "Adding a test or a script" has the rest (what the docstring says,
putting things back, when a script becomes a test).

## Self-tests and the fake board

`TREADMILL_FW_REPO=<checkout> python -m pytest -q -p no:anyio selftest` runs
everything with no hardware (about ten minutes); `selftest/test_hw_tests_offline.py`
runs the real `tests/` tree against the fake board. With `TREADMILL_FAKE_BOARD=1`
every command above runs against the fake, so a new test or script can be
tried before it meets a board.
