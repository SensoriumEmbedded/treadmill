"""The base of every error the suite raises on purpose.

Each one is a line that says what went wrong, written for the person or
agent reading the command's output. The CLI reports them through its
contract (``{"error": ...}`` on stderr and a non-zero exit) and lets
anything else, which is a bug, show its traceback.
"""


class TreadmillError(Exception):
    """What the suite knows went wrong: a message, never a traceback's worth."""
