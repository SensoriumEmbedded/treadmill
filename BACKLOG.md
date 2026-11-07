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
3. **medium, with the driver CLI** The recovery hooks belong in `Board.open`.
   The pytest plugin's `board` fixture attaches `reconnect_hook` and
   `reboot_hook`; `open_station()` in `treadmill/session.py` attaches
   neither, so `shell`, `exec` and `screen` have no rung past a reset and
   wedge where a pytest run recovers. Move the hooks into `Board.open`
   (the reboot hook needs the hex and the primary checkout, so the session
   takes a firmware spec too).
4. **medium, before soak runs** Every command drains for `DRAIN_S` (300 ms)
   before sending and most waits `TEXT_REPLY`'s 300 ms idle after, in
   `treadmill/link/trlink.py`; a ping costs about 600 ms on a quiet link
   and a 200-step menu walk spends two minutes in fixed waits. A design
   choice: drain only when the tap has seen bytes since the last command,
   and end a text reply on its known terminator where one exists.

## Video

5. **low** Every video read refines the grid. `read_video()` in
   `treadmill/observe/channels.py` decodes, refines (about 160 ms) and
   decodes again on each read, so `wait_text()` polls at about four reads
   a second. Reuse the last refined calibration while the frame has not
   moved, and refine when the decode confidence drops.

## Firmware builds

6. **low** The header patch rewrites CRLF as LF. `enable_defines_in_header()`
   in `treadmill/firmware/build.py` reads `TeensyROM.h` with universal
   newlines and writes with `newline=""`, so a CRLF header comes out LF on
   every line and the diff hash covers the whole file. Harmless in a
   worktree that is cleaned on reuse; read and write bytes to fix.
