"""The firmware's bus timing knobs: what each ``t`` letter moves, and how far a sweep may take it.

A build with ``Dbg_SerTimChg`` takes ``t<letter><digits>`` over USB serial
(``SerUSBIO.ino``; MinimalBoot's ``Min_SerUSBIO.ino`` has a subset) and
answers with a listing of every value; ``td`` puts them back to what video
detection set for the machine's standard. The defaults here are main's
``Common_Defs.h`` and serve the fake board, the docs and the sweep limits;
on a board the listing after ``td`` is the authority, since a branch may
carry others.

Times "from rise" count from ``StartCycCnt``, latched at the Phi2-rising
interrupt; "from fall" from the Phi2-falling latch; "from re-latch" from the
second latch ``DMAByte()`` takes at ``nS_DMASetup``. ``docs/knobs.md`` has
each knob's bus path, its edges and the probes that find them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

STANDARDS = ("PAL", "NTSC", "NTSC C128")
# A Phi2 cycle in ns; the C128 runs its 64 mode at the C64's NTSC rate.
CYCLE_NS = {"PAL": 1015, "NTSC": 978, "NTSC C128": 978}

MAIN = frozenset({"main"})
BOTH_IMAGES = frozenset({"main", "minimal"})


@dataclass(frozen=True)
class Knob:
    letter: str                   # the letter after 't'
    name: str                     # the firmware variable the listing prints
    digits: int                   # how many digits GetDigits() reads
    define: str = "Dbg_SerTimChg"
    unit: str = "ns"
    group: str = "cpu"            # the bus path it times: cpu | vic | dma | kernal
    phase: str = "rise"           # rise | fall | relatch | entry | cycles | mode
    images: frozenset = MAIN      # the images whose 't' takes it
    tr_plus_only: bool = False    # compiled in only on a TR+ build (Fab04_KernalReplace)
    restored_by_td: bool = True
    swept: bool = True            # a mode is set, never swept
    defaults: tuple = ()          # PAL, NTSC, NTSC C128
    low: int | None = None        # the sweep limits before couplings (limits())
    high: int | None = None

    @property
    def command(self) -> str:
        return "t" + self.letter

    def default(self, standard: str) -> int:
        return self.defaults[STANDARDS.index(standard)]

    def fits(self, value: int) -> bool:
        return 0 <= value < 10 ** self.digits

    def format(self, value: int) -> str:
        return f"{self.command}{value:0{self.digits}d}"


def _k(letter, name, digits, group, phase, defaults, low=None, high=None, **kw) -> Knob:
    if isinstance(defaults, int):
        defaults = (defaults,) * len(STANDARDS)
    return Knob(letter, name, digits, group=group, phase=phase, defaults=tuple(defaults), low=low, high=high, **kw)


# In SerUSBIO.ino's order; selftest/test_knobs.py checks letters, names and digits against it.
KNOBS: dict[str, Knob] = {k.letter: k for k in (
    _k("m", "nS_MaxAdj", 4, "cpu", "entry", (1030, 993, 993), None, 1200, images=BOTH_IMAGES),
    _k("r", "nS_RWnReady", 3, "cpu", "rise", 135, 60, None, images=BOTH_IMAGES),
    _k("p", "nS_PLAprop", 3, "cpu", "rise", 150, None, None, images=BOTH_IMAGES),
    _k("s", "nS_DataSetup", 3, "cpu", "rise", 220, 100, 480, images=BOTH_IMAGES),
    _k("h", "nS_DataHold", 3, "cpu", "rise", 390, 250, 520, images=BOTH_IMAGES),
    _k("v", "nS_VICStart", 3, "vic", "fall", 210, 60, 400, images=BOTH_IMAGES),
    _k("i", "nS_VICDHold", 3, "vic", "fall", 365, 250, 520),
    _k("a", "nS_DMAAssert", 3, "dma", "fall", 40, 0, 420),
    _k("b", "nS_DMABAWait", 3, "dma", "fall", 200, 60, 400),
    _k("e", "nS_DMASetup", 3, "dma", "fall", (454, 430, 460), 330, 520),
    _k("w", "nS_DMADataHold", 3, "dma", "relatch", (430, 424, 394), 250, None),
    _k("y", "nS_DMADataSetup", 3, "dma", "relatch", (390, 404, 374), 250, None),
    _k("k", "Cyc_KernProp", 2, "kernal", "cycles", 35, 5, 99, unit="cycles"),
    _k("x", "KSenseMode", 1, "kernal", "mode", 9, unit="mode", tr_plus_only=True, restored_by_td=False,
       swept=False),
    _k("n", "nS_KSenseSample", 3, "kernal", "relatch", 250, 50, None, tr_plus_only=True),
)}

# KSenseMode's values: the Kernal Replace HIRAM sense method, applied at the next launch.
KSENSE_MODES = {0: "Skoe", 2: "DMA", 9: "auto"}

# nS_DMASetup moves the re-latch, and the release and the latch count from it: PR #99 swept it with
# these sums held, so the bus edges they make stay put. Each sum stays this far inside the cycle.
HELD_SUMS = {"release": (("e", "w"), 60), "latch": (("e", "y"), 20)}

# Margins one knob keeps from another (couplings), in ns.
KSENSE_BELOW_LATCH = 20           # nS_KSenseSample stays this far before nS_DMADataSetup
MAXADJ_BELOW_CYCLE = 120          # nS_MaxAdj goes no lower than the cycle minus this
PLAPROP_ABOVE_DEFAULT = 120       # nS_PLAprop goes no higher than its default plus this


def standard_of(machine: str | None, video: str | None) -> str:
    """The defaults set ``td`` picks for a banner's machine and video: a PAL
    C128 keeps the PAL set (no rig has measured one)."""
    if video == "NTSC":
        return "NTSC C128" if machine == "C128" else "NTSC"
    return "PAL"


def knob_named(text: str) -> Knob:
    """``p``, ``tp`` or ``nS_PLAprop``."""
    key = text.strip()
    if key in KNOBS:
        return KNOBS[key]
    if len(key) == 2 and key[0] == "t" and key[1] in KNOBS:
        return KNOBS[key[1]]
    for knob in KNOBS.values():
        if knob.name.lower() == key.lower():
            return knob
    raise KeyError(f"no timing knob {text!r}; one of " + ", ".join(f"{k.letter} ({k.name})" for k in KNOBS.values()))


_LISTED = re.compile(r"\b(nS_\w+|Cyc_\w+|KSenseMode)\s+(\d+)")


def parse_listing(text: str) -> dict[str, int]:
    """Every knob a ``t`` reply lists, by letter: the last value of each,
    since a reply carries one listing after any ``Val Set to`` line."""
    by_name = {k.name: k.letter for k in KNOBS.values()}
    return {by_name[name]: int(value) for name, value in _LISTED.findall(text) if name in by_name}


def limits(knob: Knob, standard: str, current: Mapping[str, int]) -> tuple[int, int]:
    """How far a sweep may take ``knob``, lowest and highest, with the other
    knobs at ``current`` (the listing at the defaults). Past these a value
    either does nothing (``nS_PLAprop`` below ``nS_RWnReady``) or is known
    to wedge the machine (a DMA release that reaches the next Phi2 fall)."""
    cycle = CYCLE_NS[standard]
    low = 0 if knob.low is None else knob.low
    high = 10 ** knob.digits - 1 if knob.high is None else knob.high
    c = current
    if knob.letter == "m":
        low = cycle - MAXADJ_BELOW_CYCLE
    elif knob.letter == "r":
        high = min(high, c["p"])
    elif knob.letter == "p":
        low = c["r"]
        high = c["p"] + PLAPROP_ABOVE_DEFAULT
    elif knob.letter == "b":
        high = min(high, c["e"] - 1)
    elif knob.letter == "e":
        high = min(high, cycle - HELD_SUMS["release"][1] - c["w"], cycle - HELD_SUMS["latch"][1] - c["y"])
    elif knob.letter == "w":
        high = min(high, cycle - HELD_SUMS["release"][1] - c["e"])
    elif knob.letter == "y":
        low = max(low, c.get("n", 0) + KSENSE_BELOW_LATCH)
        high = min(high, cycle - HELD_SUMS["latch"][1] - c["e"])
    elif knob.letter == "n":
        high = min(high, c["y"] - KSENSE_BELOW_LATCH)
    return low, high
