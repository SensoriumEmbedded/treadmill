"""Build the firmware under test with the repository's own builder, cached.

``node tools/build-firmware.mjs --target <t> --out <root>/out/<key> [--tag <tag>]
[--define NAME ...]`` in the checkout the spec names. The cache key is the
commit, the dirty hash, the target, the defines, the tag and the builder's
own version, so an unchanged request is a hit and costs nothing. Until the
builder has ``--define`` (plan item 6.1), the requested defines are switched
on by editing the worktree's ``TeensyROM.h``, and MinimalBoot's
``Min_TeensyROM.h`` for those it has too, which the dirty hash records.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from treadmill.errors import TreadmillError
from treadmill.firmware.checkout import (
    DEFAULT_WORK_ROOT, Checkout, CheckoutError, describe, toolchain_env, worktree_for,
)
from treadmill.firmware.hexinfo import HexInfo, inspect_hex
from treadmill.firmware.spec import FirmwareSpec, SpecError, cache_key

BUILD_TIMEOUT_S = 900


class BuildError(TreadmillError):
    pass


@dataclass(frozen=True)
class Built:
    hex: HexInfo
    checkout: Checkout
    key: str
    out_dir: Path
    tag: str | None
    defines: tuple[str, ...]
    target: str
    cached: bool
    memory_lines: tuple[str, ...]


def builder_supports(checkout: Path, option: str) -> bool:
    text = (Path(checkout) / "tools" / "build-firmware.mjs").read_text(encoding="utf-8", errors="replace")
    return f"'{option}'" in text


def builder_version(checkout: Path) -> str:
    """A stand-in for the builder's version: the last commit that touched it."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%h", "--", "tools/build-firmware.mjs",
                              "tools/lib"], cwd=str(checkout), capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, check=True)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


MAIN_HEADER = Path("Source") / "Teensy" / "TeensyROM.h"
MINIMAL_HEADER = Path("Source") / "Teensy" / "MinimalBoot" / "Min_TeensyROM.h"


def _uncomment(header: Path, defines: tuple[str, ...]) -> list[str]:
    """Uncomment ``// #define <NAME>`` in ``header`` for each define; returns
    the names it has no line for."""
    text = header.read_text(encoding="latin-1")
    missing = []
    for name in defines:
        pattern = re.compile(rf"^(\s*)//\s*(#define\s+{re.escape(name)}\b)", re.M)
        text, n = pattern.subn(r"\1\2", text, count=1)
        if n == 0 and not re.search(rf"^\s*#define\s+{re.escape(name)}\b", text, re.M):
            missing.append(name)
    header.write_text(text, encoding="latin-1", newline="")
    return missing


def enable_defines_in_header(checkout: Path, defines: tuple[str, ...]) -> list[str]:
    """Uncomment ``// #define <NAME>`` lines for the defines asked for, in the
    worktree's TeensyROM.h and, for the ones it has as well (Dbg_SerTimChg,
    Dbg_SerLog, Dbg_SerMem), in MinimalBoot's Min_TeensyROM.h, so the minimal
    image takes the same debug letters. Returns the names TeensyROM.h lacks;
    one only the main image has is no fault in MinimalBoot's."""
    missing = _uncomment(Path(checkout) / MAIN_HEADER, defines)
    minimal = Path(checkout) / MINIMAL_HEADER
    if minimal.is_file():
        _uncomment(minimal, defines)
    return missing


def resolve_checkout(spec: FirmwareSpec, primary: Path, work_root: Path = DEFAULT_WORK_ROOT) -> Checkout:
    if spec.source_kind == "ref":
        path = worktree_for(primary, spec.source, work_root)
    elif spec.source_kind == "path":
        path = Path(spec.source)
    else:
        raise SpecError(f"{spec.describe()} has nothing to check out")
    co = describe(path)
    if co.dirty and not spec.allow_dirty:
        raise CheckoutError(f"{co.path} has uncommitted changes ({co.describe}); pass --allow-dirty "
                            "to build it anyway (the build is marked dirty and recorded as a variant)")
    return co


def build(spec: FirmwareSpec, primary: Path, target: str, *, work_root: Path = DEFAULT_WORK_ROOT,
          log=print) -> Built:
    """Build (or find in the cache) the hex the spec asks for."""
    if target not in ("tr", "tr-plus"):
        raise SpecError(f"a build needs a concrete target, not {target!r}")
    co = resolve_checkout(spec, primary, work_root)
    tag = spec.effective_tag()
    key = cache_key(co.commit, co.diff_hash, target, spec.defines, tag, builder_version(co.path))
    out_dir = work_root / "out" / key
    existing = _find_hex(out_dir, target)
    if existing is not None:
        log(f"build cache hit: {existing.name} ({key})")
        return Built(inspect_hex(existing), co, key, out_dir, tag, spec.defines, target, True,
                     _memory_lines(out_dir))
    cmd = ["node", str(co.tools / "build-firmware.mjs"), "--target", target, "--out", str(out_dir), "--force"]
    if tag:
        cmd += ["--tag", tag]
    patched: list[str] = []
    if spec.defines:
        if builder_supports(co.path, "--define"):
            for d in spec.defines:
                cmd += ["--define", d]
        else:
            if spec.source_kind != "ref":
                raise BuildError("the builder has no --define yet; a path= checkout would be edited in "
                                 "place, so use ref= (a disposable worktree) for defines")
            missing = enable_defines_in_header(co.path, spec.defines)
            if missing:
                raise BuildError(f"TeensyROM.h has no #define line for {', '.join(missing)}")
            patched = list(spec.defines)
            co = describe(co.path)   # now dirty: the patch is part of the record
    out_dir.mkdir(parents=True, exist_ok=True)
    log("building: " + " ".join(cmd))
    t0 = time.monotonic()
    result = subprocess.run(cmd, cwd=str(co.path), capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, env=toolchain_env(primary), timeout=BUILD_TIMEOUT_S)
    (out_dir / "build.log").write_text(result.stdout + "\n--- stderr ---\n" + result.stderr, encoding="utf-8")
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-25:])
        raise BuildError(f"build failed ({result.returncode}) in {time.monotonic() - t0:.0f} s:\n{tail}")
    hex_path = _find_hex(out_dir, target)
    if hex_path is None:
        raise BuildError(f"the build wrote no {target} hex into {out_dir}")
    info = inspect_hex(hex_path)
    (out_dir / "build.json").write_text(json.dumps({
        "spec": spec.describe(), "commit": co.commit, "describe": co.describe, "dirty": co.dirty,
        "diff_hash": co.diff_hash, "defines": spec.defines, "patched_header": patched, "tag": tag,
        "target": target, "hex": str(hex_path), "version": info.version, "stamp": info.stamp,
        "seconds": round(time.monotonic() - t0), "memory": _memory_lines(out_dir, result.stdout),
    }, indent=2), encoding="utf-8")
    log(f"built {hex_path.name}: {info.version}, {info.stamp} in {time.monotonic() - t0:.0f} s")
    return Built(info, co, key, out_dir, tag, spec.defines, target, False, _memory_lines(out_dir))


def _find_hex(out_dir: Path, target: str) -> Path | None:
    prefix = "TeensyROM+_" if target == "tr-plus" else "TeensyROM_"
    hits = sorted(p for p in out_dir.glob("*_full.hex") if p.name.startswith(prefix))
    return hits[-1] if hits else None


_MEMORY = re.compile(r"^.*(RAM1|RAM2|FLASH|free for local variables|stack).*$", re.M | re.I)


def _memory_lines(out_dir: Path, stdout: str | None = None) -> tuple[str, ...]:
    if stdout is None:
        log_path = out_dir / "build.log"
        if not log_path.is_file():
            return ()
        stdout = log_path.read_text(encoding="utf-8", errors="replace")
    return tuple(m.group(0).strip() for m in _MEMORY.finditer(stdout))[:40]
