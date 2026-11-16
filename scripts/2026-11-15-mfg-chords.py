"""2026-11-15: the manufacturing test's pass and fail chords, played on a board for a listen.

    treadmill exec --station <id> --as-is scripts/2026-11-15-mfg-chords.py

As-is, so a C64 the test left paused is resumed rather than reset; the
running program's timer interrupt is masked by the first chord, as at the
end of a test. Pass chords, a pause, then fail chords.
"""

import time

from treadmill.mfg import FAIL_JINGLE, PASS_JINGLE, play_chords, sid_clock

link.pause(False)
time.sleep(0.5)
clock = sid_clock(board, station)
log(f"[chords] SID clock {clock} Hz: pass")
play_chords(link, PASS_JINGLE, clock)
time.sleep(1.5)
log("[chords] fail")
play_chords(link, FAIL_JINGLE, clock)
result = {"clock_hz": clock}
