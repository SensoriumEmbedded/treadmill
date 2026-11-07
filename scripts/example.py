"""An investigation script under the harness: ``treadmill exec --station <id> scripts/example.py``.

The names are the station's: ``link``, ``board``, ``screen``, ``menu``, ``keys``, ``dbg``,
``video``, ``artifacts``, ``log``. Whatever ``result`` holds at the end comes back with --json.
"""

log(f"{station.id}: {board.banner.version} on a {board.banner.machine}, {', '.join(sorted(station.caps))}")
print(link.ping())
read = screen.read()
print("\n".join(f"|{row}|" for row in read.rows()))
(artifacts / "menu.screen.txt").write_text("\n".join(read.rows()) + "\n", encoding="utf-8")
result = {"version": board.banner.version, "source": read.source, "banner_row": read.rows()[0].strip()}
