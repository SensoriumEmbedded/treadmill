"""TR BASIC's serial path: what the C64 sends with TPUT reaches the host
whole and in order, TGET hands the host's bytes to the C64, and the menu
button while TPUT is flooding brings the board back without a crash.

While TR BASIC runs, the board ignores every host command, DMA included, and
the launch path re-initializes the KERNAL and BASIC, so no hook set before
it survives. The program (``tputecho.a``) goes in at $C000 by DMA, the "BASIC
TR Command IO access only" item leaves BASIC at READY under the TR BASIC
handler, and the person at the bench types SYS 49152 (``--attended``) until
the keyboard board can. From then on the serial port carries only TPUT and
TGET bytes, and the way back is the TeensyROM menu button.

The burst is far faster than BASIC's TPUT: 13 cycles a byte against the
firmware's 4 KB ring and its drain in the polling handler. Firmware that
writes each byte to USB from the bus interrupt instead misses cycles at
this rate, so the test also tells the two apart.
"""

import time

import pytest

from treadmill.session import fake_requested

ITEM = "/TeensyROM Specific/BASIC TR Command IO access only"
PAYLOAD_AT = 0xC000
PREAMBLE = bytes([0xFE, 0xFD, 0xFC, 0xFB])
BURST = 64 * 256                        # tputecho.a's BURST_PAGES
ECHO = 2048                             # inside TR BASIC's 4 KB TGET queue
CMD_FLOOD = 0x01
READY_S = 10.0                          # the item's launch, to BASIC's READY
TYPE_S = 120.0                          # for the person to type SYS 49152
BURST_S = 30.0
ECHO_S = 10.0
FLOOD_SAMPLE = 4096
FLOOD_POLL_S = 0.25
BUTTON_S = 60.0


def _pattern(count: int) -> bytes:
    return bytes(0x80 | (i & 0x7F) for i in range(count))


def _first_difference(got: bytes, want: bytes) -> str:
    for k, (g, w) in enumerate(zip(got, want)):
        if g != w:
            return f"byte {k}: got ${g:02X}, expected ${w:02X}"
    return f"{len(got)} of {len(want)} bytes" if len(got) != len(want) else "identical"


def _tell_operator(request: pytest.FixtureRequest, text: str) -> None:
    """A line the person at the bench sees while pytest captures output."""
    capman = request.config.pluginmanager.getplugin("capturemanager")
    if capman is None:
        print(text, flush=True)
        return
    with capman.global_and_fixture_disabled():
        print(f"\n>>> {text}", flush=True)


@pytest.mark.needs("dma")
@pytest.mark.duration(3)
def test_tput_and_tget_then_the_menu_button(request: pytest.FixtureRequest, programs):
    if fake_requested():
        pytest.skip("the fake board has no TR BASIC handler")
    if not request.config.getoption("--attended"):
        pytest.skip("needs --attended: someone types SYS 49152 and presses the TeensyROM menu button")
    board = request.getfixturevalue("at_menu")
    link, stream = board.link, board.link.stream
    payload = programs("tputecho").read_bytes()
    link.write_memory(PAYLOAD_AT, payload)
    assert link.read_memory(PAYLOAD_AT, len(payload)) == payload, "the program did not read back from $C000"

    link.launch(ITEM, "teensy")
    board.events.feed(stream.read_idle(READY_S, 1.0, READY_S))     # the handler line: the port is deaf now
    _tell_operator(request, f"At the C64's READY prompt, type SYS 49152 and press RETURN "
                            f"(waiting {TYPE_S:.0f} s).")
    head = stream.read_until(PREAMBLE, TYPE_S)
    assert head is not None, f"no preamble within {TYPE_S:.0f} s: was SYS 49152 typed at READY?"
    board.events.feed(head[:-len(PREAMBLE)])

    burst = stream.read(BURST, BURST_S)
    want = _pattern(BURST)
    assert burst == want, f"TPUT burst: {_first_difference(burst, want)}"

    echo = bytes(0x80 | ((i * 7 + 3) & 0x7F) for i in range(ECHO))
    stream.write(echo)
    back = stream.read(ECHO, ECHO_S)
    assert back == echo, f"TGET echo: {_first_difference(back, echo)}"

    # The button while the C64 keeps the TPUT ring full: the board has to
    # switch handlers before it frees the ring the bus interrupt is writing.
    stream.write(bytes([CMD_FLOOD]))
    sample = stream.read(FLOOD_SAMPLE, ECHO_S)
    assert len(sample) == FLOOD_SAMPLE and all(b & 0x80 for b in sample), "the flood did not start"
    _tell_operator(request, f"Press the TeensyROM menu button now (waiting {BUTTON_S:.0f} s).")
    # Drain the flood until it stops, so the link's commands afterwards do not
    # wade through it; only the board's own text (bit 7 clear) goes to the event log.
    deadline = time.monotonic() + BUTTON_S
    while time.monotonic() < deadline:
        chunk = stream.drain(FLOOD_POLL_S)
        board.events.feed(bytes(b for b in chunk if b < 0x80))
        if not any(b & 0x80 for b in chunk):
            break
    else:
        pytest.fail(f"the flood was still running after {BUTTON_S:.0f} s")
    board.wait_for_menu(BUTTON_S)
    assert "ready" in link.ping().lower()
