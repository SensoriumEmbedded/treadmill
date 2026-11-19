# CLAUDE.md

TReadmill is the hardware-in-the-loop validation suite for TeensyROM
firmware: Python and pytest driving real C64 and C128 machines through a
TeensyROM over USB serial, watching them through a video capture device
and, on a TR+, DMA readback. Start with [README.md](README.md).

- **Self-tests**, no hardware: `TREADMILL_FW_REPO=<firmware checkout>
  python -m pytest -q -p no:anyio selftest` (about ten minutes; the two
  offline runs of `tests/` and the manufacturing test's are most of it).
- **Driving a station** is the [`treadmill`](.claude/skills/treadmill/SKILL.md)
  skill: which command fits which ask, the `--json` and exit-code contract,
  how to read a run.
- **Adding a test or a script**: read README's "Adding a test or a script"
  first: where a check belongs, how a script is named and dated, and when a
  script becomes a test.
- **The backlog** is [BACKLOG.md](BACKLOG.md); its header has the editing
  rules. "The backlog" in conversation means its open items. Follow-ups
  from a review or a merge go there, not into the firmware project's lists.
- **Hardware.** Flashing or driving a board needs the person's go-ahead in
  the session: say which board gets which image and wait. Never write a
  port name, host, IP address or chip id into a committed file; `data/`
  holds the rig, station, calibration and glyph files and is ignored.
- **Commits.** Show the message and the staged set and wait for approval
  before committing. Never push without asking.
- **Public text.** Commit messages and docs name machines by model and
  registry id, never by pet name, and carry no private notes or local paths.
