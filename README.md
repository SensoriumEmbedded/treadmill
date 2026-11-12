# TReadmill

Hardware-in-the-loop validation for TeensyROM firmware: the suite drives real
Commodore 64 and 128 machines through the TeensyROM's own remote protocol,
watches them through a video capture device and (on a TR+) DMA readback, builds
and flashes the firmware under test, and runs for hours unattended across a
list of stations.

The firmware runs for hours without going anywhere, and you measure it.

## Layout

```
treadmill/            the package: station model, link, observation, firmware, driver, reports
  pytest_plugin.py    options, markers, fixtures, the run record
  cli.py              treadmill run | exec | shell | sweep | mfg | stations | calibrate | harvest | screen | frame | score | viewer
  driver.py           run, exec and shell as library calls
  sweeps.py           the timing-knob sweep engine: knob x probe x search, and the margin it reports
  mfg.py              the manufacturing test of new TR+ boards, one after another
tests/                the hardware tests, one concern per file, selected by markers and --station
selftest/             tests that need no hardware (fake board, rendered frames, config)
scripts/              one-off investigation scripts for `treadmill exec`; kept, never part of the suite
data/                 rigs, stations, calibrations, glyph banks, goldens, SD fixtures (private parts are git-ignored)
reports/              one folder per run: run.json, report.html, one folder per test
```

## Quick start

```
pip install -e .
treadmill stations                                   # the stations data/stations/*.toml describes
TREADMILL_FW_REPO=<TeensyROM checkout> pytest        # the self-tests, no hardware (about fourteen minutes)
treadmill run --station <id>                         # the hardware tests against the firmware on the board
treadmill run --station <id> --fw ref=main           # build main for the board, flash it, verify it, then test
treadmill run --station <id> --caps-mask dma,reset_sense,keys_kernal   # the plain-TR tier on a TR+ station
treadmill run --station <id> --fw ref=main --define Dbg_SerTimChg --tests tests/debug
treadmill run --station <id> -- -k "identity or reset" -x            # after --, pytest's own arguments
treadmill exec --station <id> scripts/example.py     # a script with the station's objects as globals
treadmill shell --station <id>                       # the same names at a Python prompt
```

Every command takes `--json` (the shell excepted) and exits 0 when all is
well, 1 when what ran is not right (a failed test, a build that broke), 2
when the request is wrong (an option, a firmware spec, a station file) and 4
when the station could not be had (no board with that chip id, a busy rig, a
port that will not open). Under `--json` the data is on stdout and
everything else on stderr.

## Rigs and stations

A station is a rig (capture device and optional controls) plus a machine from
the registry plus a TeensyROM board. To add one:

1. Copy `data/rigs/rigA.example.toml` to `data/rigs/<rig>.toml`: the capture
   device and mode (`treadmill frame --station ... --raw` shows what it
   delivers), `firmware_checkout` for the TeensyROM checkout whose tools
   build and flash, and the power or keyboard backends when the rig has them.
2. Copy `data/stations/example.example.toml` to `data/stations/<id>.toml`:
   the rig, the machine (a registry id from the validation tracker's
   `systems.csv`, named in `data/registry.toml`, or an inline `[machine]`
   table) and the board's chip id as its version banner prints it. Name
   stations descriptively (`rigA-sys29-trplus`); `treadmill stations` shows
   the capabilities each one has.
3. `treadmill calibrate --station <id>` finds the text area in the picture
   from the menu's banner row, and `treadmill harvest --station <id>` builds
   the station's glyph bank from the suite's own program (a station without
   one reads with `data/glyphs/<capture class>.npz` when that exists).
   `treadmill screen --station <id> --xcheck` then shows how far the video
   read is from the DMA read.

Files that name your own boards and machines stay out of git.

## The hardware tests

`tests/` holds one concern per file: identity and reset (`smoke/`), files to
the card and back (`transfer/`), a launched program (`launch/`), the menu's
item-type marks and navigation (`menu/`), a debug build's timing knobs
(`debug/`). A test names what it needs (`@pytest.mark.needs("keys_kernal")`,
`needs_fw("Dbg_SerTimChg")`) and skips with the reason when the station or the
build lacks it; `--caps-mask dma,keys_kernal` takes capabilities away to prove
the plain-TR tier on a richer station, and `--fw-defines` names the defines a
build flashed by other means carries. Screen goldens live in `tests/golden/`
(`--golden-dir` for another set); the SD fixtures a test needs are put under
`/treadmill` on the card by the run itself.

### Adding a test

A test is a function in a file under `tests/` that takes the fixtures it
needs: `at_menu` (the board, at the menu before and after; the invariants
after it: no crash report, no boot banner, no unknown command), `link` (the
remote protocol), `screen` (the screen through the channel policy: video
unless `exact=True` and DMA is allowed; every read is cross-checked against
DMA where both exist), `menu` (the main menu driver), `keys`, `dbg` (the
debug letters, gated on the build's defines), `programs("hello")` (an
assembled C64 program), `sd_card` and `sd_fixtures` (the generated type set,
on the card), `goldens` (`goldens.check(read, "menu/root")`), `artifacts` (the
test's folder). Mark what it needs; declare its length with
`@pytest.mark.duration(minutes)` so `--estimate` can add it up. The fake
board runs the whole tree offline (`selftest/test_hw_tests_offline.py`), so
a new test should pass or skip there before it meets a board; what the fake
cannot show (timing, a real card, the picture) is the bench's to confirm.

## Special builds

`--fw` names the firmware under test: `ref=<git ref>` builds it in a
disposable worktree of the configured checkout (cached by commit, target,
defines and tag), `path=<checkout>` builds a tree as it is, `hex=<file>` uses
a prebuilt image, and `none` (the default) tests whatever the board carries.
`--define NAME` (repeatable) compiles a debug define in; the build is tagged
with the define's mark (`Dbg_SerTimChg` gives `v0.9-tim`) so the banner and
every version reply say what the build is, and `--tag` names it otherwise. A
checkout with uncommitted changes needs `--allow-dirty` and its build carries
the `*` the firmware's builder puts on a dirty tree. The flash is skipped
when the board already runs that exact build; `--no-flash` builds without
flashing. Any build with defines, a tag or a dirty tree is a variant and is
recorded as one.

## Sweeps

`treadmill sweep p --station <id> --fw ref=main --define Dbg_SerTimChg` finds a
bus timing knob's margin: the knob (`p`, `tp` or `nS_PLAprop`; any of the
firmware's `t` knobs) walks from its default toward `--to` in `--step` steps
until the probe fails, the edge is bisected down to `--fine`, the defaults are
restored whatever happened, and the result is the last clean value, the first
failing one and every point probed, as text or `--json` and as `sweep.json` in
the run's folder. A `--to` below the knob's value walks it down instead. The
TeensyROM cannot see its own read errors, so the probes are the C64's:
`idread` launches a one-bank EasyFlash cartridge that reads its own ROM and the
EasyFlash IO2 page in a loop and counts wrong answers per path, read back by
DMA over `--dwell` seconds per sample (a TR+; a launched PRG would run under
the None IO handler, with nothing answering at IO1); `menu` resets, lets the
menu boot and compares it with its golden (any TR, by video). `--repeat N`
takes several samples a point, so an intermittent edge is a rate, not a coin
toss.

`nS_PLAprop` has an edge each way. Raised, the data leaves too late, and IO
reads fail before ROM reads, since the board drives them only after a call
into the IO handler; lowered, the select lines are sampled before the slowest
has asserted. It reaches no lower than `nS_RWnReady`: both waits count from
the start of the cycle, so below it the knob changes nothing.
`tests/sweep/test_tp_margin.py` runs both sweeps as a regression guard: the
default must be clean, and a station that names a floor in its `[margins]`
table (`nS_PLAprop = 200`) fails the test when reads go wrong below it.

## Manufacturing test

`treadmill mfg --station <id>` tests new TR+ boards one after another on a
station's rig. The station gives the rig and the machine; each board is
found by the serial string it enumerates with. A TR+ takes its power from
the machine, so switching the machine on is what starts a board. The steps
run in order:

1. Power-up: the main image boots to the menu.
2. Firmware: the version and build stamp equal the factory hex's, with no
   tag and no `*`.
3. External Ports: the built-in test passes after the operator presses MENU
   and ALT. Its `Hello from TR at:` line agrees on serial and on screen, and
   is within `time_tolerance_s` of the PC's UTC clock (a new board's
   timezone setting is UTC). The operator answers for the Ethernet and TR
   LEDs.
4. External reset: the operator presses the C64's reset button, and the TR+
   must log `Button detected` and bring the menu back.
5. Expansion Port: the built-in test passes `xpt_passes` times.
6. SD root: the card's root matches the master image's snapshot.
7. REU: hot key 2 launches the REU checker with a 512K REU, and video reads
   its pass screen.

The first failure halts the board. Its screen turns red and inverted, the
bottom row names the step and the reason, and the C64 is paused so nothing
redraws it. A passing board gets a green summary instead. Each board adds:

- a row to the profile's `log` (`data/mfg/boards.csv`): start time, chip id,
  firmware, result, the failed step and reason, every step's result and the
  LED answers;
- a folder under `reports/<run>/boards/` with its serial log, every screen
  read and `result.json`.

The console then asks for the power-off, waits for the board to leave USB,
and starts the next one. Ctrl+C ends the session; `--boards N` stops after N.

Setting it up, once:

1. Copy `data/mfg/trplus.example.toml` to `data/mfg/trplus.toml` and point
   `factory_hex` at the factory hex.
2. Run `treadmill mfg --snapshot-sd <master image folder>`, which writes
   `sd_root.json` beside the profile.
3. Run one known-good board with no REU golden in place. The REU step stops
   with "no REU checker golden" and saves the checker's screen as
   `reu.screen.txt` in that board's folder. Check it (a `?` cell matches
   anything), then copy it to the profile's `reu_golden`.

## Scripting

`treadmill exec --station <id> script.py [-- args]` opens the station the way
a test session does (the firmware under test prepared, the rig held, the
board at the menu unless `--as-is`) and runs the script as `__main__` with
these names as globals: `session`, `station`, `board`, `link`, `screen`,
`menu`, `keys`, `dbg`, `video`, `firmware`, `artifacts` (a folder of the run
to write into), `run_dir`, `log`. A `result` the script leaves is reported
(`--json` returns it), and what the board said meanwhile lands in the run's
folder like a test's. `treadmill shell --station <id>` gives the same names at
a prompt. `scripts/example.py` is a ten-line example.

From Python, `treadmill.session.open_station("<id>")` returns the same
session (`board`, `link`, `screen_channel()`, `menu()`, `keys()`, `dbg()`),
and `treadmill.driver.run_tests(RunOptions(...))` is what `treadmill run` does.

## Reports

Every run leaves `reports/<timestamp>/`: `run.json` (station, board, the
firmware spec with its commit, dirty hash, defines, tag, hex and stamp,
capabilities, capture mode, calibration residual, the suite's own commit, the
options, every test's outcome, duration and message, the summary and exit
status), `report.html` rendered from it with each failure's serial tail,
golden diff and frame strip, and one folder per test with `serial.log`,
`events.json`, every screen a golden check read and the diff when it
differed, and `xcheck.json` when video was cross-checked against DMA.

## Conventions

- Every command but the shell takes `--json`, and every one exits non-zero on failure; the codes are above.
- Every run writes `reports/<timestamp>/run.json` and `report.html`; `data/results.sqlite` comes with the nightly runs.
- The suite refuses to start when another program holds the station's rig or serial port.
- DMA commands are never sent to a plain TR; debug letters are never sent to a build without their define.
- Nothing is flashed or driven without the person's go-ahead; the station files that name boards stay private.
