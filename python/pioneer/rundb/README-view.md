# Reading the run database: `view`, `commands`, `rpc_server`

The run database is the record of runs past, present and future. These modules
are the read side of it, and they only read: the session they open refuses
writes, times every statement out and takes no locks, so a page refreshing every
few seconds cannot disturb data taking.

* `pg.py` — connections that can only read.
* `view.py` — the six views (`status`, `runlog`, `queue`, `run`, `sequences`,
  `config`) and the command line.
* `commands.py` — argument checking and the JSON envelope; imports neither
  psycopg nor MIDAS.
* `rpc_server.py` — the MIDAS client `RunDBView`, which answers the custom page
  over jrpc and does nothing else.
* `actions.py` — the exception: the one command that writes, behind two gates.
  See "Actions" below.

Status names are passed through exactly as the database stores them —
`PENDING`, `HOLDING`, `RUNSDONE` and the rest — in every row, in `queue`'s
counts and in the command line's tables. `status` also returns the whole of
`utils.status` (name, description, the five flags) so a reader can say what a
name means without this client renaming anything. A sequence carries `counts`,
its member runs tallied under those same names (`{"DONE": 4, "RUNNING": 1,
"PENDING": 4}`), beside `n_runs`, `first_run` and `last_run`.

## Running it

The command line is the manual path and needs no MIDAS at all. With
`beamtime2026_pie5/python` on `$PYTHONPATH`:

```sh
export PIONEER_RUNDB_DSN="host=localhost dbname=pioneer user=readonly password=readonly"
python -m pioneer.rundb.view status
python -m pioneer.rundb.view runlog --limit 20 [--before-id ID]  # ID: older page
python -m pioneer.rundb.view queue
python -m pioneer.rundb.view run ID
python -m pioneer.rundb.view sequences
python -m pioneer.rundb.view config ID
```

`--dsn` overrides `$PIONEER_RUNDB_DSN`, `--timeout-ms` the four-second statement
timeout. `--json` prints the reply envelope the custom page receives, byte for
byte, which is how to tell a page problem from a data problem. Exit status is 0
when the envelope says `ok`; `status` still answers with the database down.

The MIDAS client is started with the same connection string:

```sh
python -m pioneer.rundb.rpc_server --experiment bt2026 --client RunDBView \
    --dsn "host=localhost dbname=pioneer user=readonly password=readonly"
```

It seeds `/RunDBView` on connect if the keys are missing and never overwrites
them; credentials come from the command line only, never from the ODB.

## Actions

`actions.py` is the one module here that writes. It offers a single action,
`schedule_five_point`: five runs at the five target positions of
`config.target_position` with `seq_id = 2`, grouped into one sequence, carrying
whatever other settings were chosen (one configuration per device; a
`target_position` is refused, because the scan is the positions).

Two gates stand in front of it, and either one alone changes nothing:

1. the client has to have been started with `--allow-actions --write-dsn …`, so
   a client started without them has no code path that writes at all;
2. `/RunDBView/Allow actions` has to be true. It is read again on every single
   call, so unticking it takes effect at once, without restarting anything.

Both are checked in `commands.py` on every call; a call that fails either comes
back as `{"ok": false, "error": {"kind": "denied"}}` and touches nothing. Every
attempt leaves a line in the MIDAS message log — accepted (naming the sequence
and the runs it created), refused by the action, or refused by a gate, the last
one naming which gate — so a shifter who presses the button and sees nothing
happen leaves a trace somebody can look up.

`preview_five_point` takes the same arguments, runs every check and writes
nothing, so it is a **read** command: it is not behind the ODB flag, and the
page can show what a button would do on a client where the button itself is
refused. It does need the action module, because the database it reads is the
one the client was told to write; without it the reply is `denied` with a hint
saying so. It is not offered by `python -m pioneer.rundb.view`, which has no
connection to the write database: the manual preview is
`python -m pioneer.rundb.actions five-point …` without `--confirm`.

Scheduling is not atomic. `midas_run_sequence.schedule` commits each run on its
own and registers the sequence last, so a failure part way through leaves runs
in the queue. The action takes the highest run and sequence ids before it starts
and, if the write fails, the error carries
`created_anyway: {run_ids: [...], sequence_ids: [...]}` and a hint to check the
queue and cancel them. The reply never says "nothing happened" over a queue that
has just grown.

Arm it **on the laptop scratch experiment only**. It has never been run against
the experiment's own database, and that is a decision, not an oversight:

```sh
python -m pioneer.rundb.rpc_server --experiment rundb --client RunDBView \
    --dsn "host=testbeam-pgdb dbname=pioneer_rundb_test user=postgres password=..." \
    --allow-actions \
    --write-dsn "host=testbeam-pgdb dbname=pioneer_rundb_test user=postgres password=..."
```

The manual path is the same code without MIDAS, and prints what it would create
before it creates anything:

```sh
python -m pioneer.rundb.actions five-point --config-id 42 [--config-id 57] \
    --events 2000000 --write-dsn "…"            # dry run, writes nothing, exit 2
python -m pioneer.rundb.actions five-point --config-id 42 --events 2000000 \
    --write-dsn "…" --confirm                   # creates the runs
```

The command line refuses any database not named `pioneer_rundb_test`,
`pioneer_rundb_scratch` or `pioneer_rundb_actions`, before it connects to
anything: scheduling into the
experiment's own run database is not enabled in this version. Validation happens
before any write, so a refusal leaves the queue exactly as it was — `state.midas_run`,
`state.run_sequence` and `state.runs_in_sequence` all unchanged. What is checked:
every id exists, none is marked `do_not_use`, no two share a `config_type`, none
is a `target_position`, each one really has a row in its own `config.<type>`
table (a parent row with no settings would schedule nothing and report success),
none of the five target points is marked `do_not_use` (four points are not a
five-point scan, so it is refused rather than trimmed), at most 16
configurations, and `requested_events` a whole number between 1 and 10^10.

## Tests

The tests need a scratch PostgreSQL. They skip unless `$PIONEER_RUNDB_TEST_DSN`
is set and refuse any database not called `pioneer_rundb_test`, so they cannot
be pointed at the experiment's own. It is dropped and rebuilt from
`db_config.sql` once per session: the seed rows in that file are not idempotent.

```sh
cd beamtime2026_pie5/python
PIONEER_RUNDB_TEST_DSN="host=testbeam-pgdb dbname=pioneer_rundb_test user=postgres password=..." \
PYTHONPATH=$PWD python -m pytest tests -q
```

`tests/rundb_seed.py` builds the test data through the same helpers the sequencer
and the nearline daemon use, and runs on its own too:
`python tests/rundb_seed.py "<dsn>"`.

The action tests schedule runs, and the read-side tests assert on the queue down
to the number of waiting runs, so `test_actions_gating.py` builds a database of
its own on the same server — `pioneer_rundb_actions`, dropped and rebuilt on
every run. It is deliberately not `pioneer_rundb_scratch`: that one belongs to
the standalone experiment under `scratch/rundb-page-standalone/`, and rebuilding
it would wipe what its page is showing.
