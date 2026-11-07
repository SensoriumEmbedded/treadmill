"""Test 7: a build with ``Dbg_SerTimChg`` lists its bus timing knobs on a bare
``t``; the special-build path from define to board."""

import re

import pytest

from treadmill.link.dbgcmds import TIMING_NAMES

_KNOB = re.compile(r"(nS_\w+|Cyc_\w+)\s+(\d+)")


@pytest.mark.needs_fw("Dbg_SerTimChg")
def test_bare_t_lists_the_timing_knobs(dbg, artifacts):
    text = dbg.listing()
    (artifacts / "t.txt").write_text(text, encoding="utf-8")
    values = {name: int(value) for name, value in _KNOB.findall(text)}
    missing = [name for name in TIMING_NAMES.values() if name not in values]
    assert not missing, f"knobs missing from the listing: {missing}\n{text}"
    assert "Set Defaults" in text and "(td)" in text, text


@pytest.mark.needs_fw("Dbg_SerTimChg")
def test_defaults_restore(dbg):
    assert "Defaults set" in dbg.restore_defaults()
