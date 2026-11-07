"""S4: switch the Kawari's chip model over the TR+'s DMA, then the person
cycles the mains and the TR's machine line should follow.

The Kawari's extra registers (doc/REGISTERS.md in randyrossi/vicii-kawari)
unlock by writing "VIC2" to $D03F (jumper 2 shorted); $D03F bit 6 maps the
register overlay into video-memory port A ($D039 address low, $D03A high,
$D03B data), bit 7 persists writes to the EEPROM (bit 5 is the busy flag
to wait on), and $80 deactivates. CHIP_MODEL is overlay $1F: 0 = 6567R8
(NTSC), 1 = 6569R3 (PAL), 2 = 6567R56A (NTSC), 3 = 6569R1 (PAL); a change
applies on the next cold boot. The chip model is exempt from the
persistence lock jumper.

    python spikes/s4_kawari.py --uid <chip id> --read
    python spikes/s4_kawari.py --uid <chip id> --set 0 --persist
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from treadmill.link.discover import find_port   # noqa: E402
from treadmill.link.transport import open_serial   # noqa: E402
from treadmill.link.trlink import Link   # noqa: E402

ACTIVATE = 0xD03F
ADDR_LO, ADDR_HI, DATA = 0xD039, 0xD03A, 0xD03B
OVERLAY, PERSIST, BUSY, DEACTIVATE = 0x20, 0x40, 0x10, 0x80
AUTO_INC = 0x01               # port A function bits 1-2: the doc's examples set AUTO INC before a write
CHIP_MODEL, VERSION, CAP_LO = 0x1F, 0x83, 0x87
MODELS = {0: "6567R8 (NTSC)", 1: "6569R3 (PAL)", 2: "6567R56A (NTSC)", 3: "6569R1 (PAL)"}


def poke(link: Link, address: int, value: int) -> None:
    link.write_memory(address, bytes([value]))


def peek(link: Link, address: int) -> int:
    return link.read_memory(address, 1)[0]


def read_overlay(link: Link, index: int) -> int:
    poke(link, ADDR_LO, index)
    poke(link, ADDR_HI, 0x00)
    return peek(link, DATA)


def unlock(link: Link, persist: bool, attempts: int = 4) -> int:
    """Activate the extra registers and map the overlay, verifying $D03F reads
    back as asked before anything else is written: with the four unlock
    bytes arriving 600 ms apart over DMA the detector sometimes misses, and
    every read then returns $FF, which must stop the run, not pass as flags."""
    for attempt in range(1, attempts + 1):
        for b in b"VIC2":
            poke(link, ACTIVATE, b)
        poke(link, ACTIVATE, OVERLAY | AUTO_INC)
        flags = peek(link, ACTIVATE)
        if flags & 0x80 == 0 and flags & OVERLAY:
            break
        print(f"unlock attempt {attempt}: $D03F reads ${flags:02X}, not activated")
        time.sleep(0.5)
    else:
        raise RuntimeError("the Kawari's extra registers did not activate (jumper 2 open, or the unlock missed)")
    if persist:
        poke(link, ACTIVATE, OVERLAY | PERSIST | AUTO_INC)
        flags = peek(link, ACTIVATE)
        if flags & (OVERLAY | PERSIST) != OVERLAY | PERSIST:
            raise RuntimeError(f"persist flag did not set ($D03F = ${flags:02X})")
    return flags


def wait_not_busy(link: Link, timeout: float = 2.0) -> int:
    deadline = time.monotonic() + timeout
    while True:
        flags = peek(link, ACTIVATE)
        if not flags & BUSY:
            return flags
        if time.monotonic() > deadline:
            raise RuntimeError(f"Kawari persist stayed busy ($D03F = ${flags:02X})")
        time.sleep(0.05)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--uid", required=True)
    ap.add_argument("--port-hint")
    ap.add_argument("--read", action="store_true", help="unlock, read CHIP_MODEL, VERSION and CAP_LO, deactivate")
    ap.add_argument("--set", type=int, choices=range(4), help="write this chip model")
    ap.add_argument("--persist", action="store_true", help="persist the write to the Kawari's EEPROM (needed for a cold boot to pick it up)")
    args = ap.parse_args()
    if args.set is None and not args.read:
        ap.error("--read or --set N")
    with Link(open_serial(find_port(args.uid, args.port_hint).port)) as link:
        probe = link.probe()
        if not probe.dma:
            print("DMA is not allowed on this board/image; S4 needs a TR+ main image"); return 1
        print(f"board: {probe.banner.version}, machine line {probe.banner.machine} {probe.banner.video} {probe.banner.hz} Hz")
        flags = unlock(link, persist=bool(args.set is not None and args.persist))
        print(f"$D03F after unlock: ${flags:02X} (overlay on{', persist on' if flags & PERSIST else ''})")
        model = read_overlay(link, CHIP_MODEL)
        print(f"CHIP_MODEL ($1F) = {model} = {MODELS.get(model, '?')}; VERSION ($83) = ${read_overlay(link, VERSION):02X}; "
              f"CAP_LO ($87) = ${read_overlay(link, CAP_LO):02X}")
        if args.set is not None and model in MODELS:
            poke(link, ADDR_LO, CHIP_MODEL)
            poke(link, ADDR_HI, 0x00)
            poke(link, DATA, args.set)
            flags = wait_not_busy(link)
            back = read_overlay(link, CHIP_MODEL)
            print(f"wrote CHIP_MODEL = {args.set} ({MODELS[args.set]}){' with persist' if args.persist else ''}; "
                  f"read back {back}; $D03F = ${flags:02X}")
        poke(link, ACTIVATE, DEACTIVATE)
        print("extra registers deactivated; the model applies on the next cold boot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
