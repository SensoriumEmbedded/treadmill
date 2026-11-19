"""A station opened outside pytest: the library way in, and what the CLI,
``shell`` and ``exec`` use.

``open_station("rigA-sys29-trplus")`` locks the rig, opens the board, opens
the capture when the rig has one, loads or makes the calibration, loads the
glyph bank, and hands back a ``Session`` whose ``screen`` reads through the
policy of section 2.2. The pytest fixtures build the same objects through
the same helpers, so a test and a script see one behavior. With
``TREADMILL_FAKE_BOARD`` set, the station is the self-tests' fake board and
its rendered screen, so every way in works with no hardware.
``open_linkless()`` opens a station without its TeensyROM (``--no-link``):
the rig lock, the capture and the keyboard board.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from treadmill.board import Board, open_keyboard
from treadmill.c64.keys import HwKeys, KernalKeys, Keys
from treadmill.c64.menu import MenuDriver
from treadmill.link import DebugCommands
from treadmill.observe.calibrate import (
    LAYOUT_MENU, Calibration, CalibrationError, calibration_path, find_text_area, refine, with_bleed,
)
from treadmill.observe.capture import CaptureError, FrameSource, open_capture
from treadmill.observe.channels import ScreenChannel, XCheck, from_dump
from treadmill.observe.glyphs import GlyphBank, GlyphError, bootstrap_bank, load_chargen
from treadmill.station import Station, load_station
from treadmill.station.config import data_dir
from treadmill.station.kbd import KeyboardBoard
from treadmill.station.lock import RigLock

if TYPE_CHECKING:
    from treadmill.firmware.prepare import FirmwareRun

CHARGEN_ENV = "TREADMILL_CHARGEN"
FAKE_ENV = "TREADMILL_FAKE_BOARD"     # the self-tests: a fake board instead of a port


def fake_requested() -> bool:
    return bool(os.environ.get(FAKE_ENV))


def bank_path(station: Station, root: Path | None = None) -> Path:
    """The station's own bank, keyed like its calibration: a bank learns one
    picture's blur and spill, and the PAL Kawari menu reads with no unsure
    cell on its own bank against three to five a frame on the C128's."""
    return Path(root or data_dir()) / "glyphs" / f"{station.rig.capture.capture_class}__{station.machine.id}.npz"


def find_bank(station: Station, root: Path | None = None) -> Path:
    """The bank a station reads with: its own when one was harvested on its
    picture, else the one for the rig's capture class."""
    own = bank_path(station, root)
    shared = own.with_name(f"{station.rig.capture.capture_class}.npz")
    return own if own.is_file() or not shared.is_file() else shared


def open_video(station: Station, log=print) -> FrameSource | None:
    """The rig's capture, or None (with the reason logged) when it has none
    or it will not open."""
    if station.rig.capture is None:
        return None
    try:
        src = open_capture(station.rig.capture)
    except CaptureError as e:
        log(f"[video] {e}")
        return None
    log(f"[video] {src.describe()}")
    return src


def load_bank(station: Station, chargen: str | None = None, root: Path | None = None, log=print) -> GlyphBank | None:
    """The harvested bank (``find_bank``); failing that, a bootstrap from a
    character ROM named by ``chargen`` or TREADMILL_CHARGEN; failing that,
    None."""
    path = find_bank(station, root)
    if path.is_file():
        bank = GlyphBank.load(path)
        log(f"[video] glyph bank {path.name}: {bank.harvested()} of 512 codes harvested")
        return bank
    rom = chargen or os.environ.get(CHARGEN_ENV)
    if rom:
        try:
            bank = bootstrap_bank(load_chargen(rom), station.rig.capture.capture_class)
        except (OSError, GlyphError) as e:
            log(f"[video] character ROM {rom}: {e}")
            return None
        log(f"[video] no glyph bank at {path}; bootstrapped from {Path(rom).name} (run treadmill harvest)")
        return bank
    log(f"[video] no glyph bank at {path} and no character ROM; run treadmill harvest --station {station.id}")
    return None


def stored_calibration(station: Station, video: FrameSource, *, root: Path | None = None,
                       log=print) -> Calibration | None:
    """The saved calibration for (capture class, machine) when it fits the
    frames the capture delivers now, else None."""
    path = calibration_path(root or data_dir(), station.rig.capture.capture_class, station.machine.id)
    if not path.is_file():
        return None
    frame = video.wait_stable(agree=2, timeout=15.0)
    cal = Calibration.load(path)
    if not cal.fits(frame.image):
        log(f"[video] calibration {path.name} is for {cal.frame_w}x{cal.frame_h}, frames are "
            f"{frame.size[0]}x{frame.size[1]}")
        return None
    if cal.bleed is None:                      # saved before the border's bleed was part of one
        cal = with_bleed(frame.image, cal)
    log(f"[video] calibration {path.name}: cell {cal.cell_w:.1f}x{cal.cell_h:.1f} px, {cal.method}")
    return cal


def load_or_calibrate(board: Board, video: FrameSource, bank: GlyphBank | None, *, root: Path | None = None,
                      force: bool = False, save: bool = True, log=print) -> Calibration:
    """The stored calibration for (capture class, machine) when it fits the
    frames; otherwise a fresh one from the menu, refined against a DMA read
    when the board allows it, and saved."""
    station = board.station
    path = calibration_path(root or data_dir(), station.rig.capture.capture_class, station.machine.id)
    if not force:
        cal = stored_calibration(station, video, root=root, log=log)
        if cal is not None:
            return cal
    board.ensure_menu()
    frame = video.wait_stable(agree=2, timeout=15.0)
    cal = find_text_area(frame.image, LAYOUT_MENU)
    if bank is not None and board.dma_allowed():
        dma = from_dump(board.link.read_screen())
        cal = with_bleed(frame.image, refine(frame.image, cal, dma.codes, dma.charset, bank))
    cal = Calibration(**{**cal.__dict__, "capture_class": station.rig.capture.capture_class,
                         "machine_id": station.machine.id})
    if save:
        cal.save(path)
    log(f"[video] calibrated: origin ({cal.x0:.1f}, {cal.y0:.1f}), cell {cal.cell_w:.2f}x{cal.cell_h:.2f} px, "
        f"{cal.method}, residual {cal.residual:.3f}" + (f", saved {path.name}" if save else ""))
    return cal


# -- the station's hands and tools, for fixtures and scripts alike ---------------

def fake_keyboard(station: Station, tr=None):
    """For the self-tests: a factory for a fake keyboard board with the chip
    id the station names, or None when the station names none. Given the
    fake TeensyROM, what it types reaches that fake's C64."""
    if station.keyboard is None:
        return None
    from selftest.fake_kbd import FakeKeyboard, getin_codes

    chip = station.keyboard.chip
    on_type = (lambda text: tr.press_keys(getin_codes(text))) if tr is not None else None
    return lambda: KeyboardBoard.attach(FakeKeyboard(chip=chip, on_type=on_type).connect(), port="fake-keyboard")


def keyboard_line(kbd: KeyboardBoard | None, problem: str | None) -> str | None:
    """One log line on the station's keyboard board, or None when it names none."""
    if kbd is not None:
        ident = kbd.identity
        fitted = ",".join(sorted(kbd.fitted)) or "nothing"
        return (f"[kbd] keyboard board chip {kbd.chip} on {kbd.port}: {ident.get('mcu', '?')} "
                f"fw {ident.get('fw', '?')}, fitted {fitted}")
    if problem:
        return f"[kbd] {problem}; the keyboard board's capabilities are off"
    return None


def keys_for(board: Board | LinklessSession) -> Keys | None:
    """A way to press keys that reach GETIN, or None when the station has
    none: the KERNAL buffer by DMA when the TR+ can write it, else the
    keyboard board's matrix. ``--caps-mask dma`` sends a TR+'s keys through
    the keyboard board, ``keys_hw`` through DMA, ``keys_kernal`` nowhere."""
    st = board.station
    if not st.has("keys_kernal"):
        return None
    if st.has("dma"):
        return KernalKeys(board)
    if st.has("keys_hw"):
        return HwKeys(board.kbd)
    return None


def menu_for(board: Board, screen: ScreenChannel) -> MenuDriver:
    """The main menu through the screen channel, able to move when the station presses keys."""
    return MenuDriver(screen, keys_for(board), board=board)


def dbg_for(board: Board) -> DebugCommands:
    """The single-letter debug commands, gated on the flashed build's defines."""
    defines = {c[len("fw:"):] for c in board.station.caps if c.startswith("fw:")}
    return DebugCommands(board.link, defines, allow_eeprom_reset=board.station.allow_eeprom_reset)


@dataclass
class Session:
    """An open station: board, optional video, and the screen channel."""
    board: Board
    video: FrameSource | None
    calibration: Calibration | None
    bank: GlyphBank | None
    xchecks: list[XCheck] = field(default_factory=list)

    @property
    def station(self) -> Station:
        return self.board.station

    @property
    def link(self):
        return self.board.link

    @property
    def kbd(self) -> KeyboardBoard | None:
        return self.board.kbd

    @property
    def has_video(self) -> bool:
        return self.video is not None and self.calibration is not None and self.bank is not None

    def screen_channel(self) -> ScreenChannel:
        return ScreenChannel(self.board.link, self.video if self.has_video else None, self.calibration,
                             self.bank, dma_allowed=self.board.dma_allowed, on_xcheck=self.xchecks.append)

    def keys(self) -> Keys | None:
        return keys_for(self.board)

    def menu(self, screen: ScreenChannel | None = None) -> MenuDriver:
        return menu_for(self.board, screen or self.screen_channel())

    def dbg(self) -> DebugCommands:
        return dbg_for(self.board)

    def close(self) -> None:
        try:
            if self.video is not None:
                self.video.close()
        finally:
            self.board.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _open_fake(station: Station, *, attended: bool, fw_defines: tuple[str, ...], caps_mask, video: bool) -> Session:
    """The self-tests' fake board as a station: its rendered screen is the
    capture, with the synthetic calibration and bank that read it."""
    from selftest.fake_board import start_fake_board
    from selftest.synth import calibrated, fake_frame_source, harvested_bank
    from treadmill.link import Link

    fake, stream = start_fake_board(debug_defines=set(fw_defines))
    board = Board.open(station, attended=attended, link_factory=lambda: Link(stream),
                       fw_defines=fw_defines, caps_mask=caps_mask, kbd_factory=fake_keyboard(station, fake))
    if not video:
        board.mask({"video"})
        return Session(board, None, None, None)
    cal = calibrated()
    return Session(board, fake_frame_source(fake), cal, harvested_bank(cal))


def open_station(station_id: str, *, attended: bool = False, video: bool = True, chargen: str | None = None,
                 calibrate: bool = True, firmware: "FirmwareRun | None" = None,
                 caps_mask: set[str] | frozenset[str] = frozenset(), log=print) -> Session:
    """Lock, open and probe the station; open its capture and the video
    channel's calibration and bank where they exist. ``firmware`` is what
    ``prepare_firmware()`` put on the board, or found there: its defines
    become ``fw:`` capabilities and its hex gives the recovery ladder its
    reboot rung."""
    station = load_station(station_id)
    fw_defines = tuple(firmware.defines) if firmware is not None else ()
    if fake_requested():
        return _open_fake(station, attended=attended, fw_defines=fw_defines, caps_mask=caps_mask, video=video)
    board = Board.open(station, attended=attended, fw_defines=fw_defines, caps_mask=caps_mask, firmware=firmware)
    line = keyboard_line(board.kbd, board.kbd_problem)
    if line:
        log(line)
    src = bank = cal = None
    try:
        src = open_video(board.station, log) if video else None
        if src is None and video:
            board.mask({"video"})
        if src is not None:
            try:
                bank = load_bank(board.station, chargen, log=log)
                if calibrate:
                    cal = load_or_calibrate(board, src, bank, log=log)
            except (CalibrationError, CaptureError, GlyphError) as e:
                log(f"[video] {e}")
            if bank is None or cal is None:
                board.mask({"video"})
    except BaseException:
        # Calibrating goes through the board (ensure_menu, a DMA read): what it raises must not leave
        # the rig locked and the port and capture open in this process.
        if src is not None:
            src.close()
        board.close()
        raise
    return Session(board, src, cal, bank)


# -- a station without its TeensyROM ----------------------------------------------

@dataclass
class LinklessSession:
    """A station opened without its TeensyROM (``--no-link``): the rig held,
    the capture and the keyboard board open, for a script about the machine
    or the keyboard board itself. The screen reads video only, from the
    stored calibration, since making one needs the TR's menu."""
    station: Station
    lock: RigLock
    kbd: KeyboardBoard | None
    kbd_problem: str | None
    video: FrameSource | None
    calibration: Calibration | None
    bank: GlyphBank | None

    @property
    def has_video(self) -> bool:
        return self.video is not None and self.calibration is not None and self.bank is not None

    def screen_channel(self) -> ScreenChannel:
        return ScreenChannel(None, self.video if self.has_video else None, self.calibration, self.bank)

    def keys(self) -> Keys | None:
        return keys_for(self)

    def close(self) -> None:
        try:
            if self.video is not None:
                self.video.close()
        finally:
            try:
                if self.kbd is not None:
                    self.kbd.close()
            finally:
                self.lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_linkless(station_id: str, *, video: bool = True, chargen: str | None = None,
                  caps_mask: set[str] | frozenset[str] = frozenset(), log=print) -> LinklessSession:
    """Lock the rig and open the station's keyboard board and capture, never
    its TeensyROM. The capabilities are the rig's and the keyboard board's:
    no ``serial``, and no ``video`` or ``screen`` unless a stored calibration
    fits the frames."""
    station = load_station(station_id)
    lock = RigLock(station.rig.id, wait=5.0).acquire()
    kbd = src = None
    try:
        fake = fake_requested()
        kbd, problem = open_keyboard(station, fake_keyboard(station) if fake else None, discover=not fake)
        if kbd is not None:
            station = station.with_keyboard(kbd.fitted)
        line = keyboard_line(kbd, problem)
        if line:
            log(line)
        cal = bank = None
        if video and not fake:
            src = open_video(station, log)
            if src is not None:
                try:
                    bank = load_bank(station, chargen, log=log)
                    cal = stored_calibration(station, src, log=log)
                    if cal is None:
                        log(f"[video] no stored calibration fits; video is off (treadmill calibrate --station "
                            f"{station.id} makes one from the TR's menu)")
                except (CalibrationError, CaptureError, GlyphError) as e:
                    log(f"[video] {e}")
        session = LinklessSession(station, lock, kbd, problem, src, cal, bank)
        removed = set(caps_mask) | {"serial"}
        if not session.has_video:
            removed |= {"video", "screen"}
        session.station = station.masked(removed)
        return session
    except BaseException:
        if src is not None:
            src.close()
        if kbd is not None:
            kbd.close()
        lock.release()
        raise
