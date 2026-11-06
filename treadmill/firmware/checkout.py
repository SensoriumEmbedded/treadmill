"""Where a firmware build comes from.

``ref=`` builds in a detached worktree of the primary checkout under
``C:/tr-build/hil/wt/<sha12>`` (short, because the toolchain refuses long
paths on Windows); ``path=`` uses a directory as it is. Either way the commit,
``git describe --dirty``, the dirty flag and a hash of the uncommitted diff are
recorded, because the firmware's build stamp cannot tell two builds of one
commit apart. A dirty tree is refused unless the spec allows it.
"""

from __future__ import annotations

import glob
import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_WORK_ROOT = Path("C:/tr-build/hil") if os.name == "nt" else Path.home() / "tr-build" / "hil"


class CheckoutError(Exception):
    pass


@dataclass(frozen=True)
class Checkout:
    path: Path
    commit: str
    describe: str
    dirty: bool
    diff_hash: str            # sha1 of the uncommitted diff and untracked list; "" when clean
    short: str

    @property
    def tools(self) -> Path:
        return self.path / "tools"


def _git(cwd: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, check=True)
    except FileNotFoundError as e:
        raise CheckoutError("git is not on PATH") from e
    except subprocess.CalledProcessError as e:
        raise CheckoutError(f"git {' '.join(args)} in {cwd}: {e.stderr.strip()}") from e
    return out.stdout.strip()


def describe(path: Path) -> Checkout:
    """The state of a checkout as the run records it."""
    path = Path(path).resolve()
    if not (path / ".git").exists() and not _is_worktree(path):
        raise CheckoutError(f"{path} is not a git checkout")
    commit = _git(path, "rev-parse", "HEAD")
    desc = _git(path, "describe", "--always", "--dirty", "--tags")
    status = _git(path, "status", "--porcelain", "--untracked-files=all")
    dirty = bool(status.strip())
    diff_hash = ""
    if dirty:
        diff = _git(path, "diff", "HEAD")
        diff_hash = hashlib.sha1((diff + "\n" + status).encode("utf-8", "replace")).hexdigest()[:12]
    return Checkout(path=path, commit=commit, describe=desc, dirty=dirty, diff_hash=diff_hash,
                    short=commit[:12])


def _is_worktree(path: Path) -> bool:
    return (path / ".git").is_file()


def worktree_for(primary: Path, ref: str, work_root: Path = DEFAULT_WORK_ROOT) -> Path:
    """A detached worktree of ``ref`` under the work root, reused when it
    already exists at that commit."""
    primary = Path(primary).resolve()
    commit = _git(primary, "rev-parse", "--verify", f"{ref}^{{commit}}")
    target = work_root / "wt" / commit[:12]
    if target.exists():
        at = _git(target, "rev-parse", "HEAD")
        if at == commit:
            return target
        _git(primary, "worktree", "remove", "--force", str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    _git(primary, "worktree", "add", "--detach", str(target), commit)
    return target


def toolchain_env(primary: Path) -> dict[str, str]:
    """The environment a worktree build needs: the primary checkout's cached
    arduino-cli, which a worktree does not have (tools/.cache is ignored)."""
    env = dict(os.environ)
    if "ARDUINO_CLI" not in env:
        hits = sorted(glob.glob(str(Path(primary) / "tools" / ".cache" / "*" / "arduino-cli*")))
        hits = [h for h in hits if not h.endswith((".zip", ".tar.gz", ".sha256"))]
        if hits:
            env["ARDUINO_CLI"] = hits[-1]
    return env
