pytest_plugins = ["pytester"]

import pytest


@pytest.fixture(autouse=True)
def _no_warmup_carried_over(monkeypatch):
    """A run or sweep hands its warm-up to the environment for its records;
    each test starts without one, as a process does."""
    monkeypatch.delenv("TREADMILL_WARMUP", raising=False)
