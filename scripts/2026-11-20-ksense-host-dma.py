"""2026-11-20: does a host DMA access during Kernal Replace's DMA sense leave
one pass of krread on the wrong bank, and do host DMA writes then corrupt
C64 RAM?

    treadmill exec --station <id> --fw ref=main --define Dbg_SerTimChg --define Dbg_SerDMA \\
        scripts/2026-11-20-ksense-host-dma.py --json [-- --mode dma|skoe|off --plan A,B,C,scan,D,scan,E]

Firmware: a Dbg_SerTimChg build (``tx#`` sets KSenseMode). Station: a TR+
running its main image (DMA), with video (the Settings driver reads the
page), on a C64. The knobs stay at their defaults throughout.

krread runs under Kernal Replace with the DMA sense (``--mode dma``), the
Skoe sense (``skoe``), or with Special IO set to None (``off``: no Kernal
Replace at all, so every HIRAM-set read is the machine's own KERNAL and only
the RAM side means anything). Every write to $01 makes the TeensyROM sense
HIRAM again; ``KDS_Begin`` skips the sense and keeps the last verdict when
another DMA user holds the line, and krread flips HIRAM every half-pass, so
one skipped sense is one half-pass on the wrong bank (LEN - 1 reads plus the
first read). The plan's steps, in order:

- ``A``: 2 s samples, 10 of them, nothing else on the link.
- ``B``: 20 s samples, 5.
- ``C``: 2 s samples, 5, plus 10-byte DMA reads as fast as the link answers.
- ``D``: like C, with 16-byte DMA writes to $C300 (memory krread never uses),
  each read back and compared.
- ``E``: A again, 10 samples: a RAM-side miss on every pass means a byte of
  the pattern under the KERNAL changed.
- ``scan``: read the marker run by DMA 12 times; reads that are mostly the RAM
  pattern are RAM (the CPU was halted with HIRAM clear), and any other byte
  in them is a corrupted RAM byte (offset, address and value).
- ``fix``: write the RAM pattern back over the marker run by DMA; RAM-side
  reads clean after it mean the RAM itself had changed.
- ``code``: read krread's code back by DMA and list the bytes that differ
  from the assembled file (the program never writes its own code).
- ``a``, ``d``: A and D with 2 and 1 samples, to step through a change.

Wrong half-passes following the number of DMA accesses rather than the
seconds point at the host's DMA. Puts back the Kernal file, Special IO and
KSenseMode (as the Kernal margin test does) and leaves the board at the menu.
``result`` holds each step's totals and samples, and each scan's findings.
"""

import argparse
import contextlib
import time

from treadmill.c64.settings import KERNAL_REPLACE, Settings
from treadmill.probes.krread import COUNTERS, DMA, KERNAL, KERNAL_SIZE, SKOE, KrReadProbe, ksense_mode, probe_kernal
from treadmill.programs.build import assemble

PROBE_KERNAL = "/treadmill/krprobe.bin"
SPARE = 0xC300
FAST_DRAIN = 0.02

parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=("dma", "skoe", "off"), default="dma")
parser.add_argument("--plan", default="A,B,C,D,E")
opts = parser.parse_args()

if not board.dma_allowed():
    raise SystemExit("this needs a TR+ running its main image (DMA)")
if not dbg.available("t"):
    raise SystemExit("this needs a Dbg_SerTimChg build")

dbg.restore_defaults()
image, defines = probe_kernal(link.read_memory(KERNAL, KERNAL_SIZE))
link.push_file(image, PROBE_KERNAL, "sd")
prg = assemble("krread", primary=getattr(firmware, "primary", None), extra=defines)
length, mark, pat = defines["LEN"], defines["MARK"], defines["PAT"]
probe = KrReadProbe(board, screen, prg, length, log=log)   # its own counters() below read by DMA, on purpose
settings = Settings(screen, keys, log=log)


def counters(drain=None):
    b = link.read_memory(COUNTERS, 10) if drain is None else link.read_memory(COUNTERS, 10, drain=drain)
    return tuple(b[i] | b[i + 1] << 8 for i in range(0, 10, 2))


def run(name, samples, seconds, extra=None):
    rows = []
    c0 = counters()
    for _ in range(samples):
        start, accesses, mismatches, n = time.monotonic(), 1, 0, 0
        while time.monotonic() - start < seconds:
            if extra == "read":
                counters(FAST_DRAIN)
                accesses += 1
            elif extra == "write":
                data = bytes((n * 16 + k) & 0xFF for k in range(16))
                link.write_memory(SPARE, data, drain=FAST_DRAIN)
                back = link.read_memory(SPARE, 16, drain=FAST_DRAIN)
                mismatches += back != data
                accesses += 2
                n += 1
            else:
                time.sleep(0.05)
        c1 = counters()
        passes, rom, ram, rom_first, ram_first = ((a - b) & 0xFFFF for a, b in zip(c1, c0))
        c0 = c1
        rows.append({"seconds": round(time.monotonic() - start, 2), "accesses": accesses, "passes": passes,
                     "wrong_halves": rom_first + ram_first, "rom": rom, "ram": ram,
                     "rom_first": rom_first, "ram_first": ram_first, "write_mismatches": mismatches})
        log(f"[{name}] {rows[-1]}")
    total = {k: sum(r[k] for r in rows) for k in ("seconds", "accesses", "passes", "wrong_halves", "ram",
                                                  "write_mismatches")}
    total["seconds"] = round(total["seconds"], 1)
    log(f"[{name}] total {total}")
    return {"total": total, "samples": rows}


def scan(name, reads=12):
    ram_reads, bad, seen = 0, {}, {}
    for _ in range(reads):
        got = link.read_memory(mark, length)
        seen[got.hex(" ")] = seen.get(got.hex(" "), 0) + 1
        if sum(b == pat for b in got) < length // 2:
            continue
        ram_reads += 1
        for i, b in enumerate(got):
            if b != pat:
                bad.setdefault(i, set()).add(b)
        time.sleep(0.07)
    found = [{"offset": i, "address": f"${mark + i:04X}", "values": [f"${v:02X}" for v in sorted(vs)]}
             for i, vs in sorted(bad.items())]
    log(f"[{name}] {ram_reads} of {reads} reads were RAM; bytes not the pattern ${pat:02X}: {found or 'none'}")
    for text, n in seen.items():
        log(f"[{name}]   {n}x {text}")
    return {"ram_reads": ram_reads, "reads": reads, "corrupt": found, "contents": seen}


STEPS = {"A": lambda: run("A 2 s", 10, 2.0), "B": lambda: run("B 20 s", 5, 20.0),
         "C": lambda: run("C 2 s + reads", 5, 2.0, "read"), "D": lambda: run("D 2 s + writes", 5, 2.0, "write"),
         "d": lambda: run("d 2 s + writes", 1, 2.0, "write"), "a": lambda: run("a 2 s", 2, 2.0),
         "E": lambda: run("E 2 s again", 10, 2.0)}

def fix(name):
    """Write the RAM pattern back over the marker run by DMA (a write always
    reaches RAM): RAM-side reads that come clean after it mean the RAM had
    changed; still wrong means something else (HIRAM not clearing)."""
    link.write_memory(mark, bytes([pat]) * length)
    log(f"[{name}] wrote ${pat:02X} x {length} to ${mark:04X}")
    return {"wrote": length}


def code(name):
    """krread's code read back by DMA against the assembled file: a byte that
    differs was written while it ran (the program never writes its code)."""
    data = prg.read_bytes()
    load, body = data[0] | data[1] << 8, data[2:]
    got = link.read_memory(load, len(body))
    diffs = [{"address": f"${load + i:04X}", "file": f"${a:02X}", "ram": f"${b:02X}"}
             for i, (a, b) in enumerate(zip(body, got)) if a != b]
    log(f"[{name}] code ${load:04X}-${load + len(body) - 1:04X}: {diffs or 'as assembled'}")
    return {"diffs": diffs}


result = {"mode": opts.mode, "len": length, "mark": f"${mark:04X}", "pat": f"${pat:02X}", "steps": []}
with contextlib.ExitStack() as stack:
    if opts.mode == "off":
        stack.enter_context(settings.special_io("None"))
    else:
        stack.enter_context(settings.kernal_file(menu, "SD:" + PROBE_KERNAL))
        stack.enter_context(settings.special_io(KERNAL_REPLACE))
        stack.enter_context(ksense_mode(dbg, DMA if opts.mode == "dma" else SKOE))
    try:
        probe.setup()
        for i, step in enumerate(s.strip() for s in opts.plan.split(",")):
            special = {"scan": scan, "fix": fix, "code": code}
            out = special[step](f"{step} {i}") if step in special else STEPS[step]()
            result["steps"].append({"step": step, **out})
    finally:
        probe.teardown()
dbg.restore_defaults()
