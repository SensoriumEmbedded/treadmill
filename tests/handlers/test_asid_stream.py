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
rates of both.
"""

import json
import threading
import time

import pytest

from treadmill import asid
from treadmill.link.protocol import PING, to_board
from treadmill.session import fake_requested

PLAYER = "/MIDI + ASID/TeensyROM ASID Player    +TR ASID"
START_S = 20.0
PACED_S = 30.0
FLAT_OUT_S = 60.0
PING_EVERY_S = 0.005
READ_EVERY_S = 0.5
SETTLE_S = 5.0                  # after the last frame, for the C64 to read what is queued
ORDER_SETTLE_S = 2.0            # the write-order message's text to reach the player's screen


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
    yields (board, mido, port, log)."""
    if fake_requested():
        pytest.skip("the fake board has no ASID handler or USB MIDI")
    board = request.getfixturevalue("at_menu")
    mido, port = _midi_output(board.station.tr.uid)
    link = board.link
    try:
        link.launch(PLAYER, "teensy")
        vector = _wait_for_player(link)
        logger = programs("asidlog", RESUME=vector + len(asid.PLAYER_IRQ_HEAD)).read_bytes()
        link.write_memory(asid.LOGGER, logger)
        assert link.read_memory(asid.LOGGER, len(logger)) == logger, "the logger did not read back"
        # Both bytes in one DMA write: the CPU is held for all of it, so no interrupt sees half a vector.
        link.write_memory(asid.IRQ_VECTOR, asid.LOG_ENTRY.to_bytes(2, "little"))
        yield board, mido, port, asid.PairLog(link.read_memory)
        link.write_memory(asid.IRQ_VECTOR, vector.to_bytes(2, "little"))
    finally:
        port.close()


def _sysex(mido, message: bytes):
    return mido.Message("sysex", data=message[1:-1])


def _send_frames(mido, port, first: int, seconds: float, period: float, sent: list[int], stop: threading.Event):
    """Frames from ``first`` on for ``seconds``, one per ``period`` (0: as fast
    as the port takes them); ``sent[0]`` counts them as they go."""
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
        port.send(_sysex(mido, asid.frame_message(frame)))
        frame += 1
        sent[0] = frame - first


def _run_phase(board, mido, port, log: asid.PairLog, first: int, seconds: float, period: float) -> dict:
    sent = [0]
    stop = threading.Event()
    sender = threading.Thread(target=_send_frames, args=(mido, port, first, seconds, period, sent, stop),
                              name="asid-sender", daemon=True)
    logged: list[asid.Pair] = []
    stream = board.link.stream
    t0 = time.monotonic()
    sender.start()
    try:
        next_read = t0 + READ_EVERY_S
        while sender.is_alive():
            stream.write(to_board(PING))         # its reply waits for the next read's drain
            time.sleep(PING_EVERY_S)
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
            "result": result, "next_frame": first + sent[0]}


@pytest.mark.needs("dma")
@pytest.mark.duration(3)
def test_asid_pairs_reach_the_c64_intact(asid_player, artifacts):
    board, mido, port, log = asid_player
    rate = 60 if board.station.machine.region.upper() == "NTSC" else 50
    with asid.fine_timer():
        port.send(_sysex(mido, asid.write_order_message()))
        time.sleep(ORDER_SETTLE_S)
        log.poll()                               # the player's "Write Order set" text
        paced = _run_phase(board, mido, port, log, 0, PACED_S, 1.0 / rate)
        flat = _run_phase(board, mido, port, log, paced["next_frame"], FLAT_OUT_S, 0.0)
    report = {name: {**{k: v for k, v in phase.items() if k != "result"}, **phase["result"].to_dict()}
              for name, phase in (("paced", paced), ("flat_out", flat))}
    report["frame_rate_hz"] = rate
    (artifacts / "asid.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    failures = [f"{name}: {phase['result'].describe()}" for name, phase in (("paced", paced), ("flat out", flat))
                if phase["result"].errors]
    assert not failures, "; ".join(failures)
    assert paced["frames"] and flat["frames"], report
