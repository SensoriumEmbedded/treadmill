"""Soaks: each probe for ``--soak N`` minutes at the values the flashed build
ships with, and nothing may go wrong. A sweep finds where a knob's window
ends; a soak is the check that the values a release carries hold, run on
every machine before the release. The probes are the sweeps' own: idread
(the CPU's reads and writes, on a TR+), the C64's REU loop with and without
sprites (its own DMA, Special IO set to the REU and put back), the REU's
registers read back by the C64 with and without the host's DMA write jobs
beside it (regread, regread-jobs), the menu's own IO1 handler
(regread-menu), vicrom and vicrom-high (the VIC's reads, by video, the CPU's
counted on a TR+) and idread under MinimalBoot (by video). Each counts every interval and adds the
differences, so the C64's 16-bit counters unwrap (``treadmill.soaks``); a
hang counts against the soak, the probe is relaunched, and the soak goes on.
``soak.json`` in the test's folder keeps every interval, with the station,
machine and build. No knob is moved, so the build needs no debug define."""

import contextlib
import json

import pytest

from treadmill import margins
from treadmill.c64.settings import REU, Settings
from treadmill.probes import IdReadProbe
from treadmill.probes.idread import MinimalIdReadProbe
from treadmill.probes.regread import MenuReadProbe, RegReadProbe
from treadmill.probes.reuloop import ReuLoopProbe
from treadmill.probes.vicrom import VicRomProbe
from treadmill.soaks import soak

# Not timing_sensitive: a release's values are soaked on every machine, timing-tolerant ones included.
pytestmark = [pytest.mark.soak, pytest.mark.duration(10)]

INTERVAL_S = 10.0                 # well inside a 16-bit wrap: the REU client's take about 460 s
NEEDS = {"idread": "dma", "reuloop": "dma", "reuloop-sprites": "dma", "regread": "dma", "regread-jobs": "dma",
         "regread-menu": "dma", "vicrom": "video", "vicrom-high": "video", "idread-minimal": "video"}
CASES = [pytest.param(name, id=name, marks=pytest.mark.needs(cap)) for name, cap in NEEDS.items()]


@pytest.mark.parametrize("name", CASES)
def test_soak(name, at_menu, screen, keys, video, calibration, programs, firmware, artifacts, request):
    board = at_menu
    minutes = request.config.getoption("--soak")
    request.getfixturevalue("sd_card")
    image = "minimal" if name == "idread-minimal" else "main"
    if name == "idread":
        probe = IdReadProbe(board, programs("idread"), log=print)
    elif name.startswith("reuloop"):
        sprites = name == "reuloop-sprites"
        probe = ReuLoopProbe(board, programs("reuclient_sprites" if sprites else "reuclient"), sprites=sprites,
                             log=print)
    elif name in ("regread", "regread-jobs"):
        probe = RegReadProbe(board, programs("reureadback_hammer"), jobs=name == "regread-jobs", log=print)
    elif name == "regread-menu":
        probe = MenuReadProbe(board, programs("menuread"), log=print)
    elif name.startswith("vicrom"):
        if calibration is None:
            pytest.skip("vicrom judges the picture: the station needs its calibration")
        probe = VicRomProbe(board, video, calibration, programs(name), name=name, cpu=name != "vicrom", log=print)
    else:
        probe = MinimalIdReadProbe(board, screen, programs("idread", SHOW=1), log=print)
    reu = (Settings(screen, keys, log=print).special_io(REU)
           if name.startswith("reuloop") or name in ("regread", "regread-jobs") else None)
    with reu or contextlib.nullcontext():
        result = soak(probe, minutes * 60, INTERVAL_S, log=print)
    print(result.describe())
    record = {"schema": 1, **margins.context_for(board, firmware.record, image=image), **result.to_dict()}
    (artifacts / "soak.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    assert result.samples, f"{name} counted nothing in {minutes} minute(s)"
    assert result.clean, result.describe()
