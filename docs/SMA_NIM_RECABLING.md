# Recabling the SMA: from an idle DAQ to calibrated NIM copies

Use this whenever someone changes which signal goes into which SMA input: new
NIM copies, a moved TOT box, a swapped cable. The software does not guess the
layout. You tell it in the conditions, take a short run, and measure the time
offsets from that run.

Words used here:

* **TOT copy**: the TOT-box pulse of a counter. S1-S5 are detector ids 2001, 2003, 2004, 2005, 2006.
* **NIM copy**: the low-threshold logic pulse of the same counter. S1L-S5L are ids 2021, 2023, 2024, 2025, 2026.
* **Raw channel**: the SMA input number, 0-15. The conditions table `mutrig_channel_map` says which id each raw channel is.
* **Offset**: the time to subtract from an id's hits so that all copies of one particle line up. Relative to the S1 TOT copy = 0.

Paths below are relative to the `testbeam-env` workspace root. On pinky or piana
`source <beamtime2026_pie5>/software/env.sh` first (see the nearline README,
"Running it"); on the laptop work inside the `testbeam-midas` container.

You need: the DAQ idle (no run going), the elog, a pen for the cable list, and
about two hours the first time. Each step says what you should see before you
go on.

## 0. Before you start

* Note the last run number taken with the old cabling. Call it `N_old`. The new
  layout starts at the first run you take in step 2; call that run `N_new`.
* Do not edit the live conditions database yet. Everything up to step 6 works on a
  copy of the JSON files, so nothing the running nearline daemon reads changes.
* Make the working copy:

  ```bash
  mkdir -p scratch/sma-nim-pairing/recable
  cp -r main/reco_testbeam/conditions scratch/sma-nim-pairing/recable/conditions
  ```

  All edits below are to this copy. Pass it to the tools as `json:scratch/sma-nim-pairing/recable/conditions`.

## 1. Record the new layout

Read it off the cables, not from memory. For every SMA input that carries a signal write
one line: raw channel, what it is, detector id. Example (the layout of the current
development runs, raw-map row 6):

| raw channel | signal | id |
|---|---|---|
| 1 | S1 TOT | 2001 |
| 2 | S2 TOT | 2003 |
| 3 | S3 TOT | 2004 |
| 4 | S4 TOT | 2005 |
| 5 | S5 TOT | 2006 |
| 6 | RF gated by S1 | 2014 (RF marker) |
| 7 | S3 NIM copy (S3L) | 2024 |
| 8 | WaveDREAM trigger copy | 2011 |
| 9 | pi-stop in S2/S3/S4 logic | 2012 |
| 10 | S4 NIM copy (S4L) | 2025 |
| 0, 11-15 | nothing (parked) | 2002 |

Rules:

* Every raw channel that carries a signal needs a row. An unlisted raw id is a hard
  error in the decoder on its first hit, so list the parked channels too, as 2002.
* The RF goes to marker 2014. The proton current is no longer on the SMA (the
  WaveDREAM scaler input 15 has it); if one is cabled again, it goes to marker 2015.
  The decoder reads both from the map, so no job setting changes.
* NIM copies take the ids above (2021, 2023-2026), not their counter's id. That is what
  keeps the TOT and NIM words apart in the raw data.
* Write the list into the elog with the date, and the first run number you take.

You are done with this step when every cable has a line and no id is used twice
(except 2002 for parked channels).

## 2. Take a short run

A few minutes is enough for the first look. Prefer a **source run** (Sr-90 or similar,
all counters hit, no beam-rate pile-up) for the first constants; a **beam run** shows
rate effects and gives the S1 reference. Either is fine for step 4.

* Use the normal start-run procedure. Note the run number (`N_new`) and the SMA settings
  that were in force (coarse shift, link mask) in the elog.
* Until step 3 is done, the nearline daemon may stop on the new run: a raw id the map does not list is a hard error at the first hit. That is expected for the new layout; the
  raw file is still written and is what steps 4-5 read.
* Take at least two subruns if you can; the first subrun of a run is not special, but two
  files let you see if the offsets repeat.

You are done when the raw file `run0NNNN_00000.mid.lz4` exists in the inbox
(`scratch/online/` on the laptop, `/home/pioneer/inbox/` on pinky and piana).

## 3. Add raw-map row 7 at that run

In the working copy edit `scratch/sma-nim-pairing/recable/conditions/bt2026_psm_readout_map.json`,
table `mutrig_channel_map`. Two edits:

1. Close the last interval (row 6, currently open) at `N_new`. `run_end` is exclusive, so
   run `N_new` itself belongs to the new row. In `iov`, row 6 changes
   `"run_end": null` to `"run_end": N_new`.
2. Add a new interval to `iov` and a payload with the same row id to `values_by_iov`.

The shape, using row 6 as the model. `iov` entry:

```json
{
  "row_id": 6,
  "tag": "bt2026-sma-run164",
  "run_start": 882,
  "run_end": null,
  "is_active": true,
  "created_by": "jjlab",
  "comment": "Open-ended from run 882: ... 7 = S3L (2024) ... 10 = S4L (2025) ..."
}
```

becomes two entries (row 6 closed, row 7 new; use your own run number and say in the
comment what each channel is, and whether the lower boundary is confirmed):

```json
{ "row_id": 6, "tag": "bt2026-sma-run164", "run_start": 882, "run_end": N_new, "is_active": true,
  "created_by": "jjlab", "comment": "... closed at N_new by row 7." },
{ "row_id": 7, "tag": "bt2026-sma-run164", "run_start": N_new, "run_end": null, "is_active": true,
  "created_by": "<your login>", "comment": "From run N_new: <the layout of step 1 in words>. See elog <entry>." }
```

and `values_by_iov["6"]` (below) gets a sibling `values_by_iov["7"]` with one entry per
raw channel 0-15:

```json
"6": [
  {"channel_id": 0,  "vid": 2002}, {"channel_id": 1,  "vid": 2001},
  {"channel_id": 2,  "vid": 2003}, {"channel_id": 3,  "vid": 2004},
  {"channel_id": 4,  "vid": 2005}, {"channel_id": 5,  "vid": 2006},
  {"channel_id": 6,  "vid": 2014}, {"channel_id": 7,  "vid": 2024},
  {"channel_id": 8,  "vid": 2011}, {"channel_id": 9,  "vid": 2012},
  {"channel_id": 10, "vid": 2025}, {"channel_id": 11, "vid": 2002},
  {"channel_id": 12, "vid": 2002}, {"channel_id": 13, "vid": 2002},
  {"channel_id": 14, "vid": 2002}, {"channel_id": 15, "vid": 2002}
]
```

Start from a copy of the row 6 list and change only what moved. Check:

* Exactly one raw channel per marker (2014, 2015); none is fine, it means the role is absent.
* No NIM id (2021, 2023-2026) twice.
* The ids you used exist in `bt2026_psm_geometry.json` (2021 and 2023-2026 do; a new
  pseudo-id would need a geometry row first).
* Row ids are unique and `run_start` of 7 equals `run_end` of 6.

Quick syntax and overlap check:

```bash
python3 -c "import json;d=json.load(open('scratch/sma-nim-pairing/recable/conditions/bt2026_psm_readout_map.json'))['mutrig_channel_map'];print([(r['row_id'],r['run_start'],r['run_end']) for r in d['iov']])"
```

Row 7 must appear, row 6 must end where 7 starts.

## 4. Run the nearline job by hand on one subrun

This is the daemon's job on one file, with your conditions copy. It does not need the
daemon or the run database. (Details: `beamtime2026_pie5/python/pioneer/nearline/README.md`,
"Running it".)

```bash
python -m pioneer.nearline.process <inbox>/run0NNNN_00000.mid.lz4 \
    --out-dir scratch/sma-nim-pairing/recable/nearline \
    --conditions json:scratch/sma-nim-pairing/recable/conditions
```

It writes `run0NNNN_00000.py` (the job as it ran), `run0NNNN_00000.root` (the rec file,
RNTuple) and `run0NNNN_00000_hists.root` (histograms) into the output directory. Add
`--evt-max 20000` for a first quick look.

What you should see in the job log at start:

* A line that says where the RF and current channels came from ("from the raw map, role
  2014 ...") and the fine-offset roles with the raw channels you wrote. If a channel is
  wrong here, fix step 3 now; nothing downstream is trustworthy.
* No error about a raw id the map does not list.

The NIM copies are **not paired yet**: pairing needs an offset in `sma_time_alignment`,
and until then each cabled NIM copy is called uncalibrated, its words are only
histogrammed. That is what you want for step 6.

## 5. Calibrate with `smanim`

`smanim` (`psm-analysis-josh-2026/sma-nim-pairing/`, see its README "Running it")
measures the NIM-minus-TOT time difference per counter and writes the offsets as a
conditions fragment. Run it twice.

### 5a. Quick look, on the raw file

Fast, no conditions needed. Give the layout of step 1 as `channel=id` pairs:

```bash
cd psm-analysis-josh-2026/sma-nim-pairing
systemd-run --user --scope -p MemoryMax=4G -q ~/miniconda3/envs/pion314/bin/python -m smanim calibrate \
    ../../scratch/online/run0NNNN_00000.mid.lz4 \
    --channels 1=2001,2=2003,3=2004,4=2005,5=2006,6=2014,7=2024,10=2025 \
    --counters S3,S4 \
    --out ../../scratch/sma-nim-pairing/recable/calib-raw
```

* `--counters` lists which counters to analyse. In raw mode the default is S3,S4 because
  the S2 and S5 TOT times are wrong in raw words (S2 fine offset, S5 fine holds t/2);
  ask for S2 or S5 only if their TOT channel is clean in that run.
* Keep to one or two subruns per call.
* This is for looking: does the NIM copy line up at all, how wide is the peak, is the
  channel mixed up. **Do not take the constants from it for S1, S2 or S5.**

### 5b. The constants, on the decoder-output rec file

The decoder shifts each NIM channel's fine time so that it anchors on S1 (the Lag role),
and repairs S2 and S5. A raw file does not have that correction, so the constants that
the pairing layer will use come from the file the decoder wrote:

```bash
python -m smanim calibrate ../../scratch/sma-nim-pairing/recable/nearline/run0NNNN_00000.root \
    --counters S2,S3,S4,S5 \
    --out ../../scratch/sma-nim-pairing/recable/calib-rec
```

Keep the default `--tot-offsets zero`: the TOT copies stay at offset 0 in the table
(step 7), and each NIM offset is measured against its own counter's TOT copy.
`--tot-offsets s1` only reports the S2-S5 TOT-minus-S1 offsets as information; do not
copy its numbers into the table, since that would move every TOT hit downstream.

**Source runs and fine-time faults.** The decoder corrects a NIM channel's fine-time
fault per frame only against S1 coincidences. A source run with few or no S1 hits gets
no correction. If `dt_wide_<id>` (step 6) shows the NIM peak far from 0 on a source run,
the channel carries a fault: write the offset in the elog and take a beam run before
loading constants for that channel.

Outputs in `--out` (names from the README):
`sma_time_alignment_fragment.json` (the proposed table, written `is_active: false`),
`summary.json`, `counter_SX.png` per counter, `s1_view.png`, `smanim_report.pdf`.

## 6. Review the plots before you trust the numbers

Open `smanim_report.pdf`, and in the nearline job's `run0NNNN_00000_hists.root` the
directory `histograms/PIPSMSMACalibration/` (on the website: run page, section
"SMA NIM pairing"). For each counter with a NIM copy, id = its TOT id (2001, 2003-2006):

| plot | good | bad, and what it usually means |
|---|---|---|
| `dt_aligned_<id>` / report "dt zoom" | one narrow peak at 0, sigma under 1 ns, little flat background | two peaks: two particles per window or a swapped cable. Peak off 0 after alignment: the constants were not loaded. Wide peak (several ns): trigger-level or jitter problem on that channel |
| `dt_raw_<id>` | one peak within a few tens of ns of 0 (the cable delay) | no peak: NIM and TOT are not the same counter, or the offset is larger than 200 ns; look at `dt_wide` |
| `dt_wide_<id>` | one peak. At 0 means no fine-time offset on the NIM channel; away from 0 is a fine-time fault, and its position is the offset | several peaks, or a flat spread: a fault that changes within a file (the Lag role handles one value per frame; report it) |
| `dt_vs_tot_<id>` | a flat band: no walk | a slope or curve is the TOT leading-edge walk. It is measured only, nothing corrects it yet; write the size in the elog |
| `classes_<id>` | "paired" is most of the hits (above about 90 % on a clean source run); "TOT only" and "NIM only" small | a large "NIM only" is a NIM threshold that is too low (noise) or hits the TOT box misses: both are real but note them. A large "TOT only" is a dead or high-threshold NIM channel. "echo" is expected on S3 only |
| `nim_candidates_<id>` | nearly all TOT words have exactly 1 candidate | many with 2 or more: the window is too wide for the rate, or the NIM channel doubles pulses |
| `nim_width_<id>` | one narrow peak (the logic width is fixed by the discriminator) | a wide spread: the channel is not a logic pulse |

Also look at `s1_view.png` (every channel against S1): all copies of a particle should
sit near 0 relative to S1 once the offsets are in, S3L close to S3, and so on.

Stop here and sort out the cabling if:

* the pair fraction in `summary.json` is below 50 % for a counter (`smanim` lists it under
  `excluded` and will not propose an offset: the channel is not what you think it is);
* sigma is above about 2 ns;
* the offset differs by more than a few ns between two subruns of the same run.

## 7. Add `sma_time_alignment` rows

Open `calib-rec/sma_time_alignment_fragment.json` and copy the proposed offsets into
the working copy of `bt2026_psm_readout_map.json`, table `sma_time_alignment`:

1. Close the open interval (row 1) at `N_new`: `"run_end": N_new`.
2. Add `row_id: 2`, `run_start: N_new`, `run_end: null`, `is_active: true`, a comment that
   names the run, the files used and the sigma you measured.
3. Add `values_by_iov["2"]`: **all** TOT ids at 0.0 (2001, 2003-2006) plus one entry per NIM
   id you measured:

```json
"2": [
  {"channel_id": 2001, "t_offset_ns": 0.0},
  {"channel_id": 2003, "t_offset_ns": 0.0},
  {"channel_id": 2004, "t_offset_ns": 0.0},
  {"channel_id": 2005, "t_offset_ns": 0.0},
  {"channel_id": 2006, "t_offset_ns": 0.0},
  {"channel_id": 2024, "t_offset_ns": <measured, ns>},
  {"channel_id": 2025, "t_offset_ns": <measured, ns>}
]
```

Sign: the hit time becomes `t - t_offset_ns`; a NIM copy that arrives 12 ns after its
TOT copy gets `+12`. A counter with no NIM entry stays uncalibrated and unpaired; that is
the safe default for a copy you did not measure.

Re-run step 4 with the edited copy. Now each calibrated counter must show in
`classes_<id>` the pair fractions from step 6, and `dt_aligned_<id>` must have its
peak at 0. Check the finalize lines in the job log ("paired", "TOT only", "NIM only"
per counter).

## 8. Load into the conditions database (expert step; user's go-ahead)

**Back up first.** The conditions header of every produced file resolves only in this
database.

```bash
./conddb_backup.sh                                   # laptop sidecar
./conddb_backup.sh --ssh pinky --conninfo service=pioneer-conditions-admin   # pinky
```

Then, for each of the two tables (`mutrig_channel_map`, `sma_time_alignment`). Both live
in the same JSON file, but the database holds them as separate tables, and a load
replaces every active interval of the tags it names, so load the **complete** exported
table with your edit, not a fragment:

```bash
cd beamtime2026_pie5/python/pioneer/conddb
C=service=pioneer-conditions-admin
python3 condtool.py --conninfo $C export mutrig_channel_map --out /tmp/map.json
# apply the step 3 edit to /tmp/map.json (same two edits), keep the rest
python3 json2pg.py $C /tmp/map.json
python3 condtool.py --conninfo $C iov mutrig_channel_map
python3 condtool.py --conninfo $C resolve mutrig_channel_map --run <N_new>
# repeat for sma_time_alignment, then:
python3 condtool.py --conninfo $C resolve sma_time_alignment --run <N_new>
```

A load that was prepared from an older export is refused (stale fingerprint); export
again and redo the edit. The `resolve` lines must name row 7 and row 2 for run `N_new` and
the previous rows for `N_old`.

Also copy the two edits into the git copy
(`main/reco_testbeam/conditions/bt2026_psm_readout_map.json`) so the JSON and the
database stay equal: `python3 pg2json.py --check` compares them.

This is the step that changes what the live nearline daemon reads. Do not do it
during a run; do it, check the `resolve` output, and tell the shift.

## 9. Reprocess

* New runs from `N_new` on are processed with the new rows by the daemon.
* Runs already processed with the layout missing or the NIM uncalibrated (including the
  run you used for the calibration) need to be reprocessed. For one file by hand:

  ```bash
  python -m pioneer.nearline.process <inbox>/run0NNNN_00000.mid.lz4 --out-dir <the usual output dir>
  ```

  (no `--conditions`: it reads the database.) For several runs, use the nearline
  README's reprocessing section.
* Check one reprocessed run on the website: "SMA NIM pairing" section, `dt_aligned_<id>`
  peak at 0, `classes_<id>` mostly paired; and that `PIPSMSMAMonitor` and the tracker
  plots did not lose hits.
* Put in the elog: the layout (step 1), `N_new`, the offsets and their sigma, the files
  used, the date the database was changed, the backup file name.

## If something is wrong

* **Job stops on a raw id the map does not list:** a raw channel carries a signal that step 3 did
  not list. Add it (2002 if it is just parked).
* **Job stops with a message about an id on two channels, or a NIM id that is no
  counter's:** step 3 has a duplicate or a wrong id.
* **No `PIPSMSMACalibration` histograms for a counter:** its NIM copy is not in the run's
  raw map interval (the layer books them only for cabled copies).
* **Offsets look right in `smanim` but the pairing layer shows `dt_aligned` off 0:** the
  `sma_time_alignment` row does not cover the run, or the job read another conditions
  source than you edited (check the `--conditions` line in the job's `.py`).
* **To undo step 8:** `condtool.py ... deactivate <table> --row-id <id>` for each new
  row, or restore from the dump you made first.
