"""Test 2: a reset brings the menu back, booted, readable, and the same menu
the golden describes; the video channel agrees with DMA where there is DMA."""

import time

BOOT_POLL_S = 0.05            # the transport probe's cadence: fine enough to see "in progress" go by
LEGEND_TEXT = "F1 Teensy Mem"


def test_reset_to_menu(at_menu, screen, goldens, artifacts, run_dir):
    board = at_menu
    t0 = time.monotonic()
    board.reset()
    deadline = t0 + board.station.machine.boot_timeout_s
    saw_in_progress = False
    while True:
        banner = board.link.banner()
        if banner.boot == "complete":
            break
        saw_in_progress = saw_in_progress or banner.boot == "in progress"
        assert time.monotonic() < deadline, f"Boot: {banner.boot} after {deadline - t0:.0f} s"
        time.sleep(BOOT_POLL_S)
    booted_in = time.monotonic() - t0
    board.wait_for_menu()
    read = screen.wait_text(LEGEND_TEXT, row=23)
    goldens.check(read, "menu/root")
    if board.dma_allowed():
        screen.read(exact=True, xcheck_now=True)       # the screen fixture fails the test past the limit
    handlers = [e.name for e in board.events.since(t0) if e.kind == "handler"]
    assert all(h == "TeensyROM" for h in handlers), handlers
    run_dir.write_json("boot.json", {"seconds_to_boot_complete": round(booted_in, 2),
                                     "saw_in_progress": saw_in_progress}, artifacts)
