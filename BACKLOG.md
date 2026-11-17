# Backlog

Open work on the suite that is not in a plan step: defects found in review
and deferred, design choices to make later, improvements with a known
cost. One item per line, numbered once and never renumbered; new items
go at the end of their area. A closed item keeps its number and gains the
date and the commit that closed it. "The backlog" in conversation means
the open items here.

Importance: **high** blocks a plan step or hides a failure; **medium** is
a real gap that bites in a case the suite does not run yet; **low** is a
cost or a wart with a known, small fix.

## Board and link

1. **medium** Liveness is blind to the capture's no-signal drops.
   `classify()` in `treadmill/observe/liveness.py` decides NO_SIGNAL from
   blank frames and a learned pattern, while `CvCapture` already drops the
   device's no-signal frames and counts them (`no_signal_frames`,
   `last_no_signal` in `treadmill/observe/capture.py`); with a lost signal
   the ring keeps the last real frames and the verdict is STATIC. Fix:
   hand the capture's drop count and time to `classify()`.
2. **closed 2026-11-09, with the Phase 1 clean-up.** A reconnect that found
   no board escaped the recovery ladder: `find_port()` raises `NotFound`
   inside `Board._reconnect()`, and `ensure_menu()` in `treadmill/board.py`
   did not catch it, so a test errored with a traceback instead of the
   station marking itself wedged. The ladder now counts it as a failed rung.
3. **closed 2026-11-07, with the driver CLI.** The recovery hooks belong in
   `Board.open`. The pytest plugin's `board` fixture attached `reconnect_hook`
   and `reboot_hook`; `open_station()` in `treadmill/session.py` attached
   neither, so `shell`, `exec` and `screen` had no rung past a reset and
   wedged where a pytest run recovers. `Board.open` now attaches both
   (`attach_recovery`), the reboot rung when the `FirmwareRun` it is given
   knows the hex and the checkout, and `open_station()` takes that run.
4. **medium, before soak runs** Every command drains for `DRAIN_S` (300 ms)
   before sending and most waits `TEXT_REPLY`'s 300 ms idle after, in
   `treadmill/link/trlink.py`; a ping costs about 600 ms on a quiet link
   and a 200-step menu walk spends two minutes in fixed waits. A design
   choice: drain only when the tap has seen bytes since the last command,
   and end a text reply on its known terminator where one exists. The
   manufacturing test already sends with `Link.drain_when_pending` (a
   command waits only for stray bytes, and refuses a board whose bytes do
   not stop); the rest of the suite keeps the fixed drain until soak runs
   have tried it.
13. **medium, with the keyboard board** No way to drive a board whose
   firmware has no USB, such as a TR+ running an extension host (SIDHost).
   `Harness.__enter__` in `treadmill/driver.py` always goes through
   `Board.open`, which opens the serial link, so `exec --as-is` fails with
   "<port>: Cannot configure port" before the script runs; and `keys_for()` in
   `treadmill/session.py` has only `KernalKeys`, DMA through that same link.
   Pressing X to leave the host, or reading the screen by video, needs a
   link-less open (video and keys only) and the C64-USBKeyboard backend
   (`keys_hw`).
14. **closed 2026-11-11, with the key-buffer and underscore fixes.** Keys
   went to the wrong buffer on a C128 at the TR menu. `Link` picked the
   C128-mode buffer and count ($034A/$D0) from the banner's machine, but
   the TR menu is a C64 cartridge, so a C128 runs it in 64 mode, where
   GETIN reads $0277/$C6, and the keys were dropped (NTSC C128,
   2026-11-10). On a C128 the link now reads the reset vector at $FFFC by
   DMA and uses the C128-mode pair only when it holds the C128 KERNAL's
   $FF3D; anything else gets the C64 pair, as the firmware's bench
   `type.py` does.
15. **closed 2026-11-11, with the key-buffer and underscore fixes.** A file
   name's underscore read as '.'. The firmware shows '_' as PETSCII 164
   (`ASCIItoPETSCII` in `IOH_TeensyROM.c`), screen code $64, which
   `code_as_text()` read as `NO_LETTER`, so `MenuDriver.find()` never
   matched a name such as an HVSC tune's. $64 now reads as '_' in either
   ROM set, and `codes_for_text()` writes '_' as $64; the MCP server's
   table (`tools/mcp/lib/screen.mjs`, PR #86) still reads it as '.'.
16. **low, with the keyboard board** The manufacturing test's external reset
   needs the operator. `OperatorReset` in `treadmill/mfg.py` asks for the
   C64's reset button, because the keyboard board's planned protocol has no
   plain reset pulse: its `V` command resets the C64 only inside a Kawari
   standard switch. Fix: an `R <ms>` command on the same /RESET stage `V`
   uses, and an `ExternalReset` that sends it.
17. **low** A run does not record what the registry knows about its
   machine. `Machine` (`treadmill/station/model.py`) reads the systems.csv
   row's `notes`, but the station block of `run.json`
   (`treadmill/report/artifacts.py`) carries model and region only, so the
   U55 chip and a machine's diagnostic history are not beside its results.
   In the PR #99 sweeps two NTSC C128s were mixed up (which one had the
   confirmed 74F245) until the registry was read by hand. Fix: add the
   registry row's notes to the station block and to `report.html`.
27. **low** The manufacturing test can take another machine's TeensyROM as
   the next board. `RealBench.wait_for_board` in `treadmill/mfg.py` leaves
   out the ports present when the session starts, but drops a port from
   that set once it leaves USB, so a bench TeensyROM that re-enumerates
   mid-session (its machine power-cycled, or a treadmill run reflashing it)
   comes back as a new board: mfg tests the wrong board, and its power-off
   wait hangs. Excluding the chip ids that station files name would also
   refuse a board under test that a station names. Fix, a choice to make:
   remember the session's other boards by chip id rather than by port, or
   ask the operator to confirm a chip id that was on USB at the start.
28. **low** A lost frame during an Expansion Port pass fails a good board.
   `Ctx.video_until` in `treadmill/mfg.py`, which waits for a pass's prompt
   after its serial lines, calls `read_video()` unguarded: a
   `CaptureTimeout` while the C64's half runs fails the step with the
   capture's message, and the fail marking then sends DMA during the test
   of the bus. The External Ports wait and the early-fail watch read
   through `Ctx.video_text()`, which carries on. Fix: the same in
   `video_until`.

## Video

5. **low** Every video read refines the grid. `read_video()` in
   `treadmill/observe/channels.py` decodes, refines (about 160 ms a round,
   up to three rounds while the grid keeps moving) and decodes again on
   each read, so `wait_text()` polls at a few reads a second. Reuse the
   last refined calibration while the frame has not moved, and refine when
   the decode confidence drops.
9. **low** A reversed edge-line glyph reads as a solid block or a space.
   `match()` in `treadmill/observe/glyphs.py` calls a cell solid when its
   interior does not vary, and the interior leaves out the two-pixel margin
   where such a glyph's one dark row lies. On the bench card a directory
   name's first character is screen code $E4 (reversed $64), which video
   read as $A0 on one capture and as a space on the next. No golden holds
   one yet.
10. **medium, with the region switch** Banks and calibrations are keyed by
   machine, not by video standard. `bank_path()` in `treadmill/session.py`
   and `calibration_path()` in `treadmill/observe/calibrate.py` name a file
   per (capture class, machine), so the Kawari station switched to NTSC
   would read with its PAL bank and grid (cells 24.8x25.2 px against
   25.7x21.0). Key both by the board's video line as well when test 18
   switches standards.
18. **medium, with item 20** The screen-noise probe has no baseline per
   capture. The probe (pixels inside the text area that change by more
   than 48 of 255 between frames, while a DMA probe runs) lives only in
   `scripts/2026-11-12-dmasetup-sweep.py`; it finds a DMA drive
   that lands in the VIC's half of the cycle, where the data compares stay
   clean (PR #99: the Kawari's early edge sat 30-50 ns above the first
   data error). With no DMA running, the Kawari C64 reads a median of 0
   and C128 #29's capture about 1,500; on C64 #14 the menu reads 600-1,000
   while the REU client's plain screen reads 0, so the baseline depends on
   the capture and on what the screen shows, and one threshold cannot
   serve them all.
   Fix: measure the baseline at the defaults on each run and judge against
   it; use the median for a continuous probe (the REU loop) and the max
   for a burst (`z`).

## Firmware builds

6. **low** The header patch rewrites CRLF as LF. `enable_defines_in_header()`
   in `treadmill/firmware/build.py` reads `TeensyROM.h` with universal
   newlines and writes with `newline=""`, so a CRLF header comes out LF on
   every line and the diff hash covers the whole file. Harmless in a
   worktree that is cleaned on reuse; read and write bytes to fix.
12. **low** `--auto-rebuild` (plan section 2.3) is not built. A selection
   whose tests need different define sets runs once per set by hand; the
   skip in `_capability_gate()` (`treadmill/pytest_plugin.py`) names the
   `--define` flags the skipped test needs. Build it when a nightly run
   mixes debug suites with the baseline.
19. **low** Debug defines need a `ref=` build to be usable. `build()` in
   `treadmill/firmware/build.py` refuses `--define` with `path=`, since it
   would patch that checkout in place, and `DebugCommands.available()`
   (`treadmill/link/dbgcmds.py`) trusts only the defines the run compiled
   in, so a board flashed from a hand-patched `path=` build refuses `t`
   commands although its banner reads `v0.9-dma.tim*`. Worked around in the
   PR #99 soak with `--fw none --fw-defines`. Fix: build a `path=` checkout
   with defines from a disposable copy, and when nothing was built, take
   the defines from the banner's tag marks (`DEBUG_MARKS` in
   `treadmill/firmware/spec.py`).

## Tests and fixtures

7. **closed 2026-11-08, with the first bench run.** The path row of a
   directory listing was open in the goldens. The board shows `//Games`:
   the firmware appends each listed directory name, slash included, to a
   path that starts as "/" (`shown_path()` in `treadmill/c64/menu.py`), and
   the goldens and the card listing now pin it.
8. **medium, with test 8** Only generated SD fixtures exist. `type_set()` in
   `treadmill/fixtures/sdcard.py` makes every byte itself; the private form
   the plan names (`data/sdcard/<class>/manifest.json` with a sha256 and a
   local path per file, `needs_asset`) is not built, so the cartridge images
   of test 8 have nowhere to be declared.
11. **closed 2026-11-08, with the cartridge probe.** The idread probe read
   nothing on a board: a launched PRG runs under the `None` IO handler, so
   its reads of $DE05/$DE06 saw the open bus. `idread.a` is now a one-bank
   EasyFlash cartridge that counts wrong reads of its own ROM and of the
   EasyFlash IO2 page apart, since the two paths have different edges, and
   the margin test sweeps both ways.
20. **medium** The DMA write knobs have no sweep command. `treadmill sweep`
   (`treadmill/sweeps.py`) walks one knob at a time against a read probe,
   so the PR #99 sweeps of `nS_DMASetup` ran from
   `scripts/2026-11-12-dmasetup-sweep.py`: `nS_DMADataHold` and
   `nS_DMADataSetup` move with it to hold the board's own release and
   latch sums (884/844 PAL, 854/834 NTSC on #99's values), the `z` pattern
   test and the REU loop are the probes, and the noise probe (item 18)
   runs beside each. Factory NTSC C128s drift by up to 100x between passes
   at one value, so a pass up and a pass down both count. Fix: a `sweep e`
   with held sums, the three probes, and `--passes` with alternating
   direction.
21. **medium, before soak runs** No DMA soak runs under the suite.
   `tools/bench/reuspans.py` (the REU loop beside host write jobs, 600 s a
   phase) is the quickest check of a DMA timing change, but the bench
   library opens the port with `termios`, so it does not run on Windows,
   and `Link` (`treadmill/link/trlink.py`) has no WriteC64Spans to run its
   jobs. The PR #99 soak ran through a pyserial shim. The REU client's
   16-bit counters wrapped once in each 600 s phase (about 67,000 rounds),
   which reuspans prints modulo 65,536. Fix: a soak test that samples the
   counters often enough to unwrap them, and WriteC64Spans in `Link`.
22. **medium, with item 20** No view compares a knob's margins across
   machines. Each sweep stands alone: `treadmill sweep` writes `sweep.json`
   (`treadmill/cli.py`) and the DMA sweep script a text file, and
   `render_report()` (`treadmill/report/html.py`) draws one run. The PR #99
   comparisons (F245 against LS245, Kawari against a real VIC-II, all of
   our C64 numbers coming from one TR+ board) were charted by hand.
   Fix: a margin map built from the stored sweeps, one chart per knob, a
   row per machine, board and probe, clean windows as bars and the default
   (plus any value under review) as vertical lines, so a change that eats
   one machine's margin shows before it fails. Needs item 20's sweeps in
   the same results format as `sweep.json`.
23. **low, with item 20** The DMA write window is swept along one line.
   Holding the release and latch sums walks `nS_DMASetup` against
   `nS_DMADataHold` on a single diagonal, while the safe region is two-
   dimensional. Fix: a setup by data hold grid sweep with a heatmap in
   `report.html`, the chart an auto-calibration would choose its point
   from.
24. **medium** MinimalBoot's read errors under USB traffic have no
   regression test. `scripts/2026-11-15-minimalboot-usb.py` hands an
   EasyFlash image with a bank-64 chip to MinimalBoot, asks it for its
   version as fast as it answers and reads idread's counters by video: on
   the Kawari C64 (registry 002b), NTSC, main before PR #103 gave 2,963
   wrong ROM reads of 2.2 million and 1,320 wrong IO2 reads, the PR none.
   Nothing reruns it when the firmware changes. Fix:
   `tests/launch/test_minimalboot_handoff.py` with 30 s of traffic, the
   script's `board._reconnect()` made public, a fake path or a skip on the
   fake, and a bench run that fails on the commit before PR #103 and passes
   after it.
25. **medium** The hardware tests have passed on one machine only. Every
   passing run of `tests/` is on the Kawari C64 (registry 002b); the C128
   runs were of a test since dropped, and all the multi-machine numbers
   (the PR #99 sweeps on six stations) came from scripts. Detection and
   timing differ between C64 and C128 and between PAL and NTSC, which is
   what the reset, launch, menu, sweep and handler tests are there for.
   Fix: run the whole suite once on every station (README, "Which tests
   run where"), and give each station a `[margins]` floor for `nS_PLAprop`
   from its own sweep.
26. **medium, with item 20** The DMA setup margin has no regression test.
   `tests/sweep/test_tp_margin.py` guards `nS_PLAprop`, but nothing guards
   `nS_DMASetup` once PR #99's sweeps are done. Fix, once `sweep e`
   exists: `tests/sweep/test_dma_setup_margin.py`, the defaults clean on
   the `z` and REU probes and a per-station `[margins]` floor, on every
   TR+ machine class (factory NTSC C128s drift the most, item 20).
