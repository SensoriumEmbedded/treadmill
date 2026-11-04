"""The banner a TeensyROM answers VersionInfoToken with::

      FW: TeensyROM+ v0.9-dma.tim*        "(minimal)" follows when MinimalBoot answers
          Oct 30 2026, 10:00:00
      Teensy: 816MHz  62.1C  UID: 14470230
      C128  NTSC Vid  60 Hz               main image only
      Boot: complete                      main image only

The version may end in ``-<tag>`` and ``*`` (a special build on a dirty tree,
from ``tools/build-firmware.mjs --tag``). The UID is burned in at the factory,
so it tells two boards of one type apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TR = "tr"
TR_PLUS = "tr-plus"

_VERSION = re.compile(r"TeensyROM\+? v[A-Za-z0-9.\-]+\*?")
_BUILT = re.compile(r"([A-Z][a-z]{2} [ \d]\d \d{4}), (\d\d:\d\d:\d\d)")
_UID = re.compile(r"UID:\s*(\d+)")
_MHZ = re.compile(r"Teensy:\s*(\d+)MHz\s+([\d.]+)C")
_MACHINE = re.compile(r"^\s*(C64|C128)\s+(NTSC|PAL)\b(?:.*?(\d+)\s*Hz)?", re.M)
_BOOT = re.compile(r"Boot: (complete|in progress)")
_TAG = re.compile(r"TeensyROM\+? v[\d.]+(?:-([A-Za-z0-9.\-]+))?(\*)?")


@dataclass(frozen=True)
class Banner:
    version: str | None        # "TeensyROM+ v0.9-dma.tim*" as the board prints it
    built: str | None          # "Oct 30 2026, 10:00:00"
    uid: str | None
    minimal: bool
    machine: str | None        # C64 | C128 (main image, once the menu reported)
    video: str | None          # NTSC | PAL
    hz: int | None             # 50 | 60, the TOD rate the menu measured
    boot: str | None           # complete | in progress
    mhz: int | None
    temp_c: float | None
    tag: str | None            # the --tag suffix, without its leading '-'
    dirty: bool                # the '*' of a build with uncommitted edits
    raw: str

    @property
    def kind(self) -> str | None:
        """tr-plus or tr: the '+' is the whole distinction between the boards."""
        if not self.version:
            return None
        return TR_PLUS if self.version.startswith("TeensyROM+") else TR


def parse_banner(raw: str) -> Banner:
    version = (m.group(0) if (m := _VERSION.search(raw)) else None)
    built = (f"{m.group(1)}, {m.group(2)}" if (m := _BUILT.search(raw)) else None)
    uid = (m.group(1) if (m := _UID.search(raw)) else None)
    mhz, temp = (None, None)
    if (m := _MHZ.search(raw)):
        mhz, temp = int(m.group(1)), float(m.group(2))
    machine = video = None
    hz = None
    if (m := _MACHINE.search(raw)):
        machine, video = m.group(1), m.group(2)
        hz = int(m.group(3)) if m.group(3) else None
    boot = (m.group(1) if (m := _BOOT.search(raw)) else None)
    tag, dirty = None, False
    if (m := _TAG.search(raw)):
        tag, dirty = m.group(1), m.group(2) == "*"
    return Banner(version=version, built=built, uid=uid, minimal="(minimal)" in raw,
                  machine=machine, video=video, hz=hz, boot=boot, mhz=mhz, temp_c=temp,
                  tag=tag, dirty=dirty, raw=raw)
