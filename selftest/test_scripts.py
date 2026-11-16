"""The investigation scripts under ``scripts/``, held to README's "Adding a
test or a script": each is named and dated, compiles, and imports only names
the package has, so a library rename fails here and not at the next bench
session. Scripts are records of bench sessions and need what the fake board
lacks, so none of them runs. And no committed file names a port or an
address, as CLAUDE.md asks.

Only what git tracks or has staged is checked: another session's untracked
work is not judged here."""

import ast
import importlib
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.py$")
UNDATED = {"example.py"}
# The fake board's fixtures and the data examples hold placeholder ports and
# documentation addresses on purpose.
PLACEHOLDERS = ("selftest/", "data/")
PRIVATE = re.compile(r"\bCOM\d+\b|/dev/(?:tty|cu\.)\w+|\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _tracked(*paths: str) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--", *paths], cwd=REPO, capture_output=True, check=True,
                         stdin=subprocess.DEVNULL)
    return [p for p in out.stdout.decode("utf-8").split("\0") if p and (REPO / p).is_file()]


def _scripts() -> list[Path]:
    return [REPO / p for p in _tracked("scripts") if p.endswith(".py")]


def _ours(module: str) -> bool:
    return module == "treadmill" or module.startswith("treadmill.")


def _unresolved(module: str, name: str | None) -> str | None:
    try:
        mod = importlib.import_module(module)
    except ImportError as e:
        return f"import {module}: {e}"
    if name is None or name == "*" or hasattr(mod, name):
        return None
    try:
        importlib.import_module(f"{module}.{name}")
    except ImportError:
        return f"{module} has no {name}"
    return None


def test_scripts_are_named_and_dated():
    problems = []
    for path in _scripts():
        rel = path.relative_to(REPO).as_posix()
        if path.parent != REPO / "scripts":
            problems.append(f"{rel}: scripts/ has no subfolders")
            continue
        match = DATED.match(path.name)
        if not match and path.name not in UNDATED:
            problems.append(f"{rel}: not named YYYY-MM-DD-<topic>.py, with a kebab-case topic")
            continue
        try:
            doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
        except SyntaxError:
            continue                                # the compile check names it
        if not doc:
            problems.append(f"{rel}: no module docstring")
        elif match and match.group(1) not in doc.split("\n\n", 1)[0]:
            problems.append(f"{rel}: the docstring's first paragraph does not hold {match.group(1)}")
    assert not problems, "\n".join(problems)


def test_scripts_compile_and_their_treadmill_imports_resolve():
    problems = []
    for path in _scripts():
        rel = path.relative_to(REPO).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            compile(tree, str(path), "exec")
        except SyntaxError as e:
            problems.append(f"{rel}:{e.lineno}: {e.msg}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                wanted = [(alias.name, None) for alias in node.names if _ours(alias.name)]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and _ours(node.module):
                wanted = [(node.module, alias.name) for alias in node.names]
            else:
                continue
            for module, name in wanted:
                why = _unresolved(module, name)
                if why:
                    problems.append(f"{rel}:{node.lineno}: {why}")
    assert not problems, "\n".join(problems)


def test_no_port_names_or_addresses_in_committed_files():
    hits = []
    for rel in _tracked("."):
        if rel.startswith(PLACEHOLDERS):
            continue
        data = (REPO / rel).read_bytes()
        if b"\0" in data:
            continue
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if PRIVATE.search(line):
                hits.append(f"{rel}:{number}: {line.strip()}")
    assert not hits, "a port name or an address in a committed file:\n" + "\n".join(hits)
