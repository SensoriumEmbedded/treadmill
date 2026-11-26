"""2026-11-24: does the TR+ let go of the bus after a host DMA when Kernal
Replace is on and the screen is blanked?

    treadmill exec --station <id> --fw ref=<commit> \\
        scripts/2026-11-24-closedma-blank-screen.py --json [-- --screen off|on --mode kr|none --accesses 20]

Firmware: any TR+ main image; nothing here needs a Dbg_* define. Station: a
TR+ running its main image (DMA), with video (the Settings driver), on a C64
(the Skoe sense, a C64's default).

PR #106's case. While /DMA is held the address bus floats, and Kernal
Replace's check claims every held cycle that reads as a KERNAL fetch, so the
release CloseDMA asks for lands only in a cycle with BA low: a bad line or a
sprite fetch. With the screen blanked ($D011 DEN off) and no sprites there
are none, and CloseDMA gives up after its ceilings (about 100 ms): the board
answers the command with a FailToken.

It launches ``10 POKE53269,0:POKE53265,11`` / ``20 A=A+1:GOTO20``
(``--screen on`` pokes 27 instead: the control with bad lines) with Special IO
Kernal Replace and the machine's own KERNAL as the file (``--mode none``:
Special IO None, the control without the snoop). Then ``--accesses`` times it
writes 16 bytes to $C300 and reads them back, timing each and noting a
refusal; three refusals in a row end it early. Last it reads the variable A
twice, 0.5 s apart (A not changing means BASIC stopped).

Puts back the Kernal file and Special IO and leaves the board at the menu.
``result`` holds each access (op, ok, ms, error, match), the counts, and A.
"""

import argparse
import contextlib
import statistics
import time

from treadmill.c64.settings import KERNAL_REPLACE, Settings
from treadmill.link.trlink import LinkError, Refused
from treadmill.probes.krread import KERNAL, KERNAL_SIZE

POKE, GOTO, PLUS, EQUALS = b"\x97", b"\x89", b"\xaa", b"\xb2"
KERNAL_COPY = "/treadmill/krown.bin"
REMOTE = "/treadmill/blank.prg"
SPARE = 0xC300
FAST_DRAIN = 0.02

parser = argparse.ArgumentParser()
parser.add_argument("--screen", choices=("off", "on"), default="off")
parser.add_argument("--mode", choices=("kr", "none"), default="kr")
parser.add_argument("--accesses", type=int, default=20)
opts = parser.parse_args()


def basic(lines):
    out, addr = bytearray(), 0x0801
    for number, body in lines:
        nxt = addr + 4 + len(body) + 1
        out += nxt.to_bytes(2, "little") + number.to_bytes(2, "little") + body + b"\0"
        addr = nxt
    return bytes(out) + b"\0\0"


D011 = b"11" if opts.screen == "off" else b"27"
TEXT = basic([(10, POKE + b"53269,0:" + POKE + b"53265," + D011),
              (20, b"A" + EQUALS + b"A" + PLUS + b"1:" + GOTO + b"20")])
PROGRAM = b"\x01\x08" + TEXT
VARS = 0x0801 + len(TEXT)                        # VARTAB: A's entry is the first variable

if not board.dma_allowed():
    raise SystemExit("this needs a TR+ running its main image (DMA)")

log(f"{station.id}: {board.banner.version} on a {board.banner.machine}; screen {opts.screen}, mode {opts.mode}")
if opts.mode == "kr":
    link.push_file(link.read_memory(KERNAL, KERNAL_SIZE), KERNAL_COPY, "sd")
link.push_file(PROGRAM, REMOTE, "sd")
settings = Settings(screen, keys, log=log)


def timed(op, fn):
    began = time.monotonic()
    try:
        value = fn()
        return {"op": op, "ok": True, "ms": round((time.monotonic() - began) * 1000, 1)}, value
    except Refused as e:
        return {"op": op, "ok": False, "ms": round((time.monotonic() - began) * 1000, 1), "error": str(e)}, None
    except LinkError as e:
        return {"op": op, "ok": False, "ms": round((time.monotonic() - began) * 1000, 1),
                "error": f"{type(e).__name__}: {e}", "link_lost": True}, None


def a_value():
    row, entry = timed("read A", lambda: link.read_memory(VARS, 7, drain=FAST_DRAIN))
    if entry is None:
        return row.get("error", "?")
    return entry[2:7].hex() if entry[:2] == b"A\0" else f"no A at ${VARS:04X}: {entry.hex()}"


result = {"screen": opts.screen, "mode": opts.mode, "banner": f"{board.banner.version} / {board.banner.machine}",
          "accesses": []}
with contextlib.ExitStack() as stack:
    if opts.mode == "kr":
        stack.enter_context(settings.kernal_file(menu, "SD:" + KERNAL_COPY))
        stack.enter_context(settings.special_io(KERNAL_REPLACE))
    else:
        stack.enter_context(settings.special_io("None"))
    try:
        board.ensure_menu()
        link.launch(REMOTE, "sd")
        time.sleep(3.0)
        refusals_in_row = 0
        for n in range(opts.accesses):
            data = bytes((n * 16 + k) & 0xFF for k in range(16))
            row, _ = timed("write", lambda: link.write_memory(SPARE, data, drain=FAST_DRAIN))
            result["accesses"].append(row)
            back_row, back = timed("read", lambda: link.read_memory(SPARE, 16, drain=FAST_DRAIN))
            back_row["match"] = back == data if back is not None else None
            result["accesses"].append(back_row)
            log(f"[{n}] {row} {back_row}")
            if row.get("link_lost") or back_row.get("link_lost"):
                break
            refusals_in_row = refusals_in_row + 1 if not (row["ok"] and back_row["ok"]) else 0
            if refusals_in_row >= 3:
                break
        a1 = a_value()
        time.sleep(0.5)
        a2 = a_value()
        result["a"] = [a1, a2]
        result["running"] = a1 != a2
    finally:
        board.reset()
        board.wait_for_menu()

rows = result["accesses"]
ok_ms = [r["ms"] for r in rows if r["ok"]]
bad_ms = [r["ms"] for r in rows if not r["ok"]]
result["counts"] = {"ok": len(ok_ms), "refused": len(bad_ms),
                    "mismatches": sum(1 for r in rows if r.get("match") is False),
                    "ok_ms_median": statistics.median(ok_ms) if ok_ms else None,
                    "refused_ms_median": statistics.median(bad_ms) if bad_ms else None}
log(f"counts {result['counts']}")
