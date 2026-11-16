"""2026-11-15: the REU checker's REU size after each of the manufacturing test's remote actions.

The REU handler sizes the REU from what RAM2 can give when it starts (``InitHndlr_REU`` in
IOH_REU.c) and rounds down to a power of two, so one 8 KB bank short of 64 makes a 256K REU.
The manufacturing test's runs on a new TR+ got 63 banks; the same steps by hand get 512K. Each
probe presses hot key 2 at the menu, reads the bank count and size from serial, then resets back
to the menu, so the steps between probes show which one costs RAM2. Start from a fresh power-up.

    treadmill exec --station <id> scripts/2026-11-15-reu-size-after-steps.py
"""

import re
import time

KEY_2 = 0x32
XPT_PATH = "/TeensyROM Specific/TR+ C64 Expansion Port Test"
SIZE = re.compile(r"(?:Resizing REU from (\d+) banks to \d+ banks\s*)?REU Size: (\d+)K bytes")
CLEAN = re.compile(r"bytes  CLEAN")

tap = link.stream.tap


def mark() -> int:
    return len(tap.chunks)


def since(m: int) -> str:
    return b"".join(c for _, c in tap.chunks[m:]).decode("latin-1")


def wait_for(m: int, found, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hit = found(since(m))
        if hit:
            return hit
        time.sleep(0.25)
    raise RuntimeError(f"no {what} within {timeout:g} s; serial since: {since(m)[-400:]!r}")


probes = []


def probe(after: str) -> None:
    m = mark()
    keys.press([KEY_2])
    board.reu_live = True
    hit = wait_for(m, SIZE.search, 20, "REU size")
    kb = int(hit.group(2))
    banks = int(hit.group(1)) if hit.group(1) else kb // 8
    probes.append({"after": after, "banks": banks, "reu_kb": kb})
    log(f"[reu] after {after}: {banks} banks, REU {kb}K")
    time.sleep(1.0)
    board.reset()
    board.reu_live = False
    board.wait_for_menu(60)
    time.sleep(1.0)


probe("power-up")
probe("one REU launch")
link.list_directory("/", "sd")
probe("an SD root listing over USB")
m = mark()
link.launch(XPT_PATH, "teensy")
wait_for(m, lambda t: len(CLEAN.findall(t)) >= 5, 60, "the Expansion Port test's first pass")
time.sleep(2.0)
board.reset()
board.wait_for_menu(60)
time.sleep(1.0)
probe("an Expansion Port test launched over USB")
result = {"probes": probes}
