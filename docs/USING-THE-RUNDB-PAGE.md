# Using the RunDB page

This is for a shifter looking at an idle DAQ, or one that just started a run,
who wants to know what the run database says without knowing any Postgres.

## What it is

**RunDB**, in the side menu. It shows what runs have been taken, what is
queued next, and which scans (sequences) they belong to. It **only reads**.
It does not start a run, does not change the queue order, and does not touch
the sequencer. You cannot break anything by clicking around it.

## The strip at the top

Chips, refreshed once a second, straight from the ODB — they work even
if the page's own client (see below) is stopped. **Run**, **DB run** and
**Sequencer** are always shown; **Queue** and **Nearline** appear once the
client has answered its first `status` poll.

- **Run** — the run number and whether MIDAS is running, paused or stopped.
- **DB run** — whether a run is currently attached to the run database.
  - a number: that run is attached and is being recorded
  - **not attached**: normal between runs — it goes back to this at the end
    of every run
  - **key not present**: the nearline daemon has not created this key yet;
    normal if it has never started since the experiment was set up
- **Sequencer** — running, finished, or not running, plus the name of the
  loaded sequence file. If the sequencer is not running and something is
  waiting in the queue, the page adds a line: *"the sequencer is not running,
  so nothing in the queue will start."* That is the single most useful
  sentence on this page — if runs are not advancing, this is why.
- **Queue** — how many runs are queued, counted by status name exactly as the
  database stores it, e.g. *"1 RUNNING, 35 PENDING, 1 HOLDING"*.
- **Nearline** — *"Nearline — N failed"*, in red, and only shown at all when
  N is greater than zero. Its tooltip gives how many nearline jobs are still
  pending. No chip here does not mean nothing is running — it means nothing
  has failed.
- **Run database** — reachable, stale, or not answering (see "The stale
  states" below).

Status names anywhere on this page — in the strip, the Queue, the Runlog, the
detail view — are shown exactly as the run database stores them: `PENDING`,
`RUNNING`, `HOLDING`, `DONE`, `FAILED`, and the rest of `utils.status`. The
page never relabels one as "waiting", "finished" or "on hold". What it does
add is colour: red for a failure, yellow for a status a person set by hand
(chiefly a run put on hold), green for anything running or successfully
finished, grey for everything else, mainly the pending statuses. Hover any
status to see its tooltip — that text is the database's own `description`
column for that status name, not page copy.

## Reading the queue

The Queue section lists runs waiting to be taken, **lowest priority number
first** — priority 1 goes before priority 2. The row that will run next is
marked **next up**. Each row also shows its configuration in short form
(target position, degrader, beam config) so you can see what is coming before
it starts. An empty queue says plainly "Nothing is queued." Below the table
is a sentence counting the queue by status, e.g. *"37 runs in the queue:
1 RUNNING, 35 PENDING, 1 HOLDING. The lowest priority goes first."*

A run sitting in the queue with status **HOLDING** is not stuck by accident —
a person put it there on purpose (paused pending a decision), which is why it
is shown in yellow rather than grey. It will not start until a person takes
it off hold.

## Reading the runlog

The Runlog section lists runs already taken, **newest first**: run number,
database id, status, started/stopped times, duration, files, and the worst
nearline job status for that run. Click a row to expand it: the full
configuration values, the individual files and jobs, and the other runs in
its sequence.

Times come from the start/stop markers MIDAS itself writes (BOR/EOR), not
from a clock the page keeps — if a run shows "no start/stop logged," that
information was never recorded for it, not lost by the page.

If a row's configuration says **"no configuration recorded for this run,"**
that is expected for anything taken before the run database tracked
configurations — it is not a broken run, and there is nothing to fix.

**Show older** at the bottom of the table pages further back through history.
Next to it, **Refresh now** re-reads the queue, runlog and sequences
immediately instead of waiting for the next automatic poll — use it right
after you expect the database to have changed. The runlog stops accumulating
at the 400 newest runs; past that point "Show older" is replaced by a note
pointing at `python -m pioneer.rundb.view runlog --before-id N` for anything
further back.

## Sequences

One line per scan, written as a sentence — for example *"9 runs: 4 DONE,
1 RUNNING, 4 PENDING"* — plus the span of run numbers it covers. This is the
place to check whether a multi-run scan is progressing as a whole, rather
than reading it off nine separate runlog rows.

A sequence's own status can lag a step or two behind its member runs (there
is a short delay in the database before it updates); the counts next to it
are there so you can see the real picture even when the status word hasn't
caught up yet.

## The stale states, and what to do about each

The page tells these apart in words, and keeps showing the last good data,
dimmed, with the time it was read — it never blanks the screen.

- **"The RunDBView client is not answering."** The page itself (mhttpd) is
  fine; its small helper program that talks to the database has stopped. Go
  to the **Programs** page and start `RunDBView`. The page notices on its
  own — no reload needed.
- **"The run database did not answer."** The client is running, but Postgres
  isn't responding. That is a job for whoever manages the database on pinky,
  not something a shifter restarts from the web page.
- **"The RunDBView client is not coming back."** The client answered before,
  so it is running, but it is stuck — most likely parked in a slow database
  query — and has not replied within 15 seconds. The page keeps asking on its
  own; if this does not clear, restart `RunDBView` from the Programs page.

If a reply is too large for one round trip, the page asks again with a
larger buffer automatically; if it still doesn't fit, it shows fewer rows and
says so in a yellow note. Normal under a long runlog request, and it resolves
itself.

## At 3 a.m., with a stuck queue

1. Check the strip: is the sequencer running? If not, that is almost always
   the whole story — nothing in the queue starts while it is stopped. Start
   it from the **Programs** page.
2. Check "DB run" — if it never leaves "not attached" while a run is going,
   the nearline daemon may not be creating the key; that is a database
   question for the daemon owner, not something to fix from this page.
3. If the RunDB page itself says "not answering" or "not coming back" (any of
   the three), see the stale states above. The DAQ can keep running with this
   page down — it is a viewer, nothing more.

## The command line, if the page is down

Everything the page shows also comes from a small command that needs no
browser, no mhttpd, no client:

```bash
export PIONEER_RUNDB_DSN="host=localhost dbname=pioneer user=readonly password=readonly"
python -m pioneer.rundb.view status
python -m pioneer.rundb.view queue
python -m pioneer.rundb.view runlog --limit 20
python -m pioneer.rundb.view run <id>
python -m pioneer.rundb.view sequences
python -m pioneer.rundb.view config <id>
```

Add `--json` to any of these to see the exact reply the page itself gets —
useful for telling "the page is broken" from "the data is like this."

## Actions

There are none. The panel on the page, if you scroll to it, says in one
sentence that actions are disabled on this client. This page does not
schedule runs, start runs, or change the queue — as of this deploy it never
will without a separate, explicit decision to arm it.
