"""Keep the host's screen on while a command drives a board.

On a laptop with Modern Standby, the screen going off after its idle timeout
puts the whole machine into standby, whatever the sleep setting says: USB
devices drop, the board's link and the capture with them, and a run comes back
from it to a station it cannot recover. A display-required request (the one a
video player holds) keeps the screen, and so the machine, on until the command
ends. It changes no setting; a closed lid or Sleep from the Start menu still
sleeps.

That request once failed to hold (backlog 46: a run went into standby 15
minutes in, for reasons not known), so a thread also resets the display
and system idle timers once a minute while the hold lasts, which keeps the
screen on whether or not Windows still counts the request.
"""

from __future__ import annotations

import contextlib
import sys
import threading
from typing import Iterator

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
HOLD = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
NUDGE = ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED    # without ES_CONTINUOUS: resets the idle timers once
REFRESH_S = 60.0          # well inside the shortest display timeout Windows offers

WINDOWS = sys.platform == "win32"


def _set_state(flags: int) -> int:
    """SetThreadExecutionState: the previous state, or 0 when Windows refused."""
    import ctypes
    fn = ctypes.windll.kernel32.SetThreadExecutionState
    fn.argtypes = [ctypes.c_uint]          # the flags have the top bit set: not a C int
    fn.restype = ctypes.c_uint
    return fn(flags)


@contextlib.contextmanager
def screen_on(log=print, refresh: float = REFRESH_S) -> Iterator[None]:
    """Hold the screen on for the ``with`` block (on Windows; elsewhere a no-op).
    The request belongs to the calling thread, so enter it on the thread that
    waits for the command; the idle timers are reset every ``refresh``
    seconds from a thread of its own until the block ends."""
    if not WINDOWS:
        yield
        return
    held = bool(_set_state(HOLD))
    if not held:
        log("could not ask Windows to keep the screen on; a long run may stop when the screen turns off")
    stop = threading.Event()

    def nudge() -> None:
        refused = False
        while not stop.wait(refresh):
            if not _set_state(NUDGE) and not refused:
                refused = True
                log("Windows refused to reset its idle timers; a long run may stop when the screen turns off")

    thread = threading.Thread(target=nudge, name="treadmill-awake", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5.0)
        if held:
            _set_state(ES_CONTINUOUS)
