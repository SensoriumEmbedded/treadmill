# TReadmill

Hardware-in-the-loop validation for TeensyROM firmware: the suite drives real
Commodore 64 and 128 machines through the TeensyROM's own remote protocol,
watches them through a video capture device and (on a TR+) DMA readback, builds
and flashes the firmware under test, and runs for hours unattended across a
list of stations.

The firmware runs for hours without going anywhere, and you measure it.

## Layout

```
treadmill/            the package: station model, link, observation, firmware, sweeps, reports
  pytest_plugin.py    options, markers, fixtures
  cli.py              treadmill run | matrix | sweep | shell | exec | calibrate | harvest | viewer | bisect | export
tests/                the hardware tests, one concern per file, selected by markers and --station
selftest/             tests that need no hardware (fake board, recorded frames, config)
data/                 rigs, stations, calibrations, glyph banks, goldens, SD fixtures (private parts are git-ignored)
```

## Quick start

```
pip install -e .
treadmill stations                      # the stations data/stations/*.toml describes
pytest                                  # the self-tests, no hardware
pytest tests --station <id>             # the hardware tests against one station
```

A station is a rig (capture device and optional controls) plus a machine from
the registry plus a TeensyROM board. See `data/rigs/example.toml` and
`data/stations/example.toml`; copy them without the `example` and fill in your
own. Files that name your own boards and machines stay out of git.

## The hardware tests

`tests/` holds one concern per file: identity and reset (`smoke/`), files to
the card and back (`transfer/`), a launched program (`launch/`), the menu's
item-type marks and navigation (`menu/`), a debug build's timing knobs
(`debug/`). A test names what it needs (`@pytest.mark.needs("keys_kernal")`,
`needs_fw("Dbg_SerTimChg")`) and skips with the reason when the station or the
build lacks it; `--caps-mask dma,keys_kernal` takes capabilities away to prove
the plain-TR tier on a richer station, and `--fw-defines` names the defines a
build flashed by other means carries. Screen goldens live in `tests/golden/`
(`--golden-dir` for another set); the SD fixtures a test needs are put under
`/treadmill` on the card by the run itself. Every run leaves what each test saw
under `reports/<timestamp>/<test>/`.

## Conventions

- Every command takes `--json` and exits non-zero on failure.
- Every run writes `reports/<timestamp>/run.json` and its results to `data/results.sqlite`.
- The suite refuses to start when another program holds the station's serial port.
- DMA commands are never sent to a plain TR; debug letters are never sent to a build without their define.
