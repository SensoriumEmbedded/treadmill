"""Test 4: a program launched from the card runs, shows its text, keeps
ticking, answers Busy to a ping, and a reset takes it back to the menu."""

import time

import pytest

from treadmill.c64.menu import LEGEND_ROW1
from treadmill.fixtures.sdcard import FIXTURE_ROOT
from treadmill.link import Refused
from treadmill.link.state import RUNNING
from treadmill.observe.liveness import Liveness, classify

HELLO_ROW, HELLO_COLUMN, HELLO_TEXT = 12, 12, "treadmill hello"      # hello.a
LAUNCH_S = 20.0
HEARTBEAT_FRAMES = 30


@pytest.fixture
def hello_on_card(at_menu, sd_card, programs) -> str:
    path = f"{FIXTURE_ROOT}/hello.prg"
    at_menu.link.push_file(programs("hello").read_bytes(), path)
    return path


def _launch(board, screen, path: str):
    board.link.launch(path, "sd")
    read = screen.wait_text(HELLO_TEXT, row=HELLO_ROW, timeout=LAUNCH_S)
    assert read.rows()[HELLO_ROW][HELLO_COLUMN:HELLO_COLUMN + len(HELLO_TEXT)] == HELLO_TEXT
    return read


def test_launch_hello(at_menu, screen, hello_on_card):
    board = at_menu
    t0 = time.monotonic()
    _launch(board, screen, hello_on_card)
    with pytest.raises(Refused) as e:
        board.link.ping()
    assert "Busy" in e.value.text, e.value.text
    assert board.state().kind == RUNNING
    kinds = {ev.kind for ev in board.events.since(t0)}
    assert kinds & {"launch", "handler"}, kinds
    board.reset()
    board.wait_for_menu()
    screen.wait_text(LEGEND_ROW1.strip(), row=23)


@pytest.mark.needs("video")
def test_launched_program_heartbeat(at_menu, screen, video, calibration, hello_on_card):
    """The heartbeat cell keeps changing: the 6510 is running, not just the VIC."""
    board = at_menu
    t0 = time.monotonic()
    _launch(board, screen, hello_on_card)
    frames = []
    last = None
    for _ in range(HEARTBEAT_FRAMES):
        last = video.next_frame(last, timeout=2.0)
        frames.append(last)
    norms = [calibration.normalize(f.image) for f in frames]
    verdict = classify(frames, norms=norms, events=board.events, since=t0, expect_heartbeat=True)
    assert verdict.kind == Liveness.ALIVE, verdict


def test_reset_then_launch_at_once(at_menu, screen, hello_on_card):
    """A launch sent right behind a reset, as a host that does not wait for
    ``Boot: complete`` sends it: the board runs it or refuses it, and comes
    back to the menu either way."""
    board = at_menu
    board.reset()
    try:
        board.link.launch(hello_on_card, "sd")
        accepted = True
    except Refused:
        accepted = False
    if accepted:
        screen.wait_text(HELLO_TEXT, row=HELLO_ROW, timeout=LAUNCH_S)
    board.reset()
    board.wait_for_menu()
