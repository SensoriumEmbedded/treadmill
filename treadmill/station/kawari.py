"""A Kawari C64's video standard, switched by the keyboard board's ``V``.

The Kawari reads its switch header J1 only while its FPGA configures. ``V``
holds the C64 in reset, sets J1 (open or closed to ground) and pulses the
FPGA's CRESET_N, so the Kawari reloads in the standard J1 picks without a
power cycle: J1 open gives the model saved in the Kawari, closed the other
standard. The station's ``[keyboard] kawari_open`` says which standard open
gives. The TR measures the standard again after the reset, and its banner is
the check.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from treadmill.errors import TreadmillError
from treadmill.link.trlink import NotAllowed

if TYPE_CHECKING:
    from treadmill.board import Board

STANDARDS = ("PAL", "NTSC")
OTHER = {"PAL": "NTSC", "NTSC": "PAL"}


class KawariError(TreadmillError):
    """J1 open and closed both left the C64 in the other standard."""


def j1_for(board: Board, standard: str) -> str:
    return "open" if standard == board.station.keyboard.kawari_open else "closed"


def set_standard(board: Board, standard: str, log=print) -> bool:
    """Put the C64 in ``standard`` (PAL or NTSC); True when it was switched.
    A C64 already in it is left alone. When J1 gives the other standard, the
    Kawari's saved model has changed since the station file was written: J1
    is flipped once, the log says what ``kawari_open`` should be, and the
    rest of the run uses that. The station's TOD rate follows the new
    standard."""
    standard = standard.upper()
    if standard not in STANDARDS:
        raise ValueError(f"video standard {standard!r}; PAL or NTSC")
    st = board.station
    if st.standard == standard:
        return False
    if board.kbd is None or not st.has("region_switch"):
        why = f" (keyboard board: {board.kbd_problem})" if board.kbd_problem else ""
        raise NotAllowed(f"station {st.id} runs {st.standard or 'an unknown standard'} and lacks "
                         f"region_switch{why}, so it cannot switch to {standard}")
    j1 = j1_for(board, standard)
    got = _switch(board, j1)
    if got != standard:
        flipped = "closed" if j1 == "open" else "open"
        log(f"[kawari] J1 {j1} gave {got}, not {standard}: the Kawari's saved model is not what "
            f"kawari_open says; trying J1 {flipped}")
        got = _switch(board, flipped)
        if got != standard:
            raise KawariError(f"J1 open and closed both left the C64 in {got}")
        opens = standard if flipped == "open" else OTHER[standard]
        log(f"[kawari] J1 {flipped} gives {standard}: set kawari_open = \"{opens}\" in [keyboard] of "
            f"station {st.id}")
        board.station = replace(board.station, keyboard=replace(st.keyboard, kawari_open=opens))
    board.apply_tod()
    return True


def _switch(board: Board, j1: str) -> str | None:
    """``V``, then the menu back and the banner's standard. A TR+ senses the
    reset ``V`` holds and restarts the menu; a plain TR can't, so it resets
    the C64 itself, which also makes it measure the standard again."""
    board.kbd.video(j1)
    if not board.station.has("reset_sense"):
        board.reset()
    board.wait_for_menu()
    board.probe()
    return board.station.standard
