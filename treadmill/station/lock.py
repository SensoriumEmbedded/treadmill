"""One lock per rig, so two runs (or a run and the flasher) never open the
same board at once.

The lock is a byte-range lock on a file under the user's home, taken with the
operating system's locking (``msvcrt`` on Windows, ``fcntl`` elsewhere), so it
is dropped by the kernel when its holder dies and a crashed run leaves
nothing stale. The file's text names the holder's process and command, so a
waiter can say who it is waiting for. A holder exports ``TREADMILL_LOCK_HELD``
so a child process it runs (a flasher, a server under test) does not wait on
its own parent.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HELD_ENV = "TREADMILL_LOCK_HELD"


class RigBusy(Exception):
    def __init__(self, rig: str, holder: str):
        super().__init__(f"rig {rig!r} is in use by {holder or 'another process'}")
        self.holder = holder


def lock_dir() -> Path:
    env = os.environ.get("TREADMILL_LOCK_DIR")
    base = Path(env) if env else Path.home() / ".treadmill" / "locks"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


class RigLock:
    """``with RigLock("rigA"):`` holds the rig; ``wait`` seconds of patience
    before ``RigBusy`` names the holder."""

    def __init__(self, rig: str, wait: float = 0.0, command: str | None = None):
        self.rig = rig
        self.wait = wait
        self.command = command or " ".join(sys.argv)
        self._fd: int | None = None
        self.nested = False

    @property
    def path(self) -> Path:
        return lock_dir() / f"{self.rig}.lock"

    def holder(self) -> str:
        """Who holds the lock, from the text after the locked first byte
        (Windows refuses a read that overlaps a locked region)."""
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except OSError:
            return ""
        try:
            os.lseek(fd, 1, os.SEEK_SET)
            return os.read(fd, 4096).decode("utf-8", "replace").strip()
        except OSError:
            return ""
        finally:
            os.close(fd)

    def acquire(self) -> "RigLock":
        held = os.environ.get(HELD_ENV, "")
        if self.rig in held.split(os.pathsep):
            self.nested = True
            return self
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self.wait
        while True:
            # The lock covers the first byte; the text after it names the holder.
            os.lseek(fd, 0, os.SEEK_SET)
            if _try_lock(fd):
                break
            if time.monotonic() >= deadline:
                holder = self.holder()
                os.close(fd)
                raise RigBusy(self.rig, holder)
            time.sleep(0.25)
        text = f"pid {os.getpid()} since {time.strftime('%Y-%m-%d %H:%M:%S')}: {self.command}\n"
        os.lseek(fd, 1, os.SEEK_SET)
        os.ftruncate(fd, 1)
        os.write(fd, text.encode("utf-8"))
        self._fd = fd
        os.environ[HELD_ENV] = os.pathsep.join(p for p in [held, self.rig] if p)
        return self

    def release(self) -> None:
        if self.nested or self._fd is None:
            return
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None
            held = [p for p in os.environ.get(HELD_ENV, "").split(os.pathsep) if p and p != self.rig]
            if held:
                os.environ[HELD_ENV] = os.pathsep.join(held)
            else:
                os.environ.pop(HELD_ENV, None)

    def __enter__(self) -> "RigLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
