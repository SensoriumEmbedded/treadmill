"""Test 7: a build with ``Dbg_SerTimChg`` lists its bus timing knobs on a bare
``t``, each knob takes a value, and ``td`` brings it back; the special-build
path from define to board. The Kernal Replace pair (``KSenseMode``,
``nS_KSenseSample``) is compiled into a TR+ build only, and ``td`` leaves
``KSenseMode`` where it was: pinned here, so a firmware that changes either
shows up as a failure rather than as a sweep reading the wrong knob."""

import pytest

from treadmill.knobs import KNOBS, parse_listing
from treadmill.link.banner import TR_PLUS

pytestmark = pytest.mark.needs_fw("Dbg_SerTimChg")


def _expected(board) -> set[str]:
    plus = board.banner is not None and board.banner.kind == TR_PLUS
    return {k.letter for k in KNOBS.values() if plus or not k.tr_plus_only}


def _off_default(letter: str, default: int) -> int:
    """A value next to the default: a nanosecond or a cycle away, or another sense mode."""
    if KNOBS[letter].unit == "mode":
        return 0 if default != 0 else 2
    return default - 1 if default > 0 else default + 1


def test_bare_t_lists_the_timing_knobs(at_menu, dbg, artifacts):
    text = dbg.listing()
    (artifacts / "t.txt").write_text(text, encoding="utf-8")
    listed, want = set(parse_listing(text)), _expected(at_menu)
    assert listed == want, (f"listed {sorted(KNOBS[l].name for l in listed)}, expected "
                            f"{sorted(KNOBS[l].name for l in want)} on this board\n{text}")
    assert "Set Defaults" in text and "(td)" in text, text


def test_each_knob_takes_a_value_and_td_restores_it(at_menu, dbg):
    defaults = parse_listing(dbg.restore_defaults())
    try:
        for letter in sorted(_expected(at_menu)):
            knob, default = KNOBS[letter], defaults[letter]
            value = _off_default(letter, default)
            assert parse_listing(dbg.set_knob(letter, value))[letter] == value, f"{knob.command}: not taken"
            reply = dbg.restore_defaults()
            assert "Defaults set" in reply, reply
            after = parse_listing(reply)[letter]
            if knob.restored_by_td:
                assert after == default, f"td left {knob.name} at {after}, not its default {default}"
            else:
                assert after == value, f"td moved {knob.name} to {after}: the knob table says td leaves it"
                dbg.set_knob(letter, default)
    finally:
        dbg.restore_defaults()
        if "x" in defaults:
            dbg.set_knob("x", defaults["x"])
