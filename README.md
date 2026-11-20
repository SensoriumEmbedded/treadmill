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
scripts/              dated one-off investigation scripts for `treadmill exec`; kept, never part of the suite
spikes/               the Phase 1 feasibility probes and their results; a record, nothing new goes here
data/                 rigs, stations, calibrations, glyph banks, goldens, SD fixtures (private parts are git-ignored)
reports/              one folder per run: run.json, report.html, one folder per test
```

## Quick start

```
pip install -e .
treadmill stations                                   # the stations data/stations/*.toml describes
TREADMILL_FW_REPO=<TeensyROM checkout> pytest        # the self-tests, no hardware (about ten minutes)
treadmill run --station <id>                         # the hardware tests against the firmware on the board
treadmill run --station <id> --fw ref=main           # build main for the board, flash it, verify it, then test
treadmill run --station <id> --caps-mask dma,reset_sense,keys_kernal   # the plain-TR tier on a TR+ station
treadmill run --station <id> --fw ref=main --define Dbg_SerTimChg --tests tests/debug
treadmill run --station <id> -- -k "identity or reset" -x            # after --, pytest's own arguments
treadmill run --station <id> --region both           # a Kawari station: the tests in PAL, then in NTSC
treadmill exec --station <id> scripts/example.py     # a script with the station's objects as globals
treadmill exec --no-link --station <id> <script>     # the same without opening the TeensyROM
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
   build and flash, and the power backend when the rig has one.
2. Copy `data/stations/example.example.toml` to `data/stations/<id>.toml`:
   the rig, the machine (a registry id from the validation tracker's
   `systems.csv`, named in `data/registry.toml`, or an inline `[machine]`
   table) and the board's chip id as its version banner prints it, plus a
   `[keyboard]` table when the machine has a keyboard board (below). Name
   stations descriptively (`rigA-sys29-trplus`); `treadmill stations` shows
   the capabilities each one has.
3. `treadmill calibrate --station <id>` finds the text area in the picture
   from the menu's banner row, and `treadmill harvest --station <id>` builds
   the station's glyph bank from the suite's own program (a station without
   one reads with `data/glyphs/<capture class>.npz` when that exists).
   `treadmill screen --station <id> --xcheck` then shows how far the video
   read is from the DMA read. Both files are kept per video standard, as
   the board's banner gives it (`<capture class>__<machine>__PAL`), so a
   station that switches standards calibrates and harvests once in each. A
   file named without the standard is read when the standard's own is
   missing.

Files that name your own boards and machines stay out of git.

### The keyboard board

The C64-USBKeyboard board sits in a C64's keyboard connector and speaks
its own protocol (protocol 1, the C64-USBKeyboard repository's
`Docs/Host_Protocol.md`) on its own USB serial port. It lives in one
machine, so the station names it, in a `[keyboard]` table:

- `chip`: its chip id. It enumerates as `C64USBKeyboard_<chip>`, which is
  how it is found. The TeensyROM's discovery never sends TR commands to it:
  when a board's chip id finds no port, the probe opens only ports whose
  serial string is empty or all digits.
- `kawari_open`: `PAL` or `NTSC`, the standard a Kawari gives with its
  switch header J1 open (it gives the other one closed). `--region` needs
  it; the manufacturing test reads J1 from the keyboard board instead.
- `tod`: the station's TOD rate, `standard` (50 Hz on PAL, 60 on NTSC, the
  default), `50`, `60` or `off`.

It opens after the TeensyROM (or alone, under `--no-link`), under the same
rig lock, and its `?` reply says which outputs are fitted. The station's capabilities come from that
reply:

| When | Capabilities |
|---|---|
| it answers | `keys_hw`, `keys_kernal`, `nmi` |
| `tod` is fitted | `tod_select` |
| `menu` is fitted | `tr_button` |
| `alt` is fitted | `tr_alt` |
| `reset` is fitted | `ext_reset` |
| `kawari` and `reset` are fitted, on a machine with a Kawari, with `kawari_open` set | `region_switch` |

It takes its power from the C64, so it is on USB only while the machine is
on. A station whose keyboard board does not answer opens without these
capabilities, and the tests that need them skip, saying why.

What the suite does with it:

- **Keys.** The `keys` fixture presses keys by DMA on a TR+ and through the
  keyboard board's matrix elsewhere, so the menu tests run on a plain TR;
  `--caps-mask dma` sends a TR+'s keys through the keyboard board too.
  The `kbd` fixture is the board itself, for real keys on any TR: holds by
  matrix position (`Matrix.RUN_STOP` and the rest in
  `treadmill/c64/keys.py`) and RESTORE. After the test it lets go of
  anything still held and sets the TOD rate back.
- **Reset.** `board.ext_reset()` pulls the C64's /RESET without the TR.
  When a TR+ can sense the reset, the recovery ladder tries it after the
  TR's own reset and before a USB reboot, so a TR+ in a handler that
  ignores the port, such as TR BASIC, comes back without one. A plain TR
  cannot sense the reset.
- **Video standard.** With `region_switch`, its `V` command switches the
  Kawari's standard with the C64 held in reset and no power cycle. `run
  --region PAL|NTSC|both` sets the standard before the tests, checks it on
  the TR's banner and reads the picture with that standard's calibration
  and glyph bank. When a Kawari's saved model has changed, J1 open no
  longer gives what `kawari_open` says; the switch then flips J1 once and
  names the value to set.
- **TOD.** When a station opens with its TeensyROM, the TOD line is set to
  the station's rate. `exec --no-link` leaves it as it is.
- **Manufacturing test.** It resets the C64 and switches the standard in
  place of the operator (steps 1 and 4 below).

The board's own features (every protocol command and every fitted output)
are checked by a script, not by the suite: `treadmill exec
--no-link --station <id> scripts/2026-11-19-keyboard-board-features.py`
(about six minutes; it restarts the C64 many times).

## The hardware tests

`tests/` holds one concern per file: identity and reset (`smoke/`), files to
the card and back (`transfer/`), a launched program (`launch/`), the menu's
item-type marks and navigation (`menu/`), a debug build's timing knobs
(`debug/`), a timing knob's margin (`sweep/`), IO handlers the host
drives over USB (`handlers/`: TR BASIC's
TPUT and TGET, which needs `--attended` until the test types its SYS
through the keyboard board, and an ASID stream the C64's reads are logged
against, which needs `pip install -e .[midi]`), and what the suite relies
on the keyboard board for (`keyboard/`: real keys, an external reset, the
region switch). A test names what it needs
(`@pytest.mark.needs("keys_kernal")`, `needs_fw("Dbg_SerTimChg")`) and skips with the reason when the station or the
build lacks it; `--caps-mask dma,keys_kernal` takes capabilities away to prove
the plain-TR tier on a richer station, and `--fw-defines` names the defines a
build flashed by other means carries. Screen goldens live in `tests/golden/`
(`--golden-dir` for another set); the SD fixtures a test needs are put under
`/treadmill` on the card by the run itself. These eight folders are the
areas; a new one gets a line here when its first test lands.

A station runs in whatever video standard its machine is in, and
`run.json` records it (`station.standard`, from the TR's banner). On a
station with `region_switch`, `--region PAL` or `NTSC` switches it first,
and `--region both` is two runs, PAL then NTSC, each with its own folder
(`<timestamp>-pal`, `<timestamp>-ntsc`), the second whatever the first
did; the exit code is the worse of the two, and under `--json` each run's
record is in `runs`. A station that cannot switch stops with exit 2 unless
it is already in that standard.

### Which tests run where

The whole suite runs on every station before a firmware release, and on a
firmware PR that touches a pinned path: about 5 minutes a station, 12 with
the ASID test, and the skips cover what a station lacks. Reset, launch,
menu, sweep and handler tests matter on each machine class (C64 and C128,
PAL and NTSC), since detection and timing differ between them; the transfer
tests and the debug listing need one station per TR kind. There is no
plain-TR station yet, so the plain-TR tier is proven with `--caps-mask`;
on a TR+ station with a keyboard board, `--caps-mask
dma,screen_dma,reset_sense` leaves the keys, pressed through the keyboard
board as on a plain TR.

### Adding a test or a script

Decide first where the check belongs:

- A string, a cut, a layout or a parser: the firmware's own host tests
  (`Source/Teensy/tests/`). The bench would only repeat them.
- A question for the bench, or a PR to try once: a script in `scripts/`.
- A check only hardware can make (bus timing, DMA, video, one machine's
  variance, a known issue coming back), needed again on each firmware change
  or on more than one machine: a test in `tests/`.
- TReadmill's own logic: `selftest/`.

**A test** is a function in a file under `tests/` that takes the fixtures it
needs: `at_menu` (the board, at the menu before and after; the invariants
after it: no crash report, no boot banner, no unknown command), `link` (the
remote protocol), `screen` (the screen through the channel policy: video
unless `exact=True` and DMA is allowed; every read is cross-checked against
DMA where both exist), `screen_now` (for a test that switches the video
standard: a function that returns a screen channel for the standard the
board runs now), `menu` (the main menu driver), `keys` (DMA on a TR+, else
the keyboard board), `kbd` (the keyboard board itself), `dbg` (the
debug letters, gated on the build's defines), `programs("hello")` (an
assembled C64 program, or a raw payload a test writes in by DMA), `sd_card`
and `sd_fixtures` (the generated type set, on the card), `goldens` (`goldens.check(read, "menu/root")`), `artifacts` (the
test's folder), `warn("...")` (a line in the test's warnings in `run.json` and
`report.html`, for what to look at without failing). Mark what it needs; declare its length with
`@pytest.mark.duration(minutes)` so `--estimate` can add it up. The module
docstring names what the test guards (a PR, a commit or a Known-Issues
section) and the machines where that matters. The fake board runs the whole
tree offline (`selftest/test_hw_tests_offline.py`), so a new test passes or
skips there before it meets a board, and stays quick there: one that is slow
on the fake (`pytest --durations`) gets a shorter fake path or skips with
`fake_requested()`, as the ASID and TR BASIC tests do, so the self-tests stay
near ten minutes. What the fake cannot show (timing, a real card, the
picture) is the bench's to confirm, and a test has run on a bench before it
is committed.

**A script** is `scripts/YYYY-MM-DD-<topic>.py`: the day it first ran on a
bench, a kebab-case topic, no subfolders. The first paragraph of its
docstring carries the same date, and the docstring gives the `treadmill
exec` line, the firmware it needs (a ref, PR or commit, and defines), what
the station needs (a TR+, keys, a card, video) and what `result` holds. It
refuses early, with the reason, when the station or the build lacks
something (`dbg.available`, `keys is None`, `station.has`); it puts back
what it changed (knobs in a `finally`, files it posted, settings, the board
at the menu), or its docstring names what stays; it sets `result` and
writes only under `artifacts`. `selftest/test_scripts.py` checks the name,
the date, that the script compiles and that its `treadmill` imports
resolve; scripts are not run against the fake.

**A script becomes a test** when only hardware shows the failure, it has
caught one on some firmware or measures a margin a change could eat, it
needs to run again on each firmware change or on several machines, and its
verdict is a number or an exact compare (one that needs eyes or ears stays a
script, or becomes an attended test). A BACKLOG item proposes it. The test
uses fixtures and the public library API only. On the bench it passes on the
firmware that fixed the issue and fails on a build with the defect, where one
exists, on one station per machine class its docstring claims. The commit
names the stations by model and registry id and the firmware that passed and
failed. The script stays as the record.

**Upkeep.** When a library change breaks a script's import, the same commit
fixes the script if its question is still open, or deletes it if it was
promoted or answered. Goldens are refreshed from a bench read, never by
hand. A flaky test is not retried or quietly skipped: a machine's variance
becomes a rate with a threshold, a harness fault a BACKLOG item. No port
name, host, IP address, chip id or pet name goes into a committed file
(`selftest/test_scripts.py` checks for ports and addresses).

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

`treadmill sweep p --station <id> --fw ref=main --define Dbg_SerTimChg --both`
finds a bus timing knob's margin each way: the knob (`p`, `tp` or
`nS_PLAprop`; any of the firmware's `t` knobs) is probed once at its default,
then walks down to its lower limit and up to its upper one in `--step` steps
until a probe fails, and each edge is bisected down to `--fine`. Without
`--both` it walks one way, toward `--to` (default: the upper limit). `--scan
LO:HI` probes every step of a range instead, with no bisection. `--passes N`
repeats the search or the scan, alternating which edge or which end goes
first; a value counts as clean only if it was clean in every pass, and an
edge that moves between passes is marked drifting. The defaults are restored
whatever happened, and the result is each edge (how far it is clean, where
it fails, which probe failed there) and every point of every pass, as text
or `--json` and as `margins.json` in the run's folder.

`treadmill/knobs.py` is the knob table: each knob's letter, digits, unit,
the bus path it times, the images and boards that take it (`tx` and `tn`,
the Kernal Replace pair, on a TR+ build only), its defaults per standard,
and the limits a sweep keeps to, which follow the other knobs: `nS_PLAprop`
goes no lower than `nS_RWnReady` (both waits count from the cycle's start, so
below it the knob changes nothing), and `nS_DMASetup` moves with
`nS_DMADataHold` and `nS_DMADataSetup` the other way, so the release and the
latch, as the board's defaults put them, stay where they are (`--free` moves
it alone). A spec past the limits is refused. `KSenseMode` is a mode, set
with `tx`, not swept, and `td` leaves it where it was.

The TeensyROM cannot see its own read errors, so the probes are the C64's,
in `treadmill/probes/`: `idread` launches a one-bank EasyFlash cartridge
that reads its own ROM and the EasyFlash IO2 page in a loop and counts wrong
answers per path, read back by DMA over `--dwell` seconds per sample (a TR+;
a launched PRG would run under the None IO handler, with nothing answering
at IO1); `menu` resets, lets the menu boot and compares it with its golden
(any TR, by video). `--probe` repeats for several probes at each point: the
point fails if any fails, and the record keeps each one's counts. While a
knob that times DMA (or `nS_MaxAdj`, which moves every cycle's start) is off
its default, a probe reads its counters by DMA only with the defaults back
in place; and while a DMA knob is off, nothing resets the C64 (a C128 can
hang on DMA right after a reset), so the `menu` probe is refused for those
knobs. `--repeat N` takes several samples a point, so an intermittent edge is
a rate, not a coin toss.

`nS_PLAprop` has an edge each way. Raised, the data leaves too late, and IO
reads fail before ROM reads, since the board drives them only after a call
into the IO handler; lowered, the select lines are sampled before the
slowest has asserted. `tests/sweep/test_tp_margin.py` runs both sweeps as a
regression guard: the default must be clean, and a station's floor fails the
test when reads go wrong short of it.

A station's floors are in its `[margins]` table, per edge, as knob values:
`nS_PLAprop = { up = 200, down = 140 }` means the up edge must be clean at
least to 200 and the down edge at least to 140. `treadmill margins table`
(or `csv`) lists the records under `--reports`, a line each, and `treadmill
margins suggest --station <id>` prints the floors the station's last
`--last` records of each knob support, one coarse step inside the worst edge
each way and never past the default, to copy into its file. A test compares
each edge with the newest earlier record of the same station, knob, edge and
probes on another build: a shrink of more than twice `--fine` (10 ns at
least) is a warning, not a failure, since machines drift.

## Manufacturing test

`treadmill mfg --station <id>` tests new TR+ boards one after another on a
station's rig. The station gives the rig and the machine; each board is
found by the serial string it enumerates with. A TR+ takes its power from
the machine, so switching the machine on is what starts a board. A
TeensyROM already on USB when the session starts is skipped for the whole
session by its chip id, even when it leaves USB and comes back: it belongs
to another machine on the bench. A board under test switched on before the
session is skipped with them, until the session is restarted with it off;
with a keyboard board named in the station, switching the test C64 off and
on is enough, since a skipped board that leaves USB with the keyboard board
was in that C64. The startup message lists only the TeensyROMs. The steps
run in order, launching the two built-in tests through the menu by keys, as
an operator does (a remote launch changes how the firmware lays out RAM2,
and with it the REU's size):

1. Power-up: the main image boots to the menu, and the board's banner shows
   the C64 in the station's video standard (`region`). The rig's
   calibration, glyph bank and SID clock are for that standard, so a C64 in
   the other one, a Kawari left switched by other work on the bench, would
   misread every video check. It is not the board's fault: the C64 is set
   to the station's standard, and the same board starts over under its
   number. No CSV row is written for the attempt; the board's `result.json`
   lists it under `restarts`. When the station's keyboard board has
   `kawari` and `reset` fitted, its `V` command does it with the C64 held
   in reset, and the board never leaves USB. It sets J1 to the other
   position from where the keyboard board reports it, so it works whatever
   the Kawari's saved model is; a `kawari_open` that disagrees is named,
   with the value to set. It gets one try a board: a C64 still in the wrong
   standard after it, or a keyboard board that does not answer (with a
   warning saying why), goes to the operator, who is asked to power off,
   set the Kawari and power on. A `V` setting does not outlast a power
   cycle: the keyboard board powers down with the C64, and J1 goes back to
   open.
2. Firmware: the version and build stamp equal the factory hex's, with no
   tag and no `*`.
3. External Ports: first the TR+ must have the USB thumb drive mounted
   (its root lists), within 10 s: the test itself gives the drive 1.5 s,
   which a stick still coming up after power-on misses. Then the built-in
   test passes after the operator presses MENU
   and ALT. Its `Hello from TR at:` line agrees on serial and on screen, and
   is within `time_tolerance_s` (5 s) of UTC when it arrived (a new board's
   timezone setting is UTC). UTC is this PC's clock corrected by its offset
   from `ntp_server`, measured before each board; with no answer the PC's
   clock is used as it is, with a warning. The firmware keeps only the
   whole seconds of its time fetch and prints whole seconds, so a good
   board reads 0 to 2 s behind. A clock off by whole half hours
   on a board whose settings were not reset is reported as its timezone
   setting. A clock that fails after the screen showed the time fetch
   failing ("NTP Response timeout!" or "RTC not updated") is reported as
   that failure: with no RTC battery on the bench, such a clock counts
   from power-on. Then the C64's bottom row asks whether the Ethernet port's
   green LED and the TR LED are on, and the operator answers Y or N on the
   C64's keyboard (`led_timeout_s`, 120 s, for each). The test's keys come
   through the KERNAL's GETIN vector, which mfg points at a stub that never
   returns one as soon as the Hello line arrives: the test stays at its
   prompt and the answers wait in the keyboard buffer. A key in the moment
   before that, or the TR's MENU button, ends the test, and the step fails
   saying so; the console asks the operator to leave the C64's keyboard
   alone until the questions. The board's part, up to the button prompt,
   gets 45 s (its slowest fail path, two DHCP timeouts, ends in a fail line
   by then), counted while video reads frames that show no prompt; with
   every frame lost, `epc_timeout_s` is added. `epc_timeout_s` (180 s)
   starts when video shows the prompt. A C64 whose jiffy clock stands
   still for 1 s under the LED questions fails then.
4. External reset: the station's keyboard board pulses the C64's /RESET
   when its reset output is fitted; otherwise, or when it does not answer
   (with a warning saying why), the operator presses the C64's reset
   button. The TR+ must log `Button detected` and bring the menu back. The
   C64 resets whether the TR+ senses it or not: its screen changing with
   no `Button detected` 2 s later fails the step then, not at
   `reset_timeout_s`.
5. Expansion Port: the built-in test passes `xpt_passes` times. A pass that
   stops at its address tests, which report on screen only, fails on the
   screen's reason as soon as its prompt shows. A C64 that stops mid-test
   fails the pass as serial shows it: the TR+'s `Sout Timeout!` (a message
   the C64 did not take), or one line 50 times in a row (a loop, such as
   `?Stat: c3` from a C64 writing a status the TR+ does not know, named in
   the reason). A TR+ in such a loop never goes quiet for a DMA command, so
   its C64 is reset and the failure marked on the menu (`reset_before_mark`
   in `result.json`). Each pass's whole screen is held to the test's
   (`tests/golden/mfg/expansion_port_pass.screen.txt`, the clock a
   wildcard): a wrong character fails the pass, naming the data bit, and a
   second DMA read and video say whether it is in the C64's screen memory
   (the C64 read the TR+'s message text wrong) or in the TR+'s first read
   only. Either way the bus dropped a bit, so it is never retried.
6. SD root: the card's root matches the master image's snapshot.
7. REU: hot key 2 launches the REU checker with a 512K REU. Its text is
   yellow and green on grey, which video cannot read reliably, so the
   verdict is its colors. Once it is up (its border green) a red cell in its
   bank map (one cell per 64 KB) fails at once; otherwise, when the map has
   held still for 2 s, it must show exactly eight green cells from the
   start and a green "THE REU IS WORKING!" line. The last frame and the map
   as read are kept (`reu.png`, `reu.colors.txt`).

The first failure halts the board. Its screen turns red and inverted, the
bottom row names the step and the reason, and the C64 is paused so nothing
redraws it. The firmware takes no memory commands while the REU checker's
REU is set up, so an REU failure is marked on the menu after a reset. A
board that misses a reply is not left at the link's "no reply": the reason
says whether the TR+ left USB, restarted, answers again, or stays on USB
with its firmware silent (and whether the C64 still shows the menu), and
one that cannot answer is not marked. A passing board gets a green summary
instead. Before the
pause the SID plays four chords, rising on a pass and falling on a fail,
then falls silent; the TR's LED flashes while the C64 is paused. Each
board adds:

- a row to the profile's `log` (`data/mfg/boards.csv`): start time, chip id,
  firmware, result, the failed step and reason, every step's result and the
  LED answers;
- a folder under `reports/<run>/boards/` with its serial log, every screen
  read, on a failure the capture's last 3 s before it was found
  (`frames.png`), and `result.json`, whose `waits` holds the seconds each
  wait took (the menu's return, each pass's pattern lines, the operator's
  buttons), the measurements a timeout is set from.

The console then asks for the power-off, waits for the board to leave USB,
and starts the next one. Ctrl+C ends the session; `--boards N` stops after N.

Setting it up, once:

1. Copy `data/mfg/trplus.example.toml` to `data/mfg/trplus.toml` and point
   `factory_hex` at the factory hex.
2. Run `treadmill mfg --snapshot-sd <master image folder>`, which writes
   `sd_root.json` beside the profile.
3. If the test machine has a keyboard board, name it in the station's
   `[keyboard]` table ("The keyboard board" above); without one the
   operator presses reset and sets the standard.

## Scripting

`treadmill exec --station <id> script.py [-- args]` opens the station the way
a test session does (the firmware under test prepared, the rig held, the
board at the menu unless `--as-is`) and runs the script as `__main__` with
these names as globals: `session`, `station`, `board`, `link`, `screen`,
`menu`, `keys`, `kbd` (the keyboard board, or None), `dbg`, `video`,
`firmware`, `artifacts` (a folder of the run to write into), `run_dir`,
`log`. A `result` the script leaves is reported (`--json` returns it), and
what the board and the keyboard board said meanwhile lands in the run's
folder like a test's. `treadmill shell --station <id>` gives the same names
at a prompt. `scripts/example.py` is a ten-line example.

`--no-link` (on `exec` and `shell`) opens the station without its
TeensyROM: the rig held, the capture and the keyboard board open. `board`,
`link`, `menu`, `dbg` and `firmware` are None, `screen` reads video with
the stored calibration, and `keys` presses through the keyboard board. It
takes no firmware options. It is for a machine whose TeensyROM does not
answer on USB, such as a TR+ running an extension host, and for the
keyboard board's own script.

From Python, `treadmill.session.open_station("<id>")` returns the same
session (`board`, `link`, `screen_channel()`, `menu()`, `keys()`, `dbg()`,
`kbd`), `open_linkless("<id>")` the one `--no-link` opens, and
`treadmill.driver.run_regions(RunOptions(...))` is what `treadmill run` does
(`run_tests` for one run; `region="both"` needs `run_regions`).

## Reports

Every run leaves `reports/<timestamp>/`: `run.json` (station with its
video standard and keyboard board, board, the
firmware spec with its commit, dirty hash, defines, tag, hex and stamp,
capabilities, capture mode, calibration residual, the suite's own commit, the
options, every test's outcome, duration and message, the summary and exit
status), `report.html` rendered from it with each failure's serial tail,
golden diff and frame strip, and one folder per test with `serial.log`,
`events.json`, `kbd.log` (the keyboard board's lines both ways) when the
station has one open, every screen a golden check read and the diff when it
differed, and `xcheck.json` when video was cross-checked against DMA.

## Conventions

- Every command but the shell takes `--json`, and every one exits non-zero on failure; the codes are above.
- Every run writes `reports/<timestamp>/run.json` and `report.html`; `data/results.sqlite` comes with the nightly runs.
- The suite refuses to start when another program holds the station's rig or serial port.
- DMA commands are never sent to a plain TR; debug letters are never sent to a build without their define; the port search sends TR commands only to the board it looks for or to an unnamed port, never to the keyboard board.
- Nothing is flashed or driven without the person's go-ahead; the station files that name boards stay private.
