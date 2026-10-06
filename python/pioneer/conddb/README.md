# `pioneer.conddb` — the conditions database

The **conditions database**: the campaign store for the constants offline
reconstruction reads — DRS4 cell widths, RF frequency, time alignment, energy
calibration, run-indexed corrections to the begin-of-run ODB dump. It is a
separate database from the run database in `../rundb/`, with its own schema and
its own consumers, but it is administered the same way and during the same
beamtime, which is why it lives beside it.

**Where it lives in 2026.** The database `conditions` sits on the DAQ host
pinky, in the same PostgreSQL 18 cluster as the run database `pioneer`, owned
by `cond_admin` and read as `cond_viewer`. The nearline job on pinky reads it,
piana reads it through the existing ssh forward, and the laptop keeps a mirror.
This supersedes the earlier plan of serving it from a separate analysis host so
that reconstruction would not depend on the DAQ machine being up. For this
beamtime the nearline job runs on pinky anyway, and when the database is down a
job fails and is reprocessed from the JSON snapshot
(`process.py --conditions json:<snapshot>`). It never falls back to stale
constants silently. The tables coexist with the run database without trouble:
`schema_pg.sql` applies to any empty database, so moving the campaign store to
another server later is a deployment change, not a schema change.

Every host reaches it through one libpq **service name**: `pioneer-conditions`
(read, `cond_viewer`) or `pioneer-conditions-admin` (write, `cond_admin`),
defined in `~/.pg_service.conf`, with the passwords in `~/.pgpass` only. The
tools take the service as a conninfo (`service=pioneer-conditions-admin`), and
psql resolves it and the password itself.

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

## Workflow: the database first

Constants are written to the database first. The JSON containers in
`reco_testbeam/conditions/` are exports of it: git keeps them, and a job reads
them as the fallback when the database is down. So the order is always
database, then export, then commit. It is never the other way round.

**The MuPix mask and timewalk** have their own writers, which do all of this
with `--db` (default `service=pioneer-conditions-admin`). They read the table,
apply the edit, print a dry-run diff, and with `--write` load the complete
table and refresh the snapshot. See the two sections below.

**Any other table** (`sma_coarse_shift`, `sma_rf`, `wd_rf`, `wd_channel_map`, ...) uses
the generic path. Export the table, edit the file, then load it back:

```bash
C=service=pioneer-conditions-admin
python3 condtool.py --conninfo $C export sma_coarse_shift --out /tmp/sma.json
$EDITOR /tmp/sma.json            # add or change intervals; keep the rest as it is
python3 json2pg.py $C /tmp/sma.json
python3 condtool.py --conninfo $C iov sma_coarse_shift      # check what is active now
python3 condtool.py --conninfo $C resolve sma_coarse_shift --run 430
```

**A load replaces every active interval of each tag the container names.** It
does not merge. That is why the file you load must hold the **complete** table,
which is what `condtool export` gives you. A file holding only the new interval
would retire all the others. To shorten or retire one interval without
touching the rest, use `condtool close` / `deactivate` instead.

**A stale edit is refused, not merged.** `condtool export` records the table's
fingerprint in the file's `_export` key. The fingerprint is the highest row_id
the table has used plus its number of active intervals. Every load and every
`close` takes a new row_id, and every `deactivate` changes the count. If
somebody changed the table between your export and your load, `json2pg`
refuses the file, because loading it would retire the other person's intervals.
It exits 1 and writes nothing. Export again and redo the edit on the new file.
`--force` loads anyway. `--expect-fingerprint TABLE=MAX,ACTIVE` states the
fingerprint explicitly for a file without an `_export` key. The loader stores
nothing of `_export`, and the C++ layers ignore it.

Every load, from any tool, also runs this check inside its own transaction.
On PostgreSQL it first takes `LOCK TABLE cond_iov IN SHARE ROW EXCLUSIVE MODE`;
on SQLite it uses `BEGIN IMMEDIATE`. Under that lock it re-checks the state
its statements were computed from. Two loads at the same time therefore run
one after the other, and the second fails instead of silently overwriting the
first. The `--db` writers pass the fingerprint from their own read, so the same
rule covers the window between their read and their load.

`--set-default` is needed to move a table's default tag. `--replace` deletes the
table first, so use it on dev databases only.

**Export to git** after a change. The snapshot job (**Snapshots and backups**)
writes its own copies and never touches the git checkout. Commit only when
asked to:

```bash
python3 pg2json.py service=pioneer-conditions \
    --out-dir $PIONEERSYS/reco_testbeam/conditions --check
git -C $PIONEERSYS/reco_testbeam diff --stat conditions/
```

`pg2json` writes the five `bt2026_*` containers, and `--check` reloads them into
an in-memory SQLite database and compares every tag and active interval, cell
for cell, with the database. A table in the database that none of the five
files names is not exported. Such a table always fails `--check`. Without
`--strict` it only prints a warning; with `--strict` it is an error, nothing is
written and the exit code is 3. The snapshot job runs with `--strict`. When you
add a table, add it to `pg2json.CONTAINERS`. What an export does and does not
carry over is described below (**Export**).

## Tools

```bash
C=service=pioneer-conditions-admin            # service=pioneer-conditions to read only
# load constants (append-only: nothing an existing output file points at is deleted)
python3 json2pg.py $C container.json
python3 json2sqlite.py out/conditions.db container.json

# inspect
python3 condtool.py --conninfo $C tables
python3 condtool.py --conninfo $C iov wd_energy_calibration --all
python3 condtool.py --conninfo $C resolve wd_energy_calibration --run 193

# close an interval, or retire one, without deleting history
python3 condtool.py --conninfo $C close wd_rf --row-id 3 --run-end 300
python3 condtool.py --conninfo $C deactivate wd_rf --row-id 3 --comment "wrong cable map"

# export: one table, or the five bt2026 containers
python3 condtool.py --conninfo $C export wd_rf --out wd_rf.json
python3 pg2json.py $C --out-dir DIR --check
```

`--docker NAME` (psql run inside a container) is still accepted by `json2pg` and
`condtool`, for a host without a postgres client. The laptop has psql 18 and
reaches the sidecar through the same service names
(`PGSERVICEFILE=scratch/conddb/pg_service.host.conf`).

**Passwords never go on a command line or into output.** Use `~/.pgpass` (or
`PGPASSWORD`). The tools print only the service/host/port/dbname of a conninfo,
because the service stamps the conninfo it used into every output file.

| file | what |
|---|---|
| `cond_loader.py` | the shared loader: append-only writes, schema version gate, `psql`/`sqlite3` execution, one place so the front-ends cannot drift |
| `json2pg.py`, `json2sqlite.py` | load JSON containers into a database. `--docker NAME` runs `psql` inside a container, so the host needs no client |
| `cond_export.py` | the inverse mapping: one table of a database back into container form (`export_table`), plus the active-state comparison `--check` uses |
| `pg2json.py` | export the database into the five `bt2026_*` containers (fixed table-to-file map); `--check` reloads them into SQLite and compares |
| `snapshot.py` | `pg_dump` plus the five containers into `~/bt2026/conddb-snapshots/<UTC>/`, `latest` moved, copy to the backup disk; the fallback the nearline job reads when the database is down. See **Snapshots and backups** |
| `condtool.py` | list tables, tags and intervals; resolve what a given run will read; close or deactivate an interval; `export` one table |
| `containers.py` | build a container in the canonical format (`channel_table`, `parameter_table`, `write_container`) |
| `merge_conditions.py` | merge several containers into one |
| `odb_apply_overrides.py` | rebuild the corrected ODB tree offline from a raw `ODBHeader` and an `odb_overrides` payload — an independent implementation of what the C++ ODB layer does, used to check it |
| `mupix_mask.py` | add an interval to the MuPix pixel mask (`mupix_pixel_mask`) from a noisy-pixel study or a pixel list, in the database (`--db`) or a container; see below |
| `mupix_timewalk.py` | fit the MuPix timewalk per chip from nearline `_hists.root` files and add the constants as an interval of `mupix_timewalk`, in the database (`--db`) or a container; see below |

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

`mupix_mask.py` fills it, in the database with `--db` (the normal path) or in
a container file. It reads the study JSON of psm-analysis
`mupix-timewalk/noisy_pixels.py` (`noisy_pixels.json`, key
`recommended.pixel_mask.pixels`, or a per-run `noisy_pixels_runNNNNN.json`,
key `hot.pixels`), whose chips are raw ids and are converted through
`mupix_chip_map` at the study's run (`--map-run` overrides), or a plain JSON/CSV
list of `vid`/`chip`, `col`, `row` and optionally `reason`:

```bash
export PYTHONPATH=/workdir/beamtime2026_pie5/python:$PYTHONPATH
# dry run against the database: the intervals after, the pixels, the diff of the table
python -m pioneer.conddb.mupix_mask --db add noisy_pixels.json \
  --run-start 459 --last-run 459 --split --comment "hot pixels of run 459"
# apply it: the whole table is loaded in one transaction, then the snapshot runs
python -m pioneer.conddb.mupix_mask --db add noisy_pixels.json \
  --run-start 459 --last-run 459 --split --comment "hot pixels of run 459" --write
python -m pioneer.conddb.mupix_mask --db show --run 459
python -m pioneer.conddb.mupix_mask --db check     # the decoder's rules, over every run
```

**`--db [CONNINFO]`** defaults to `service=pioneer-conditions-admin`. It reads
`mupix_pixel_mask` and `mupix_chip_map` with `cond_export.export_table` (active
intervals, renumbered 1..N), applies exactly the edit the file mode applies,
and prints the diff of the table as it will be loaded. `--write` loads the
**complete** resulting table through `cond_loader` in one transaction, because
a load replaces every active interval of the tag. It leaves out the rows the
edit deactivated, since the load retires and pins the database's own copies
of those. The write is refused if the table's intervals changed between the
read and the load. After a successful write the tool runs
`python -m pioneer.conddb.snapshot --conninfo CONNINFO` when that module exists.
Otherwise it prints a reminder to export by hand. A failed snapshot is only a
warning, because the constants are already in the database. The next nearline job
reads the new interval, with no commit or redeploy needed.

**Without `--db`**, `--write` edits a container file, **not the database the
nearline job reads**, and the tool says so after writing. By default the file is
the git-tracked `reco_testbeam/conditions/bt2026_psm_readout_map.json`. A job
reads it only when run with `--conditions json:DIR`. This is the development path, and the way
to try a mask in a scratch copy (`--conditions DIR`). `--table-out FILE`
writes the table alone, for loading into another database with `json2pg.py`.
Deploy order for the code:
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
applies when it writes the hits of `/Event/muquad`, corrected and in time order,
to `/Event/muquad_twc` (job option `applyTimewalkCorrection`, on by default):
every pixel time of a listed chip becomes t - W(ToT). W is the peak of
dt = t(pixel) - t(S1) against the pixel ToT, in ns, so it includes the chip's
constant offset from S1 and the corrected dt is about 0 at every ToT. It lives in
`reco_testbeam/conditions/bt2026_psm_readout_map.json` and ships empty on
[0, open) under the default tag `bt2026-timewalk`, which leaves every time, and
so the order, as it was. It is a `parameter_set` of parallel arrays:

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
# dry run against the database: the intervals after, the constants, the diff of the table
python -m pioneer.conddb.mupix_timewalk --db add twc_fit_run00459.json \
  --run-start 459 --last-run 459 --split --comment "timewalk of run 459"
# apply it: the whole table is loaded in one transaction, then the snapshot runs
python -m pioneer.conddb.mupix_timewalk --db add twc_fit_run00459.json \
  --run-start 459 --last-run 459 --split --comment "timewalk of run 459" --write
python -m pioneer.conddb.mupix_timewalk --db show --run 459
python -m pioneer.conddb.mupix_timewalk --db check     # the layer's rules, over every run
```

`add` follows `mupix_mask.py`: `--db [CONNINFO]` (the database, default
`service=pioneer-conditions-admin`, the whole table loaded on `--write`), or else
the same container lookup (`--conditions`, `NL_CONDITIONS_DIR`,
`$PIONEERSYS/reco_testbeam/conditions`), a dry run unless `--write`, `--last-run` inclusive and `--run-end` exclusive, a refusal
of any overlap with an active interval of the tag unless `--split`, which
deactivates the overlapped rows and adds back their parts outside the new
range with their payload and a flat `Split from row N [a, b): ...` comment.
An overlapped interval that already holds constants also needs `--replace`
(its constants are dropped in the overlap, and the tool names the chips that
become uncorrected there); there is no union, a chip has one curve.
`--skip-vid VID` leaves a chip out. `--empty` in place of the fit JSON adds an
interval without constants (n_chips 0, the correction is a copy there), for
runs with no valid constants such as DAC tuning runs. `add` also takes a psm-analysis
`walk_fit.py` JSON, with `--form` picking one of its forms (`inv`/`inverse`,
`pow`/`power`, `exp`, `lin_inv`; default `lin_inv`). It refuses to write a
table the layer would reject. As for the mask, without `--db` `--write` edits
a container file, the git-tracked one by default. To try constants without
touching the database, copy the container to a scratch directory, point
`--conditions` at it, and run a job in json mode on that directory
(`process.py --conditions json:DIR`).

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

For every tag a container touches, the tag-wide payload is first pinned onto
every interval of the tag that reads it (every interval without a payload of
its own, active or already inactive). Then the active intervals are deactivated
and the container's intervals are inserted with fresh `row_id`s. So "which
constants produced this file?" stays answerable for every file ever written,
which is the whole point of the provenance the reconstruction records. (Until
2026-09-30 only the active intervals were pinned, so an inactive interval
that a container carried itself, like the superseded `bt2026-v3` chip map,
read the new tag-wide payload after a reload.)

## Export

`pg2json.py` and `condtool export` write what a job can read, not the
database's history:

* **Active intervals only**, renumbered 1..N in load order, with the
  `values_by_iov` keys following. The retired intervals stay in the database,
  and a container carrying them would insert them again as new inactive rows on
  every reload. A tag with no active interval keeps its `tags` entry but not
  its tag-wide payload.
* Tags keep their default flag and description. A tag keeps its payload kind:
  tag-wide cells come back as `values`, per-interval cells as `values_by_iov`.
  Cells keep their types (int, real, text, bool, null), arrays keep their
  order, and open-ended intervals keep `run_end: null`. `inserted_at` is left
  out, so a reload records when it happened. Reals are read with
  `extra_float_digits = 3`, so they are exact whatever the server's setting.
  A `-0.0` is loaded as `'-0'::float8`, because a bare `-0.0` literal is
  numeric negation and would store +0. SQLite stores -0.0 as 0 whatever you do.
* Three things are not stored in the database: the **order** of tags,
  channels, columns and keys; whether a single cell was written as a scalar or
  as a one-element array; and a table's free-text top-level `description`.
  `pg2json` takes all three from the existing file in `--out-dir` (or
  `--order-from DIR`), wherever the same tag, interval, channel or key exists.
  So exporting into the git checkout changes only what the database changed.
  With no earlier file, tags come in order of their first active interval,
  channels ascending, columns and keys by name, and a single cell is an array
  only when the same key is an array elsewhere in the table, or is one of the
  writers' parallel arrays (`pg2json.LIST_KEYS`: the mask's `vid`/`col`/`row`/
  `reason`, the timewalk's `vid`/`form`/`p0`-`p2`/`tot_min`/`tot_max`/`comment`).

On the first export of the git containers of 2026-09-30, `bt2026_wavedream_*`
come back byte for byte. `bt2026_psm_readout_map.json` loses its five inactive
intervals (the superseded `bt2026-v3` chip map and `bt2026-sma` SMA map, and
three split-off `mupix_timewalk` rows), so its row ids and `values_by_iov` keys
are renumbered. `bt2026_psm_channel_map.json` and `bt2026_psm_geometry.json`
were hand-formatted (one payload row per line, aligned columns), so they come
back in the standard `json.dumps(indent=2)` layout with the same content. After
that first export, export → load → export is byte-identical.

`--replace` deletes a table first. It is for a throwaway database and says so.

## Snapshots and backups

`snapshot.py` keeps a copy of the database next to it on pinky, in two forms:
the five `bt2026_*` containers, which the nearline job reads when the database
is down, and a `pg_dump`, which the database can be rebuilt from. It runs every
hour from cron, after every `--db --write` of the two MuPix writers, and by
hand.

```
~/bt2026/conddb-snapshots/
  20260930T213000Z/            one snapshot (UTC time it was taken)
    bt2026_*.json              the five containers (pg2json --check --strict)
    conditions.dump            pg_dump -Fc --no-owner
    conditions.dump.list       its pg_restore --list, the proof it reads back
    fingerprint.json           what the database looked like (see below)
    pg2json.log                the export's output, including the check
    MANIFEST                   sha256 of every file, plus server/pg_dump/tool versions
  latest -> 20260930T213000Z   the newest; what the nearline runbook uses
  .lock                        flock: cron and a writer's hook never overlap
  cron.log                     one line per snapshot written, and every warning
```

`latest` holds the containers at its top level, so
`process.py --conditions json:~/bt2026/conddb-snapshots/latest` works as is
(nearline README, *Conditions DB down*). The job resolves the link when it
renders, so the rendered `.py` and the log name the concrete
`<UTC time>` directory it read.

**What one run does.** It fingerprints the database: per table, the highest
`row_id`, the active interval count, the interval and cell counts and the
newest `inserted_at`, plus a sha256 of the rows of each of the five `cond_*`
tables. If that equals the `fingerprint.json` of `latest`, and `latest`'s
MANIFEST still verifies, it stops: exit 0, and with `--quiet` no output at
all. Otherwise it builds a new snapshot in `.building-XXXX/`: the dump, checked
with `pg_restore --list`, then `pg2json --check --strict` taking the order of
tags, channels and keys from the previous snapshot. `--strict` makes a table
in the database that none of the five containers names an error instead of a
warning, because a snapshot without it would fail any job that needs it: the
snapshot fails, pg2json's message goes to stderr (also with `--quiet`, so it
lands in `cron.log`), and the fix is to add the table to `pg2json.CONTAINERS`.
It fingerprints the database
again, and if a write landed during the build, the build is thrown away and
redone (three tries). Then the directory is renamed to its UTC time and
`latest` is swapped in one rename, so a reader always finds a complete
snapshot. A failure at any step leaves `latest` where it was and exits 1.

**The backup copy.** When `/home/pinky/backup` is a mount point, every
snapshot that `/home/pinky/backup/conddb/` lacks is copied there (verified
against its MANIFEST), and that directory's own `latest` is moved. When the
disk is not mounted (it was found unmounted after the 09-27 reboot), the run
warns on stderr and still exits 0, because the snapshot itself is fine. The
next run that finds the disk mounted copies whatever it missed, even if the
database has not changed since. `--backup-dir`, `--backup-mount DIR`
(`''` to skip the mount check) and `--no-backup` change this.

**Elsewhere than pinky.** `PIONEER_CONDDB_SNAPSHOT_ROOT`,
`PIONEER_CONDDB_BACKUP_DIR` and `PIONEER_CONDDB_BACKUP_MOUNT` replace the three
defaults (`--root`, `--backup-dir`, `--backup-mount`). The writers' hook passes
only `--conninfo`, so on a laptop set at least the first, for example
`PIONEER_CONDDB_SNAPSHOT_ROOT=$WORKSPACE/scratch/conddb-snapshots`, before a
`--db --write` against the sidecar.

**Credentials.** The default is `service=pioneer-conditions`, the read-only
`cond_viewer`. That is enough for `pg_dump`, which needs SELECT on the five
`cond_*` tables and USAGE on `public` (the grants in the website's
`db/conddb_viewer.sql`). After a write the writers pass their own
`service=pioneer-conditions-admin`, which works too.

**Exit codes.** 0: a snapshot was written, or nothing had changed. Backup
problems are warnings only. 1: the snapshot failed (the database is
unreachable, `pg_dump` or the export failed, or another snapshot held the lock
for more than `--lock-timeout`, 300 s). 2: usage.

**Retention.** Nothing is ever deleted. One snapshot is about 0.8 MB (the
`wd_timebase` container is 0.5 MB of that, the dump 0.2 MB), and one is written
only when the database changed. Every run prints the total
(`N snapshot(s), X MB in ...`). To prune by hand, delete old `<UTC time>/`
directories, never the one `latest` points at. Directories the tool did not
make (`pre-conditions-*` from the deploy backup, `cron.log`) are left alone.

### Running it

By hand (the manual path, e.g. before reprocessing or after a
`json2pg`/`condtool` change, which do not trigger a snapshot themselves):

```bash
cd /home/pinky/bt2026/beamtime2026_pie5/python
python3 -m pioneer.conddb.snapshot            # prints each step; "unchanged since ..." if nothing to do
python3 -m pioneer.conddb.snapshot --force    # a new snapshot even if nothing changed
ls -l ~/bt2026/conddb-snapshots/latest        # where it points, i.e. when it was taken
(cd ~/bt2026/conddb-snapshots/latest && sha256sum -c MANIFEST)   # every file intact
```

The very first snapshot has no earlier one to take its order from. Take it
from the git containers, so that it matches their layout:

```bash
python3 -m pioneer.conddb.snapshot --order-from ~/bt2026/reco/repo/reco_testbeam/conditions
```

(Without that hint, the rows of `psm_geometry` come out with their keys in
alphabetical order, and the second snapshot, hinted by the first, moves the
optional keys `plane`/`rot_z_rad` to the end once. The content is the same and
so are the job's ConditionsHeader hashes. Only the bytes differ, once.)

From cron, as `pinky` (`crontab -e`, no root needed). Create the directory
first, because the shell opens `cron.log` before the tool runs:

```bash
mkdir -p ~/bt2026/conddb-snapshots
```
```
17 * * * * cd /home/pinky/bt2026/beamtime2026_pie5/python && /usr/bin/python3 -m pioneer.conddb.snapshot --quiet >> /home/pinky/bt2026/conddb-snapshots/cron.log 2>&1
```

cron runs with `HOME=/home/pinky`, so `~/.pg_service.conf` and `~/.pgpass` are
found as in a login shell, and `psql`/`pg_dump` are in `/usr/bin`. The code that
runs is whatever the checkout has checked out: pull there, not somewhere else.
Check it once after installing: an hour later `cron.log` should be unchanged
(nothing to do) or hold one `written` line, and never a `FAILED`.

To restore a dump into a scratch database. `cond_admin` is NOCREATEDB, so the
superuser creates the database; `cond_admin`, as its owner, restores into it
(`btree_gist` is a trusted extension, so the owner may create it):

```bash
sudo -u postgres createdb -O cond_admin conditions_restore
pg_restore --no-owner -h localhost -U cond_admin -d conditions_restore \
    ~/bt2026/conddb-snapshots/latest/conditions.dump
```

Tests: `python/tests/test_conddb_snapshot.py`. They run against a fake
database, plus one PostgreSQL test when `PIONEER_CONDDB_TEST_DSN` names a
scratch database (its name must contain `test` or `scratch`).

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
