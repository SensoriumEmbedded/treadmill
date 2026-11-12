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
   and end a text reply on its known terminator where one exists.
13. **medium, with the keyboard board** No way to drive a board whose
   firmware has no USB, such as a TR+ running an extension host (SIDHost).
   `Harness.__enter__` in `treadmill/driver.py` always goes through
   `Board.open`, which opens the serial link, so `exec --as-is` fails with
   "COM94: Cannot configure port" before the script runs; and `keys_for()` in
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
