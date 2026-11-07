"""A station opened outside pytest: the library way in, and what the CLI,
``shell`` and ``exec`` use.

``open_station("rigA-sys29-trplus")`` locks the rig, opens the board, opens
the capture when the rig has one, loads or makes the calibration, loads the
glyph bank, and hands back a ``Session`` whose ``screen`` reads through the
policy of section 2.2. The pytest fixtures build the same objects through
the same helpers, so a test and a script see one behavior.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from treadmill.board import Board
from treadmill.observe.calibrate import (
    LAYOUT_MENU, Calibration, CalibrationError, calibration_path, find_text_area, refine,
)
from treadmill.observe.capture import CaptureError, FrameSource, open_capture
from treadmill.observe.channels import ScreenChannel, XCheck, from_dump
from treadmill.observe.glyphs import GlyphBank, GlyphError, bootstrap_bank, load_chargen
from treadmill.station import Station, load_station
from treadmill.station.config import data_dir

CHARGEN_ENV = "TREADMILL_CHARGEN"


def bank_path(station: Station, root: Path | None = None) -> Path:
    return Path(root or data_dir()) / "glyphs" / f"{station.rig.capture.capture_class}.npz"


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
    """The harvested bank for the rig's capture class; failing that, a
    bootstrap from a character ROM named by ``chargen`` or TREADMILL_CHARGEN;
    failing that, None."""
    path = bank_path(station, root)
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


def load_or_calibrate(board: Board, video: FrameSource, bank: GlyphBank | None, *, root: Path | None = None,
                      force: bool = False, save: bool = True, log=print) -> Calibration:
    """The stored calibration for (capture class, machine) when it fits the
    frames; otherwise a fresh one from the menu, refined against a DMA read
    when the board allows it, and saved."""
    station = board.station
    path = calibration_path(root or data_dir(), station.rig.capture.capture_class, station.machine.id)
    frame = video.wait_stable(agree=2, timeout=15.0)
    if path.is_file() and not force:
        cal = Calibration.load(path)
        if cal.fits(frame.image):
            log(f"[video] calibration {path.name}: cell {cal.cell_w:.1f}x{cal.cell_h:.1f} px, {cal.method}")
            return cal
        log(f"[video] calibration {path.name} is for {cal.frame_w}x{cal.frame_h}, frames are "
            f"{frame.size[0]}x{frame.size[1]}; recalibrating")
    board.ensure_menu()
    frame = video.wait_stable(agree=2, timeout=15.0)
    cal = find_text_area(frame.image, LAYOUT_MENU)
    if bank is not None and board.dma_allowed():
        dma = from_dump(board.link.read_screen())
        cal = refine(frame.image, cal, dma.codes, dma.charset, bank)
    cal = Calibration(**{**cal.__dict__, "capture_class": station.rig.capture.capture_class,
                         "machine_id": station.machine.id})
    if save:
        cal.save(path)
    log(f"[video] calibrated: origin ({cal.x0:.1f}, {cal.y0:.1f}), cell {cal.cell_w:.2f}x{cal.cell_h:.2f} px, "
        f"{cal.method}, residual {cal.residual:.3f}" + (f", saved {path.name}" if save else ""))
    return cal


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
    def has_video(self) -> bool:
        return self.video is not None and self.calibration is not None and self.bank is not None

    def screen_channel(self) -> ScreenChannel:
        return ScreenChannel(self.board.link, self.video if self.has_video else None, self.calibration,
                             self.bank, dma_allowed=self.board.dma_allowed, on_xcheck=self.xchecks.append)

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


def open_station(station_id: str, *, attended: bool = False, video: bool = True, chargen: str | None = None,
                 calibrate: bool = True, log=print) -> Session:
    """Lock, open and probe the station; open its capture and the video
    channel's calibration and bank where they exist."""
    station = load_station(station_id)
    board = Board.open(station, attended=attended)
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
