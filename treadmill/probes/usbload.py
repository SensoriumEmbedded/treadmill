"""usbload: the host keeping the board's USB busy while a probe counts.

A probe's workload is the C64's; this is the board's: version requests, a
batch at a time, each batch's replies read before the next, for as long as
the probe dwells. Every request is a USB interrupt in the middle of the
board's bus cycles, so an interrupt that arrives late is the common case
rather than the rare one: ``nS_MaxAdj``'s upper edge shows only under such
load, and PR #103's MinimalBoot errors came with it. It runs in the probe's
own thread, between its counter reads, so it never shares the link with
them.
"""

from __future__ import annotations

import time

from treadmill.link import Link
from treadmill.link.protocol import VERSION_INFO, to_board

BATCH = 8                         # requests written at once, as the PR #103 script sent them
REPLY_S = 2.0                     # a batch's first reply byte
QUIET_S = 0.05                    # then the batch is done when the line is this quiet


def usb_load(link: Link, seconds: float) -> int:
    """Version requests for ``seconds``; returns how many went out."""
    stream, sent = link.stream, 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        stream.write(to_board(VERSION_INFO) * BATCH)
        sent += BATCH
        stream.read_idle(REPLY_S, QUIET_S, REPLY_S * BATCH)
    return sent
