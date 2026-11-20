"""2026-11-15: MinimalBoot under host traffic, and back to the main image.

    treadmill exec --station <id> scripts/2026-11-15-minimalboot-usb.py --json [-- <seconds>]

A TR+ station with video. The main image hands an EasyFlash image with a chip in
bank 64 or above to MinimalBoot (FileParsers.ino), so the script builds idread with
SHOW defined (its counters in hex on the top row, since MinimalBoot refuses DMA)
and adds an erased bank-64 chip the cartridge never selects. Put on the card and
launched:

  1. the port drops and comes back as MinimalBoot: the firmware check says minimal
     and the version reply carries "(minimal)";
  2. for <seconds> (default 60) the host asks for the version in batches, every
     reply counted, while idread counts wrong ROM and IO2 reads on the C64; video
     reads the top row before and after;
  3. a reset from MinimalBoot restarts the main image, which comes back to the menu
     with the build it had before.

result: the replies, the counters before and after, and the two banners.
"""
import sys
import time

from treadmill.link import LinkError, TransportError
from treadmill.link.protocol import VERSION_INFO, to_board
from treadmill.observe.channels import CONFIDENCE_FLOOR
from treadmill.programs.build import CHIP_SIZE, assemble

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
REMOTE = "/treadmill/idread-mb.crt"
EXTRA_BANK = 64
DROP_S = 20.0                   # the launch, to the main image restarting as MinimalBoot
MINIMAL_S = 20.0                # MinimalBoot answering after the port comes back
START_S = 15.0                  # MinimalBoot loading the image and starting the C64
BATCH = 8                       # version requests written at once
MARK = b"(minimal)"
REPLY_S = 2.0
DIGITS = {**{0x30 + i: i for i in range(10)}, **{0x01 + i: 10 + i for i in range(6)}}
FIELDS = ((0, 4), (5, 6), (12, 6))      # passes, wrong ROM, wrong IO2 on the top row


def chip(bank: int, load: int, data: bytes) -> bytes:
    return (b"CHIP" + (0x10 + CHIP_SIZE).to_bytes(4, "big") + bytes(2) + bank.to_bytes(2, "big")
            + load.to_bytes(2, "big") + CHIP_SIZE.to_bytes(2, "big") + data)


def counters(label: str) -> tuple[int, int, int] | None:
    """(passes, wrong ROM, wrong IO2) from the top row, or None when no read
    of it is whole."""
    for attempt in range(5):
        read = screen.read_video()
        (artifacts / f"{label}-{attempt}.screen.txt").write_text(read.text() + "\n", encoding="utf-8")
        values = []
        for start, width in FIELDS:
            cells = range(start, start + width)
            if any(int(read.codes[i]) not in DIGITS or read.confidence[i] < CONFIDENCE_FLOOR for i in cells):
                break
            values.append(int("".join("%x" % DIGITS[int(read.codes[i])] for i in cells), 16))
        else:
            return tuple(values)
        time.sleep(0.3)
    return None


def wait_minimal() -> str:
    deadline = time.monotonic() + DROP_S
    while not board.link.stream.ended and time.monotonic() < deadline:
        time.sleep(0.1)
    if not board.link.stream.ended:
        raise AssertionError(f"the port did not drop within {DROP_S:.0f} s: the main image kept the launch")
    board.reconnect()
    deadline = time.monotonic() + MINIMAL_S
    while time.monotonic() < deadline:
        if board.link.fw_check() == "minimal":
            return board.link.version()
        time.sleep(0.5)
    raise AssertionError(f"MinimalBoot did not answer the firmware check within {MINIMAL_S:.0f} s")


result = {"seconds": SECONDS}
before = board.banner
result["main_before"] = {"version": before.version, "built": before.built, "minimal": before.minimal}
primary = getattr(firmware, "primary", None) or station.rig.firmware_checkout
image = assemble("idread", primary=primary, extra={"SHOW": 1}).read_bytes()
image += chip(EXTRA_BANK, 0x8000, bytes([0xFF]) * CHIP_SIZE)
(artifacts / "idread-mb.crt").write_bytes(image)

try:
    link.push_file(image, REMOTE, "sd")
    link.launch(REMOTE, "sd")
    version = wait_minimal()
    result["minimal_version"] = version.strip()
    assert MARK.decode() in version, f"the version reply does not say minimal: {version!r}"

    deadline = time.monotonic() + START_S
    first = None
    while time.monotonic() < deadline:
        first = counters("start")
        if first and first[0]:
            break
        time.sleep(0.5)
    assert first and first[0], f"idread is not counting on the C64's top row: {first}"
    result["counters_start"] = first

    stream = board.link.stream
    replies = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < SECONDS:
        stream.write(to_board(VERSION_INFO) * BATCH)
        for _ in range(BATCH):
            got = stream.read_until(MARK, REPLY_S)
            assert got is not None, f"reply {replies + 1} did not come within {REPLY_S:.0f} s"
            replies += 1
    elapsed = time.monotonic() - t0
    stream.drain(0.3)
    result["replies"] = replies
    result["replies_per_s"] = round(replies / elapsed, 1)

    last = counters("end")
    result["counters_end"] = last
    assert last, "the top row did not read whole after the traffic"
    passes = (last[0] - first[0]) & 0xFFFF
    result["passes"] = passes
    result["rom_reads"] = passes * 512
    result["io2_reads"] = passes * 256
    result["wrong_rom"] = last[1] - first[1]
    result["wrong_io2"] = last[2] - first[2]
    assert passes, "idread stopped counting during the traffic"

    try:
        print(board.link.reset())
    except (LinkError, TransportError) as e:      # the reply can be cut off by the port dropping
        print(f"reset: {e}")
    board.wait_for_menu()
finally:
    board.ensure_menu()

after = board.banner
result["main_after"] = {"version": after.version, "built": after.built, "minimal": after.minimal}
assert not after.minimal and after.built == before.built, "the main image did not come back with the same build"
board.link.delete_file(REMOTE, "sd")
result["ok"] = True
print(result)
