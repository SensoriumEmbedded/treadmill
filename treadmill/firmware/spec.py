"""What to run: a firmware spec.

``ref=main``, ``ref=investigate/x``, ``path=<worktree>``, ``hex=<file>`` or
``none``, with a target (``tr`` | ``tr-plus`` | ``auto``), debug defines and
an optional tag. The spec is what ``run.json`` records and what the build
cache is keyed on.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from treadmill.errors import TreadmillError

TARGETS = ("auto", "tr", "tr-plus")
_DEFINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TAG = re.compile(r"^[A-Za-z0-9.\-]+$")

# The short mark each debug define contributes to a build's tag, in display
# order. The same table is proposed for the firmware builder (plan item 6.1),
# so a build tagged by either side reads the same way.
DEBUG_MARKS: dict[str, str] = {
    "Dbg_SerDMA": "dma",
    "Dbg_SerTimChg": "tim",
    "Dbg_SerMem": "mem",
    "Dbg_SerASID": "asid",
    "Dbg_SerSD": "sd",
    "Dbg_SerSwift": "sw",
    "Dbg_SerLog": "log",
    "DbgIOTraceLog": "iot",
    "DbgCycAdjLog": "cyc",
    "DbgSpecial": "spc",
    "DbgMsgs_IO": "msg",
    "DbgMsgs_SW": "msw",
    "DbgMsgs_M2S": "m2s",
    "DbgSignalASIDIRQ": "sig",
    "DbgSignalIsrPHI2": "phi",
    "DbgLEDSignalPolling": "led",
}


class SpecError(TreadmillError, ValueError):
    pass


@dataclass(frozen=True)
class FirmwareSpec:
    source_kind: str                      # ref | path | hex | none
    source: str = ""                      # the ref, the directory, or the hex file
    target: str = "auto"
    defines: tuple[str, ...] = ()
    tag: str | None = None                # explicit; else derived from the defines
    allow_dirty: bool = False

    @property
    def is_variant(self) -> bool:
        """Anything other than a clean, define-free, untagged build is a
        variant: stored, never mixed into the baseline trends."""
        return bool(self.defines) or self.tag is not None or self.source_kind == "path"

    def effective_tag(self) -> str | None:
        """The tag a build gets: the explicit one, else the marks of its
        defines joined with '.', else none."""
        if self.tag:
            return self.tag
        if not self.defines:
            return None
        marks = [DEBUG_MARKS.get(d, "x") for d in sorted(self.defines, key=_mark_order)]
        return ".".join(marks)

    def describe(self) -> str:
        head = self.source_kind if self.source_kind == "none" else f"{self.source_kind}={self.source}"
        parts = [head, f"target={self.target}"]
        if self.defines:
            parts.append("defines=" + ",".join(self.defines))
        if self.tag:
            parts.append(f"tag={self.tag}")
        return " ".join(parts)


def _mark_order(define: str) -> int:
    keys = list(DEBUG_MARKS)
    return keys.index(define) if define in DEBUG_MARKS else len(keys)


def parse_spec(text: str, *, target: str = "auto", defines: tuple[str, ...] = (),
               tag: str | None = None, allow_dirty: bool = False) -> FirmwareSpec:
    text = (text or "none").strip()
    if target not in TARGETS:
        raise SpecError(f"target {target!r}: use {', '.join(TARGETS)}")
    for d in defines:
        if not _DEFINE.match(d):
            raise SpecError(f"define {d!r}: a C identifier, no value")
    if tag is not None and not _TAG.match(tag):
        raise SpecError(f"tag {tag!r}: letters, digits, '.' and '-' only")
    if text == "none":
        if defines or tag:
            raise SpecError("--fw none takes no defines or tag: nothing is built")
        return FirmwareSpec("none", "", target, (), None, allow_dirty)
    kind, sep, value = text.partition("=")
    if not sep or kind not in ("ref", "path", "hex") or not value:
        raise SpecError(f"firmware spec {text!r}: use ref=<git ref>, path=<worktree>, hex=<file> or none")
    if kind == "hex" and (defines or tag):
        raise SpecError("hex=<file> is prebuilt: its defines and tag are whatever it was built with")
    return FirmwareSpec(kind, value, target, tuple(dict.fromkeys(defines)), tag, allow_dirty)


def cache_key(commit: str, diff_hash: str, target: str, defines: tuple[str, ...],
              tag: str | None, builder_version: str) -> str:
    text = "|".join([commit, diff_hash, target, ",".join(sorted(defines)), tag or "", builder_version])
    return hashlib.sha1(text.encode()).hexdigest()[:12]
