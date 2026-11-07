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
          current: Banner | None = None, force: bool = False, log=print) -> Flashed:
    """Flash unless the board already runs the hex (or ``force``), then verify."""
    t0 = time.monotonic()
    if not force and board_runs(current, info):
        log(f"board already runs {info.version} ({info.stamp}); flash skipped")
        return Flashed(info, current, 0.0, True)
    cmd = ["node", str(Path(primary) / "tools" / "flash-firmware.mjs", ), "--hex", str(info.path), "--uid", uid]
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


def relaunch(info: HexInfo, uid: str, primary: Path, port: str | None, log=print) -> Banner:
    """Bring a board back without changing what it runs: into the bootloader,
    then the loader boots the firmware already there (``teensy_loader_cli -b``
    where it exists), else the flasher rewrites the same hex."""
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
    return flash(info, uid, primary, port_hint=port, force=True, log=log).banner
