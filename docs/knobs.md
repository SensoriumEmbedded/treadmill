# The timing knobs and their margin tests

A TeensyROM build with `Dbg_SerTimChg` takes `t<letter><digits>` over USB
serial and answers with a listing of every knob's value; `td` puts them back
to what video detection set for the machine's standard (the firmware's
`Source/Teensy/SerUSBIO.ino`, and `MinimalBoot/Min_SerUSBIO.ino` for the
minimal image's subset). A *margin* is how far a knob moves each way from
its default before the bus path it times goes wrong; the tests under
`tests/sweep/` measure it per machine and hold it to each station's floors.

`treadmill/knobs.py` is the table this page describes, and
`selftest/test_knobs.py` checks its letters, names and digits against the
configured checkout. The defaults below are TeensyROM PR #121's, whose
waits convert to Teensy cycles once before they spin; main's
(`Source/Teensy/MinimalBoot/Common/Common_Defs.h`) convert on every pass, end
up to 12 ns late, and are up to 8 ns lower (135, 220, 210 and 365 for
`nS_RWnReady`, `nS_DataSetup`, `nS_VICStart` and `nS_VICDHold`). On a board
the listing after `td` is the authority. The firmware's
`docs/Architecture/DMA-Phi2-Timing.md` explains the DMA cycle these knobs
time, and `docs/Architecture/Constraints.md` why the PHI2 path cannot grow.

Times *from rise* count from `StartCycCnt`, latched at the Phi2-rising
interrupt; *from fall* from the Phi2-falling latch; *from re-latch* from the
second latch `DMAByte()` takes at `nS_DMASetup`.

## The knobs

| Variable (command) | Digits, unit | Images | Board | What it times | PAL | NTSC | NTSC C128 |
|---|---|---|---|---|---|---|---|
| `nS_MaxAdj` (tm) | 4, ns | main, minimal | both | ISR entry: an interrupt this long after the last is late, and the count is pulled back by the excess | 1030 | 993 | 993 |
| `nS_RWnReady` (tr) | 3, ns | main, minimal | both | from rise: read address and R/W | 143 | 143 | 143 |
| `nS_PLAprop` (tp) | 3, ns | main, minimal | both | from rise: read ROML/ROMH/IO1/IO2, then drive the byte or call the IO handler | 150 | 150 | 150 |
| `nS_DataSetup` (ts) | 3, ns | main, minimal | both | from rise: latch a C64 write to a TR-served address | 221 | 221 | 221 |
| `nS_DataHold` (th) | 3, ns | main, minimal | both | from rise: release a TR-driven read | 390 | 390 | 390 |
| `nS_VICStart` (tv) | 3, ns | main, minimal | both | from fall: sample address and ROMH in a VIC cycle (Ultimax images) | 215 | 215 | 215 |
| `nS_VICDHold` (ti) | 3, ns | main | both | from fall: release a VIC-cycle ROMH drive | 370 | 370 | 370 |
| `nS_DMAAssert` (ta) | 3, ns | main | TR+ | from fall: assert /DMA | 40 | 40 | 40 |
| `nS_DMABAWait` (tb) | 3, ns | main | TR+ | from fall: sample BA, steal the cycle or skip it | 200 | 200 | 200 |
| `nS_DMASetup` (te) | 3, ns | main | TR+ | from fall: drive R/W and address, then re-latch | 454 | 430 | 460 |
| `nS_DMADataHold` (tw) | 3, ns | main | TR+ | from re-latch: release data on a DMA write | 430 | 424 | 394 |
| `nS_DMADataSetup` (ty) | 3, ns | main | TR+ | from re-latch: latch data on a DMA read | 390 | 404 | 374 |
| `Cyc_KernProp` (tk) | 2, Teensy cycles (1.23 ns at 816 MHz) | main | TR+ | Kernal Replace's Skoe sense: wait before sampling ROMH on a KERNAL read | 35 | 35 | 35 |
| `KSenseMode` (tx) | 1, mode | main | TR+ build only | the HIRAM sense method from the next launch: 0 Skoe, 2 DMA, 9 auto (DMA on a C128); a mode, not swept, and `td` leaves it | 9 | 9 | 9 |
| `nS_KSenseSample` (tn) | 3, ns | main | TR+ build only | from re-latch: sample ROMH in the cycle stolen after a write to $00/$01 (DMA sense) | 250 | 250 | 250 |

On a plain TR the DMA and Kernal Replace knobs exist but time nothing, and a
letter a build does not know sends its digits on as commands of their own,
so `tx` and `tn` go only to a board whose listing shows them. A PAL C128
keeps the PAL set; no rig has measured one.

## How far a sweep goes

A sweep refuses a spec past these limits (exit 2). Several follow another
knob's value; the cycle is 1015 ns on PAL and 978 ns on NTSC.

| Variable | Lowest | Highest | Why |
|---|---|---|---|
| `nS_MaxAdj` | the cycle less 120 | 1200 | far below the cycle every wait collapses into contention |
| `nS_RWnReady` | 0 | `nS_PLAprop` | past it, it is `nS_PLAprop` that sets the sample point; every machine swept was clean down to 60, the floor until then |
| `nS_PLAprop` | `nS_RWnReady` | its default plus 120 | below `nS_RWnReady` the wait returns at once |
| `nS_DataSetup` | 0 | 480 | no lower edge is expected: the wait has run out by the time a handler reads the data |
| `nS_DataHold` | 250 | 520 | a Phi2 check caps the drive near fall plus 50 anyway |
| `nS_VICStart` | 60 | 310 | at 320 a TR+ on NTSC stopped answering on USB until a power cycle (BACKLOG 39) |
| `nS_VICDHold` | 250 | its default, 370 | higher breaks Ultimax cartridges on NTSC (`Common_Defs.h`); down only until BACKLOG 39 sets the cap |
| `nS_DMAAssert` | 0 | 420 | |
| `nS_DMABAWait` | 60 | 400, and below `nS_DMASetup` | |
| `nS_DMASetup` | 330 | 520, and the held sums inside the cycle | |
| `nS_DMADataHold` | 250 | the cycle less 60, less `nS_DMASetup` | the release collapses near the next fall |
| `nS_DMADataSetup` | 250, and 20 above `nS_KSenseSample` | the cycle less 20, less `nS_DMASetup` | |
| `Cyc_KernProp` | 5 | 99 | |
| `nS_KSenseSample` | 50 | 20 below `nS_DMADataSetup` | the sense address is held only until the latch |

`nS_DMASetup` is swept with the release (`nS_DMASetup + nS_DMADataHold`,
884/854/854) and the latch (`nS_DMASetup + nS_DMADataSetup`, 844/834/834)
held where the board's defaults put them, as PR #99 swept it: the two other
knobs move the other way, set in the order that never takes a sum past its
value on the way. `--free` moves it alone; `--grid` maps it against
`nS_DMADataHold` with only the latch held.

## What breaks each way, and what finds it

| Variable | Down | Up | Probes | Test |
|---|---|---|---|---|
| `nS_MaxAdj` | below the cycle every cycle is pulled back; reads fail when the shift eats a margin | late interrupts go uncorrected; shows only under USB load | idread, and idread under USB load | `test_cpu_margins.py`, `test_minimalboot_margins.py` (both without load and under it) |
| `nS_RWnReady` | address or R/W read before valid | stops at `nS_PLAprop` | idread | the same two |
| `nS_PLAprop` | selects sampled before asserted (IO first) | data leaves late (IO first) | idread; the menu at the floors | the same two |
| `nS_DataSetup` | write latched before the data is valid | latched after the fall | idread (IO2 counts the writes) | the same two |
| `nS_DataHold` | data released before the C64 latches | drive into the VIC's half (capped) | idread | the same two |
| `nS_VICStart` | ROMH sampled before the VIC's address is valid | the VIC latches before the data arrives | vicrom | `test_vic_margins.py` |
| `nS_VICDHold` | released before the VIC latches | drives into the CPU's half | vicrom | `test_vic_margins.py` |
| `nS_DMAAssert` | nothing below the code's own floor | /DMA lands in the CPU's cycle | zpattern, cpuwatch, noise | `test_dma_margins.py` |
| `nS_DMABAWait` | wrong steal decision on badline and sprite cycles | the same | zpattern, reuloop-sprites, noise | `test_dma_margins.py` |
| `nS_DMASetup` | the drive lands in the VIC's half: noise first, 30-50 ns before data errors | R/W and address late: write errors | zpattern, reuloop, noise | `test_dma_margins.py` |
| `nS_DMADataHold` | write data released too early | the release reaches the next fall: collapse | zpattern, reuloop, noise | `test_dma_margins.py` |
| `nS_DMADataSetup` | read latched before the data is valid | latched after the fall | zpattern, reuloop, noise | `test_dma_margins.py` |
| `Cyc_KernProp` | ROMH not yet reacted: the KERNAL taken for RAM | the replacement driven late | krread (Skoe sense, a C64) | `test_kernal_margins.py` |
| `nS_KSenseSample` | ROMH not yet asserted: a wrong sense | stops 20 below `nS_DMADataSetup` | krread (DMA sense, any TR+) | `test_kernal_margins.py` |

## The probes

All live in `treadmill/probes/`. A probe counts on the C64 or reads the
picture; the TeensyROM cannot see its own bus errors. While a knob that times
DMA is off its default (or `nS_MaxAdj`, which moves every cycle's start), a
probe reads its counters by DMA only with the defaults back in place, and
nothing resets the C64 (a C128 can hang on DMA right after a reset).

| Probe | Workload | Fails on | Needs |
|---|---|---|---|
| `idread` | an EasyFlash cartridge reading its ROM and its IO2 page, counted apart | wrong reads | a TR+ (counters by DMA) |
| `idread-minimal` | the same, handed to MinimalBoot with a bank-64 chip, counters on the top row | wrong reads | video, any TR |
| `menu` | reset, boot, compare the root listing with its golden | cells that differ; no boot | video or DMA; not for a DMA knob |
| `zpattern` | the firmware's `z###`: host DMA writes and reads at $C000, five patterns | bad bytes, pre-fill included; a stall is a hang | `Dbg_SerDMA`, a TR+ |
| `reuloop`, `reuloop-sprites` | `reuclient.a`: REU stash, fetch and compare, the C64's own DMA; all eight sprites on for the second | bad rounds | Special IO at the REU (set and put back), a TR+ |
| `cpuwatch` | `cpuwatch.a` checks its own writes while `z###` runs beside it | passes with a wrong byte | `Dbg_SerDMA`, a TR+ |
| `noise` | beside another probe: pixels that change by more than 48 levels between frames | three times the baseline and 100 pixels above it | video |
| `vicrom` | `vicrom.a`, an Ultimax cartridge whose glyphs the VIC fetches from ROMH | frames off the reference picture by the noise rule | video, any TR |
| `krread` | `krread.a` under Kernal Replace, the probe KERNAL its own with the filler inverted; its counts on the top row, read by video, so no host DMA reaches it while it runs | reads without the replacement, or with it over RAM | a TR+ with video; the Kernal file, Special IO and `KSenseMode` set and put back |

`--usb-load` (and the tests' loaded cases) runs version requests through
idread's dwell, so the board's interrupts arrive late.

## A margin run

```
treadmill run --station <id> --fw ref=main --define Dbg_SerTimChg --define Dbg_SerDMA --tests tests/sweep
```

about 90 minutes a TR+ station, less on a plain TR, which skips the DMA and
Kernal Replace tests. Each sweep leaves a `margins.json` in its test's
folder; `treadmill margins table` lists them, `treadmill margins map` charts
them (an overview per machine with every knob's window around its default,
red where it failed and green at a limit, and the same windows on a time line
from the Phi2 rise, a section per firmware routine; then, collapsed, a row per
record, its clean
window and every value probed beneath it; `--mermaid` for a PR), and
`treadmill margins suggest --station <id>`
prints the `[margins]` floors to copy into the station file. A test fails an
edge short of its floor and warns when an edge shrank against the newest
earlier record of the same station, knob, edge and probes on another build.
Sweeps under USB load are recorded but held to no floor.
