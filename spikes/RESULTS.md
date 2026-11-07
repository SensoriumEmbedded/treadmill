# Phase 1 step 0: spike results

Bench PC, rig A. The board under test was a TR+ (chip id in the station
file) running `TeensyROM+ v0.9-pr85` (a review build, not main) in an NTSC
C128 at the TeensyROM menu. Dates are 2026-11-06.

## S1: the capture chain (S-Video -> HDMI converter -> MS2109)

`spikes/s1_capture.py --device 1 --backend dshow --seconds 20`

- **Device order is not stable.** Media Foundation lists 0 = laptop webcam,
  1 = `USB Video` (the MS2109), 2 = a phone virtual camera; DirectShow lists
  0 = webcam, 1 = MS2109, 2 = phone camera, 3 = NVIDIA Broadcast. A virtual
  camera shows a spinner, then black, and is easy to mistake for a dongle
  with no signal. The rig file names the index; resolving it from the device
  name is a follow-up.
- **Media Foundation does not work with this dongle here**: `VideoCapture`
  opens it, then `read()` fails with `MF_E_HW_MFT_FAILED_START_STREAMING`
  or blocks for minutes. **DirectShow works.** `CvCapture` now defaults to
  DirectShow on Windows (`api = "dshow"` in the rig file overrides).
- **The mode must be passed at open.** Properties set after opening leave
  the DirectShow backend on the device's default pixel format, which for
  1280x720 is YUY2 at 10 fps. Passed as open parameters: MJPG 1280x720 runs
  at 60 fps, MJPG 1920x1080 at 30, YUY2 720x480 at 30. `CvCapture` now
  passes them at open.
- **Another program holding the dongle (VLC with `dshow://`) makes both
  backends fail** in the same ways as above. The Windows capability consent
  store (`CapabilityAccessManager\ConsentStore\webcam`, `LastUsedTimeStop`
  = 0) names the holder; worth a check in `open_capture` before blaming the
  device.
- 20 s run after the fix: 201 frames at 10 fps (the pre-fix mode), 0 read
  errors, one size, frame-to-frame difference mean 1.35 / p95 1.40 / max
  2.02 on a static menu (chroma noise; no tearing spikes). First frame 3.2 s
  after open.
- Offline calibration from one frame (menu layout): origin (144.3, 26.1),
  cell 24.8 x 25.2 px, background (0,0,0), border (185,0,127) (this
  machine's theme has a purple border), residual 0.039.
- The MS2109 sits on a generic USB hub (`VID_214B&PID_7250`), port 4.
- PAL input was not tried (the machine on the bench is NTSC; S4 is later).

## S2: the board over USB serial

`spikes/s2_serial.py --uid <id> --port-hint COMnn`

- Found by the device serial string without opening any port; `fw_check`
  main, `VersionInfo` parsed (version with its `pr85` tag, build stamp,
  chip id, `C128 NTSC Vid 60 Hz`, `Boot: complete`, 816 MHz, 59 C).
- Ping 5x "ready!", DMA screen read 3x: the exact menu, charset
  lower/upper. SD root listing: 27 entries.
- **0 unprompted bytes** between commands (risk R4 did not show).
- Latency is dominated by the transport's idle wait, not the board:
  fw_check 301 ms, version 606 ms, ping 608 ms, read_screen 904 ms,
  list_directory 643 ms. Tuning the idle timeouts is a follow-up.
- The TR+ is in a **C128**, not the C64 the first station file named: the
  board's own machine line now also triggers the C128 reset hold-off
  (`Board.is_c128`), and the station file needs the right registry id.

## S3: DMA screen vs video decode

`spikes/s3_xcheck.py --uid <id> --chargen characters.901225-01.bin`

- With a bank bootstrapped from the character ROM (no harvest yet), the
  first run disagreed on 17.1% of cells (11.4% after refining the grid
  against the DMA read). 66 of those were **uppercase letters read as
  their lowercase code**: plain correlation picked the right template, but
  `GlyphBank._groups()` merged identical glyphs across both character sets
  and the canonical code came from the other set (upper/graphics 1-26 and
  lower/upper 65-90 are the same A-Z glyphs). Fixed: groups stay within a
  set; regression test `test_identical_glyphs_in_both_charsets_keep_their_own_codes`.
- After the fix: **5.9%** disagreement (refined grid, 3 passes), 3 glyph
  confusions (`8`/`B` and `6`/`8` in the clock, a reverse `D`), the rest
  blank cells read as thin-bar glyphs: column 0 and column 39 (the border's
  bleed into the edge columns) and the cell left of a bright `D`. These are
  spill the ROM bank's synthetic blur underestimates; the harvest exists to
  measure it. The 1% gate is judged after the harvest.
- Grid: the bounding-box calibration is within 0.7 normalized px (about
  1 frame px) horizontally and 0.3 vertically of a centroid fit over 197
  glyphs; `refine()` moved only `cell_h`. A centroid-based affine fit may
  be a better `refine` than the grid search; follow-up.
- `read_video(..., follow=True)` scored far worse (17-22%, ~200 unsure)
  than the same frame without tracking: `track()` re-derived the origin
  from the absolute edges with the *refined* cell height, which moved the
  grid 1.9 px. Fixed: a calibration keeps the edges it was made from and
  tracking moves the origin by how far they moved since. Real frames now
  read the same with or without tracking (4.7%), and a picture shifted by
  (-5, +3) px tracks to within 0.01 px.

## Harvest (the first state-changing spike)

`treadmill harvest --station <id> --fresh --color 15` (TREADMILL_CHARGEN set)

- Pushes the four sparse `allcodes` programs to the SD card, launches each,
  averages 20 frames, resets; about 90 s for 512 codes. Worked first time
  on the C128 station (the PRG runs in C64 mode; the menu comes back in
  C128 mode after the reset).
- **What the first harvests taught:**
  - The harvested templates are sharp (2-px strokes) and a sharp template
    forgives half a pixel of misalignment where the ROM-rendered, blurred
    bootstrap forgives two. **Every launch re-locks the capture chain with
    its own sub-pixel phase, and the text moves 1-2 px against the border
    between the C128-mode menu and a C64-mode program**, so one calibration
    cannot serve every picture: each harvest page is now aligned to the
    character-ROM rendering before its cells are cut, and every video read
    refines its grid on the cells it read confidently (`refine_grid`).
  - `refine()` was rewritten twice. A centroid fit is exact on the sparse
    harvest screen but biased by neighbors on dense text and by the chain's
    asymmetric blur against ROM glyphs (1.8 px off against the correlation
    optimum). Phase correlation was biased by the 16-px periodic content.
    The version that stays is a coarse-to-fine correlation search that
    warps the *rendered* screen (640x400, one channel) instead of the frame:
    about 330 ms for a full search, 160 ms for a per-read refinement; it
    recovers synthetic nudges of 3 px and scale errors of 0.6% with 0
    decode errors.
  - Harvesting the text in light blue instead of light grey changed nothing
    (same errors), so color is not the problem; the distance map is now
    luma-weighted with a tenth of the chroma energy, which halved the
    blank-cell errors on the menu.
  - The bank's running mean averaged a peak-normalized template with raw
    samples on a second harvest; it now averages normalized contributions.
- **Cross-check with the final bank, live, DMA-refined grid: 2.4%
  disagreement (24 of 1000 cells), mean confidence 0.83.** The ROM
  bootstrap scores 3.0% on the same frames. Not yet at the 1% gate at that
  point (it is now; see below). What was left: blank cells under the green highlight bar read as a top-line
  glyph (the bar's spill downward is larger than an isolated block's in the
  harvest; also true for the ROM bank), the border's bleed into columns 0
  and 39 read as thin bars, and a few look-alikes in the reverse-video
  banner. All reproducible offline from the saved frames
  (`C:/tr-build/hil/spikes/s3*/`, `C:/tr-build/hil/harvest/`), so the rest
  of the decoder work needs no bench.
- Per-launch geometry, measured: the launched picture is up and steady
  0.75 s after the launch command; frame-to-frame alternation of 0.05 px
  only; the border edge does not move between pictures while the text does.

### The 1% gate, offline (2026-11-07)

The saved frames of both pictures, replayed against their DMA reads after
three decoder changes; nothing was re-run on the bench.

- A run of solid cells (the menu's reverse banner and its highlight bar)
  spills more into the blank row beside it than one harvested block's
  margin predicts: a third of the bar's level into the top pixel row on the
  PAL picture and some into the second, where the margin predicted a fifth
  and nothing. Chroma is not the cause: the luma-only numbers are the same.
  The blank decision now also allows a margin residual up to half the
  brightest neighbor's mean level (`SPILL_FRACTION`), since a stroke of the
  cell's own is at its text level.
- The border's edge is not a glyph's: on the C128 it ramps over ten frame
  pixels into the last column (the left edge is sharp), far wider than the
  solid block's margin that stood in for the border in the composite. Each
  calibration now measures the border's bleed per edge from its own frame
  (`with_bleed`: a low quantile over the edge's cells, the reverse top bar
  taking the bottom edge's profile) and the composite subtracts that.
- A read's own grid refinement, from a stored calibration a pixel or two
  off, landed short: the cells it trusted were all on the near side. It
  now refines up to three rounds while the grid keeps moving.

| Picture | Bank | Grid refined against DMA | Stored calibration, a read's own refinement |
|---|---|---|---|
| C128 menu, four frames | C128 | 0.3-0.8% | 0.3-0.6% |
| Kawari C64 PAL menu | Kawari | 0.3% | 0.3% |
| Kawari C64 PAL menu | C128 | 0.3% | 0.3% |

Of the cells left, two or three per frame are the clock's digits, which
ticked between the frame and the DMA read that followed it; the rest are
one or two cells at the border that read at a confidence too low to
print. The gate holds on both pictures with either bank.
`treadmill score --frame --dma --charset` replays a saved frame this way.

## S5: build from a worktree

`spikes/s5_build.py --primary <firmware checkout> --variant`

- `ref=main` for the TR+: worktree `C:/tr-build/hil/wt/<sha12>` on the
  primary checkout's toolchain cache, 163 s, `TeensyROM+ v0.9` stamped with
  the commit time; the same request again is a cache hit in 1 s.
- `--define Dbg_SerTimChg` through the header-patch fallback: 161 s, the
  tree reads dirty, version `TeensyROM+ v0.9-tim*`, file
  `TeensyROM+_0.9-tim-dirty_full.hex`, memory report captured (RAM1 free
  for locals 79868 clean, 75772 with the define). Nothing flashed.

## The Kawari C64, PAL (same TR+, same evening)

The 250425 C64 with the small-board Kawari, set to PAL, after the C128.

- **S2:** machine line `C64 PAL Vid 50 Hz`, boot complete, DMA allowed;
  same board and build as before.
- **S1 under PAL:** the converter takes the 50 Hz input and the dongle still
  delivers 1280x720, at about 53 fps; the text area is 1029 x 526 px (cells
  25.7 x 21.0 px) against 993 x 631 under NTSC, so the PAL picture arrives
  vertically squashed. The bounding-box calibration finds it (residual
  0.10, a purple border again).
- **The dongle's no-signal pattern is a set of color bars**, and it shows
  them for the first part of a second after every open, sometimes longer,
  and after a reset until the converter's HDMI output comes back; a stable
  pair of frames right after opening was the bars, which calibrated to
  nonsense (origin (0.6, -29), magenta background). A frame of the bars is
  now the rig's `no_signal` pattern (`data/rigs/rigA-nosignal.png`,
  `[capture] no_signal`), and `CvCapture` drops frames that match it and
  counts them; it also discards the first 1.5 s after opening. Liveness
  should treat "all frames dropped" as NO_SIGNAL; a reset that leaves the
  picture on the bars is answered by another reset (seen once).
- **Harvest on the Kawari** (`--out`, a separate bank): four pages, grid
  refined to residual 0.02-0.03 against the ROM rendering. The templates
  correlate 0.98 with the C128 bank's (246-248 of 256 best-match their own
  code; the rest are the usual look-alikes), but the margins differ: the
  solid block spills 0.17 downward and 0.11 rightward here against 0.11
  and 0.04 on the C128 picture.
- **Cross-check on the PAL menu, DMA-refined grid:** Kawari bank 3.0% and
  4.6% on two runs (confidence 0.87-0.89); the C128 bank 5.5% on the same
  picture (confidence 0.73). One bank per capture chain is not enough:
  the picture's standard, or the machine, belongs in the bank's key, or
  the bank is harvested per station. Same leftovers as on the C128.
- S4 (chip model over DMA, mains cycle) is scripted in `spikes/s4_kawari.py`
  from the Kawari's REGISTERS.md; see below for the run.

## S4: the Kawari's chip model over DMA, then a mains cycle

`spikes/s4_kawari.py --uid <chip id> --read` / `--set 0 --persist`

- **Works.** Unlock with "VIC2" at $D03F, overlay on with persist
  ($D03F = $60, the port function bit added later made no difference),
  CHIP_MODEL at overlay $1F through $D039/$D03A/$D03B, deactivate; after
  the mains cycle the TR's machine line went from `C64 PAL Vid 50 Hz` to
  `C64 NTSC` and the picture from 25.7 x 21.0 px cells to 24.8 x 25.2.
  Written back to 1 (6569R3, PAL) at the end of the session.
- **The readback of CHIP_MODEL is the live model**, not the stored one: it
  read 1 right after 0 was written, and only the cold boot showed the
  change. So a write is verified by the boot, never by the readback.
- **The unlock misses about every other time** when the four bytes arrive
  600 ms apart over DMA (every read then returns $FF); the script verifies
  that $D03F reads back as written before touching anything else, and
  retries. A WriteC64Spans command would deliver the four bytes in one DMA
  session; worth using once the link has it.
- **The TOD line did not follow the standard:** `NTSC 50 Hz`. The Kawari
  changes the VIC's timing, the TOD clock comes from the keyboard board,
  so the PAL/NTSC x TOD matrix (test 18) is two independent knobs on this
  station, as the plan assumed.
- The register path is usable for the suite; driving the Kawari utility
  from the TR menu with DMA keystrokes remains the fallback if a firmware
  revision changes the registers.
