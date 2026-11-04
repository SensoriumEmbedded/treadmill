"""Rig, machine and station: the hardware a test runs against.

A *rig* is PC-side equipment that stays put (capture device, power control,
keyboard board, region switch, scope). A *machine* is a row of the registry
(the validation tracker's ``systems.csv``). A *station* is one rig, one machine
and one TeensyROM board, and carries the capability set the tests select on.
"""

from treadmill.station.model import (  # noqa: F401
    CaptureSpec,
    KeysSpec,
    Machine,
    PowerSpec,
    Rig,
    Station,
    TRBoard,
    capabilities,
    tier,
)
from treadmill.station.config import (  # noqa: F401
    ConfigError,
    data_dir,
    list_stations,
    load_rig,
    load_station,
)
