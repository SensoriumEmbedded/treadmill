"""hostjobs: the host's DMA write jobs beside a C64 program that counts.

A probe's workload is the C64's; this one keeps the board's DMA busy beside
it, as the bench's ``reuhammer.py`` (TeensyROM PR #119) and this repo's REU
read-back soak run it: WriteC64Spans jobs of ``PAYLOAD`` random bytes at
``TARGET``, back to back for as long as the probe dwells, each ``SLICE``
bytes started on the C64's next store to ``SAMPLE_STORE``. A CIA2 timer NMI
the probe starts (``start_nmi``) makes that store every ``NMI_PERIOD``
cycles, so the board takes the bus at the moment a sample player's DMA
would. On the 250407 C64 in NTSC, main's REU registers read back 510 times
wrong in 3,148,992 reads at the defaults under these jobs, and never
without them (BACKLOG 21).

The C64 program must store to ``SAMPLE_STORE`` in its NMI and leave
``TARGET`` .. ``TARGET + PAYLOAD`` alone. The jobs run in the probe's own
thread between its counter reads, so they never share the link with them.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from treadmill.link import Link, Refused

TARGET, PAYLOAD, SLICE, WAIT_US = 0x6000, 1024, 33, 200
SAMPLE_STORE = 0xD418
NMI_PERIOD = 200                  # cycles between the NMI's stores
CIA2_TA, CIA2_ICR, CIA2_CRA = 0xDD04, 0xDD0D, 0xDD0E
REFUSED_QUIET_S = 1.0             # the board discards a refused job's payload until its line is this quiet


@dataclass
class Jobs:
    """What one dwell's jobs did."""
    sent: int = 0
    refused: int = 0
    last_refusal: str | None = None
    last: bytes | None = None     # the last payload the board took, for ``landed``


def start_nmi(link: Link) -> None:
    """CIA2's timer A running continuously, its NMI on: the C64's store to
    ``SAMPLE_STORE`` every ``NMI_PERIOD`` cycles."""
    latch = NMI_PERIOD - 1
    link.write_memory(CIA2_TA, bytes([latch & 0xFF, latch >> 8]))
    link.write_memory(CIA2_ICR, b"\x81")
    link.write_memory(CIA2_CRA, b"\x11")


def host_jobs(link: Link, seconds: float, rng: random.Random) -> Jobs:
    """Locked WriteC64Spans jobs, back to back, for ``seconds``."""
    done = Jobs()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        data = rng.randbytes(PAYLOAD)
        try:
            link.write_spans([(TARGET, PAYLOAD)], data, slice_bytes=SLICE, gap_us=WAIT_US, lock_to=SAMPLE_STORE,
                             drain=0.0)
        except Refused as e:
            done.refused += 1
            done.last_refusal = e.text
            time.sleep(REFUSED_QUIET_S)
            continue
        done.sent += 1
        done.last = data
    return done


def landed(link: Link, jobs: Jobs) -> bool:
    """The last job's bytes are in the C64's memory; True when no job ran."""
    return jobs.last is None or link.read_memory(TARGET, len(jobs.last)) == jobs.last
