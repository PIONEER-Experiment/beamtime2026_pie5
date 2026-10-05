# Using the RunDB page

This is for a shifter looking at an idle DAQ, or one that just started a run,
who wants to know what the run database says without knowing any Postgres.

## What it is

**RunDB**, in the side menu. It shows what runs have been taken, what is
queued next, and which scans (sequences) they belong to. It never starts a
run, never changes the queue order and never touches the sequencer. Apart
from one button, it only reads, so you cannot break anything by clicking
around it. The one button is **Clear queue...** (see "Clearing the queue"
below). It is there only when the page has been armed for it; on an
unarmed page, which is the normal state until someone decides otherwise, the
page reads and nothing else.

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

If the queue is full of runs you no longer want, see "Clearing the queue"
below.

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

## Clearing the queue

Use this when runs are queued that should not be taken: a scan set up with the
wrong settings, or a queue left over from a previous shift. It sets the queued
runs to `CANCELLED`. It does not start or stop anything.

### When the button is there

**Clear queue...** sits in the heading of the Queue section. It is shown only
when both of these hold:

1. the page has been armed for actions (the yellow line at the top of the
   Actions panel says "This client is armed for actions"; if it does not say
   that, the button never appears, and clearing has to be done from the
   command line, see below), and
2. the queue currently has at least one `PENDING` or `HOLDING` run. With
   nothing to cancel, there is no button.

### The dialog

Pressing the button opens a dialog and the client immediately lists what
it would cancel. Nothing has been changed yet.

- **The list.** One row per run: DB id, run number ("not taken" if it has not
  been started), status exactly as the database stores it, priority, requested
  events, and what happens to it when you press OK: `CANCELLED`, or "kept".
  The sentence above the list says how many runs will be cancelled.
- **Which runs.** Only `PENDING` runs by default. Runs on hold (`HOLDING`)
  are left alone unless you tick **also cancel HOLDING runs**. That box is
  unticked every time the dialog opens, so you have to ask for it each time;
  ticking it makes the client list again.
- **Operator.** Type your name (at most 64 characters, one line). It is
  required: the OK button stays dead, with the reason written under it, until
  there is a name. The name is recorded with every cancelled run and in the
  MIDAS message. The page remembers it until you reload, so you type it once.
- **Close**, Esc, or a click outside the box closes the dialog and changes
  nothing.
- If the queue is longer than 2000 runs, only the first 2000 are listed and
  only those are cancelled; the dialog says so. Press the button again for
  the rest.

The OK button says how many runs it will cancel ("Cancel 12 runs").

### What OK does

- Every listed run marked `CANCELLED` goes to `CANCELLED`. It is one step:
  either they all change or none does.
- OK sends only the runs marked `CANCELLED`. A run the dialog showed as "kept"
  is never cancelled by that OK, even if the sequencer has stopped in the
  meantime: open the dialog again to see it listed for cancelling.
- Each cancelled run gets an annotation in the run database naming you and
  saying it was cancelled from the RunDB page. It is stored in the database's
  `logs.run_annotations`; the page itself does not show annotations.
- One line is written to the MIDAS Messages page, from `RunDBView`, with your
  name, how many runs were cancelled, and their DB ids.
- Only the runs the dialog listed are touched. A run scheduled while the dialog
  was open is not cancelled, and neither is one that changed status in the
  meantime (for example one the sequencer has just taken, or one someone put on
  hold). Those are reported as "skipped" in the result.
- A `CLAIMED` or `RUNNING` run, and anything already finished, is never touched.
  Clearing the queue does not stop the run that is going.

### When the sequencer is running

The sequencer picks the queued run with the lowest priority number and spends
a while setting up that run's configuration (moving devices) **while it is
still `PENDING`**. Cancelling it at that moment would leave the sequencer
starting a run the database says is cancelled. So while the sequencer is
running, the next run is **kept `PENDING`**. When several `PENDING` runs share
the lowest priority number, all of them are kept, because there is no telling
which of them the sequencer will take. The run the sequencer has already
picked is kept too, even if a run with a lower priority number was queued
after it picked it (the sequencer can wait at a prompt for minutes with a run
loaded).

The dialog shows such runs as "kept - sequencer is loading it", with a yellow
line explaining it. To remove them as well:

1. Stop the sequencer on the **Sequencer** page.
2. Open **Clear queue...** again. Nothing is kept now, and OK cancels the
   rest.

The sequencer's state is read from the ODB each time you press the button and
again when you press OK, so it cannot be set from the page. If the page cannot
tell, it assumes the sequencer is running. There is a short window, between
that read at OK and the change in the database, that cannot be closed from
here: if you start the sequencer at the very moment you press OK, stop it, look
at the queue, and start it again.

### The result line

After OK the dialog closes and a line stays under the queue until you press
**Dismiss**. The queue is re-read straight away.

- **"Cancelled N runs."** followed by the DB ids that are now `CANCELLED`, and
  the name recorded.
- **"Kept DB id ... PENDING: the sequencer is about to take it."** A run that
  became the next run after the dialog listed it. The case above: stop the
  sequencer, then clear again.
- **"Skipped N runs whose status changed after the dialog listed them"**,
  with the id and the status each has now. Look at the queue and clear again
  if you still want them gone.
- **"Nothing was cancelled."** (red) The client refused, with its reason: for
  example the name was missing, the database was busy ("try again in a
  moment"), or the page is not armed. Nothing was written.
- An error saying **whether the runs were cancelled "is not known"** means the
  database lost the change at the last moment, while saving it. Look at the
  queue before clearing again.
- **"The client did not answer. The runs may still have been cancelled. Look
  at the queue before pressing again."** (red) The request went out and no
  usable answer came back, so it may or may not have been carried out. Check
  the Queue section, or **Refresh now**, before trying again. The page never
  sends the same clear twice by itself.

### Sequences and runplan steps

A run in a scan (a sequence) that is cancelled makes the **whole sequence
`FAILED`**: the database treats `CANCELLED` as a failure and marks the
sequence accordingly (after the short delay mentioned under "Sequences"). The
sequence's other runs are not cancelled unless they were queued and listed too.
A runplan step whose run was cancelled is reported by the runplan as
cancelled.

### It cannot be undone from this page

There is no un-cancel. A `CANCELLED` run stays in the runlog as it is. To run
that configuration again, schedule it afresh from **ConfigDB**. Check the list
before pressing OK.

### From the command line, if the page is down or not armed

The same thing without a browser, MIDAS or the client. It needs a connection
string for a database user that may write (ask whoever manages the database;
do not paste the password into a log):

```bash
python -m pioneer.rundb.actions clear-queue --operator "Your Name" \
    --write-dsn "host=localhost dbname=pioneer user=shifter"
```

Without `--yes` this only **previews**: it lists the runs (marking those that
would be kept), says "nothing was written", and exits with status 3. When the
list is right, run it again with `--yes` added to cancel them. Without
`--run-id`, `--yes` takes a fresh look at the queue and cancels what that
preview would cancel, which may differ from what you saw if the queue changed
in between. To cancel exactly the runs you previewed, give each one:
`--yes --run-id 812 --run-id 813 ...`. Other options:

- `--include-holding` also cancels `HOLDING` runs.
- `--include-head` says the sequencer is stopped, so the next run is cancelled
  too. Without it the next run is kept, because this command cannot see MIDAS.
  **Use it only after stopping the sequencer on the Sequencer page.**
- `--keep-run-id ID` keeps that run as well, for when the sequencer is running
  and you know which run it has loaded (`/Nearline/Info/Run DB PK`). Not
  together with `--include-head`.
- `--json` prints the exact reply the page would get.

The annotations say "cancelled from the command line". This path writes no line
to the MIDAS Messages page, since it does not talk to MIDAS. It does not look
at `/RunDBView/Allow actions` either: it is the manual path, and whoever can
run it has the database password.

## Other actions

The page can also schedule a five-point scan, but only on a scratch
database. On the experiment's own database the Actions panel says so in one
line ("offered on scratch databases only; use the ConfigDB page"), and
five-point scans are scheduled from ConfigDB as before.

Everything else here is read-only. The page does not schedule other runs, start
runs, change the queue order or touch the sequencer. On a page that has not been
armed, the panel says in one sentence that actions are disabled on this client.
