"""The station model and the capability rules.

Capabilities are strings a test names in ``@pytest.mark.needs(...)``. They are
derived here from what the rig has, what the registry says about the machine
and what the board turned out to be when probed, so a test never guesses: it
either has the capability or it is skipped with the reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from treadmill.margins import Floor

# Board kinds as the version banner reports them: "TeensyROM+ v0.9" is a TR+.
TR = "tr"
TR_PLUS = "tr-plus"

# Capability names, kept in one place so a typo in a marker is caught by the
# plugin rather than silently skipping every test.
CAPS = frozenset({
    "serial",         # a USB serial link to the board (every station)
    "tcp",            # the board's TCP listener is reachable
    "video",          # the rig captures the machine's video output
    "screen",         # some way to read the text screen (video or DMA)
    "dma",            # the board is a TR+ running its main image: memory read/write
    "screen_dma",     # the exact screen channel (dma)
    "reset_sense",    # a TR+ detects external resets
    "keys_kernal",    # keys can reach programs that read the KERNAL buffer (DMA or the board)
    "keys_hw",        # the keyboard board drives the matrix: any program, any TR
    "nmi",            # RESTORE through the keyboard board
    "tr_button",      # the keyboard board pulses the TeensyROM button
    "tr_alt",         # the keyboard board pulses the TeensyROM's second button
    "tod_select",     # the keyboard board drives the 50/60 Hz TOD line
    "ext_reset",      # the keyboard board drives the C64's /RESET, with no help from the TR
    "power",          # the rig can cut and restore the machine's mains
    "region_switch",  # the machine's video standard can be switched (a Kawari by the keyboard board, or a U64)
    "c128",           # the machine is a C128
    "c64u",           # the machine is a C64 Ultimate
    "u64_api",        # the Ultimate REST API is reachable
    "scope",          # the rig has the scope on the bench
    "mcp_server",     # a firmware checkout with tools/mcp is configured
    "client_path",    # TeensyROM-Web and trom are installed for client-path tests
    "eeprom_reset",   # the station allows the 'e' letter (settings reset)
})

# Attributes a test can match with needs("kernal:JiffyDOS") and the like.
PREFIXED_CAPS = ("kernal:", "fw:", "asset:")

# What the keyboard board can give a station; a skip for one of these says why the board isn't there.
KEYBOARD_CAPS = frozenset({"keys_hw", "keys_kernal", "nmi", "tr_button", "tr_alt", "tod_select", "ext_reset",
                           "region_switch"})


@dataclass(frozen=True)
class CaptureSpec:
    backend: str = "opencv"        # opencv | av
    device: int | str = 0           # index or device name
    fourcc: str = "MJPG"
    width: int = 1280
    height: int = 720
    fps: int = 30
    source: str = "svideo"         # svideo | composite | dvi: the machine's output
    converter: str = "generic"     # names the S-Video-to-HDMI converter; the glyph bank is per converter
    api: str | None = None         # OpenCV backend: dshow | msmf | any; the platform's default when unset
    no_signal: str | None = None   # PNG of what the device shows with no input (relative to data/rigs); such frames are dropped

    @property
    def capture_class(self) -> str:
        """The name a calibration, palette and glyph bank are stored under."""
        return f"{self.source}-{self.converter}"


@dataclass(frozen=True)
class PowerSpec:
    backend: str = "none"           # none | serial-relay | http-plug | u64 | manual
    port: str | None = None         # serial-relay: the micro's USB serial port
    url: str | None = None          # http-plug: the plug's local API base
    channel: int = 0
    off_seconds: float = 5.0        # how long the mains stays off in a power cycle


@dataclass(frozen=True)
class KeyboardSpec:
    """The C64-USBKeyboard board in the station's machine. Its capabilities
    come from the outputs its own ``?`` says are fitted, once it is open."""
    chip: str                        # its chip id: the USB serial string is C64USBKeyboard_<chip>
    kawari_open: str | None = None   # PAL | NTSC: the standard a Kawari gives with its switch header J1 open
    tod: str | int = "standard"      # standard | 50 | 60 | off: the station's TOD rate


@dataclass(frozen=True)
class Rig:
    id: str
    capture: CaptureSpec | None = None
    power: PowerSpec | None = None
    region_switch: str | None = None   # u64: the Ultimate switches its own standard (a Kawari's is the keyboard board's)
    scope: bool = False
    u64_url: str | None = None         # the Ultimate's REST base URL, when the machine is one
    firmware_checkout: str | None = None   # the TeensyROM checkout whose tools build and flash
    mcp_checkout: str | None = None        # a checkout that has tools/mcp (test 24)
    trom_path: str | None = None           # the trom CLI, for client-path tests


@dataclass(frozen=True)
class Machine:
    id: str                          # the registry id (systems.csv) or a local name
    model: str = "C64"               # C64 | C128 | SX-64 | C64U
    region: str = "NTSC"             # PAL | NTSC | switchable
    kernal: str = "stock"            # stock | JiffyDOS | JaffyDOS | ...
    nickname: str = ""
    pca: str = ""
    notes: str = ""
    boot_timeout_s: float = 60.0     # DHCP and the startup SID can make the menu slow
    timing_tolerant: bool = False    # the C64U forgives bus timing faults a real C64 does not
    has_kawari: bool = False

    @property
    def is_c128(self) -> bool:
        return self.model.upper().startswith("C128")

    @property
    def is_c64u(self) -> bool:
        return self.model.upper() in {"C64U", "U64"}


@dataclass(frozen=True)
class TRBoard:
    uid: str                         # the chip id the version banner reports ("UID: 19272290")
    kind: str | None = None          # tr | tr-plus; None until probed
    port_hint: str | None = None     # a COM port to try first; discovery goes by uid
    tcp_host: str | None = None      # host[:port] of the TCP listener, when wired


@dataclass(frozen=True)
class Station:
    id: str
    rig: Rig
    machine: Machine
    tr: TRBoard
    allow_eeprom_reset: bool = False
    settings_baseline: Mapping[str, object] = field(default_factory=dict)
    margins: Mapping[str, Floor] = field(default_factory=dict)  # knob name -> how far each edge must be clean
    keyboard: KeyboardSpec | None = None
    kbd_fitted: frozenset[str] | None = None   # the keyboard board's fitted outputs; None until it answers
    standard: str | None = None      # PAL | NTSC as the TR's banner last reported it; None until then
    caps: frozenset[str] = frozenset()

    def with_probe(self, kind: str, listener_on: bool = False,
                   fw_defines: tuple[str, ...] = (), standard: str | None = None) -> "Station":
        """The station once the board answered: its kind decides the DMA caps,
        and the video standard its banner reports, when it reports one, which
        calibration and glyph bank read the picture."""
        tr = replace(self.tr, kind=kind)
        st = replace(self, tr=tr, standard=standard or self.standard)
        return replace(st, caps=capabilities(st, listener_on=listener_on, fw_defines=fw_defines))

    def with_keyboard(self, fitted: frozenset[str] | None) -> "Station":
        """The station once its keyboard board answered: what it has fitted
        decides the keyboard caps; the probe's caps stay as they were. None:
        the keyboard board is gone, and its caps with it."""
        st = replace(self, kbd_fitted=None if fitted is None else frozenset(fitted))
        defines = tuple(c[len("fw:"):] for c in self.caps if c.startswith("fw:"))
        return replace(st, caps=capabilities(st, listener_on="tcp" in self.caps, fw_defines=defines))

    def masked(self, removed: set[str]) -> "Station":
        """A station with capabilities taken away (--caps-mask), to prove the
        baseline tier on a richer rig."""
        return replace(self, caps=frozenset(c for c in self.caps if c not in removed))

    def has(self, *caps: str) -> bool:
        return all(c in self.caps for c in caps)


def capabilities(station: Station, *, listener_on: bool = False,
                 fw_defines: tuple[str, ...] = ()) -> frozenset[str]:
    """The capability set of a station, from its rig, machine and board."""
    rig, machine, tr = station.rig, station.machine, station.tr
    caps: set[str] = {"serial"}
    if rig.capture is not None:
        caps |= {"video", "screen"}
    if tr.kind == TR_PLUS:
        caps |= {"dma", "screen_dma", "screen", "reset_sense", "keys_kernal"}
    fitted = station.kbd_fitted
    if fitted is not None:
        caps |= {"keys_hw", "keys_kernal", "nmi"}
        for output, cap in (("tod", "tod_select"), ("menu", "tr_button"), ("alt", "tr_alt"), ("reset", "ext_reset")):
            if output in fitted:
                caps.add(cap)
        # V holds the C64 in reset while the FPGA reloads, and only the station knows which standard J1 open gives.
        if {"kawari", "reset"} <= fitted and machine.has_kawari and station.keyboard and station.keyboard.kawari_open:
            caps.add("region_switch")
    if rig.power is not None and rig.power.backend != "none":
        caps.add("power")
    if rig.region_switch == "u64" and machine.region.lower() == "switchable":
        caps.add("region_switch")
    if tr.tcp_host and listener_on:
        caps.add("tcp")
    if machine.is_c128:
        caps.add("c128")
    if machine.is_c64u:
        caps.add("c64u")
    if rig.u64_url:
        caps.add("u64_api")
    if rig.scope:
        caps.add("scope")
    if rig.mcp_checkout:
        caps.add("mcp_server")
    if rig.trom_path:
        caps.add("client_path")
    if station.allow_eeprom_reset:
        caps.add("eeprom_reset")
    caps.add(f"kernal:{machine.kernal}")
    for define in fw_defines:
        caps.add(f"fw:{define}")
    return frozenset(caps)


def tier(station: Station) -> str:
    """A short label for reports: which tier of the plan the station reaches."""
    caps = station.caps
    if "c64u" in caps:
        return "U64"
    parts = ["0+" if "dma" in caps else "0"]
    if "keys_hw" in caps:
        parts.append("1")
    if "power" in caps:
        parts.append("2")
    if "region_switch" in caps:
        parts.append("3")
    return "/".join(parts)


def unknown_caps(names: tuple[str, ...]) -> list[str]:
    """The capability names a marker used that no station can ever have."""
    bad = []
    for name in names:
        if name in CAPS or name.startswith(PREFIXED_CAPS):
            continue
        bad.append(name)
    return bad
