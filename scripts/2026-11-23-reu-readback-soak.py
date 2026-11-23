"""2026-11-23: do the C64's writes to the REU's registers read back wrong while
the host streams locked WriteC64Spans jobs?

    treadmill exec --station <id> --fw ref=<ref> scripts/2026-11-23-reu-readback-soak.py --json \\
        [-- --seconds 300 --idle-seconds 60 --hammer --unlocked --payload 33 --sprites --usb 0.02
            --read-at 0 --read 53248 --tap]

Firmware: a TR+ main image with locked WriteC64Spans (0.9 and later). Station:
a TR+ running its main image (DMA), with video (the Settings driver).

On a TR+ in an NTSC C128, c64cast's REU read-back workload found $DF02-$DF08
writes that read back $FF or kept their old value: isrPHI2 missed or answered
late the cycle the 6510 wrote them in. TeensyROM PRs #108 (interrupts masked
from flash), #109 (USB register accesses in flight at Phi2 rise) and #110
(an asynchronous DMA started on the next cycle's BA) each fix one cause.
``reureadback.a`` (PR #111's client) counts each register write that reads
back wrong. Special IO is set to the REU, a CIA2 NMI stores to $D418 every
``--nmi-period`` cycles, and the C64 runs ``--idle-seconds`` with no host
jobs, then ``--seconds`` with locked WriteC64Spans jobs of ``--payload``
bytes into $6000 back to back, each slice started on a $D418 store
(``--unlocked``: the gap instead). ``--usb S`` adds S seconds of version
requests after each job, and ``--read N`` an N-byte DMA read at
``--read-at`` ($6000 unless given; never $D000-$DFFF, where a read clears
the CIAs' interrupt flags). ``--read-at 0 --read 53248`` pushes everything
else out of the Teensy's 32 KB D-cache, so a flash literal loaded inside an
interrupt mask (v0.9.0.1's SampleWatchStart) misses there.
``--sprites`` turns on eight sprites, so BA drops on most lines.
``--hammer`` runs the client's read loop instead of its transfers:
$DF02-$DF08 set once and read back every 9 cycles, so a late
isrPHI2 shows on an IO2 read about 60 times as often (rounds are then 256
passes, and a register's last read is the wrong value itself). The counters
are read every 2 s, with the last payload read back to check it landed.

``--tap`` needs a build with DbgSnoopTap (TeensyROM PR #114 or later): it
arms the snoop tap's bus timing records and takes the ring every second, so
each phase also counts isrPHI2 entries more than 1.1 µS after the last
(``late_entries``), IO decodes reached after Phi2 fell (``late_decodes``)
and DMA asserts, with the PCs the late ones interrupted, whether or not a
C64 access landed on them. The ring holds 1,024 entries, so a load with
many DMA asserts a second loses some (``tap_lost``): ``--payload 33`` makes
a locked job one slice.

Each cause shows under its own load. Release builds on 2026-11-23, wrong
hammer reads per 300 s run:

- rigA-sysMarco1-trplus (NTSC C128 in C64 mode; nS_PLAprop clean past
  270 ns): v0.9.0.1 never missed under plain locked jobs, at 16 to 1,000
  jobs a second, with or without the NMI, sprites, and version requests or
  4 KB reads between jobs (about 60 million IO2 reads). With
  ``--read-at 0 --read 53248`` (about 4,480 jobs a run) every wrong read is
  $00, a cycle not answered: v0.9.0.1 521 and 839, #109+#110 586, #108 214
  and 49, all three 105, 5 and 231. #108 is the fix there; the residual
  with all three is not explained.
- rigA-sys14-trplus (NTSC C64; nS_PLAprop clean only to 215 ns), plain
  ``--hammer``: locked (about 39k jobs) v0.9.0.1 2,217, #108 4,239, all
  three 2,373, a cause in the lock-to-store path that none of them touches;
  ``--unlocked`` (about 30k jobs) v0.9.0.1 14 and 24, #108 49 and 33,
  #109+#110 0, all three 0. Wrong reads there are mostly the right value
  with extra bits set: late data.

``--tap`` costs ISR time. On sys14 a DbgSnoopTap build of PR #114 made
446,070 wrong reads under locked jobs and 7,709 with none, so it cannot
watch a machine with that little margin; on Marco1 it missed 18 under
one-slice jobs (``--payload 33``, where release missed none) and none under
the sweep (a build of all three plus #114).

Puts back Special IO and leaves the board at the menu. ``result`` holds,
per phase: seconds, jobs and bytes sent, refused jobs (with the last
reason), payloads that did not land, REU rounds and bad pages, and each
register's misses with the value a second read found after its last one;
``misses`` is the jobs phase's total.
"""

import argparse
import collections
import contextlib
import random
import time

from treadmill.c64.settings import REU, Settings
from treadmill.link import SNOOP_ARM, SNOOP_STOP, SNOOP_TAKE, LinkError, Refused
from treadmill.probes.usbload import usb_load
from treadmill.programs.build import assemble

READY, READY_VALUE = 0xC104, 0x5A
COUNTERS = 0xC110                # rounds, bad pages; then misses (low, high) and last reads per register
COUNTER_BYTES = 0x27
REGISTERS = ["$DF02", "$DF03", "$DF04", "$DF05", "$DF06", "$DF07", "$DF08"]
CIA2_TA, CIA2_ICR, CIA2_CRA = 0xDD04, 0xDD0D, 0xDD0E
SAMPLE_STORE = 0xD418
TARGET = 0x6000
REMOTE = "/treadmill/reureadback.prg"
SAMPLE_S = 2.0
TAP_SAMPLE_S = 1.0
TAP_PAGE, TAP_BUS_TIMING = 0x90, 16                   # a page the client never touches; the flag works on any
LATE_ENTRY, LATE_ENTRY_PC, DMA_ASSERT, _, LATE_DECODE, LATE_DECODE_PC = range(0xC0, 0xC6)
START_S = 15.0
LAUNCH_QUIET_S = 1.0

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=float, default=300.0, help="the jobs phase")
parser.add_argument("--idle-seconds", type=float, default=60.0, help="the phase with no host jobs first")
parser.add_argument("--payload", type=int, default=1024, help="bytes a job writes to $6000")
parser.add_argument("--slice", type=int, default=33, help="slice bytes")
parser.add_argument("--wait", type=int, default=200, help="longest wait for a store in µs (the gap with --unlocked)")
parser.add_argument("--nmi-period", type=int, default=200, help="cycles between the NMI's $D418 stores")
parser.add_argument("--unlocked", action="store_true", help="sliced jobs with a gap, not locked to $D418")
parser.add_argument("--usb", type=float, default=0.0, help="seconds of version requests after each job")
parser.add_argument("--read", type=int, default=0, help="bytes read back by DMA after each job")
parser.add_argument("--read-at", type=lambda s: int(s, 0), default=TARGET, help="where --read starts (default $6000)")
parser.add_argument("--sprites", action="store_true", help="eight sprites on, so BA drops on most lines")
parser.add_argument("--hammer", action="store_true", help="the client reads $DF02-$DF08 back in a loop, no transfers")
parser.add_argument("--tap", action="store_true", help="count late isrPHI2 entries and decodes (a DbgSnoopTap build)")
opts = parser.parse_args()

if not board.dma_allowed():
    raise SystemExit("this needs a TR+ running its main image (DMA)")
if opts.tap:
    try:
        link.snoop_tap(SNOOP_STOP)
    except LinkError as e:
        raise SystemExit(f"--tap needs a build with DbgSnoopTap: {e}")
if not 0 < opts.payload <= 0x1000 or not 0 < opts.slice < 256 or not 0 <= opts.wait < 256:
    raise SystemExit("--payload is 1 to 4096 bytes, --slice 1 to 255 and --wait 0 to 255")
# A DMA read of $D000-$DFFF clears the CIAs' interrupt flags and the REU's status, and the
# read command's length is 16 bits.
if opts.read and (opts.read_at < 0 or opts.read_at + opts.read > 0x10000
                  or (opts.read_at < 0xE000 and opts.read_at + opts.read > 0xD000)):
    raise SystemExit("--read-at and --read must stay within $0000-$CFFF or $E000-$FFFF")


class Counters:
    """The client's 16-bit counters, read often enough to unwrap them."""

    def __init__(self):
        self.last = self._read()
        self.rounds = self.bad = 0
        self.misses = [0] * len(REGISTERS)
        self.last_read = [None] * len(REGISTERS)

    @staticmethod
    def _read() -> bytes:
        return link.read_memory(COUNTERS, COUNTER_BYTES, drain=0.0)

    def sample(self) -> int:
        """Reads the counters; returns the rounds since the last read."""
        now = self._read()
        word = lambda b, at: b[at] | b[at + 1] << 8
        rounds = (word(now, 0) - word(self.last, 0)) & 0xFFFF
        self.rounds += rounds
        self.bad += (word(now, 2) - word(self.last, 2)) & 0xFFFF
        for k in range(len(REGISTERS)):
            n = ((now[0x10 + k] | now[0x18 + k] << 8) - (self.last[0x10 + k] | self.last[0x18 + k] << 8)) & 0xFFFF
            if n:
                self.misses[k] += n
                self.last_read[k] = f"${now[0x20 + k]:02X}"
        self.last = now
        return rounds

    def summary(self) -> dict:
        return {"rounds": self.rounds, "bad_pages": self.bad,
                "registers": {name: {"misses": n, "last_read": self.last_read[k]}
                              for k, (name, n) in enumerate(zip(REGISTERS, self.misses)) if n},
                "misses": sum(self.misses)}


class Tap:
    """The snoop tap's bus timing records, counted take by take."""

    def __init__(self):
        self.late_entries = self.late_decodes = self.asserts = self.lost = 0
        self.entry_pcs, self.decode_pcs = collections.Counter(), collections.Counter()
        self.decoded = collections.Counter()
        pages = bytearray(256)
        pages[TAP_PAGE] = TAP_BUS_TIMING
        link.snoop_tap(SNOOP_ARM, bytes(pages), drain=0.0)

    def take(self, mode: int = SNOOP_TAKE) -> None:
        t = link.snoop_tap(mode, drain=0.0)
        self.lost += t.recorded - len(t.entries)
        e = t.entries
        for i, (cyc, addr, data, info) in enumerate(e):
            detail = e[i + 1] if i + 1 < len(e) and e[i + 1][3] == info + 1 else None
            if info == LATE_ENTRY and detail:
                self.late_entries += 1
                self.entry_pcs[f"0x{detail[0]:08X} exc {detail[1]}"] += 1
            elif info == LATE_DECODE and detail:
                self.late_decodes += 1
                self.decode_pcs[f"0x{detail[0]:08X}"] += 1
                self.decoded[f"{'R' if data & 0x40 else 'W'} ${addr:04X}"] += 1
            elif info == DMA_ASSERT:
                self.asserts += 1

    def summary(self) -> dict:
        return {"late_entries": self.late_entries, "late_decodes": self.late_decodes, "dma_asserts": self.asserts,
                "tap_lost": self.lost, "late_entry_pcs": dict(self.entry_pcs.most_common(12)),
                "late_decode_pcs": dict(self.decode_pcs.most_common(12)),
                "late_decode_accesses": dict(self.decoded.most_common(12))}


def run_phase(name: str, seconds: float, jobs: bool) -> dict:
    counters, rng = Counters(), random.Random(1)
    tap = Tap() if opts.tap else None
    every = TAP_SAMPLE_S if tap else SAMPLE_S
    row = {"jobs": 0, "bytes": 0, "refused": 0, "last_refusal": None, "not_landed": 0, "usb_requests": 0}
    start = time.monotonic()
    next_sample, data = start + every, None
    while (now := time.monotonic()) < start + seconds:
        if jobs:
            data = rng.randbytes(opts.payload)
            try:
                link.write_spans([(TARGET, len(data))], data, slice_bytes=opts.slice, gap_us=opts.wait,
                                 lock_to=None if opts.unlocked else SAMPLE_STORE, drain=0.0)
                row["jobs"] += 1
                row["bytes"] += len(data)
            except Refused as e:
                row["refused"] += 1
                row["last_refusal"] = e.text
                data = None
            if opts.usb:
                row["usb_requests"] += usb_load(link, opts.usb)
            if opts.read:
                link.read_memory(opts.read_at, opts.read, drain=0.0)
                row["reads"] = row.get("reads", 0) + 1
        else:
            time.sleep(0.1)
        if now >= next_sample:
            next_sample += every
            if tap:
                tap.take()
            if counters.sample() == 0:
                row["client_stopped_at_s"] = round(now - start, 1)
                break
            if data is not None and link.read_memory(TARGET, len(data), drain=0.0) != data:
                row["not_landed"] += 1
    counters.sample()
    row.update(seconds=round(time.monotonic() - start, 1), **counters.summary())
    if tap:
        tap.take(SNOOP_STOP)
        row.update(tap.summary())
    log(f"[{name}] {row}")
    return row


prg = assemble("reureadback" + ("_hammer" if opts.hammer else "") + ("_sprites" if opts.sprites else ""),
               primary=firmware.primary if firmware else None)
link.push_file(prg.read_bytes(), REMOTE, "sd")
settings = Settings(screen, keys, log=log)
result = {"options": vars(opts), "phases": {}}
with contextlib.ExitStack() as stack:
    stack.enter_context(settings.special_io(REU))
    try:
        board.ensure_menu()
        link.write_memory(READY, b"\0")
        link.launch(REMOTE, "sd")
        # The launch's own lines (the REU's preload among them) can come a second or more after its
        # ack, so each poll waits for the line to go quiet first.
        deadline = time.monotonic() + START_S
        while True:
            time.sleep(0.5)
            if link.read_memory(READY, 1, drain=LAUNCH_QUIET_S)[0] == READY_VALUE:
                break
            if time.monotonic() > deadline:
                raise SystemExit("the client never reported ready")
        latch = opts.nmi_period - 1
        link.write_memory(CIA2_TA, bytes([latch & 0xFF, latch >> 8]))
        link.write_memory(CIA2_ICR, b"\x81")
        link.write_memory(CIA2_CRA, b"\x11")
        check = Counters()
        time.sleep(1.0)
        if check.sample() == 0 or check.bad:
            raise SystemExit(f"the client counted {check.rounds} good and {check.bad} bad rounds in 1 s: "
                             "is Special IO an REU?")
        result["phases"]["idle"] = run_phase("idle", opts.idle_seconds, jobs=False)
        result["phases"]["jobs"] = run_phase("jobs", opts.seconds, jobs=True)
        result["misses"] = result["phases"]["jobs"]["misses"]
    finally:
        board.reset()
        board.wait_for_menu()
