# `pioneer.conddb` — the conditions database

The **conditions database**: the campaign store for the constants offline
reconstruction reads — DRS4 cell widths, RF frequency, time alignment, energy
calibration, run-indexed corrections to the begin-of-run ODB dump. It is a
separate database from the run database in `../rundb/`, with its own schema and
its own consumers, but it is administered the same way and during the same
beamtime, which is why it lives beside it.

It is a separate database because it is meant to be served **from a different
machine**: the analysis/nearline host, not the DAQ host that serves `../rundb/`.
Offline reconstruction has to resolve the constants a run was processed with
whether or not the DAQ is up, and reprocessing outlives the beamtime the run
database describes. One server for both would put a reco job's constants behind
the availability of live DAQ state, and would tie the campaign store's lifetime
to an operational database.

The tables themselves would coexist without trouble — `schema_pg.sql` applies
to an empty schema as readily as an empty database, and `PICondPgLayer` treats
`options=-c search_path=cond` as a normal conninfo — so this is a deployment
decision, not a schema constraint. It is the deployment that has to change
first if the two are ever consolidated.

The C++ that reads it is `shared/gaudi/conditions` in the offline software
(`PICondSqliteLayer`, `PICondPgLayer`); the DDL here is that code's on-disk
contract. See `shared/gaudi/conditions/CONDITIONS_TABLES.md` for the format and
the precedence rules, and `CONDITIONS_ODB_LAYER.md` for the ODB layer.

## Schema

| file | what |
|---|---|
| `schema_pg.sql` | PostgreSQL, version 2. The campaign store. Applied to an empty database only |
| `schema_sqlite.sql` | SQLite, version 2. Same relational shape; a local convenience |
| `migrate_v1_to_v2_pg.sql`, `migrate_v1_to_v2_sqlite.sql` | migration for a database created before the constraints existed. Reports duplicate cells and overlapping intervals rather than dropping them |

Four tables — `cond_tables`, `cond_tags`, `cond_iov`, `cond_values` — plus a
`cond_schema` version row. PostgreSQL expresses the one-active-interval-per-run
rule directly as an `EXCLUDE USING gist` constraint; SQLite uses a trigger.

`cond_loader.py` applies the DDL itself when the recorded version is 0, and
refuses to write when the version is anything it does not know, so the schema
and the writer cannot drift apart.

## Tools

```bash
# load constants (append-only: nothing an existing output file points at is deleted)
python3 json2pg.py --docker testbeam-pgdb container.json
python3 json2sqlite.py out/conditions.db container.json

# inspect
python3 condtool.py --docker testbeam-pgdb tables
python3 condtool.py --docker testbeam-pgdb iov wd_energy_calibration --all
python3 condtool.py --docker testbeam-pgdb resolve wd_energy_calibration --run 193

# close an interval, or retire one, without deleting history
python3 condtool.py --docker testbeam-pgdb close wd_rf --row-id 3 --run-end 300
python3 condtool.py --docker testbeam-pgdb deactivate wd_rf --row-id 3 --comment "wrong cable map"
```

**Passwords never go on a command line or into output.** Use `PGPASSWORD` or
`~/.pgpass`; the tools print only the host and database name of a conninfo,
because the service stamps the conninfo it used into every output file.

| file | what |
|---|---|
| `cond_loader.py` | the shared loader: append-only writes, schema version gate, `psql`/`sqlite3` execution, one place so the front-ends cannot drift |
| `json2pg.py`, `json2sqlite.py` | load JSON containers into a database. `--docker NAME` runs `psql` inside a container, so the host needs no client |
| `condtool.py` | list tables, tags and intervals; resolve what a given run will read; close or deactivate an interval |
| `containers.py` | build a container in the canonical format (`channel_table`, `parameter_table`, `write_container`) |
| `merge_conditions.py` | merge several containers into one |
| `odb_apply_overrides.py` | rebuild the corrected ODB tree offline from a raw `ODBHeader` and an `odb_overrides` payload — an independent implementation of what the C++ ODB layer does, used to check it |
| `mupix_mask.py` | add an interval to the MuPix pixel mask (`mupix_pixel_mask` in `bt2026_psm_readout_map.json`) from a noisy-pixel study or a pixel list; see below |
| `mupix_timewalk.py` | fit the MuPix timewalk per chip from nearline `_hists.root` files and add the constants as an interval of `mupix_timewalk` in `bt2026_psm_readout_map.json`; see below |

## The MuPix pixel mask

`mupix_pixel_mask` is the list of hot MuPix pixels the decoder `PITMidasMusip`
drops (job option `applyPixelMask`, on by default; nearline setting
`PSM_PIXEL_MASK`). It lives in `reco_testbeam/conditions/bt2026_psm_readout_map.json`
and ships as an empty mask on [0, open) under the default tag
`bt2026-hot-pixels`. It is a `parameter_set` of parallel arrays:

| key | what |
|---|---|
| `n_pixels` | required; 0 is an empty mask and then no arrays are written (an empty JSON array has no cells in the database, so the two would hash differently) |
| `vid` | the chip's **detector id** (10011-10014, 10021-10024), not the raw chip id of the pixel word |
| `col`, `row` | 0-255 and 0-249, the 256 x 250 sensor |
| `reason` | optional, one string per pixel |

The chip is the detector id because the raw chip id is the global ASIC id the
FEB Mapping assigns, and that Mapping was reprogrammed mid-beamtime (which is
why `mupix_chip_map` has intervals); a hot pixel belongs to its sensor, so a
mask keyed by the detector id stays on it. Rows 250-255 cannot be masked: they
come only from corrupted words, which the decoder drops as out of range before
it consults the mask. A run the table does not resolve for (no interval, or
two overlapping active ones) fails the decoder's `initialize()` (the shipped
open interval means that is always a mistake), as does a mask naming an
unknown chip, a chip the raw-channel map gives to two raw ids, a pixel off the
sensor or a pixel twice.

`mupix_mask.py` fills it. It reads the study JSON of psm-analysis
`mupix-timewalk/noisy_pixels.py` (`noisy_pixels.json`, key
`recommended.pixel_mask.pixels`, or a per-run `noisy_pixels_runNNNNN.json`,
key `hot.pixels`), whose chips are raw ids and are converted through
`mupix_chip_map` at the study's run (`--map-run` overrides), or a plain JSON/CSV
list of `vid`/`chip`, `col`, `row` and optionally `reason`:

```bash
export PYTHONPATH=/workdir/beamtime2026_pie5/python:$PYTHONPATH
# dry run: what changes, the intervals after, the pixels, the diff of the file
python -m pioneer.conddb.mupix_mask add noisy_pixels.json \
  --run-start 459 --last-run 459 --split --comment "hot pixels of run 459"
# apply it; --table-out also writes the table alone, for the loaders
python -m pioneer.conddb.mupix_mask add noisy_pixels.json \
  --run-start 459 --last-run 459 --split --comment "hot pixels of run 459" \
  --write --table-out /tmp/mupix_pixel_mask.json
python -m pioneer.conddb.mupix_mask show --run 459
python -m pioneer.conddb.mupix_mask check     # the decoder's rules, over every run
python3 json2pg.py --docker testbeam-pgdb /tmp/mupix_pixel_mask.json
```

**`--write` edits a git-tracked file:** by default the container is
`reco_testbeam/conditions/bt2026_psm_readout_map.json`, so a mask is not in
place until that change is committed to reco_testbeam. On pinky the file is
in the nearline daemon's own checkout; commit there, since a dirty checkout
blocks the next `git pull` of reco_testbeam. Deploy order for the code:
`nearline_job.py` sets the decoder's `applyPixelMask` (and `smaDiagnostics`)
unconditionally and a `PITMidasMusip` built before them rejects unknown
properties, so pull and rebuild reco_testbeam before pulling
beamtime2026_pie5.

The container is `--conditions PATH` (a directory means its
`bt2026_psm_readout_map.json`), else the directory in `NL_CONDITIONS_DIR`, else
`$PIONEERSYS/reco_testbeam/conditions`, the same one the nearline job reads. It
refuses a column or row off the sensor, a pixel listed twice, a raw chip the
chip map does not have at the conversion run, a detector id that is no chip of
it, and a new interval overlapping an active interval of the same tag.
`--split` carves the new interval out instead, append-only like `condtool.py
close`: each overlapped interval is deactivated, not edited, and its parts
outside the new range come back as new rows with its payload and the comment
`Split from row N [a, b): <its original comment>` (the original once, however
often a remnant is split again). An overlapped interval that already masks
pixels is not silently unmasked: `--split` then also needs `--union` (the new
range gets its pixels plus the new ones, one row per stretch where the
overlapped masks differ) or `--replace-mask` (the new pixels alone; it prints,
per overlapped row, how many of its pixels are dropped). Whatever the change,
the tool refuses to write a table the decoder would reject: every run must
resolve to exactly one active interval of the default tag, and every mask must
pass the decoder's rules (`PIMuPixMask::ResolveMask`) against the chip map of
its runs, including that exactly one raw chip id maps to each masked detector
id; `check` runs the same test on the file as it is. `--tag NAME
--tag-description TEXT` puts a trial mask under a tag of its own (never the
default), which a job reads with the decoder's `pixelMaskTag` (nearline
`PSM_PIXEL_MASK_TAG`). `--last-run` is inclusive, `--run-end` exclusive, and
neither means open-ended. Nothing is written without `--write`.

## The MuPix timewalk constants

`mupix_timewalk` holds the per-chip walk curves `PIPSMMuPixTimewalkCorrection`
applies when it copies `/Event/muquad` to `/Event/muquad_twc` (job option
`applyTimewalkCorrection`, on by default): every pixel time of a listed chip
becomes t - W(ToT). W is the peak of dt = t(pixel) - t(S1) against the pixel
ToT, in ns, so it includes the chip's constant offset from S1 and the
corrected dt is about 0 at every ToT. It lives in
`reco_testbeam/conditions/bt2026_psm_readout_map.json` and ships empty on
[0, open) under the default tag `bt2026-timewalk`, which makes the correction a
plain copy. It is a `parameter_set` of parallel arrays:

| key | what |
|---|---|
| `n_chips` | required; 0 is an empty table and then no arrays are written |
| `vid` | the chip's **detector id** (10011-10014, 10021-10024), not the raw chip id |
| `form` | the curve, ToT in counts of 256 ns: `inverse` p0 + p1 / (ToT - p2), `power` p0 + p1 * ToT^(-p2), `exp` p0 + p1 * exp(-ToT / p2), `lin_inv` p0 + p1 / ToT + p2 * ToT |
| `p0`, `p1`, `p2` | numbers (written as floats) |
| `tot_min`, `tot_max` | integers, 0 <= tot_min <= tot_max <= 31: W is held at W(tot_min) below tot_min and at W(tot_max) above tot_max |
| `comment` | optional, one string per chip (the tool writes the fit range and chi2/ndf) |

The curve must be finite at every integer ToT in [tot_min, tot_max]:
`inverse` needs p2 < tot_min, `power` and `lin_inv` need tot_min >= 1, `exp`
needs p2 > 0. A VID listed twice, a VID the chip map does not give to exactly
one raw chip id, an unknown form, a non-finite parameter or a bad ToT range
fails the layer's `initialize()`; a mapped chip the table does not list
passes through uncorrected. `lin_inv` is the form chosen from runs 459 and
429.

`mupix_timewalk.py` fits the constants and writes them. `fit` reads the
layer's raw-time histograms `histograms/PIPSMMuPixTimewalkCorrection/twc_dt_vs_tot_raw_<vid>`
(ToT 32 bins, dt 300 bins over [-150, 450) ns) from nearline `_hists.root`
files, sums them over the files and fits each chip the way psm-analysis
`mupix-timewalk/walk_fit.py` does: a Gaussian plus a flat background per ToT
column (iminuit `ExtendedBinnedNLL`, scipy start values), then the walk form
to the column peaks (iminuit `LeastSquares`). The fit starts at ToT 4
(`--fit-tot-min`; below it crosstalk ghosts pull the peaks), runs over the
contiguous good columns and stops three columns before a cliff in the chip's
ToT spectrum (the monitor's `tot_vs_chip`, else the histogram's own ToT
projection) or at a peak step more than 6 ns out of line, since the ToT
saturates and the last columns pile up below the curve. `tot_max` is the last
good column (the curve is extrapolated beyond the fit range up to it; above it
W is held), `tot_min` the lowest ToT from 2 (`--tot-min`) at which W is
finite and at most 330 ns above W at the last fitted column,
W(tot) − W(fit_tot_max) ≤ 330 ns (`W_SANE_SPAN`). The limit is relative
because W carries each chip's constant offset from S1, which differs by tens
of ns between chips; 330 ns sits above W(2) − W(fit_tot_max) of every chip of
runs 459 and 429 (208-298 ns), so tot_min stays 2 there, and well below
W(1) − W(fit_tot_max) (430-610 ns). A tot_min raised above `--tot-min` is
printed as a WARNING and recorded in the chip's `tot_min_raised`. The
psm-analysis prototype (`walk_fit.py`) still uses an absolute limit,
W(tot_min) ≤ 250 ns; on runs 459 and 429 both give tot_min 2 on every chip,
but a chip with a large positive offset could differ. A walk fit with a
parameter at a limit of its range is printed as a WARNING by `fit` and again
by `add`. A chip with fewer than 5 columns in its range is
refused: the JSON says why, `add` leaves it out and it stays uncorrected. The
histograms are filled from the raw times, so a fit does not depend on the
constants the job applied. Ten subruns of run 459 are too few (three chips
are refused); eighteen are enough for every chip.

```bash
export PYTHONPATH=/workdir/beamtime2026_pie5/python:$PYTHONPATH
# fit: summed over the files; one PNG per chip with --plots
python -m pioneer.conddb.mupix_timewalk fit out/run00459_000{00..17}_hists.root \
  --out twc_fit_run00459.json --plots twc_plots/
# dry run: what changes, the intervals after, the constants, the diff of the file
python -m pioneer.conddb.mupix_timewalk add twc_fit_run00459.json \
  --run-start 459 --last-run 459 --split --comment "timewalk of run 459"
# apply it; --table-out also writes the table alone, for the loaders
python -m pioneer.conddb.mupix_timewalk add twc_fit_run00459.json \
  --run-start 459 --last-run 459 --split --comment "timewalk of run 459" \
  --write --table-out /tmp/mupix_timewalk.json
python -m pioneer.conddb.mupix_timewalk show --run 459
python -m pioneer.conddb.mupix_timewalk check     # the layer's rules, over every run
python3 json2pg.py --docker testbeam-pgdb /tmp/mupix_timewalk.json
```

`add` follows `mupix_mask.py`: the same container lookup (`--conditions`,
`NL_CONDITIONS_DIR`, `$PIONEERSYS/reco_testbeam/conditions`), a dry run
unless `--write`, `--last-run` inclusive and `--run-end` exclusive, a refusal
of any overlap with an active interval of the tag unless `--split`, which
deactivates the overlapped rows and adds back their parts outside the new
range with their payload and a flat `Split from row N [a, b): ...` comment.
An overlapped interval that already holds constants also needs `--replace`
(its constants are dropped in the overlap, and the tool names the chips that
become uncorrected there); there is no union, a chip has one curve.
`--skip-vid VID` leaves a chip out. `add` also takes a psm-analysis
`walk_fit.py` JSON, with `--form` picking one of its forms (`inv`/`inverse`,
`pow`/`power`, `exp`, `lin_inv`; default `lin_inv`). It refuses to write a
table the layer would reject. As for the mask, `--write` edits the
git-tracked container, which must then be committed to reco_testbeam (on
pinky in the nearline daemon's checkout). To try constants without touching
it, copy the container to a scratch directory and point `NL_CONDITIONS_DIR`
at it for both the tool and the nearline job.

**Environment.** `add`, `show` and `check` need only the standard library.
`fit` needs numpy, scipy and iminuit, uproot to read the files (or PyROOT when
uproot is missing), and matplotlib for `--plots`. On the laptop the conda env
`beamtune-psm` has all of them (`~/miniconda3/envs/beamtune-psm/bin/python`);
`psm-nearline` has uproot but not scipy or iminuit. The `testbeam-midas`
container has scipy, iminuit and matplotlib but not uproot, so there `fit`
reads through PyROOT after `source /software/root/install/bin/thisroot.sh`.
The tests are `python/tests/test_mupix_timewalk.py`; the fit tests skip where
the fit packages are missing.

## Append-only, and why

For every tag a container touches, the constants the currently active intervals
were serving are pinned onto those intervals, the intervals are deactivated, and
the container's intervals are inserted with fresh `row_id`s. So "which constants
produced this file?" stays answerable for every file ever written, which is the
whole point of the provenance the reconstruction records.

`--replace` deletes a table first. It is for a throwaway database and says so.

## Who else has a copy

Three places, each of which must be regenerated when the original here changes:

* `reco_testbeam/tests/test_conditions_{sqlite,pg}.cpp` embed the DDL verbatim
  between `// --- schema_<dialect>.sql begin ---` markers, so the C++ tests
  exercise the real DDL from a build tree that need not have this repo beside it.
* `psm-nearline-website-2026/db/conddb_schema.sql`, so the site's dev database
  and tests have a schema without importing a loader.
* `beamline-simulation/psm/psm_conditions.py` copies `containers.py`, because a
  notebook there imports it by path and a simulation checkout should not need
  this repo beside it.

```bash
python3 check_copies.py     # exit code = number of copies out of date
```

Run it after touching any `.sql` here or `containers.py`. A copy whose
repository is not checked out beside this one is reported as skipped, not as a
failure.
