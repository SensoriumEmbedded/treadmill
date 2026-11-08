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
2. **medium** A reconnect that finds no board escapes the recovery ladder.
   `find_port()` raises `NotFound` inside `Board._reconnect()`, and
   `ensure_menu()` in `treadmill/board.py` catches `LinkError`,
   `TransportError`, `PowerUnavailable` and `Refused` only, so a test
   errors with a traceback instead of the station marking itself wedged.
   One tuple entry.
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
11. **high, blocks test 20 on a TR+** The idread probe reads nothing on a
   board. The firmware runs a launched PRG under the `None` IO handler
   (`Loading IO handler: None` on the bench), so `idread.a`'s reads of
   $DE05/$DE06 see the open bus, and `treadmill sweep p --probe idread`
   failed every read at the default 150. `tests/sweep/test_tp_margin.py`
   picks idread whenever the station has DMA, and the fake board serves
   the registers to a PRG, so the offline sweep tests pass. Ways out: carry
   the loop in a generic 8K cartridge image and count wrong reads of its
   own ROM at $8000 (nS_PLAprop gates ROML reads as it gates IO1); or
   require a Special IO handler that answers, as `tools/bench/regsweep.py`
   does with the REU; or keep the menu probe only.
