"""The pytest side of TReadmill: options, markers and the station fixture.

Step 1 of the plan: enough for the self-tests and for ``pytest tests --station``
to load a station and apply the capability skips. The link, firmware, capture
and recovery fixtures arrive with their modules.
"""

from __future__ import annotations

import pytest

from treadmill.station import ConfigError, load_station
from treadmill.station.model import unknown_caps


def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("treadmill")
    g.addoption("--station", action="store", default=None,
                help="the station id (data/stations/<id>.toml) the hardware tests run against")
    g.addoption("--caps-mask", action="store", default="",
                help="comma-separated capabilities to take away, to prove a lower tier on a richer rig")
    g.addoption("--include-tolerant", action="store_true", default=False,
                help="run timing_sensitive tests on timing-tolerant machines too")
    g.addoption("--soak", action="store", type=int, default=0,
                help="run soak tests with this many iterations (0 = skip them)")
    g.addoption("--estimate", action="store_true", default=False,
                help="print the expected duration of the selected tests and exit")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "needs(*caps): station capabilities the test requires")
    config.addinivalue_line("markers", "needs_fw(*defines): firmware defines the flashed build must carry")
    config.addinivalue_line("markers", "needs_asset(*names): private assets the test needs")
    config.addinivalue_line("markers", "timing_sensitive: skipped on timing-tolerant machines")
    config.addinivalue_line("markers", "destructive: changes board settings and restores them")
    config.addinivalue_line("markers", "soak: long-running; selected only with --soak N")
    config.addinivalue_line("markers", "client_path: drives the board through TeensyROM-Web and trom")
    config.addinivalue_line("markers", "duration(minutes): the declared estimate")


@pytest.fixture(scope="session")
def station(request: pytest.FixtureRequest):
    """The station named by --station, or a skip for tests that need one."""
    station_id = request.config.getoption("--station")
    if not station_id:
        pytest.skip("no --station given")
    try:
        st = load_station(station_id)
    except ConfigError as e:
        pytest.exit(f"station {station_id!r}: {e}", returncode=2)
    mask = {c for c in request.config.getoption("--caps-mask").split(",") if c}
    if mask:
        st = st.masked(mask)
    return st


def _marker_args(item: pytest.Item, name: str) -> tuple[str, ...]:
    out: list[str] = []
    for m in item.iter_markers(name=name):
        out.extend(str(a) for a in m.args)
    return tuple(out)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply the capability, soak and tolerant-machine rules with explicit reasons.

    The station itself is loaded lazily by the fixture; here only the marker
    names are checked, so a misspelled capability fails collection instead of
    silently skipping every test that names it.
    """
    soak_n = config.getoption("--soak")
    for item in items:
        bad = unknown_caps(_marker_args(item, "needs"))
        if bad:
            raise pytest.UsageError(f"{item.nodeid}: unknown capability {', '.join(bad)}")
        if item.get_closest_marker("soak") and not soak_n:
            item.add_marker(pytest.mark.skip(reason="soak test; run with --soak N"))


@pytest.fixture(autouse=True)
def _capability_gate(request: pytest.FixtureRequest):
    """Skip a test whose station lacks a capability it needs. Runs per test so
    the station's probed capabilities (board kind, firmware defines) count."""
    item = request.node
    needs = _marker_args(item, "needs")
    needs_fw = _marker_args(item, "needs_fw")
    tolerant_only = item.get_closest_marker("timing_sensitive") is not None
    if not (needs or needs_fw or tolerant_only):
        return
    st = request.getfixturevalue("station")
    missing = [c for c in needs if c not in st.caps]
    if missing:
        pytest.skip(f"station {st.id} lacks {', '.join(missing)}")
    fw_missing = [d for d in needs_fw if f"fw:{d}" not in st.caps]
    if fw_missing:
        pytest.skip(f"firmware lacks {', '.join(fw_missing)}; run with --define or --auto-rebuild")
    if tolerant_only and st.machine.timing_tolerant \
            and not request.config.getoption("--include-tolerant"):
        pytest.skip(f"{st.machine.model} is timing-tolerant; --include-tolerant to run anyway")
