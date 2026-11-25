"""Write a hex to the station's board with the repository's flasher, then
check the board runs it; and the board-recovery helpers the ladder uses.

``node tools/flash-firmware.mjs --hex <file> --uid <chip id>`` identifies every
attached board first, refuses an image built for the other cartridge, writes
over USB with no button to press (Windows), waits for the board to come back
and compares the stamp. The suite then verifies on its own: main image
answering, the banner's version and stamp equal to the hex's.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from treadmill.errors import TreadmillError
from treadmill.firmware.hexinfo import HexInfo
from treadmill.link import Link, LinkError, TransportError, find_port, open_serial
from treadmill.link.banner import Banner

FLASH_TIMEOUT_S = 300
BOOTLOADER_BAUD = 134      # the Teensy core's documented soft-reboot request
REAPPEAR_S = 90.0
HALFKAY = ("16C0", "0478")     # the Teensy bootloader's USB vendor and product
HALFKAY_S = 10.0           # how long a board takes to show up in the bootloader after the request


class FlashError(TreadmillError):
    pass


@dataclass(frozen=True)
class Flashed:
    hex: HexInfo
    banner: Banner
    seconds: float
    skipped: bool


def board_runs(banner: Banner | None, info: HexInfo) -> bool:
    """Whether the board already runs this exact build: version string and
    stamp both equal (the tag and dirty marker ride on the version string)."""
    return (banner is not None and banner.version == info.version
            and banner.built == info.stamp and not banner.minimal)


def flash(info: HexInfo, uid: str, primary: Path, *, port_hint: str | None = None,
          current: Banner | None = None, force: bool = False, named: bool = True, log=print) -> Flashed:
    """Flash unless the board already runs the hex (or ``force``), then
    verify. ``named=False`` leaves out ``--uid``: for the one board on USB,
    waiting in the bootloader, which cannot say its chip id."""
    t0 = time.monotonic()
    if not force and board_runs(current, info):
        log(f"board already runs {info.version} ({info.stamp}); flash skipped")
        return Flashed(info, current, 0.0, True)
    cmd = ["node", str(Path(primary) / "tools" / "flash-firmware.mjs", ), "--hex", str(info.path)]
    if named:
        cmd += ["--uid", uid]
    log("flashing: " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(primary), capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=FLASH_TIMEOUT_S)
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-20:])
        raise FlashError(f"flash failed ({result.returncode}):\n{tail}")
    banner = verify(info, uid, port_hint)
    return Flashed(info, banner, time.monotonic() - t0, False)


def verify(info: HexInfo, uid: str, port_hint: str | None = None, timeout: float = REAPPEAR_S) -> Banner:
    """The board, found again by chip id, must answer as the main image with
    the hex's version string and stamp."""
    found = find_port(uid, port_hint, timeout=timeout)
    with Link(open_serial(found.port)) as link:
        image = link.fw_check()
        banner = link.banner() if image else None
    if image != "main" or banner is None:
        raise FlashError(f"after flashing, the board answered as {image or 'nothing'}, not the main image")
    problems = []
    if banner.version != info.version:
        problems.append(f"version {banner.version!r} != hex {info.version!r}")
    if banner.built != info.stamp:
        problems.append(f"stamp {banner.built!r} != hex {info.stamp!r}")
    if problems:
        raise FlashError("the board does not run the hex: " + "; ".join(problems))
    return banner


# -- recovery -----------------------------------------------------------------

def request_bootloader(port: str) -> None:
    """Ask the running sketch to enter the Teensy bootloader: the core reboots
    when its USB serial port is opened at 134 baud. Works while the firmware's
    main loop is hung, since the USB interrupt handles it; needs USB serial."""
    import serial
    try:
        with serial.Serial(port, BOOTLOADER_BAUD, timeout=0.2):
            time.sleep(0.2)
    except serial.SerialException:
        pass   # the port vanishes as the board reboots


def bootloader_serial(uid: str) -> str:
    """The USB serial number HalfKay shows for the board with this chip id:
    the id divided by ten, in hex (checked on one board)."""
    return f"{int(uid) // 10:X}"


def halfkay_serials() -> list[str] | None:
    """The serial numbers of the boards waiting in the bootloader, upper
    case without leading zeros; None where this host cannot tell."""
    vid, pid = HALFKAY
    found: list[str] = []
    if sys.platform == "win32":
        query = (f"Get-PnpDevice -PresentOnly | Where-Object InstanceId -like 'USB\\VID_{vid}&PID_{pid}\\*' "
                 "| ForEach-Object InstanceId")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", query], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        found = [line.strip().rsplit("\\", 1)[-1] for line in out.splitlines() if line.strip()]
    elif sys.platform.startswith("linux"):
        for dev in Path("/sys/bus/usb/devices").glob("*"):
            try:
                ids = ((dev / "idVendor").read_text().strip().upper(), (dev / "idProduct").read_text().strip().upper())
                if ids == (vid, pid):
                    found.append((dev / "serial").read_text().strip())
            except OSError:
                continue
    else:
        return None
    return [s.upper().lstrip("0") or "0" for s in found]


def waiting_alone(uid: str, timeout: float | None = None) -> bool:
    """Whether this board, and no other, is waiting in the bootloader."""
    want = bootloader_serial(uid)
    deadline = time.monotonic() + (HALFKAY_S if timeout is None else timeout)
    while True:
        serials = halfkay_serials()
        if serials is None or serials == [want]:
            return serials == [want]
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def relaunch(info: HexInfo, uid: str, primary: Path, port: str | None, log=print) -> Banner:
    """Bring a board back without changing what it runs: into the bootloader,
    then the loader boots the firmware already there (``teensy_loader_cli -b``
    where it exists), else the flasher rewrites the same hex. A board in the
    bootloader cannot say its chip id, so the flasher refuses it by ``--uid``;
    when it is the one board waiting there, by its HalfKay serial, it is
    written without one, which the flasher allows for a board on its own."""
    if port:
        request_bootloader(port)
        time.sleep(1.0)
    cli = shutil.which("teensy_loader_cli")
    if cli:
        subprocess.run([cli, "--mcu=TEENSY41", "-b", "-s"], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60)
        try:
            return verify(info, uid, port)
        except (FlashError, LinkError, TransportError) as e:
            log(f"boot-only relaunch did not bring the board back ({e}); rewriting the same image")
    if waiting_alone(uid):
        log(f"the board waits in the bootloader (HalfKay serial {bootloader_serial(uid)}): writing it unnamed")
        return flash(info, uid, primary, port_hint=port, force=True, named=False, log=log).banner
    return flash(info, uid, primary, port_hint=port, force=True, log=log).banner
