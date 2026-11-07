"""The base of every error the suite raises on purpose, and the exit codes
every command answers with.

Each error is a line that says what went wrong, written for the person or
agent reading the command's output. The CLI reports them through its
contract (``{"error": ...}`` on stderr and a non-zero exit) and lets
anything else, which is a bug, show its traceback.
"""

EXIT_OK = 0
EXIT_FAILED = 1          # the command ran and what it checked is not right: a failed test, a build that broke
EXIT_USAGE = 2           # the request itself: an option, a spec, a station file
EXIT_NO_STATION = 4      # the station could not be had: no board by that chip id, a busy rig, a dead port


class TreadmillError(Exception):
    """What the suite knows went wrong: a message, never a traceback's worth.
    ``code`` is the exit code it stands for; a subclass that means the
    request or the station sets its own."""

    code: int = EXIT_FAILED
