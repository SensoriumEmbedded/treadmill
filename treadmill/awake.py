"""Keep the host's screen on while a command drives a board.

On a laptop with Modern Standby, the screen going off after its idle timeout
puts the whole machine into standby, whatever the sleep setting says: USB
devices drop, the board's link and the capture with them, and a run comes back
from it to a station it cannot recover. A display-required request (the one a
video player holds) keeps the screen, and so the machine, on until the command
ends. It changes no setting; a closed lid or Sleep from the Start menu still
sleeps.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Iterator

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
HOLD = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED

WINDOWS = sys.platform == "win32"


def _set_state(flags: int) -> int:
    """SetThreadExecutionState: the previous state, or 0 when Windows refused."""
    import ctypes
    fn = ctypes.windll.kernel32.SetThreadExecutionState
    fn.argtypes = [ctypes.c_uint]          # the flags have the top bit set: not a C int
    fn.restype = ctypes.c_uint
    return fn(flags)


@contextlib.contextmanager
def screen_on(log=print) -> Iterator[None]:
    """Hold the screen on for the ``with`` block (on Windows; elsewhere a no-op).
    The request belongs to the calling thread, so enter it on the thread that
    waits for the command."""
    if not WINDOWS:
        yield
        return
    if not _set_state(HOLD):
        log("could not ask Windows to keep the screen on; a long run may stop when the screen turns off")
        yield
        return
    try:
        yield
    finally:
        _set_state(ES_CONTINUOUS)
