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
   have tried it. Its text replies still end on the idle: about a dozen a
   board (banner, ping, reset), about 3 s, where the banner reply could end
   on its `Boot:` line and the ping reply on its `ready!` line.
13. **closed 2026-11-19, with the link-less open and the keyboard board's
   keys.** No way to drive a board whose
   firmware has no USB, such as a TR+ running an extension host (SIDHost).
   `Harness.__enter__` in `treadmill/driver.py` always went through
   `Board.open`, which opens the serial link, so `exec --as-is` failed with
   "<port>: Cannot configure port" before the script ran; and `keys_for()` in
   `treadmill/session.py` had only `KernalKeys`, DMA through that same link.
   `exec` and `shell` now take `--no-link` (video and the keyboard board,
   no TR), and `keys_for()` falls back to `HwKeys` on the keyboard board's
   matrix.
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
16. **closed 2026-11-19, with the keyboard board's reset.** The
   manufacturing test's external reset needed the operator. `OperatorReset`
   in `treadmill/mfg.py` asked for the C64's reset button, because the
   keyboard board's planned protocol had no plain reset pulse. Protocol 1
   has `R`, and `KeyboardReset` sends it through the keyboard board the
   board under test opened, falling back to the operator, with the reason,
   when that board is missing or has no reset fitted.
17. **low** A run does not record what the registry knows about its
   machine. `Machine` (`treadmill/station/model.py`) reads the systems.csv
   row's `notes`, but the station block of `run.json`
   (`treadmill/report/artifacts.py`) carries model and region only, so the
   U55 chip and a machine's diagnostic history are not beside its results.
   In the PR #99 sweeps two NTSC C128s were mixed up (which one had the
   confirmed 74F245) until the registry was read by hand. Fix: add the
   registry row's notes to the station block and to `report.html`.
27. **closed 2026-11-17, with the boards on USB at the start skipped by
   chip id.** The manufacturing test could take another machine's
   TeensyROM as the next board. `RealBench.wait_for_board` in
   `treadmill/mfg.py` left out the ports present when the session started,
   but dropped a port from that set once it left USB, so a bench TeensyROM
   that re-enumerated mid-session (its machine power-cycled, or a treadmill
   run reflashing it) came back as a new board: mfg tested the wrong board,
   and its power-off wait hung. The boards on USB at the start are now
   remembered by the chip id their serial string names, with no port
   opened, and none is taken that session; a board under test switched on
   early is skipped with them until the session is restarted.
28. **closed 2026-11-17, with the guarded video_until.** A lost frame
   during an Expansion Port pass failed a good board. `Ctx.video_until` in
   `treadmill/mfg.py`, which waits for a pass's prompt after its serial
   lines, called `read_video()` unguarded: a `CaptureTimeout` while the
   C64's half ran failed the step with the capture's message, and the fail
   marking then sent DMA during the test of the bus. A read that raises
   now counts as not matching, as in `Ctx.video_text()`.
29. **low** The manufacturing test's REU step waits out a 2 s settle.
   `watch_reu_checker` in `treadmill/mfg.py` passes once the checker's bank
   map has stayed unchanged for `REU_SETTLE_S` (2.0 s), but in the 11 bench
   runs of 2026-11-17 the map stopped changing 1.68-1.76 s after the watch
   began (result.json `waits.reu_last_change`), so each board spends the
   whole 2 s waiting. A shorter settle, set from more boards'
   `reu_last_change`, saves about 1.5 s a board; a checker that changes its
   map again after a pause would then be judged early, so measure first.
33. **low** The Kawari switch is two algorithms. `set_standard()` in
   `treadmill/station/kawari.py` (`--region`) maps the standard to J1
   through the station's `kawari_open` and flips once on a wrong result,
   while `KeyboardSwitch` in `treadmill/mfg.py` reads J1 from the keyboard
   board's `?` and sets the other position. On a Kawari whose saved model
   changed, `--region` spends a wasted `V` (about 5 s) each switch, and a
   station without `kawari_open` has no `region_switch` though mfg switches
   the same hardware. Fix: one helper in `kawari.py` that reads J1 and
   flips it from the standard the banner shows, used by both, with
   `kawari_open` dropped from the `region_switch` rule.
42. **high** The reboot rung strands the board in the Teensy bootloader.
   `Board._rung_reboot()` (`treadmill/board.py`) calls `relaunch()`
   (`treadmill/firmware/flash.py`), which opens the port at 134 baud and so
   sends the Teensy into the bootloader. Without `teensy_loader_cli` on the
   host (none on the Windows bench) the boot-only step is skipped, and
   the forced `flash()` passes `--uid`, which `flash-firmware.mjs` refuses
   for a board in the bootloader, since it cannot read the chip id there
   ("No attached board has chip ID ..."). Every later test then errors in
   its setup. It ended two runs on 2026-11-21, each after the laptop's
   Modern Standby dropped the board: the C64C #24's, before the screen-on
   hold existed, and the C128 Marco1's second run (46), which left 16 of
   21 tests unrun. On the one board
   checked, HalfKay's USB serial number is
   the chip id divided by ten, in hex. Fix: when the only board in the
   bootloader (16C0:0478) has that serial, flash it without `--uid`, which
   the script allows for a single board; and try a reconnect before the
   rung, since a board that rebooted on its own runs again by itself.
47. **low** The manufacturing test leaves the board swap's power-off to the
   operator. `treadmill mfg` (`treadmill/mfg.py`) asks for the C64 to be
   powered off before each board comes out (`POWER_OFF`) and on again once
   the next is in (`CONNECT`). On a station with `power`, the mains relay
   could hold the machine off itself for the swap, which would also make
   sure no board goes in live. Fix: `Power.off()` before the `POWER_OFF`
   prompt and `on()` after `CONNECT`'s, when the station has `power`. The
   relay restores power by itself after its 10-minute `OFF` limit, so a
   longer swap needs the `OFF` renewed.

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
10. **closed 2026-11-19, with the region switch.** Banks and calibrations
   were keyed by machine, not by video standard. `bank_path()` in
   `treadmill/session.py` and `calibration_path()` in
   `treadmill/observe/calibrate.py` named a file per (capture class,
   machine), so the Kawari station switched to NTSC would have read with
   its PAL bank and grid (cells 24.8x25.2 px against 25.7x21.0). Both now
   add the standard from the board's banner
   (`<class>__<machine>__<PAL|NTSC>`), with the machine's file as the
   fallback where it cannot be the other standard's (`unkeyed_fits()`).
18. **closed 2026-11-19, with the DMA probes and the margin map.** The
   screen-noise probe had no baseline per capture. The `noise` probe
   (`treadmill/probes/noise.py`) now measures one sample of the probe it
   runs beside at the defaults, before the first point and after the last,
   and fails a point whose worst frame pair is three times that and 100
   pixels above it. It judges the max for every workload: over the PR #99
   runs the median missed C64 #14's REU failure (11 pixels against 0).
32. **medium** A link-less session reads with no standard's files.
   `open_linkless()` in `treadmill/session.py` has no TR banner, so the
   station's standard stays None and `find_calibration()`/`find_bank()`
   look only for the names from before files were kept per standard. Once
   a Kawari station has only its `__PAL` and `__NTSC` files, `exec
   --no-link` logs "no stored calibration fits" and reads no screen; on the
   Kawari C64 (registry 002b) today it reads with the old PAL calibration
   in either standard. Needs a way to tell the standard without the TR: a
   fixed-standard machine's registry region, else the keyboard board's J1
   with `kawari_open`, or the picture's geometry against both stored
   calibrations.
41. **medium** A machine read with the class glyph bank can take a
   subfolder for the drive's root. C64 #14 (registry 14, real VIC-II) has
   no bank of its own, and `svideo-generic.npz` reads the up-dir entry's
   arrows as `??` there (and the letter O as `?`), so `Listing.is_root` in
   `treadmill/c64/menu.py` (no `<Up Dir>` first) is true inside
   `//treadmill`. `ensure_root()` then stays put, the Settings driver's
   `open("/treadmill")` sees no new listing, and Kernal Replace's file is
   neither set nor put back ("the listing of /treadmill did not appear",
   three times on 2026-11-21, each time the run before had left the menu in
   that folder). #14 was recalibrated and harvested that day (colors 15,
   14, 13 and 7: the menu's) and now reads `<Up Dir>`; a bank harvested
   over the old calibration read L as E and H as F low on the screen
   instead. Left to do: judge the root from the path row (`/` alone) as
   well as the up-dir entry, and calibrate and harvest every station
   before its first run.
48. **closed 2026-11-22, with the capture opened by name.** The rig opened
   its capture by DirectShow index, and the list reorders when devices
   re-enumerate. On 2026-11-22 index 1 had become the phone's virtual
   camera and the capture dongle had moved to 2, so the C128 Marco1's
   margin run read a black picture for 100 minutes with no read errors:
   seven video tests failed, the two vicrom sweeps passed on black
   (baseline 0, against about 118,000 on the dongle), and the noise probe
   saw nothing beside the DMA sweeps. `[capture] device` may now be the
   DirectShow name, looked up at each open (`treadmill/observe/dshow.py`,
   `CvCapture._index()`), and a session whose board is at its menu drops
   video when the capture shows only black for 2 s (`check_picture()`,
   `picture_ok()` in `treadmill/session.py`). At another moment the same
   camera showed a lock icon, which the black check lets through: the name
   is what keeps another camera out.

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
20. **closed 2026-11-19, with the DMA probes and the margin map.** The
   DMA write knobs had no sweep command. `treadmill sweep e` holds the
   release and latch sums (`--free` drops them), `--probe` takes
   `zpattern`, `reuloop`, `reuloop-sprites`, `cpuwatch` and `noise`, and
   `--passes` alternates the order.
21. **medium, before soak runs** No DMA soak runs under the suite.
   `tools/bench/reuspans.py` (the REU loop beside host write jobs, 600 s a
   phase) is the quickest check of a DMA timing change, but the bench
   library opens the port with `termios`, so it does not run on Windows,
   and `Link` (`treadmill/link/trlink.py`) has no WriteC64Spans to run its
   jobs. The PR #99 soak ran through a pyserial shim. The REU client's
   16-bit counters wrapped once in each 600 s phase (about 67,000 rounds),
   which reuspans prints modulo 65,536. Fix: a soak test that samples the
   counters often enough to unwrap them, and WriteC64Spans in `Link`.
22. **closed 2026-11-19, with the DMA probes and the margin map.** No
   view compared a knob's margins across machines. `treadmill margins map`
   draws every stored `margins.json` (`treadmill/report/margin_map.py`),
   `--mermaid` gives the PR form, and `treadmill margins import` brings the
   PR #99 script's runs onto the map.
23. **closed 2026-11-19, with the DMA probes and the margin map.** The
   DMA write window was swept along one line. `--scan LO:HI --grid LO:HI`
   maps `nS_DMASetup` by `nS_DMADataHold` with the latch held, drawn as a
   heatmap on the margin map rather than in `report.html`.
24. **closed 2026-11-20, with the MinimalBoot margins.** MinimalBoot's read
   errors under USB traffic had no regression test.
   `tests/sweep/test_minimalboot_margins.py` hands idread to MinimalBoot as
   the script did (`Board.reconnect_after_drop`; the fake re-enumerates
   too), checks every knob's default under USB load and sweeps `nS_MaxAdj`
   under it. The bench run that fails on the commit before PR #103 is item
   25's.
25. **medium** The hardware tests have passed on one machine only. Every
   passing run of `tests/` is on the Kawari C64 (registry 002b); the C128
   runs were of a test since dropped, and all the multi-machine numbers
   (the PR #99 sweeps on six stations) came from scripts. Detection and
   timing differ between C64 and C128 and between PAL and NTSC, which is
   what the reset, launch, menu, sweep and handler tests are there for.
   Fix: run the whole suite once on every station (README, "Which tests
   run where"), then the margin run on each (`treadmill run --station <id>
   --fw ref=main --define Dbg_SerTimChg --define Dbg_SerDMA --tests
   tests/sweep`, about 90 minutes a TR+ station), and give each station its
   `[margins]` floors from `treadmill margins suggest`. Prove the margin
   tests on the bench where a known-bad build exists: `nS_PLAprop`'s floor
   on NTSC against the commit before `UpdateItemTypeReg`, `nS_DMASetup` on
   C128 #29 against PR #99's 414 build, and MinimalBoot under load against
   the commit before PR #103.
26. **closed 2026-11-19, with the DMA margin test.** The DMA setup margin
   had no regression test. `tests/sweep/test_dma_margins.py` sweeps
   `nS_DMASetup` with its sums held, and the other four DMA knobs, both
   ways on every TR+ station, three passes on a C128, against the station's
   floors.
30. **closed 2026-11-19, with the screen teardown's getattr.** A skip in
   setup after `screen` was reported as an error. The `screen` fixture's
   teardown (`treadmill/pytest_plugin.py`) read `request.node.rep_failed`,
   which `pytest_runtest_makereport` sets only for the call phase.
   `test_card_type_set` (`tests/menu/test_item_types.py`) sets up `menu`
   and `screen` before `sd_fixtures`, whose `sd_card` skips on a station
   with no SD card; there was no call phase, so the teardown raised
   AttributeError and the run failed on a skip. It now reads the attribute
   with `getattr`, and `selftest/test_plugin.py` holds the case.
31. **low** The TR BASIC test needs a person. `tests/handlers/test_tr_basic.py`
   skips without `--attended`, because someone types SYS 49152 at READY
   and later presses the TeensyROM menu button. On a station with `keys_hw`
   the keyboard board can type the SYS (`kbd.type`), and with `tr_button`
   (its `menu` output; the Kawari C64's board has none fitted) it can press
   the button (`kbd.button`). Fix: use each when the station has it, and keep
   `--attended` for what is left.
34. **low** The region test's switch back can hide its failure.
   `tests/keyboard/test_region_switch.py` switches back with
   `set_standard()` in a `finally`; if that raises, its error replaces the
   test's own assertion, and the rest of the run reads the other standard's
   picture with the session's calibration and bank. Fix: catch the switch
   back's error and report both, and mark the station so later video tests
   skip instead of misreading.
35. **low** The Special IO handlers' own IO reads have no margin probe.
   idread counts the EasyFlash handler's IO2 page; the REU's registers,
   SwiftLink's and the other Special IO handlers answer through their own
   code after `nS_PLAprop`, each a different time into the cycle. The
   firmware's `tools/bench/regsweep.py` (with `regread.a`) reads the chosen
   handler's registers back in a loop while it steps `nS_PLAprop`. Fix: a
   `regread` probe in `treadmill/probes/` on that program, the handler set
   through the Settings driver, swept in `test_cpu_margins.py` for each
   handler a station's machine uses.
36. **low** MinimalBoot's DMA assert time has no knob. The EasyFlash
   bank-swap pause MinimalBoot runs for large images asserts /DMA at the
   compiled `nS_DMAAssert`, and MinimalBoot's `t` takes no `ta`
   (`Min_SerUSBIO.ino`), so no sweep can reach it. Fix: a `ta` in
   MinimalBoot's `t`, which is a firmware change and the maintainer's
   call; then a swap-pause probe under MinimalBoot.
37. **low** MinimalBoot's `nS_VICStart` is not swept. MinimalBoot's `t`
   takes `tv`, but vicrom is an Ultimax image the main image serves itself,
   so `tests/sweep/test_vic_margins.py` times the main image's VIC cycles
   only. Fix: hand off to MinimalBoot first (idread with its bank-64 chip),
   then launch vicrom from MinimalBoot, which loads 8K-Hi images itself
   (`Min_DriveDirLoad.ino`), and sweep `tv` there with the vicrom probe.
38. **low** `--estimate` counts the tests `-m` and `-k` deselect.
   `pytest_collection_modifyitems()` in `treadmill/pytest_plugin.py` adds up
   the declared durations and clears the list before pytest's own `-m` and
   `-k` deselection runs, so `treadmill run --tests tests/sweep --estimate --
   -m "not alike"` reports 21 tests and about 144 minutes for a selection of
   10 that comes to about 70. Fix: total the estimate in a
   `trylast=True` hook, after deselection.
39. **medium** A high `nS_VICStart` hangs the TeensyROM, and the VIC
   knobs' upper limits stop short of it. On the Kawari C64 (002b) in NTSC,
   a TR+ on main a1dbf5e3 with `Dbg_SerTimChg` and `Dbg_SerDMA`, vicrom
   running, `nS_VICStart` was clean from 85 to 310; at 320 the probe's next
   DMA read of its counters ($0F00) got no reply, and the board answered
   nothing on USB after (the reset rungs, then a forced reflash that found
   no board) until the C64 was power-cycled
   (`reports/20261120-073513-ntsc`). `isrPHI2()` (`ISRs.c`) holds a
   VIC-cycle drive until `nS_VICDHold` (365) whatever `nS_VICStart` is, so
   the later sample alone should not end the interrupt later;
   `Common_Defs.h` says a higher `nS_VICDHold` breaks Ultimax cartridges on
   NTSC, which may be the same failure. Until this is settled,
   `treadmill/knobs.py` caps `nS_VICStart` at 310 and `nS_VICDHold` at its
   default, so its sweep goes down only. Fix: reproduce attended (each hang
   needs a power cycle), on a build with `DbgSignalIsrPHI2` and a scope, to
   see whether the interrupt overruns the next Phi2 rise or the DMA read
   path waits with interrupts off; step without DMA reads (the border's
   step on video) to tell the two apart; then set both caps from what it
   shows, per standard if PAL has more room. A station without power
   control cannot recover from such a hang: the run ends there.
40. **closed 2026-11-21, with krread's counts read by video.** The check
   ran (`scripts/2026-11-20-ksense-host-dma.py`,
   `scripts/2026-11-20-kr-host-dma-basic.py`, Kawari C64 002b, main
   a1dbf5e3) and the answer is wider than the DMA sense, so it goes to the
   firmware: host DMA reads or writes while a program runs under Kernal
   Replace corrupt the program's RAM bit by bit, under either sense, and
   `10 A=A+1:GOTO 10` stops within seconds to a minute at about 40 accesses
   a second; Kernal Replace off, or on with no DMA, or on with USB traffic
   and no DMA, ran clean. Separately, a write to $01 while a host access
   holds the line is not sensed (`KDS_Begin` keeps the last verdict), about
   one wrong half-pass of krread per three accesses. A build with PR #106
   (the release past a snooped bus) still crashed the BASIC loop under both
   senses (2026-11-21). krread now shows its counts
   on the top row and the probe reads them by video, so the suite sends no
   DMA while krread runs. The item as it was: Host writes into the C64's
   memory may go astray under Kernal
   Replace's DMA sense. On the Kawari C64 (002b) in NTSC, a TR+ on main
   a1dbf5e3, `KSenseMode` 2, krread with a hold handshake (the host wrote a
   flag by DMA before and after each counter read, the loop waited between
   passes while it was set) gave counts no loop could make: 30550 first-read
   misses in 369 passes, at most one each, and 369 passes in 2 s where Skoe
   made about 2344, as if it sat held (`reports/20261120-094916-ntsc`). The
   same probe reading its counters without writing had been clean from 55 to
   384 an hour before. `KDS_Begin` (`IOH_KernalReplace.c`) defers to another
   DMA user that already holds the line, but nothing shown here keeps a
   remote write from starting while a sense's own DMA cycle is in flight,
   and a remote write is what the TeensyROM Suite does to a running C128
   program. krread no longer writes into its loop. Fix: an exec script that
   runs krread under each sense and, beside it, writes and reads back a
   pattern in a free page by DMA, counting mismatches; if the DMA sense's
   count is not zero, it goes to the firmware with that script.
43. **medium** A sweep loses its whole record when a probe's own recovery
   fails. After a hang, `ReuLoopProbe.sample()` (`treadmill/probes/reuloop.py`)
   resets at the defaults and waits 60 s for the menu. On the C128 Marco1
   (2026-11-21) the REU loop with sprites hung at `nS_DMABAWait` 180, the
   menu was still booting at 60 s, and the `LinkError` went up through
   `Sweep.run()` (`treadmill/sweeps.py`): the test ended with no
   margins.json, though the walk had found 190 clean and 180 failing. The
   next test's setup reset the machine without trouble. Fix: the probe's
   recovery goes through the station ladder (another reset) before it
   gives up, and `Sweep.run()` writes the record it has, with that edge
   open, when a probe raises.
44. **medium** A noise-only failure becomes an edge on one sighting. A walk
   stops at its first failure, so one disturbed stretch of frames ends it.
   On the C64C #24 (2026-11-21) run 1's four noise-only DMA edges (one large
   event each, 11,717 to 324,241 pixels over the limit, the next value
   inward clean) did not recur in run 2 at the same values. On the C128
   Marco1 the same day a noise-only edge repeated at one value in all three
   passes (`nS_DMADataHold` 429), so some are real. On Marco1 on 2026-11-22
   the point straight after a REU-loop hang and its relaunch failed on
   noise alone twice in one pass (`nS_DMABAWait` 215 and 85, 530,000 to
   580,000 pixels, nearly the whole picture), where the other two passes
   were clean from 60 to 400: the relaunched program had likely not
   settled. Fix: sample a point whose only failure is the noise probe once
   more before the walk stops there, and fail it only if the repeat fails
   too.
45. **low** vicrom's baseline is the worst frame at the defaults.
   `VicRomProbe` (`treadmill/probes/vicrom.py`) takes the frame of its
   baseline dwell that differs most from the reference, so one disturbed
   frame raises the limit for the whole sweep. On 2026-11-21 the baselines
   were 52,491 to 83,073 pixels on C64 #14 and the C64C #24 and 115,476 to
   120,561 on the C128 Marco1, which puts the limit at 157,000 to 362,000;
   Marco1's `nS_VICStart` 80 cleared it by 2%. Fix: look at the baseline
   frames to see what differs at the defaults, then judge by a high
   percentile of the frames rather than the worst, or mask what moves.
46. **high** The screen-on hold once failed to keep the laptop out of
   Modern Standby. With `treadmill run` holding `ES_DISPLAY_REQUIRED`
   (`treadmill/awake.py`), the C128 Marco1's first run went 95 minutes on
   2026-11-21 without a standby, but its second run, started two minutes
   after the first ended, went into Modern Standby 15 minutes in, at
   20:08. USB dropped, and the run lost its station when the laptop woke
   at 20:26 (42). Windows gives the reason as SC_MONITORPOWER, but it gave
   the same at 14:41 with no run going and at 16:59 during the C64C #24's
   run, before the hold existed: on this laptop that is the ordinary
   display timeout (60 minutes on AC). Presence sensing is off, and the run
   logged no refusal from Windows. What ended the hold is not known. Fix:
   during a run, check that the hold shows in `powercfg /requests`
   (elevated), and try whether a hold taken after the machine has been
   idle past the timeout still counts; a thread that calls
   `SetThreadExecutionState(ES_DISPLAY_REQUIRED)` every minute resets the
   idle timer and would not depend on either.
49. **medium** A REU loop that fails every round is never relaunched.
   `ReuLoopProbe` (`treadmill/probes/reuloop.py`) relaunches the loop only
   after a hang. On the C128 Marco1 on 2026-11-22, `nS_DMASetup` 490 left
   the loop running with every round bad (2,713 of 2,713, twice the normal
   round rate), and it stayed that way at every later point, the default
   460 included, while zpattern read clean. Passes 2 and 3 found false
   edges at 455 and 485, the record says the default failed, and the test
   passed. In the same run the loop reported 65,477 rounds, none bad, at
   `nS_DMADataHold` 434 (normal is about 1,450). Fix: take every round bad,
   or a round count far from the baseline's, as a broken loop: relaunch at
   the defaults and sample the point again; and fail the test when a pass
   fails at the default.
50. **medium** The ASID stream test checks the pairs, not when they
   arrive. `asidlog.a` stores each pair's two bytes and nothing else,
   `test_asid_stream.py` compares content and order (`compare_pairs` in
   `treadmill/asid.py`), and `asid.json` gives each phase's average rate
   only. A firmware change that delays or bunches frames without
   corrupting them passes, though a listener hears it as tempo wobble.
   Raised with PR #109's bench run, where the byte check was the one that
   mattered: its window adds under a microsecond per USB register access.
   Fix: have the logger stamp pairs from a free-running timer the player
   does not program (a CIA timer; check the player's setup). A stamp on
   every pair halves the ring to 6,144 pairs, so either read it more often
   or stamp only the pair that starts a frame (register 0, $80). Stamp the
   host's sends too, since the Windows timer adds its own jitter. Report
   each paced frame's interval against the frame period (spread, worst
   lateness) and, flat out, the longest gap between pairs, with thresholds
   from runs of one build against itself.
