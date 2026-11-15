"""``treadmill.asid``: the messages match the firmware's decode, the log
reads back across its wrap, and the comparison names what went wrong."""

import pytest

from treadmill import asid


def _decode(message: bytes) -> list[tuple[int, int]]:
    """IOH_ASID.c's DecodeSendSIDRegData for SID1, without a forced order."""
    assert message[:3] == bytes([0xF0, 0x2D, 0x4E]) and message[-1] == 0xF7
    pairs, n = [], 0
    for mask in range(4):
        for bit in range(7):
            if message[3 + mask] & (1 << bit):
                value = message[11 + n] | (0x80 if message[7 + mask] & (1 << bit) else 0)
                pairs.append((asid.TYPE_SID1 | asid.ID_TO_REG[mask * 7 + bit], value))
                n += 1
    assert 12 + n <= len(message)                  # the firmware's "more regs expected" check
    return pairs


def test_a_frame_message_decodes_to_its_pairs():
    for frame in (0, 1, 7, 255, 1000):
        message = asid.frame_message(frame)
        assert all(b < 0x80 for b in message[1:-1])
        assert _decode(message) == asid.frame_pairs(frame)
    values = {v for f in range(256) for v in asid.frame_values(f)}
    assert len(values) == 256                      # every bit of every value is exercised
    assert not {p[0] & 0x1F for p in asid.frame_pairs(3)} & {4, 11, 18}     # no voice control registers


def test_the_write_order_message_puts_each_id_at_its_own_place():
    message = asid.write_order_message()
    order = [255] * asid.NUM_IDS
    for cnt in range(0, len(message) - 4, 2):      # IOH_ASID.c's APT_WriteOrder loop
        order[message[cnt + 3] & 0x3F] = cnt // 2
    assert order == list(range(asid.NUM_IDS)) and message[2] == asid.APT_WRITE_ORDER


class _Memory:
    def __init__(self):
        self.bytes = bytearray(0x10000)

    def read(self, address: int, length: int) -> bytes:
        return bytes(self.bytes[address:address + length])

    def log(self, pairs, count_from: int) -> None:
        """What asidlog.a does for each pair, from a count already logged."""
        n = count_from
        for a, d in pairs:
            at = asid.LOG + (n * 2) % (asid.LOG_END - asid.LOG)
            self.bytes[at], self.bytes[at + 1] = a, d
            n += 1
        self.bytes[asid.LOG_COUNT:asid.LOG_COUNT + 3] = n.to_bytes(3, "little")


def test_the_pair_log_reads_across_the_ring_s_wrap():
    mem = _Memory()
    log = asid.PairLog(mem.read)
    sent = [((i * 5) & 0xFF, i & 0xFF) for i in range(log.capacity * 2 + 123)]
    done = 0
    got = []
    for chunk in (4000, 5000, 3000, 6000, log.capacity * 2 + 123 - 18000):
        mem.log(sent[done:done + chunk], done)
        done += chunk
        got += log.poll()
    assert got == sent and log.poll() == []


def test_the_pair_log_refuses_a_read_that_fell_behind():
    mem = _Memory()
    log = asid.PairLog(mem.read)
    mem.log([(0x80, 0)] * (log.capacity * 3 // 4 + 1), 0)
    with pytest.raises(asid.LogError, match="read it more often"):
        log.poll()


def _frames(n):
    return [p for f in range(n) for p in asid.frame_pairs(f)]


def test_an_intact_stream_compares_clean_and_other_types_are_counted_apart():
    want = _frames(20)
    logged = [(0x20, 0x41)] * 3 + want[:100] + [(0x40, 0)] + want[100:]
    result = asid.compare_pairs(logged, want)
    assert result.errors == 0 and result.matched == len(want)
    assert result.other == {0x20: 3, 0x40: 1}


def test_a_garbled_a_missing_and_a_doubled_pair_are_told_apart():
    want = _frames(20)
    logged = list(want)
    logged[50] = (logged[50][0], logged[50][1] ^ 0xFF)        # a value read late
    del logged[200]                                           # the queue moved on
    logged.insert(300, logged[299])                           # the queue did not
    result = asid.compare_pairs(logged, want)
    assert (result.garbled, result.missing, result.extra) == (1, 1, 1), result.describe()
    assert result.errors == 3 and "pair 50" in result.examples[0]


def test_a_parser_error_counts_and_a_short_log_shows_as_missing():
    want = _frames(5)
    result = asid.compare_pairs(want[:-10] + [(asid.TYPE_ERROR, 0)], want)
    assert result.missing == 10 and result.other == {asid.TYPE_ERROR: 1} and result.errors == 11


def test_the_board_s_midi_output_is_found_by_chip_id():
    names = ["Microsoft GS Wavetable Synth 0", "TeensyROM-12345678 1", "TeensyROM-2345678 2"]
    assert asid.board_midi_name("2345678", names) == "TeensyROM-2345678 2"
    with pytest.raises(asid.LogError, match="0 MIDI outputs"):
        asid.board_midi_name("1234", names)
