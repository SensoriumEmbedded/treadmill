"""S3: the DMA screen read against the video decode of the same picture.

Grabs a settled frame from the capture, finds the text area from the menu's
background rectangle, reads the screen by DMA through the link, decodes the
frame with a bank bootstrapped from a character ROM (before any harvest),
and reports the per-cell disagreement; then refines the calibration against
the DMA read and decodes again. Read-only on the board.

    python spikes/s3_xcheck.py --uid <chip id> --chargen <characters.901225-01.bin> --out C:/tr-build/hil/spikes/s3
"""

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from treadmill.link.discover import find_port   # noqa: E402
from treadmill.link.transport import open_serial   # noqa: E402
from treadmill.link.trlink import Link   # noqa: E402
from treadmill.observe.calibrate import find_text_area, refine   # noqa: E402
from treadmill.observe.capture import CvCapture   # noqa: E402
from treadmill.observe.channels import from_dump, read_video, xcheck   # noqa: E402
from treadmill.observe.glyphs import GlyphBank, bootstrap_bank, load_chargen   # noqa: E402
from treadmill.station.model import CaptureSpec   # noqa: E402


def report(label, video, dma, out):
    x = xcheck(video, dma)
    print(f"{label}: disagreement {x.disagreement*100:.2f}% ({len(x.cells)} cells), charset video={video.charset} "
          f"dma={dma.charset}, mean confidence {video.mean_confidence():.3f}, unsure {(video.confidence < 0.25).sum()}")
    for r, c, v, d in x.cells[:30]:
        print(f"   row {r:2d} col {c:2d}: video {v:3d} dma {d:3d}")
    return {"disagreement": x.disagreement, "cells": x.cells, "charset_agrees": x.charset_agrees,
            "confidence": video.mean_confidence()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--uid", required=True)
    ap.add_argument("--port-hint")
    ap.add_argument("--station", help="take the capture spec (device, backend, no-signal pattern) from this station's rig")
    ap.add_argument("--device", type=int, default=1)
    ap.add_argument("--api", default="dshow")
    ap.add_argument("--chargen")
    ap.add_argument("--bank", help="a harvested bank .npz to use instead of the ROM bootstrap")
    ap.add_argument("--out", default="C:/tr-build/hil/spikes/s3")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.bank:
        bank = GlyphBank.load(args.bank)
        print(f"bank {args.bank}: {bank.harvested()} of 512 codes harvested")
    else:
        bank = bootstrap_bank(load_chargen(args.chargen), "svideo-generic")
        print("bank bootstrapped from the character ROM")

    if args.station:
        from treadmill.station import load_station
        spec = load_station(args.station).rig.capture
    else:
        spec = CaptureSpec(device=args.device, api=args.api)
    with CvCapture(spec) as src:
        print("capture:", src.describe())
        frame = src.wait_stable(agree=2, timeout=15.0)
        print(f"frame {frame.seq} at {frame.at:.2f}, {frame.size}")
        found = find_port(args.uid, args.port_hint)
        with Link(open_serial(found.port)) as link:
            probe = link.probe()
            if not probe.dma:
                print("no DMA on this board/image; S3 needs a TR+ main image"); return 1
            t0 = time.perf_counter()
            dma = from_dump(link.read_screen())
            print(f"DMA read in {(time.perf_counter()-t0)*1000:.0f} ms, charset {dma.charset}")
            frame2 = src.wait_stable(agree=2, timeout=15.0)   # the picture after the read, in case it changed
    cv2.imwrite(str(out / "frame.png"), frame.image)
    cal = find_text_area(frame.image, "menu")
    print(f"calibration: origin ({cal.x0:.2f}, {cal.y0:.2f}) cell {cal.cell_w:.3f}x{cal.cell_h:.3f} "
          f"bg {cal.background} border {cal.border} floor {cal.noise_floor:.3f} residual {cal.residual:.4f}")
    results = {"calibration_bbox": cal.__dict__.copy()}
    video = read_video(frame, cal, bank, None, follow=False)
    results["bbox"] = report("bounding-box calibration, free charset", video, dma, out)
    video_hint = read_video(frame, cal, bank, dma.charset, follow=False)
    results["bbox_hint"] = report("bounding-box calibration, DMA charset", video_hint, dma, out)
    t0 = time.perf_counter()
    cal2 = refine(frame.image, cal, dma.codes, dma.charset, bank)
    print(f"refined in {(time.perf_counter()-t0)*1000:.0f} ms: origin ({cal2.x0:.2f}, {cal2.y0:.2f}) cell "
          f"{cal2.cell_w:.3f}x{cal2.cell_h:.3f} residual {cal2.residual:.4f} ({cal2.method})")
    results["calibration_refined"] = cal2.__dict__.copy()
    video2 = read_video(frame, cal2, bank, dma.charset, follow=False)
    results["refined_hint"] = report("refined calibration, DMA charset", video2, dma, out)
    video3 = read_video(frame2, cal2, bank, dma.charset, follow=True)
    results["refined_second_frame"] = report("refined calibration, second frame, tracking", video3, dma, out)
    (out / "video.txt").write_text(video2.text() + "\n")
    (out / "dma.txt").write_text(dma.text() + "\n")
    np.save(out / "dma_codes.npy", dma.codes)
    (out / "dma_charset.txt").write_text(dma.charset)
    cv2.imwrite(str(out / "norm.png"), cal2.normalize(frame.image))
    (out / "summary.json").write_text(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
