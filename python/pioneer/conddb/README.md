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
