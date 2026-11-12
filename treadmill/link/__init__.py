"""The TeensyROM remote protocol over a transport.

``Link`` speaks the commands ``docs/ControlComms.md`` documents, over a
``ByteStream`` that is a USB serial port (pyserial), a TCP connection to the
board's listener, or an in-process pipe for the self-tests. The link keeps
the port open for a whole run, so the board's unprompted output (boot banners,
crash reports, handler swaps) is captured as events between commands.
"""

from treadmill.link.banner import Banner, parse_banner  # noqa: F401
from treadmill.link.dbgcmds import DebugCommands  # noqa: F401
from treadmill.link.discover import Found, NotFound, find_port, teensyrom_uid, wait_for_teensyrom  # noqa: F401
from treadmill.link.state import State, derive_state  # noqa: F401
from treadmill.link.unsolicited import Event, EventLog, classify  # noqa: F401
from treadmill.link.protocol import ProtocolError  # noqa: F401
from treadmill.link.transport import (  # noqa: F401
    ByteStream,
    TransportError,
    list_teensyrom_ports,
    open_pipe,
    open_serial,
    open_tcp,
    parse_host,
)
from treadmill.link.trlink import (  # noqa: F401
    Link,
    LinkError,
    NoReply,
    NotAllowed,
    Probe,
    Refused,
    ScreenDump,
    drive_number,
)
