"""2026-11-20: does host DMA corrupt a BASIC program running under Kernal
Replace?

    treadmill exec --station <id> --fw ref=main --define Dbg_SerTimChg \\
        scripts/2026-11-20-kr-host-dma-basic.py --json [-- --mode skoe|dma|off --seconds 60]

Firmware: a Dbg_SerTimChg build (``tx#`` sets KSenseMode). Station: a TR+
running its main image (DMA), with video (the Settings driver), on a C64.

krread showed host DMA reads and writes corrupting the code a program runs
while Kernal Replace is active, with either HIRAM sense, and not with
Kernal Replace off. krread reads the KERNAL and flips HIRAM all the time;
this asks whether an ordinary program sees it. It launches
``10 A=A+1:GOTO 10`` (the interpreter reads its text from RAM on every pass
and the KERNAL's IRQ runs 60 times a second) with Special IO set to Kernal
Replace and the machine's own KERNAL as the file (``--mode skoe``, the C64's
default sense, or ``dma``), or with Special IO None (``off``). Then, for
``--seconds``, the host writes 16 bytes to $C300 and reads them back as
fast as the link answers (``--idle``: nothing between the checks, the
control; ``--usb``: version requests as fast as the board answers instead,
USB traffic with no DMA, counted as accesses). Every 10 s it reads the program text back by DMA and the variable
A twice, 0.5 s apart (A not changing means BASIC stopped).

Puts back the Kernal file, Special IO and KSenseMode and leaves the board at
the menu. ``result`` holds each check: seconds, accesses, write mismatches,
the program bytes that differ, and whether A moved.
"""

import argparse
import contextlib
import time

from treadmill.c64.settings import KERNAL_REPLACE, Settings
from treadmill.probes.krread import DMA, KERNAL, KERNAL_SIZE, SKOE, ksense_mode
from treadmill.probes.usbload import usb_load

PROGRAM = bytes([0x01, 0x08,                     # load address $0801
                 0x0F, 0x08, 0x0A, 0x00,         # link $080F, line 10
                 0x41, 0xB2, 0x41, 0xAA, 0x31,   # A = A + 1
                 0x3A, 0x89, 0x31, 0x30, 0x00,   # : GOTO 10
                 0x00, 0x00])                    # end of program
TEXT = PROGRAM[2:]
VARS = 0x0801 + len(TEXT)                        # VARTAB: A's entry is the first variable
KERNAL_COPY = "/treadmill/krown.bin"
REMOTE = "/treadmill/aloop.prg"
SPARE = 0xC300
FAST_DRAIN = 0.02

parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=("skoe", "dma", "off"), default="skoe")
parser.add_argument("--seconds", type=float, default=60.0)
parser.add_argument("--idle", action="store_true", help="no writes between the checks (the control)")
parser.add_argument("--usb", action="store_true", help="version requests between the checks instead of DMA")
opts = parser.parse_args()

if not board.dma_allowed():
    raise SystemExit("this needs a TR+ running its main image (DMA)")

dbg.restore_defaults()
link.push_file(link.read_memory(KERNAL, KERNAL_SIZE), KERNAL_COPY, "sd")
link.push_file(PROGRAM, REMOTE, "sd")
settings = Settings(screen, keys, log=log)


def a_value():
    entry = link.read_memory(VARS, 7)
    return entry[2:7].hex() if entry[:2] == b"A\0" else f"no A at ${VARS:04X}: {entry.hex()}"


def check(name, start, accesses, mismatches):
    text = link.read_memory(0x0801, len(TEXT))
    diffs = [f"${0x0801 + i:04X} {a:02X}->{b:02X}" for i, (a, b) in enumerate(zip(TEXT, text)) if a != b]
    a1 = a_value()
    time.sleep(0.5)
    a2 = a_value()
    row = {"seconds": round(time.monotonic() - start, 1), "accesses": accesses, "write_mismatches": mismatches,
           "text_diffs": diffs, "a": a2, "running": a1 != a2}
    log(f"[{name}] {row}")
    return row


result = {"mode": opts.mode, "checks": []}
with contextlib.ExitStack() as stack:
    if opts.mode == "off":
        stack.enter_context(settings.special_io("None"))
    else:
        stack.enter_context(settings.kernal_file(menu, "SD:" + KERNAL_COPY))
        stack.enter_context(settings.special_io(KERNAL_REPLACE))
        stack.enter_context(ksense_mode(dbg, DMA if opts.mode == "dma" else SKOE))
    try:
        board.ensure_menu()
        link.launch(REMOTE, "sd")
        time.sleep(3.0)
        start, accesses, mismatches, n, next_check = time.monotonic(), 0, 0, 0, 0.0
        result["checks"].append(check("start", start, accesses, mismatches))
        while time.monotonic() - start < opts.seconds:
            if opts.idle or opts.usb:
                if opts.usb:
                    accesses += usb_load(link, 1.0)
                else:
                    time.sleep(0.05)
                if time.monotonic() - start >= next_check + 10.0:
                    next_check += 10.0
                    result["checks"].append(check(f"{next_check:.0f} s", start, accesses, mismatches))
                continue
            data = bytes((n * 16 + k) & 0xFF for k in range(16))
            link.write_memory(SPARE, data, drain=FAST_DRAIN)
            mismatches += link.read_memory(SPARE, 16, drain=FAST_DRAIN) != data
            accesses += 2
            n += 1
            if time.monotonic() - start >= next_check + 10.0:
                next_check += 10.0
                result["checks"].append(check(f"{next_check:.0f} s", start, accesses, mismatches))
        result["checks"].append(check("end", start, accesses, mismatches))
    finally:
        board.reset()
        board.wait_for_menu()
dbg.restore_defaults()
