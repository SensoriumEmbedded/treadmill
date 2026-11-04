"""TReadmill: hardware-in-the-loop validation for TeensyROM firmware.

The objects every layer shares live here or one import away:

- ``Station``: a rig, a machine and a TeensyROM board, with the capabilities
  the tests select on (``treadmill.station``).
- ``Link``: the TeensyROM remote protocol over a transport (``treadmill.link``).
- ``ScreenRead``: 1000 screen codes with attributes and a per-cell confidence,
  whichever channel produced it (``treadmill.observe``).
- ``FirmwareSpec``: what to build, tag, flash and verify (``treadmill.firmware``).
"""

__version__ = "0.0.1"

from treadmill.station import Station, load_station, list_stations  # noqa: F401
