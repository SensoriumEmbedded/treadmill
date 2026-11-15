"""The ASID stream: SID register pairs sent over the board's USB MIDI reach
the C64 intact while the host keeps the serial link busy.

The TeensyROM ASID Player reads each (register, value) pair from IO1 in its
interrupt, and an IO1 read the board answers late hands the C64 a wrong
byte. A logger (``asidlog.a``) goes in front of the player's interrupt by
DMA and records every pair the C64 reads; the test reads the log back as it
fills and lines it up with what it sent. Pings and the log reads keep the
board queueing USB serial replies meanwhile, the path late answers have come
from (the Teensy core masking interrupts while it queues a USB transfer).

Two phases: frames at the machine's frame rate, as a player sends them, then
frames as fast as the board takes them, which the firmware paces by reading
MIDI only once the C64 has emptied its queue. Run once on main and once on a
candidate to compare: ``asid.json`` in the test's folder has the counts and
rates of each phase, written as the phase ends.

Two host loads: pings, and file transfers (an 8 KB file pushed to the card
and read back, round after round), which the firmware also takes while the
ASID Player runs. A file command holds the main loop, so the player is fed
no pairs while one runs and its reads overlap only the start of each; small
files make many starts.
"""

import json
import threading
import time
from pathlib import Path

import pytest

from treadmill import asid
from treadmill.link.protocol import PING, to_board
from treadmill.session import fake_requested

PLAYER = "/MIDI + ASID/TeensyROM ASID Player    +TR ASID"
START_S = 20.0
PACED_S = 30.0
FLAT_OUT_S = 60.0
PING_EVERY_S = 0.005
PING_REPLY = b"ready!\n"        # the ASID handler leaves the menu's ping answer as it is
PING_SETTLE_S = 5.0
READ_EVERY_S = 0.25             # flat out the C64 takes ~12k pairs/s; the ring holds 12288
LOG_DRAIN_S = 0.01              # every ping's reply is taken first, so nothing else is in flight
SETTLE_S = 5.0                  # after the last frame, for the C64 to read what is queued
ORDER_SETTLE_S = 2.0            # the write-order message's text to reach the player's screen
LOAD_FILE = "/treadmill/asidload.bin"
LOAD_FILE_BYTES = 8192


class _Pings:
    """Pings sent without waiting for their replies, and the replies taken
    before the next command: a late one would land in that command's reply."""

    def __init__(self, stream):
        self._s = stream
        self.sent = 0
        self.pending = 0

    def send(self) -> None:
        self._s.write(to_board(PING))
        self.sent += 1
        self.pending += 1

    def settle(self) -> None:
        deadline = time.monotonic() + PING_SETTLE_S
        while self.pending:
            left = deadline - time.monotonic()
            if left <= 0 or self._s.read_until(PING_REPLY, left) is None:
                raise AssertionError(f"{self.pending} ping replies still owed after {PING_SETTLE_S:.0f} s")
            self.pending -= 1


def _midi_output(uid: str):
    try:
        import mido
        names = mido.get_output_names()          # imports the rtmidi backend
    except ImportError as e:
        pytest.skip(f"no MIDI backend ({e}): pip install -e .[midi]")
    try:
        name = asid.board_midi_name(uid, names)
    except asid.LogError as e:
        pytest.skip(str(e))
    return mido, mido.open_output(name)


def _wait_for_player(link) -> int:
    """The player's interrupt handler, once the player has hooked $0314."""
    deadline = time.monotonic() + START_S
    while time.monotonic() < deadline:
        vector = int.from_bytes(link.read_memory(asid.IRQ_VECTOR, 2), "little")
        if link.read_memory(vector, len(asid.PLAYER_IRQ_HEAD)) == asid.PLAYER_IRQ_HEAD:
            return vector
        time.sleep(0.5)
    pytest.fail(f"the player's interrupt (ldx $DEC2 / ldy $DEC4 behind $0314) did not appear within {START_S:.0f} s")


@pytest.fixture
def asid_player(request: pytest.FixtureRequest, programs):
    """The ASID Player running with the logger in front of its interrupt;
    yields (board, mido, port, pings, log)."""
    if fake_requested():
        pytest.skip("the fake board has no ASID handler or USB MIDI")
    board = request.getfixturevalue("at_menu")
    mido, port = _midi_output(board.station.tr.uid)
    link = board.link
    pings = _Pings(link.stream)

    def read(at: int, length: int) -> bytes:
        pings.settle()
        return link.read_memory(at, length, drain=LOG_DRAIN_S)

    try:
        link.launch(PLAYER, "teensy")
        vector = _wait_for_player(link)
        logger = programs("asidlog", RESUME=vector + len(asid.PLAYER_IRQ_HEAD)).read_bytes()
        link.write_memory(asid.LOGGER, logger)
        assert link.read_memory(asid.LOGGER, len(logger)) == logger, "the logger did not read back"
        # Both bytes in one DMA write: the CPU is held for all of it, so no interrupt sees half a vector.
        link.write_memory(asid.IRQ_VECTOR, asid.LOG_ENTRY.to_bytes(2, "little"))
        yield board, mido, port, pings, asid.PairLog(read)
        pings.settle()
        link.write_memory(asid.IRQ_VECTOR, vector.to_bytes(2, "little"))
    finally:
        port.close()


def _sysex(mido, message: bytes):
    return mido.Message("sysex", data=message[1:-1])


def _send_frames(mido, port, first: int, seconds: float, period: float, sent: list[int], stop: threading.Event,
                 gate: threading.Lock):
    """Frames from ``first`` on for ``seconds``, one per ``period`` (0: as fast
    as the port takes them); ``sent[0]`` counts them as they go. Each send
    holds ``gate``."""
    start = time.monotonic()
    end = start + seconds
    frame = first
    while not stop.is_set():
        now = time.monotonic()
        if now >= end:
            break
        if period:
            due = start + (frame - first) * period
            if due > now:
                time.sleep(due - now)
        with gate:
            port.send(_sysex(mido, asid.frame_message(frame)))
        frame += 1
        sent[0] = frame - first


class _Files:
    """The file load: one round pushes the file to the card and reads it back.

    A round holds the frame sender's gate: a send blocks while the board is
    not reading MIDI, as in a file command, and a blocked send stalled the
    host's own serial writes until the board gave up on the post ("Timeout!")."""

    def __init__(self, link, gate: threading.Lock):
        self._link = link
        self._gate = gate
        self._data = bytes((i * 13 + 7) & 0xFF for i in range(LOAD_FILE_BYTES))
        self.sent = 0

    def send(self) -> None:
        with self._gate:
            self._link.push_file(self._data, LOAD_FILE, "sd")
            back = self._link.get_file(LOAD_FILE, "sd")
        assert back == self._data, f"round {self.sent + 1}: the file read back differs ({len(back)} bytes)"
        self.sent += 1

    def remove(self) -> None:
        self._link.delete_file(LOAD_FILE, "sd")


class _PingLoad:
    def __init__(self, pings: _Pings):
        self._pings = pings

    @property
    def sent(self) -> int:
        return self._pings.sent

    def send(self) -> None:
        self._pings.send()
        time.sleep(PING_EVERY_S)


def _run_phase(mido, port, load, gate: threading.Lock, log: asid.PairLog, first: int, seconds: float,
               period: float) -> dict:
    sent = [0]
    stop = threading.Event()
    sender = threading.Thread(target=_send_frames, args=(mido, port, first, seconds, period, sent, stop, gate),
                              name="asid-sender", daemon=True)
    logged: list[asid.Pair] = []
    load_before = load.sent
    t0 = time.monotonic()
    sender.start()
    try:
        next_read = t0 + READ_EVERY_S
        while sender.is_alive():
            load.send()
            if time.monotonic() >= next_read:
                logged += log.poll()
                next_read = time.monotonic() + READ_EVERY_S
    finally:
        stop.set()
        sender.join()
    elapsed = time.monotonic() - t0
    want = [p for f in range(first, first + sent[0]) for p in asid.frame_pairs(f)]
    deadline = time.monotonic() + SETTLE_S
    while time.monotonic() < deadline:
        logged += log.poll()
        if sum(1 for p in logged if p[0] & asid.TYPE_MASK == asid.TYPE_SID1) >= len(want):
            break
        time.sleep(READ_EVERY_S)
    result = asid.compare_pairs(logged, want)
    return {"frames": sent[0], "seconds": round(elapsed, 2),
            "pairs_per_s": round(result.received / elapsed, 1) if elapsed else 0.0,
            "load_rounds": load.sent - load_before, "result": result, "next_frame": first + sent[0]}


def _record(path: Path, report: dict, name: str, phase: dict) -> None:
    report[name] = {**{k: v for k, v in phase.items() if k != "result"}, **phase["result"].to_dict()}
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


@pytest.mark.needs("dma")
@pytest.mark.duration(3)
@pytest.mark.parametrize("load_name", ["pings", "files"])
def test_asid_pairs_reach_the_c64_intact(asid_player, artifacts, load_name):
    board, mido, port, pings, log = asid_player
    video = (board.banner.video if board.banner else None) or board.station.machine.region   # a Kawari switches
    rate = 60 if video.upper() == "NTSC" else 50
    report: dict = {"frame_rate_hz": rate, "load": load_name}
    out = artifacts / "asid.json"
    gate = threading.Lock()
    load = _PingLoad(pings) if load_name == "pings" else _Files(board.link, gate)
    with asid.fine_timer():
        port.send(_sysex(mido, asid.write_order_message()))
        time.sleep(ORDER_SETTLE_S)
        log.poll()                               # the player's "Write Order set" text
        paced = _run_phase(mido, port, load, gate, log, 0, PACED_S, 1.0 / rate)
        _record(out, report, "paced", paced)
        flat = _run_phase(mido, port, load, gate, log, paced["next_frame"], FLAT_OUT_S, 0.0)
        _record(out, report, "flat_out", flat)
    if isinstance(load, _Files):
        load.remove()
    failures = [f"{name}: {phase['result'].describe()}" for name, phase in (("paced", paced), ("flat out", flat))
                if phase["result"].errors]
    assert not failures, "; ".join(failures)
    assert paced["frames"] and flat["frames"], report
