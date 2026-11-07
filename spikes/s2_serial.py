"""S2: the board over USB serial through the suite's link, read-only.

Finds the port by chip id, then: fwcheck, the version banner, a few pings
with timing, the probe (image and board kind), DMA screen reads when the
board allows them, and a directory listing of the SD root. Counts every
unprompted byte the board sent between commands (risk R4).

    python spikes/s2_serial.py --uid <chip id> --out C:/tr-build/hil/spikes/s2
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from treadmill.link.discover import find_port   # noqa: E402
from treadmill.link.transport import list_teensyrom_ports, open_serial   # noqa: E402
from treadmill.link.trlink import Link, LinkError   # noqa: E402


def timed(label, fn, times=1):
    out = None
    stamps = []
    for _ in range(times):
        t0 = time.perf_counter()
        out = fn()
        stamps.append(round((time.perf_counter() - t0) * 1000, 1))
    print(f"{label}: {stamps} ms")
    return out, stamps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--uid", required=True)
    ap.add_argument("--port-hint")
    ap.add_argument("--out", default="C:/tr-build/hil/spikes/s2")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ports = list_teensyrom_ports()
    print("PJRC ports:", ports)
    found = find_port(args.uid, args.port_hint)
    print(f"board {args.uid} on {found.port} (by {found.by})")

    stray: list[bytes] = []
    summary = {"port": found.port, "found_by": found.by}
    with Link(open_serial(found.port), on_unsolicited=stray.append) as link:
        image, ms = timed("fw_check", link.fw_check, 3)
        summary["image"] = image
        summary["fw_check_ms"] = ms
        text, ms = timed("version", link.version)
        print("version text:\n" + "\n".join("  | " + l for l in text.splitlines()))
        banner = link.banner()
        summary["banner"] = {k: v for k, v in banner.__dict__.items() if k != "raw"}
        print("banner:", json.dumps(summary["banner"]))
        try:
            ready, ms = timed("ping x5", link.ping, 5)
            print("ping ->", repr(ready))
            summary["ping"] = ready
            summary["ping_ms"] = ms
        except LinkError as e:
            print("ping refused:", e)
            summary["ping"] = f"refused: {e}"
        probe = link.probe()
        summary["probe"] = {"image": probe.image, "dma": probe.dma, "c128": link.c128}
        print("probe:", summary["probe"])
        if probe.dma:
            dump, ms = timed("read_screen x3", link.read_screen, 3)
            summary["read_screen_ms"] = ms
            summary["screen"] = {"address": dump.address, "charset": dump.charset, "rows": dump.rows}
            print(f"screen at ${dump.address:04X}, charset {dump.charset}:")
            for i, row in enumerate(dump.rows):
                print(f"  {i:2d} |{row}|")
            (out / "screen.txt").write_text("\n".join(dump.rows) + "\n")
        try:
            listing, ms = timed("list_directory sd:/", lambda: link.list_directory("/", "sd"))
            names = [getattr(e, "name", e) for e in (listing if isinstance(listing, list) else getattr(listing, "entries", listing))]
            summary["sd_root"] = [str(n) for n in names][:40]
            print(f"SD root: {len(names)} entries: {summary['sd_root'][:12]}")
        except LinkError as e:
            print("list_directory failed:", e)
            summary["sd_root"] = f"failed: {e}"
        summary["stray_bytes"] = sum(len(b) for b in stray)
        summary["stray_samples"] = [b[:80].decode("latin-1") for b in stray[:5]]
        print(f"unprompted bytes between commands: {summary['stray_bytes']} in {len(stray)} chunk(s)")
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
