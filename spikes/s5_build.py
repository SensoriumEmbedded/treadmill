"""S5: build the firmware under test from a disposable worktree, cached.

``ref=main`` for the TR+ through the suite's build step: the worktree under
the short build root, the primary checkout's toolchain cache, the hex and
its banner stamp, the memory report lines, then the same request again (a
cache hit). With ``--variant`` a second build with ``Dbg_SerTimChg``, which
exercises the header-patch fallback and the derived tag. No flashing.

    python spikes/s5_build.py --primary <firmware checkout> [--variant]
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from treadmill.firmware.build import build   # noqa: E402
from treadmill.firmware.spec import parse_spec   # noqa: E402


def run(spec, primary, label):
    t0 = time.monotonic()
    b = build(spec, primary, "tr-plus")
    secs = time.monotonic() - t0
    print(f"{label}: {'cache hit' if b.cached else 'built'} in {secs:.0f} s -> {b.hex.path.name}")
    print(f"   version {b.hex.version!r} stamp {b.hex.stamp!r} tag {b.tag!r} defines {b.defines}")
    print(f"   checkout {b.checkout.path} commit {b.checkout.commit[:12]} describe {b.checkout.describe} dirty {b.checkout.dirty}")
    for line in b.memory_lines:
        print("   | " + line)
    return {"label": label, "cached": b.cached, "seconds": round(secs), "hex": str(b.hex.path), "version": b.hex.version,
            "stamp": b.hex.stamp, "tag": b.tag, "defines": list(b.defines), "commit": b.checkout.commit,
            "describe": b.checkout.describe, "dirty": b.checkout.dirty, "key": b.key, "memory": list(b.memory_lines)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--primary", required=True, help="the firmware checkout whose tools and toolchain cache are used")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--variant", action="store_true", help="also build with Dbg_SerTimChg")
    ap.add_argument("--out", default="C:/tr-build/hil/spikes/s5")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    primary = Path(args.primary)
    results = []
    spec = parse_spec(f"ref={args.ref}", target="tr-plus")
    results.append(run(spec, primary, "main, first request"))
    results.append(run(spec, primary, "main, second request"))
    if args.variant:
        var = parse_spec(f"ref={args.ref}", target="tr-plus", defines=("Dbg_SerTimChg",))
        results.append(run(var, primary, "main + Dbg_SerTimChg"))
    (out / "summary.json").write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
