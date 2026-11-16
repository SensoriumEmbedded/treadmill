"""nS_DMASetup (knob e) swept with the read latch and write release held where the
defaults put them, for TeensyROM PR #99 (DMAByte's setup wait made fine). 2026-11-12.

    treadmill exec --station <id> --fw ref=<branch> --define Dbg_SerTimChg --define Dbg_SerDMA \
        scripts/2026-11-12-dmasetup-sweep.py -- --from 330 --to 480 --step 10 [--probe z|reu|both]
        [--passes 300] [--dwell 10] [--prg reuclient.prg]

Per value: e = value, w = (default e + default w) - value, y = (default e + default y) - value.
The sums come from the defaults `td` applies on this machine, so PAL, NTSC and NTSC C128 each
hold their own (884/844, 854/834, 854/834 on PR #99's values).

- z: the firmware's z### DMA pattern test at $C000 (five patterns; each pass a prefill write,
  a read, a pattern write and a read back). Lone host DMA writes and reads.
- reu: reuclient.prg (tools/bench/reuclient.a) loops REU stash, fetch and compare; it runs
  --dwell seconds at the value, and its counters are read back at the defaults. The C64's own
  REU transfers, whose first byte is the one PR #99 is about. Needs Special IO set to an REU.
- noise: while each probe runs, the pixels inside the text area that change by more than
  NOISE_LEVEL between consecutive frames; the median per frame pair, against the same count
  with no DMA. A drive that lands in the VIC's half of the cycle shows here, not in the counts.

The defaults are restored (`td`) in a finally, and the C64 is reset to the menu at the end.
"""
import argparse
import re
import statistics
import threading
import time

import cv2
import numpy as np

p = argparse.ArgumentParser(prog="dmasetup-sweep")
p.add_argument("--from", dest="start", type=int, required=True)
p.add_argument("--to", dest="stop", type=int, required=True)
p.add_argument("--step", type=int, default=10)
p.add_argument("--probe", choices=("z", "reu", "both"), default="z")
p.add_argument("--passes", type=int, default=300, help="z### pages per pattern")
p.add_argument("--dwell", type=float, default=10.0, help="seconds of the REU loop per value")
p.add_argument("--prg", help="reuclient.prg, for --probe reu or both")
args = p.parse_args()
if args.probe != "z" and not args.prg:
    p.error("--probe reu needs --prg")

NOISE_LEVEL = 48
READY, ROUNDS = 0xC104, 0xC110
KNOB = re.compile(r"(nS_\w+|Cyc_\w+)\s+(\d+)\s+\(t(\w)#+\)")
PATTERN = re.compile(r"^(\$\w\w(?:/\$\w\w alt)?) over \$(\w\w) @ \$\w+: (\d+) bad of (\d+) bytes(?:, pre-fill (\d+) bad)?",
                     re.M)


def knobs(listing: str) -> dict[str, int]:
    return {m.group(3): int(m.group(2)) for m in KNOB.finditer(listing)}


class Noise:
    """Changed-pixel counts between consecutive frames, inside the text area, while running."""

    def __init__(self):
        self.ok = video is not None and screen.calib is not None
        self.counts: list[int] = []
        self._stop = threading.Event()
        self._thread = None
        if self.ok:
            area = screen.calib.text_area
            x, y, w, h = area() if callable(area) else area
            self.box = (int(x), int(y), int(x + w), int(y + h))

    def _crop(self, frame):
        x0, y0, x1, y1 = self.box
        return cv2.cvtColor(frame.image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)

    def _run(self):
        last = None
        prev = None
        prev_frame = None
        while not self._stop.is_set():
            try:
                last = video.next_frame(after=last, timeout=2.0)
            except Exception:  # noqa: BLE001 - a dropped frame only thins the sample
                continue
            img = self._crop(last)
            if prev is not None and prev.shape == img.shape:
                count = int(np.count_nonzero(cv2.absdiff(img, prev) > NOISE_LEVEL))
                self.counts.append(count)
                if count >= self.worst[0]:
                    self.worst = (count, prev_frame, last.image)
                if len(self.counts) == 60:
                    self.sample = last.image
            prev, prev_frame = img, last.image

    def save(self, name: str) -> None:
        if self.sample is not None:
            cv2.imwrite(str(artifacts / f"{name}.png"), self.sample)
        if self.worst[1] is not None:
            cv2.imwrite(str(artifacts / f"{name}.worst.png"), np.hstack([self.worst[1], self.worst[2]]))

    def __enter__(self):
        self.counts = []
        self.worst = (-1, None, None)
        self.sample = None
        if self.ok:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._thread:
            self._stop.set()
            self._thread.join(5)

    def summary(self) -> dict | None:
        if not self.ok or not self.counts:
            return None
        return {"pairs": len(self.counts), "median": statistics.median(self.counts), "max": max(self.counts)}


def set_point(value: int, sums: tuple[int, int]) -> dict[str, int]:
    dbg.set_knob("e", value)
    dbg.set_knob("w", sums[0] - value)
    got = knobs(dbg.set_knob("y", sums[1] - value))
    want = {"e": value, "w": sums[0] - value, "y": sums[1] - value}
    if any(got.get(k) != v for k, v in want.items()):
        raise SystemExit(f"knobs did not take: wanted {want}, the board lists {got}")
    return want


def z_probe(passes: int) -> dict:
    link._send(f"z{passes:03d}".encode())
    text, end = "", time.monotonic() + 30 + passes * 0.2
    while time.monotonic() < end and len(PATTERN.findall(text)) < 5 and "\n  no " not in text:
        text += link.stream.read_idle(5.0, 2.0, 60.0).decode("latin-1", "replace")
    rows = PATTERN.findall(text)
    if len(rows) < 5:
        return {"error": "the pattern test did not finish", "tail": text[-400:]}
    bad = sum(int(r[2]) for r in rows[1:])        # row 0 is the $ff-over-$ff control
    prefill = sum(int(r[4] or 0) for r in rows)
    return {"bad": bad, "bytes": sum(int(r[3]) for r in rows[1:]), "prefill_bad": prefill,
            "control_bad": int(rows[0][2]),
            "patterns": {f"{r[0]} over ${r[1]}": int(r[2]) for r in rows}}


def reu_counts() -> tuple[int, int]:
    lo, hi, elo, ehi = link.read_memory(ROUNDS, 4)
    return lo | hi << 8, elo | ehi << 8


def start_client(data: bytes) -> None:
    try:
        link.delete_file("/REUCLNT.PRG")
    except Exception:  # noqa: BLE001 - not there yet
        pass
    link.post_file(data, "/REUCLNT.PRG")
    link.write_memory(READY, b"\0")
    link.launch("/REUCLNT.PRG")
    end = time.monotonic() + 15
    while time.monotonic() < end:
        time.sleep(0.5)
        try:
            if link.read_memory(READY, 1)[0] == 0x5A:
                break
        except Exception:  # noqa: BLE001 - the board is still launching
            pass
    else:
        raise SystemExit("reuclient never reported ready")
    r0, e0 = reu_counts()
    time.sleep(1.0)
    r1, e1 = reu_counts()
    good, bad = (r1 - r0) & 0xFFFF, (e1 - e0) & 0xFFFF
    if good + bad == 0:
        raise SystemExit("reuclient is not looping: is Special IO set to an REU?")
    log(f"reuclient at the defaults: {good} good and {bad} bad rounds in 1 s")
    result["reu_at_defaults"] = {"good": good, "bad": bad}


def reu_probe(value: int, sums: tuple[int, int], dwell: float) -> dict:
    dbg.restore_defaults()
    r0, e0 = reu_counts()
    set_point(value, sums)
    time.sleep(dwell)
    dbg.restore_defaults()
    r1, e1 = reu_counts()
    return {"rounds": (r1 - r0) & 0xFFFF, "bad_pages": (e1 - e0) & 0xFFFF}   # rounds = good rounds only


if not (dbg.available("t") and (args.probe == "reu" or dbg.available("z"))):
    raise SystemExit("this build lacks Dbg_SerTimChg or Dbg_SerDMA: flash one with both defines")

values = list(range(args.start, args.stop + (1 if args.step > 0 else -1), args.step))
defaults = knobs(dbg.restore_defaults() + dbg.listing())
sums = (defaults["e"] + defaults["w"], defaults["e"] + defaults["y"])
machine = f"{board.banner.machine} {board.banner.video}"
log(f"{station.id} {board.banner.version} {machine}: defaults e{defaults['e']} "
    f"w{defaults['w']} y{defaults['y']}, holding release/latch at {sums[0]}/{sums[1]}")

result = {"station": station.id, "firmware": board.banner.version, "machine": machine,
          "defaults": {k: defaults[k] for k in ("e", "w", "y")}, "sums": list(sums), "z": [], "reu": []}
noise = Noise()
try:
    with noise:
        time.sleep(3)
    result["noise_baseline"] = noise.summary()
    noise.save("baseline")
    if args.probe in ("z", "both"):
        board.reset()
        time.sleep(6)
        for value in values:
            point = set_point(value, sums)
            with noise:
                row = {**point, **z_probe(args.passes)}
            row["noise"] = noise.summary()
            noise.save(f"z-e{value}")
            dbg.restore_defaults()
            result["z"].append(row)
            log(f"z  e{value}: {row.get('bad', '?')}/{row.get('bytes', '?')} bad, pre-fill {row.get('prefill_bad', '?')}, "
                f"noise {row['noise']}" + (f"  {row['error']}" if "error" in row else ""))
            link.ping()
    if args.probe in ("reu", "both"):
        board.reset()
        time.sleep(6)
        prg = open(args.prg, "rb").read()
        start_client(prg)
        for value in values:
            with noise:
                row = {"e": value, **reu_probe(value, sums, args.dwell)}
            row["noise"] = noise.summary()
            noise.save(f"reu-e{value}")
            result["reu"].append(row)
            log(f"reu e{value}: {row['bad_pages']} bad of {row['rounds']} rounds, noise {row['noise']}")
            r0, _ = reu_counts()
            time.sleep(0.5)
            if reu_counts()[0] == r0:              # the client stopped: crashed past an edge
                log(f"reu e{value}: the client stopped counting; resetting and relaunching it")
                row["client_stopped"] = True
                board.reset()
                time.sleep(6)
                start_client(prg)
finally:
    try:
        dbg.restore_defaults()
    finally:
        board.reset()

(artifacts / "dmasetup-sweep.txt").write_text(
    "\n".join([f"defaults {result['defaults']} sums {result['sums']} baseline noise {result.get('noise_baseline')}"]
              + [f"z   {r}" for r in result["z"]] + [f"reu {r}" for r in result["reu"]]) + "\n", encoding="utf-8")
