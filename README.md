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

## Conventions

- Every command takes `--json` and exits non-zero on failure.
- Every run writes `reports/<timestamp>/run.json` and its results to `data/results.sqlite`.
- The suite refuses to start when another program holds the station's serial port.
- DMA commands are never sent to a plain TR; debug letters are never sent to a build without their define.
