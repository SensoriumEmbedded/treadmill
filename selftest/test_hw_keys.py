"""Keys through the keyboard board: GETIN codes as its typed text, the
choice between DMA and the board, the menu moved by real keys, and the
``kbd`` fixture."""

import json
import textwrap
from pathlib import Path

import pytest

from selftest.fake_kbd import getin_codes
from selftest.test_plugin import lab  # noqa: F401  (the fake station and environment)
from selftest.test_station_keyboard import QUIET, kb_lab  # noqa: F401
from treadmill.c64 import keys as k
from treadmill.c64.keys import HwKeys, KernalKeys, KeysUnavailable, typed_text
from treadmill.cli import EXIT_OK, main
from treadmill.session import keys_for, open_station

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("codes, text", [
    ([k.HOME, k.CRSR_RIGHT, k.CRSR_DOWN, k.CRSR_DOWN, k.RETURN], "{home}{right}{down}{down}{return}"),
    ([k.F1, k.F2, k.F7, k.F8, k.STOP, k.SHIFT_RETURN], "{f1}{f2}{f7}{f8}{stop}{shift-return}"),
    ([k.CLEAR, k.DELETE, k.INSERT, k.CRSR_UP, k.CRSR_LEFT], "{clr}{del}{inst}{up}{left}"),
    (b'LOAD"*",8,1', 'load"*",8,1'),
    ([0xC1, 0xDA, 0x5C, k.UP_ARROW, k.LEFT_ARROW, k.SPACE, 0x5B, 0x5D, 0x3F], "AZ\\^_ []?"),
])
def test_getin_codes_as_typed_text(codes, text):
    assert typed_text(codes) == text


@pytest.mark.parametrize("code", [0x05, 0x61, 0x7B, 0x7D, 0xFF])
def test_a_code_with_no_key_is_refused(code):
    with pytest.raises(KeysUnavailable, match=f"\\${code:02X}"):
        typed_text([code])


def _typable(code: int) -> bool:
    try:
        typed_text([code])
    except KeysUnavailable:
        return False
    return True


def test_what_is_typed_reads_back_as_the_same_codes():
    """Every code typed_text takes, read back through the fake's copy of the
    KERNAL's decode tables: the 22 named keys, 52 letters and 35 others."""
    codes = bytes(c for c in range(256) if _typable(c))
    assert len(codes) == 109
    assert getin_codes(typed_text(codes)) == codes


@pytest.mark.parametrize("mask, kind", [
    (set(), KernalKeys),
    ({"dma"}, HwKeys),
    ({"keys_hw"}, KernalKeys),
    ({"keys_kernal"}, None),
    ({"dma", "keys_hw"}, None),
])
def test_keys_come_by_dma_then_by_the_keyboard_board(kb_lab: Path, mask, kind):  # noqa: F811
    with open_station("kb", video=False, caps_mask=mask) as s:
        press = keys_for(s.board)
        assert (type(press) if press is not None else None) is kind
        if kind is HwKeys:
            assert press.kbd is s.kbd


def test_the_menu_moves_by_the_keyboard_board_s_keys(kb_lab: Path, capsys):  # noqa: F811
    code = main(["run", "--station", "kb", "--caps-mask", "dma,screen_dma,reset_sense",
                 "--tests", str(REPO / "tests" / "menu" / "test_navigation.py"),
                 "--golden-dir", str(REPO / "tests" / "golden"), "--reports", str(kb_lab / "reports"),
                 "--json", "--", *QUIET])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    payload = json.loads(out)
    assert payload["summary"] == {"passed": 3}, payload["tests"]
    logs = [p.read_text(encoding="utf-8") for p in Path(payload["run_dir"]).rglob("kbd.log")]
    assert any("> S {f6}" in log for log in logs) and any("> S  " in log for log in logs)


KBD_TESTS = textwrap.dedent("""
    from treadmill.c64.keys import Matrix

    def test_hold(kbd):
        kbd.hold(Matrix.RUN_STOP)
        assert kbd.status()["held"] == "77"

    def test_the_hold_was_let_go(kbd):
        assert kbd.status()["held"] == "-"

    def test_keys(keys):
        assert type(keys).__name__ == "HwKeys"
""")


def test_the_kbd_fixture(kb_lab: Path, pytester: pytest.Pytester):  # noqa: F811
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_kb=KBD_TESTS)
    result = pytester.runpytest("--station", "kb", "--caps-mask", "dma,screen_dma,reset_sense",
                                "--reports", str(kb_lab / "reports"), *QUIET)
    result.assert_outcomes(passed=3)


def test_the_kbd_fixture_skips_without_a_keyboard_board(lab: Path, pytester: pytest.Pytester):  # noqa: F811
    pytester.makeconftest('pytest_plugins = ["treadmill.pytest_plugin"]')
    pytester.makepyfile(test_kb="def test_hold(kbd):\n    raise AssertionError('must skip')\n")
    result = pytester.runpytest("--station", "s1", "--reports", str(lab / "reports"), "-rs", *QUIET)
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*station s1 lacks keys_hw*"])
