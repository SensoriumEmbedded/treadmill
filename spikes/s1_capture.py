"""S1: the capture chain alone (machine -> S-Video -> HDMI converter -> MS2109).

Opens the dongle through the suite's CvCapture, streams for a while, and
reports what the chain delivers: the mode it settled on, the frame rate,
read errors, size changes, frame-to-frame difference (tearing shows as
spikes on a static picture) and whether the menu's text area can be found
offline. Touches no board.

    python spikes/s1_capture.py --device 2 --seconds 20 --out C:/tr-build/hil/spikes/s1
"""

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from treadmill.observe.calibrate import CalibrationError, find_text_area   # noqa: E402
from treadmill.observe.capture import CvCapture, frame_difference, save_strip   # noqa: E402
from treadmill.station.model import CaptureSpec   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--device", type=int, default=2)
    ap.add_argument("--backend", default="msmf", choices=["msmf", "dshow", "any"])
    ap.add_argument("--fourcc", default="MJPG")
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--layout", default="menu", choices=["menu", "basic"])
    ap.add_argument("--out", default="C:/tr-build/hil/spikes/s1")
    args = ap.parse_args()
    w, h = (int(v) for v in args.size.split("x"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    spec = CaptureSpec(device=args.device, fourcc=args.fourcc, width=w, height=h, fps=args.fps)
    t0 = time.monotonic()
    src = CvCapture(spec, args.backend)
    first = src.next_frame(timeout=15.0)
    t_first = time.monotonic() - t0

    sizes: dict[str, int] = {}
    diffs: list[float] = []
    stds: list[float] = []
    gaps: list[float] = []
    last = first
    n = 0
    t_start = time.monotonic()
    deadline = t_start + args.seconds
    while time.monotonic() < deadline:
        f = src.next_frame(last, timeout=5.0)
        n += 1
        key = f"{f.size[0]}x{f.size[1]}"
        sizes[key] = sizes.get(key, 0) + 1
        if f.size == last.size:
            diffs.append(frame_difference(last.image, f.image))
        stds.append(float(f.image.std()))
        gaps.append(f.at - last.at)
        last = f
    elapsed = time.monotonic() - t_start
    desc = src.describe()
    cv2.imwrite(str(out / "first.png"), first.image)
    cv2.imwrite(str(out / "last.png"), last.image)
    save_strip(src.ring(), out / "ring.png")
    src.close()

    cal = None
    cal_error = None
    try:
        cal = find_text_area(last.image, args.layout)
        cv2.imwrite(str(out / "norm.png"), cal.normalize(last.image))
    except CalibrationError as e:
        cal_error = str(e)

    d = np.array(diffs) if diffs else np.zeros(1)
    g = np.array(gaps) if gaps else np.zeros(1)
    summary = {
        "device": args.device, "backend": args.backend, "requested": f"{args.fourcc} {w}x{h}@{args.fps}",
        "actual": desc.get("actual"), "first_frame_s": round(t_first, 2),
        "frames": n, "seconds": round(elapsed, 2), "fps": round(n / elapsed, 2) if elapsed else None,
        "read_errors": desc.get("read_errors"), "sizes": sizes,
        "gap_ms": {"mean": round(float(g.mean()) * 1000, 1), "max": round(float(g.max()) * 1000, 1)},
        "frame_diff": {"mean": round(float(d.mean()), 3), "median": round(float(np.median(d)), 3),
                       "p95": round(float(np.percentile(d, 95)), 3), "max": round(float(d.max()), 3)},
        "pixel_std": {"min": round(min(stds), 2), "max": round(max(stds), 2)},
        "calibration": None if cal is None else {
            "x0": round(cal.x0, 2), "y0": round(cal.y0, 2), "cell": [round(cal.cell_w, 3), round(cal.cell_h, 3)],
            "text_area_px": [round(cal.cell_w * 40, 1), round(cal.cell_h * 25, 1)],
            "background": [int(v) for v in cal.background], "border": [int(v) for v in cal.border],
            "noise_floor": round(cal.noise_floor, 4), "method": cal.method, "residual": round(cal.residual, 4)},
        "calibration_error": cal_error,
        "files": [str(out / n) for n in ("first.png", "last.png", "ring.png", "norm.png")],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
