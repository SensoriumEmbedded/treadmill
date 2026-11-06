"""The firmware under test: a spec (what to build), a checkout (where), a
build (cached, tagged), a flash and a verification that the board runs it."""

from treadmill.firmware.spec import FirmwareSpec, SpecError, parse_spec  # noqa: F401
from treadmill.firmware.hexinfo import HexInfo, inspect_hex  # noqa: F401
