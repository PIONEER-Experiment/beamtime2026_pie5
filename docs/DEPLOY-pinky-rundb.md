# Deploying the RunDB custom page into the pinky MIDAS experiment

Target: the running `bt2026` experiment on **pinky**
(`/home/pinky/bt2026/beamtime2026_pie5`). This adds a read-only custom page
(`RunDB`, side menu) plus a small MIDAS client (`RunDBView`) that answers it
over `jrpc`. Nothing existing changes: no equipment, no frontend rebuild, no
run transition is touched. Steps 0-5 install it read-only: the client only
reads Postgres, starts no runs and writes nothing to the run database. Step 6
is a separate, later step that arms one write, **Clear queue**, and has its own
check and its own way back.

**Every step below is done by a person at the keyboard, in order, watching the
output before moving to the next one.** This is not something to script or
run unattended.

## 0. Before you start (5 min, pinky)

| Check | Why | Command / expectation |
|---|---|---|
| Branch | the page and client are on `feature/rundb-custom-page` | `git -C /home/pinky/bt2026/beamtime2026_pie5 log --oneline develop..feature/rundb-custom-page` |
| psycopg already installed | `pioneer.rundb.pg` and `pioneer.rundb.interface` both `import psycopg`; the nearline daemon already imports `pioneer.rundb.interface` (`python/pioneer/nearline/daemon.py`) and has been running on pinky, so the same Python already has it | `python3 -c "import psycopg; print(psycopg.__version__)"` |
| `pioneer.rundb` importable | needs `beamtime2026_pie5/python` on `PYTHONPATH` | `PYTHONPATH=/home/pinky/bt2026/beamtime2026_pie5/python python3 -c "import pioneer.rundb.view"` |
| `midas` importable | needs the MIDAS python package on `PYTHONPATH`; the existing `/Programs/NearlineDaemon/Start command` (`/usr/bin/python /home/pinky/bt2026/beamtime2026_pie5/python/pioneer/nearline/daemon.py --midas-client NearlineDaemon --midas-host localhost --midas-expt bt2026`) runs the same account, imports the same `midas` package, and carries **no** explicit `PYTHONPATH=` in its Start command — so both paths are already on the environment mhttpd starts programs with. Confirm this holds for a plain login shell before relying on it below | `python3 -c "import midas; print(midas.__file__)"` |
| Event ID / equipment name | not applicable — `RunDBView` is a plain MIDAS client, no equipment | — |
| Postgres reachable | the client reads `host=localhost dbname=pioneer` as role `readonly` | `psql -U readonly -d pioneer -h localhost -c 'select 1'` |
| Client name free | `/Programs` must not already have a `RunDBView` entry | `odbedit -e bt2026 -c 'ls /Programs'` |

If the `midas` or `pioneer.rundb` import above fails, the Start command in
step 3 needs an explicit `env PYTHONPATH=...` prefix (see the note there)
instead of relying on the ambient environment.

## 1. Code (pinky account)

```bash
cd /home/pinky/bt2026/beamtime2026_pie5
git fetch origin
git checkout feature/rundb-custom-page      # or develop once merged
```

There is nothing to build: `pioneer.rundb.rpc_server` is a plain Python
module, and the page is static HTML/CSS/JS served straight out of `custom/`.
Do **not** restart anything yet.

## 2. ODB preparation (`odbedit -e bt2026`)

### 2a. The page itself

Four keys, all **absolute paths**, exactly as they exist on pinky (this
sequence is byte-identical to the one already written up in
`custom/README.md`'s "ODB registration" section):

```bash
odbedit -e bt2026 -c 'create STRING /Custom/RunDB'
odbedit -e bt2026 -c 'set /Custom/RunDB /home/pinky/bt2026/beamtime2026_pie5/custom/rundb.html'
odbedit -e bt2026 -c 'create STRING "/Custom/rundb.js!"'
odbedit -e bt2026 -c 'set "/Custom/rundb.js!" /home/pinky/bt2026/beamtime2026_pie5/custom/js/rundb.js'
odbedit -e bt2026 -c 'create STRING "/Custom/rundb-rpc.js!"'
odbedit -e bt2026 -c 'set "/Custom/rundb-rpc.js!" /home/pinky/bt2026/beamtime2026_pie5/custom/js/rundb-rpc.js'
odbedit -e bt2026 -c 'create STRING "/Custom/rundb.css!"'
odbedit -e bt2026 -c 'set "/Custom/rundb.css!" /home/pinky/bt2026/beamtime2026_pie5/custom/css/rundb.css'
```

The trailing `!` on the three asset keys is what keeps them out of the side
menu (only `/Custom/RunDB` becomes a menu entry, **RunDB**, at
`?cmd=custom&page=RunDB`); the page key has no dot, which routes it through
`show_custom_page()` instead of mhttpd's asset cache. These paths are
absolute, so a rewrite of `/Custom/Path` by another frontend (musip does this
— see `custom/README.md`) cannot orphan this page.

### 2b. The client program

```bash
odbedit -e bt2026 -c 'create INT /Programs/RunDBView/Required'
odbedit -e bt2026 -c 'set /Programs/RunDBView/Required n'
odbedit -e bt2026 -c 'create STRING /Programs/RunDBView/"Start command"'
odbedit -e bt2026 -c 'set /Programs/RunDBView/"Start command" "/usr/bin/python -m pioneer.rundb.rpc_server --experiment bt2026 --client RunDBView --dsn \"host=localhost dbname=pioneer user=readonly password=readonly\""'
```

`Required = n`: unlike `NearlineDaemon` and `SlowControl`, `RunDBView` only
serves a viewer page. If it is down the page says so in words and keeps
showing the ODB strip; there is no reason for MIDAS to flag the whole run as
not-OK over it.

Do **not** add `--allow-actions` or `--write-dsn` to this Start command in
this step. The client is read-only by construction when they are absent
(`pg.py` never imports the write path at all). They are added in step 6, and
only then.

If the import check in step 0 failed, use this Start command instead, which
sets `PYTHONPATH` explicitly rather than relying on the ambient environment:

```
env PYTHONPATH=/home/pinky/bt2026/beamtime2026_pie5/python:/home/pinky/packages/midas/python /usr/bin/python -m pioneer.rundb.rpc_server --experiment bt2026 --client RunDBView --dsn "host=localhost dbname=pioneer user=readonly password=readonly"
```

`/RunDBView` (the client's own configuration subtree, separate from
`/Programs/RunDBView`) is **not** created here — the client seeds it itself
on first connect, key by key, never overwriting anything already there
(`rpc_server.py:seed`). It ends up holding:

| key | seeded default |
|---|---|
| `Allow actions` | `false` |
| `Poll seconds` | `5.0` |
| `Runlog rows` | `50` |
| `Runlog refresh seconds` | `30.0` |
| `Max reply kB` | `256` |
| `Stale seconds` | `20.0` |
| `Client name` | `RunDBView` |
| `Database` | written on every connect, e.g. `readonly@localhost:5432/pioneer` (no password) |

## 3. First start

On the mhttpd **Programs** page, start `RunDBView`. Watch **Messages**:

```
RunDBView: connected to readonly@localhost:5432/pioneer, actions disabled
```

The client also prints `RunDBView: reading readonly@localhost:5432/pioneer,
actions not built` when it starts, but that line goes to the client's own
standard output (wherever the Programs entry sends it), not to Messages.

If `/RunDBView` did not exist yet there is also a line naming how many keys
it seeded. No output arrives from Postgres or the ODB beyond that — the
client answers `jrpc` only when the page asks it something.

## 4. Verify (10 min)

1. **CLI first, before touching the page** — this isolates a page problem
   from a data problem:
   ```bash
   PYTHONPATH=/home/pinky/bt2026/beamtime2026_pie5/python \
     PIONEER_RUNDB_DSN="host=localhost dbname=pioneer user=readonly password=readonly" \
     python3 -m pioneer.rundb.view status --json
   PYTHONPATH=/home/pinky/bt2026/beamtime2026_pie5/python \
     PIONEER_RUNDB_DSN="host=localhost dbname=pioneer user=readonly password=readonly" \
     python3 -m pioneer.rundb.view queue --json
   ```
   Both should print `"ok":true` envelopes with data in them.
2. **The page** — open the web interface, pick **RunDB** from the side menu.
   The live strip, Queue, Runlog and Sequences sections all render. The strip
   should read "Run database: reachable".
3. **Queue matches Postgres directly** — compare the page's Queue section
   against:
   ```bash
   psql -U readonly -d pioneer -h localhost \
     -c "select id,priority,status from state.midas_run where status='PENDING' order by priority"
   ```
   Same rows, same order (lowest priority first).
4. **Stop/start recovery** — stop `RunDBView` from the Programs page. Within
   `Stale seconds` (20 s by default) the page's tables should dim and the
   strip should read "Run database: client not answering", still showing the
   last good data with its read time. Start `RunDBView` again: the page
   recovers on its own, with no reload.
5. **No side effect on the database** — before and after the whole check:
   ```bash
   psql -U readonly -d pioneer -h localhost -c "select count(*) from state.midas_run"
   ```
   The count must be identical; this client never writes.
6. **No new alarms** — check the MIDAS Alarms page before and after; starting
   and stopping `RunDBView` should raise none.

## 5. Rollback

```bash
odbedit -e bt2026 -c 'stop RunDBView'     # or use the Programs page
odbedit -e bt2026 -c 'rm /Custom/RunDB'
odbedit -e bt2026 -c 'rm "/Custom/rundb.js!"'
odbedit -e bt2026 -c 'rm "/Custom/rundb-rpc.js!"'
odbedit -e bt2026 -c 'rm "/Custom/rundb.css!"'
odbedit -e bt2026 -c 'rm /Programs/RunDBView'
```

`/RunDBView` (the client's own settings subtree) can be left in place — it is
inert without the client and without the page, and a later redeploy picks it
straight back up. Nothing on the Postgres side needs undoing after steps 0-5:
that client has never issued a write. If step 6 was done, disarm first (6h);
runs it cancelled stay `CANCELLED`, and there is no bulk un-cancel, so
re-schedule them from ConfigDB if they are wanted.

## 6. Arming Clear queue (separate step, after 0-5 have been verified)

This lets the RunDB page set queued runs to `CANCELLED` (see
`docs/USING-THE-RUNDB-PAGE.md`, "Clearing the queue"). It is the only write
the client offers on this database. Do it only when the user has said to arm
it, with a person at the keyboard, one step at a time, like everything above.

What it writes: `state.midas_run.status` of queued runs, and one row per run
in `logs.run_annotations`. The `shifter` role already has UPDATE on
`state.midas_run` and INSERT on `logs.run_annotations` (read-only check on
2026-10-05); confirm that again before relying on it:

```bash
psql -U readonly -d pioneer -h localhost -c \
  "select has_table_privilege('shifter','state.midas_run','UPDATE'), has_table_privilege('shifter','logs.run_annotations','INSERT')"
```

The five-point scheduling action is **not** armed by this: the client refuses it
on any database that is not a scratch one, and the page hides that form. So
only Clear queue becomes live.

### 6a. Back up first

Take a dump of the run database and copy it off pinky, and check that the copy
is readable, before anything below. Use the usual verified-backup procedure
(dump, globals and configs; see the backup notes for the pinky database). This
document does not repeat the commands, because I have not checked the current
backup script and path here; use the one you trust.

### 6b. Code

```bash
cd /home/pinky/bt2026/beamtime2026_pie5
git pull           # on the branch step 1 checked out; confirm the new commit is in
```

The page assets carry `?v=3`; reload the page in the browser (Ctrl+Shift+R)
if it still shows the old one, since mhttpd serves assets with a 24 h expiry.

### 6c. The write password: `~/.pgpass`, typed by the operator

The write connection must not carry its password in the Start command: the
Start command lives in the ODB, and anyone who can open the mhttpd Programs or
ODB pages can read it. Put the password where only the pinky account can
read it instead, in `~/.pgpass` of the account that runs `RunDBView` (the same
account that starts it from the Programs page; confirm which that is). libpq
reads the file by itself when the connection string has no password.

The operator types the password; do not paste it into a shell history, a
chat or a log. For example, in an editor:

```
localhost:5432:pioneer:shifter:<password>
```

then

```bash
chmod 600 ~/.pgpass
ls -l ~/.pgpass                    # -rw------- and owned by the pinky account
psql "host=localhost dbname=pioneer user=shifter" -c 'select 1'    # no password prompt
```

If `psql` still prompts, the line does not match (host, port, database or
user) or the mode is wrong; libpq ignores a `.pgpass` that others can read.

### 6d. The Start command

Add the two flags. The password is not in it; the existing read-only `--dsn`
stays as it is:

```bash
odbedit -e bt2026 -c 'set /Programs/RunDBView/"Start command" "/usr/bin/python -m pioneer.rundb.rpc_server --experiment bt2026 --client RunDBView --dsn \"host=localhost dbname=pioneer user=readonly password=readonly\" --allow-actions --write-dsn \"host=localhost dbname=pioneer user=shifter\""'
```

(If step 0 made you use the `env PYTHONPATH=...` form, keep that prefix and add
the same two flags at the end.) Starting a client with `--allow-actions` and no
`--write-dsn` refuses to start.

### 6e. The ODB flag, then restart

```bash
odbedit -e bt2026 -c 'set /RunDBView/"Allow actions" y'
```

Then on the **Programs** page stop and start `RunDBView`. Messages now reads

```
RunDBView: connected to readonly@localhost:5432/pioneer, actions enabled
```

(the client's standard output says `... actions built`; that line does not
reach Messages).

and the RunDB page shows a yellow "This client is armed for actions" line and,
when the queue has `PENDING` or `HOLDING` runs, a **Clear queue...** button in
the Queue heading. The Actions panel says the five-point scan is offered on
scratch databases only.

### 6f. Live check, sequencer stopped

1. On the **Sequencer** page the sequencer is not running (check the strip).
2. On **ConfigDB** schedule two runs of 1 event each (dummy configurations the
   user names; do not use anything that would move the target or degrader
   unless that is what was agreed).
3. On RunDB, press **Clear queue...**, type your name, check the list is
   exactly your two runs (and nothing else, or that anything else listed is
   something you really mean to cancel) and press **Cancel 2 runs**.
4. The result line reads "Cancelled 2 runs." and both rows have left the queue.
5. Messages shows a line like
   `RunDBView: action clear_queue accepted: operator 'NAME' cancelled 2 run(s) [ids] (PENDING), skipped 0`.
6. The database agrees (read-only role):
   ```bash
   psql -U readonly -d pioneer -h localhost -c \
     "select r.id, r.status, a.author, a.note from state.midas_run r join logs.run_annotations a on a.run_id = r.id where r.id in (<id1>, <id2>)"
   ```
   Status `CANCELLED`, author your name, note starting "cancelled from the RunDB
   page (Clear queue)".
7. The queue count in `select count(*) from state.midas_run` has not dropped:
   cancelled runs stay in the table.

### 6g. Live check, sequencer running

Only with the user's agreement, on a quiet moment, since it means the sequencer
runs with a queue.

1. Schedule two more 1-event runs as above, with the same priority if you can,
   and start the sequencer.
2. Press **Clear queue...** again. The dialog should list the run the sequencer
   is about to take as "kept - sequencer is loading it" with the yellow
   explanation.
3. After OK, the kept run is still `PENDING` (OK sends only the runs marked
   for cancelling), and the annotation of each cancelled run ends
   "; sequencer running, next run(s) <id> left PENDING".
4. Stop the sequencer, clear again, and the kept run is cancelled.

The sequencer's state is read from `/PySequencer/State/Running`, and while it
runs the loaded run from `/Nearline/Info/Run DB PK` is kept as well. If the
first read fails for any reason the client assumes the sequencer is running, so
the worst case is one run left for a second clear.

### 6h. Disarming, at once

```bash
odbedit -e bt2026 -c 'set /RunDBView/"Allow actions" n'
```

The flag is read again on every call, so this takes effect on the next press
with no restart: the client then answers `denied` to a clear, and the Messages
line says "refused ... actions disabled in /RunDBView/Allow actions". The
button may stay on screen until the page next reads `status` (within `Poll
seconds`) and then goes away. To remove the write path completely, set the
Start command back to the read-only form in step 2b and restart `RunDBView`;
`~/.pgpass` can then be deleted.

After this deploy, the piana mirror sync rule applies (see the deploy
procedure notes): the pinky checkout must be mirrored to piana.

## 7. Known issues

1. **`db_viewer.sql` is shipped, not applied.** It is a byte-identical copy of
   `psm-nearline-website-2026/db/rundb_viewer.sql` (which requires the copy to
   exist), kept next to the client for reference. `CREATE INDEX` on
   `logs.slow_control` (1.6M rows, PK index only) takes a lock that blocks
   writers, so applying it on pinky is a beamtime scheduling decision, not
   part of this deploy. Do not run it here.
2. **`RunDBView` must be a unique client name in the experiment.** If a second
   copy is ever started (e.g. by mistake from a second Programs entry or a
   manual shell), the second one refuses to connect
   (`throw_if_already_running=True`); this is intentional, not a bug to work
   around.
3. **The run-sequence status can lag its member runs** until the database's
   `mr_status_change` trigger fires (`db_config.sql:814-836`). The Sequences
   section shows the member counts beside the sequence status for exactly
   this reason — read the counts, not only the status word, if they seem to
   disagree.
4. **Historical runs have no configuration.** Every run taken before the run
   database carried configurations (all of them predating this deploy) shows
   "no configuration recorded for this run" in the Queue/Runlog/detail views.
   That is expected, not a sign the client is broken.
5. **Actions are not armed by steps 0-5.** The action panel on the page renders
   as a single inert sentence ("Actions are disabled on this client") as long
   as `--allow-actions` is absent from the Start command. Arming is step 6 and
   is a decision for the user to make explicitly, not something to do while
   deploying the read-only page. Even once armed, only Clear queue is live on
   pinky; the five-point form is refused and hidden on any database that is not
   a scratch one. A refused action attempt
   now leaves a line in the MIDAS Messages log — accepted, refused by the
   action itself, or refused by a gate (naming which one) — so anyone who
   presses a button that does nothing can find the trace afterwards.
6. **Cancelling cannot be undone from the page.** A `CANCELLED` run stays
   cancelled; re-schedule it from ConfigDB. A cancelled run inside a scan also
   sets that sequence to `FAILED` (database trigger).

This whole document — every ODB write, every restart, every verification
query — is meant to be run by a person at the keyboard, one step at a time,
never by a script or an unattended job.
