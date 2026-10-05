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
* `actions.py` — the exception: the commands that write (clearing the queue,
  and on scratch databases a five-point scan), behind two gates. See "Actions"
  below.

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

`actions.py` is the one module here that writes. It offers two actions,
`clear_queue` and `schedule_five_point`. `clear_queue` is described last, under
"Clearing the queue"; the two gates and the message-log lines below apply to both. First,
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

Arm the five-point scan **on the laptop scratch experiment only**. The action
and its preview refuse any database that is not a scratch one (`_require_scratch`,
in the action itself, so the page and the command line behave alike); the
status reply carries `client.five_point_offered`, true only when the write
connection string names `pioneer_rundb_test`, `pioneer_rundb_scratch` or
`pioneer_rundb_actions`, and the page shows its five-point form only when that
is true. That is a decision, not an oversight:

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

The `five-point` command line refuses any database not named
`pioneer_rundb_test`, `pioneer_rundb_scratch` or `pioneer_rundb_actions`, before
it connects to anything: scheduling into the experiment's own run database is
not enabled in this version. Validation happens
before any write, so a refusal leaves the queue exactly as it was — `state.midas_run`,
`state.run_sequence` and `state.runs_in_sequence` all unchanged. What is checked:
every id exists, none is marked `do_not_use`, no two share a `config_type`, none
is a `target_position`, each one really has a row in its own `config.<type>`
table (a parent row with no settings would schedule nothing and report success),
none of the five target points is marked `do_not_use` (four points are not a
five-point scan, so it is refused rather than trimmed), at most 16
configurations, and `requested_events` a whole number between 1 and 10^10.

### Clearing the queue

`clear_queue` sets waiting runs to `CANCELLED`. Unlike five-point it is meant
for the experiment's own database. It writes straight through psycopg on the
write connection (not through `interface`), in one transaction with
`statement_timeout` 5 s and `lock_timeout` 3 s; a timeout rolls everything back.
The write connection gets `connect_timeout=5` unless its connection string sets
one.

`preview_clear_queue` (a read command, no ODB flag, but it needs the action
module for its connection, like `preview_five_point`):

| | |
|---|---|
| args | `include_holding` (JSON `true`/`false`, default `false`) |
| reply | `statuses` (`["PENDING"]`, or `["PENDING", "HOLDING"]`), `sequencer_running`, `runs` (one entry per queued run in those statuses, in queue order, at most 2000: `id`, `status`, `priority`, `midas_run_number`, `requested_events`), `will_cancel` (ids), `kept_head` (ids), `head_reason` (a sentence or `null`), `total` (the full count), `capped` (`true` if `runs` was cut at 2000) |

`clear_queue` (an action: both gates):

| | |
|---|---|
| args | `run_ids` (non-empty list of at most 2000 ids, the ones the dialog showed), `include_holding` (default `false`), `operator` (required text, trimmed, one line, at most 64 characters; the author of the annotations) |
| reply | `cancelled` (ids), `kept_head` (ids left `PENDING`), `skipped` (`[{id, status}]`, ids whose status was no longer in `statuses` when the row was locked, `status` null if the run is gone), `statuses`, `sequencer_running`, `operator` |
| errors | `usage` (bad arguments, nothing written), `denied` (a gate is closed), `db` (busy, timed out, or refused: "nothing was cancelled"), `internal` (no write connection string; or the commit itself failed, so the outcome is not known: look at the queue) |

Only the ids named are touched, and each only if it is still `PENDING` (or
`HOLDING`, if asked) when the transaction locks it (`SELECT ... FOR UPDATE`).
The protected runs are worked out once after that, and the locked rows that are
not protected are cancelled by id, so `cancelled`, `kept_head` and `skipped`
describe exactly the rows that were changed. `kept_head` lists only ids that
were sent: the page sends the preview's `will_cancel`, so a run the dialog
showed as kept is never cancelled by that OK, even if the sequencer stopped in
between. `CLAIMED`, `RUNNING` and finished runs are never in the chosen
statuses. Ids must be whole numbers; `2.7` is refused, not truncated (this now
holds for every id argument of every command). Each cancelled run gets one
`logs.run_annotations` row with the operator as author and the same note for
the whole batch: `cancelled from the RunDB page (Clear queue)` (`... the
command line ...` from the CLI), and while the sequencer was running
`; sequencer running, next run(s) <ids> left PENDING` (or just `; sequencer
running` if nothing was protected). A `CANCELLED` run makes its sequence
`FAILED` through the existing trigger.

**`sequencer_running` is server-side.** It is not an argument of either command:
`commands.parse_args` refuses it as an unknown key, so a caller cannot send it.
`rpc_server.sequencer_running` reads `/PySequencer/State/Running` from the ODB on
every call to either command and hands it to `dispatch_envelope` as
`server_args`. Only an explicit `false` means stopped; a failed read or any
other value counts as running. While running, every `PENDING` run at the lowest
priority (ties included, by `IS NOT DISTINCT FROM`, so a NULL priority works) is
kept, because the sequencer loads that run's settings while it is still
`PENDING` and cancelling it then gives the nearline daemon an invalid state
transition at run start. While running, the server also reads
`/Nearline/Info/Run DB PK` (the run `sequencer/config_loader.py` loaded) and
passes it as `loaded_run_id`, another server-side extra the caller cannot
send; that run is kept as well if it is still `PENDING`, because the priority
head can move while the sequencer waits over a loaded run. A failed read, a
missing key or `0` protects nothing extra. Stop the sequencer and clear again
to take the kept runs. Between the ODB reads and the UPDATE there is a short
window that cannot be closed from here.

`status` reads the ODB flag too (it reads no other ODB key), and reports
`client.actions_allowed` as the flag **and** a built action module, so the
page knows whether to show its write buttons.

Audit lines in the MIDAS log (`RunDBView: action clear_queue accepted: operator
'NAME' cancelled N run(s) [ids] (PENDING), kept next run [ids] (sequencer
running), skipped N`) say who, how many and which ids, cut at 40 ids with "and N
more". A refused one carries the error kind and message, and summarises its
arguments (operator, how many ids, `include_holding`) in at most 200
characters instead of echoing them.

`status` also carries `client.five_point_offered` beside `actions_allowed` and
`actions_built`; see above.

The manual path is the same two commands through the same command layer, so
`--json` prints what the page would get:

```sh
python -m pioneer.rundb.actions clear-queue --operator NAME \
    --write-dsn "host=localhost dbname=pioneer user=shifter"           # preview, exit 3
python -m pioneer.rundb.actions clear-queue --operator NAME \
    --write-dsn "..." --yes [--run-id ID ...] [--include-holding] \
    [--include-head | --keep-run-id ID] [--json]
```

Without `--yes` it previews and writes nothing. `--include-head` replaces the
ODB read (there may be no MIDAS): without it the sequencer is treated as running
and the next run is kept; give it only with the sequencer stopped. It is allowed
on any database, takes no notice of `/RunDBView/Allow actions` and writes no MIDAS
message (it has no MIDAS client); the annotations say it came from the command
line. The preview exits 3 (not 2, which argparse uses for a bad command line).
With `--yes` and `--run-id` (repeatable) it clears exactly those ids, the list
a preview showed; with `--yes` alone it clears the `will_cancel` list of a fresh
preview taken at that moment, up to 2000 per call. `--keep-run-id ID` stands in
for the `/Nearline/Info/Run DB PK` read.

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
