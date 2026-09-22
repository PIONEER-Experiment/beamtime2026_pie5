# MIDAS custom pages

## `caenhv.html` — CAEN DT1470ET high voltage

Operator page for the 4-channel `CaenHV` slow-control equipment: one row per
channel with VSET / VMON / ISET / IMON / MAXV, an on/off checkbox, a decoded
STAT word and the per-channel alarm state, plus a small read-only table of the
ramp speeds and trip times.

Everything it shows comes from `/Equipment/CaenHV/...` and `/Alarms/Alarms/...`.
The page writes only to three keys, all through the normal MIDAS controls:

| control | ODB key | how |
| --- | --- | --- |
| VSET | `/Equipment/CaenHV/Variables/Demand[i]` | `modbvalue` inline edit (click, type, Enter) |
| ISET | `/Equipment/CaenHV/Settings/Current Limit[i]` | `modbvalue` inline edit |
| MAXV | `/Equipment/CaenHV/Settings/Voltage Limit[i]` | `modbvalue` inline edit |
| On/Off | `/Equipment/CaenHV/Variables/ChState[i]` | checkbox; switching **on** asks for confirmation first |

Switching a channel on pops up a `dlgConfirm` that also reminds the operator
that a channel whose front-panel switch is in OFF or KILL (status `DIS` /
`KILL`) will not come on: the board acknowledges `PAR:ON` and silently does
nothing. Switching off is immediate, no confirmation.

`IMON` shows `n/a` while the driver's "never read" sentinel (`-1`) is in
`Variables/Current`. The decoded Status, Alarm, Pol, Name and IMON cells are
refreshed by the page's own 1 s `mjsonrpc_db_get_values` poll; the plain
numeric cells are `modbvalue` and are refreshed by mhttpd itself.

STAT bit numbers are duplicated in the page's `HV_BITS` array. The single
source of truth is `scfe/caen_hv_fe.h` (`enum stat_bit_t`) — if a bit moves
there, move it here too.

### ODB registration

mhttpd serves `/Custom/<name>` files relative to the **one** global key
`/Custom/Path`, so register the page with:

```
odbedit -e caenhv -c 'create STRING /Custom/Path'
odbedit -e caenhv -c 'set /Custom/Path /workdir/beamtime2026_pie5/custom'
odbedit -e caenhv -c 'create STRING /Custom/CaenHV'
odbedit -e caenhv -c 'set /Custom/CaenHV caenhv.html'
```

The page then appears in the left-hand menu as **CaenHV** and at
`?cmd=custom&page=CaenHV`.

`/Custom/Path` must be the path **as mhttpd sees it**. mhttpd runs inside the
`testbeam-midas` container, where `/home/jlabo/github/pioneer/testbeam-env` is
mounted at `/workdir`, so the container path is
`/workdir/beamtime2026_pie5/custom` and the host path is
`/home/jlabo/github/pioneer/testbeam-env/beamtime2026_pie5/custom`. Use the
container path. A trailing slash is optional (mhttpd adds one); a bare `/` or a
value with no `/` in it is rejected with an `add_custom_path` error.

> **Warning — `/Custom/Path` is single-global.** There is one such key for the
> whole experiment and other frontends fight over it. In particular musip's
> `quads_config_fe` **rewrites `/Custom/Path` on every start** to
> `$HOME/musip/custom`, which would orphan this page at its next restart. See
> `midas_files/wavedream-scalar-readout/docs/REGISTRY.md`, section "Things that
> are single-global and get fought over". If both frontends must coexist in one
> experiment, either symlink this file into whatever directory wins, or keep this
> page in a MIDAS experiment (e.g. `caenhv`) that musip does not attach to.

## `rundb.html` — the 2026 run database

Shifter page for the run database: what has been taken, what is queued and
which scans those runs belong to. It is **read-only** — it starts no runs,
changes no priorities and does not touch the sequencer. Four sections:

| section | what it shows |
| --- | --- |
| live strip | run state and number, the database id of the run in progress, the sequencer, and whether the run database is answering. ODB only, once a second |
| Queue | runs waiting to be taken, lowest priority first, with their configuration summary; the one that goes next is marked |
| Runlog | newest runs first, with start/stop/duration, files and nearline jobs; click a row for the full configuration values, the individual jobs and the other runs of its sequence; **Show older** pages back |
| Sequences | one line per scan with its member runs counted by status ("9 runs: 4 DONE, 1 RUNNING, 4 PENDING") and the run-number span |

Status names are shown exactly as they are stored — `DONE`, `PENDING`,
`HOLDING`, `RUNSDONE` — and never translated into a word of the page's own.
Whoever reads this page also reads `psql`, the sequencer log and the nearline
messages, and one vocabulary for the eleven names in `utils.status` is enough.

What the page does with that table is colour them and order them: the flags
(`issuccess`, `isfailure`, `ispending`, `isrunning`, `isuser`) give each name
its colour — red for a failure, yellow for something a person put on hold, green
for running or succeeded, grey otherwise — and decide which status a rolled-up
cell shows when several rows disagree. The `description` column is the tooltip.
A status somebody adds to `utils.status` tomorrow therefore renders correctly
without a change here, in grey if its flags say nothing.

Counts are grouped the same way: "37 runs in the queue: 1 RUNNING, 35 PENDING,
1 HOLDING".

Runs taken before the database carried configurations have none, and that is
normal: those rows say *no configuration recorded for this run* rather than
showing an empty cell.

### What it talks to

| source | how | when |
| --- | --- | --- |
| ODB | `mjsonrpc_db_get_values`, answered by mhttpd itself | 1 Hz |
| `RunDBView` client | `mjsonrpc_call("jrpc", {client_name, cmd, args, max_reply_length})` | `status` + `queue` every `Poll seconds`; `runlog` + `sequences` every `Runlog refresh seconds`; `run` on a click; all four again on **Refresh now** |

The ODB half keeps working when the client is stopped, which is the point of
the split: the strip stays live and the page says in words which half is
missing. Two different failures get two different sentences, both naming the
time the data on screen was read, and the tables stay up and go dim rather
than disappearing:

* the client is not answering — mhttpd's `jrpc` returns only a status and no
  reply when it cannot reach the client. Start `RunDBView` from the Programs
  page; the page picks it up on its own, with no reload.
* the database did not answer — the client is running, so this is Postgres.

A reply that does not fit in `Max reply kB` comes back as a short `too_large`
envelope naming the size it needed; the page asks again for that size, capped
at four times the configured buffer, and if it still does not fit it halves the
row count and says so. It never renders half a reply.

A third failure has its own sentence: a client that is running but stuck in a
query never answers at all, so every command gives up after 15 s rather than
leaving the poller parked on a promise that will not settle and the page
quietly still.

Two of the page's states are worth knowing about because they are not errors.
`status` answers `ok` with `database.reachable` false when Postgres is down —
that is the whole point of that command — so "last read at" is stamped only
from replies that actually carried rows, and the tables dim while the strip
stays live. And **Show older** stops at 400 runs, saying so and pointing at
`python -m pioneer.rundb.view runlog --before-id N`, because the whole table is
re-rendered on every refresh.

Same data on the command line, which is what to use when the page is
unavailable or the client will not start:

```
python -m pioneer.rundb.view status|queue|runlog|sequences|run ID|config ID
```

### Files

| file | what |
| --- | --- |
| `rundb.html` | the page: stock MIDAS resources, `mhttpd_init('RunDB')`, the section skeleton |
| `js/rundb-rpc.js` | transport only — the `jrpc` envelope and its retry, the ODB read, the serialised visibility-aware poller, and the pure helpers (status words, durations, escaping) |
| `js/rundb.js` | rendering only — the strip, the three tables, the expandable run detail, the stale messages, the action panel |
| `css/rundb.css` | fills, borders and outlines from the `midas.css` custom properties (`--mred`, `--myellow`, `--mgreen`, `--mgray`, `--mblue`); text colours are literal, because those properties are *background* pastels (`midas.css:24-28`) and grey-on-white prose at 3 a.m. is not a colour scheme. No web fonts, no CDN |
| `js/rundb-rpc.test.js` | `node --test` for the pure helpers, the envelope handling and the rendering. Node is **not** a dependency of this repo and must not become one — the page has no build step. Run it wherever node happens to exist, e.g. `docker run --rm --cpus=8 -v "$PWD/custom:/w" -w /w node:22-alpine node --test js/rundb-rpc.test.js` |

The `?v=1` on the two scripts and the stylesheet is not decoration: mhttpd
stamps `Expires: +24h` on anything served as an asset, with no `ETag` and no
`Last-Modified`, so without a version token an edit is invisible for a day.
Bump it when you change a file. The page itself is exempt — its `/Custom` key
has no dot, so it routes through `show_custom_page()`, which sets no cache
headers.

There is a static harness under
`scratch/rundb-page-standalone/page-fixture/index.html` that loads the real css
and js with canned replies (client down, database down, a reply that is too
large, a run with and without configurations), so the page can be looked at
with no mhttpd, no client and no Postgres.

### ODB registration

Unlike `caenhv.html` above, these four keys hold **absolute paths**, so the page
does not depend on `/Custom/Path` and cannot be orphaned when another frontend
rewrites it. The three asset keys end in `!`, which is what keeps them out of
the side menu; the page key carries no dot, which is what keeps it out of
mhttpd's caching path.

```
odbedit -c 'create STRING /Custom/RunDB'
odbedit -c 'set /Custom/RunDB /home/pinky/bt2026/beamtime2026_pie5/custom/rundb.html'
odbedit -c 'create STRING "/Custom/rundb.js!"'
odbedit -c 'set "/Custom/rundb.js!" /home/pinky/bt2026/beamtime2026_pie5/custom/js/rundb.js'
odbedit -c 'create STRING "/Custom/rundb-rpc.js!"'
odbedit -c 'set "/Custom/rundb-rpc.js!" /home/pinky/bt2026/beamtime2026_pie5/custom/js/rundb-rpc.js'
odbedit -c 'create STRING "/Custom/rundb.css!"'
odbedit -c 'set "/Custom/rundb.css!" /home/pinky/bt2026/beamtime2026_pie5/custom/css/rundb.css'
```

mhttpd serves an asset under its **key name**, not under the path it points at,
which is why the page asks for `rundb.css?v=1` and `rundb.js?v=1` and not for
`css/rundb.css`. The paths above are the paths **as mhttpd sees them**: on this
laptop mhttpd runs in the `testbeam-midas` container, where the workspace is
mounted at `/workdir`, so they read `/workdir/beamtime2026_pie5/custom/...`.

The page then appears in the left-hand menu as **RunDB** and at
`?cmd=custom&page=RunDB`.

### `/RunDBView` — the page's configuration

Deliberately not under `/Custom`: mhttpd renders any `/Custom` subdirectory as a
sidenav submenu, with no way to hide it. The client seeds this subtree when it
connects; the page falls back to the same defaults when it is absent, so it
works before the client has ever run.

| key | default | what it does |
| --- | --- | --- |
| `Client name` | `RunDBView` | the MIDAS client the page sends `jrpc` to |
| `Poll seconds` | 5.0 | how often `status` and `queue` are re-read |
| `Runlog rows` | 50 | rows per **runlog** page (the page and the client both cap at 200). The queue and the sequences are asked for without a limit and get the client's own default: both are short, and a queue cut off at row 50 would be a queue that lies |
| `Runlog refresh seconds` | 30 | how often the runlog and sequences are re-read |
| `Max reply kB` | 256 | the reply buffer asked of mhttpd; also the base of the 4x retry cap |
| `Stale seconds` | 20 | after this long with no answer the tables are dimmed |
| `Allow actions` | false | re-read by the client on every action; see below |
| `Database` | the client's DSN with the password masked, e.g. `host=localhost dbname=pioneer user=readonly password=***` (`pg.describe_dsn`) | display only |

Changing any of these takes effect on the next poll — no reload.

### Actions

Normally there are none, and the panel says so in one line. Writing to the run
database needs the client to have been started with `--allow-actions` *and*
`/RunDBView/Allow actions` to be true, both re-read by the client on every
single call, and neither is the case on pinky.

When both are set the panel appears, and it schedules one thing: a five-point
scan. There is a picker per configuration type, filled from the configurations
that have already appeared in the queue and the runlog — so opening the panel
costs no extra round trips — plus an "other config id" field that looks up
anything else with `config {id}`. `target_position` is not offered at all: the
five positions *are* the scan (`actions.py` refuses a request that sets one),
and a configuration marked `do_not_use` is listed but cannot be chosen.

The button stays dead until at least one configuration is chosen, the events per
run are a whole number the client would accept, **and** the client has confirmed
what the request would do. That last part is a read command,
`preview_five_point`: it runs every check the write runs and counts the target
positions it finds, without writing anything. The page sends it whenever the
form changes (after a short pause, so typing an events field is one preview and
not six) and puts its answer in the sentence, so the number of runs promised is
the client's count and not this page's guess. While it is in flight the line
says so; if it comes back refused, the line says why and the button stays
dead.

> This will schedule 5 runs (target positions of sequence 2) with degrader
> position degrader x=3.5 mm (4 mm before L1) and pie5 epics pie5_epics #26 at
> 1 000 000 events each, as one sequence.

Pressing it repeats that sentence in a `dlgConfirm`, and only then sends
`schedule_five_point` with `{config_ids, requested_events}`. The whole form is
frozen from the moment the question appears, so what is confirmed is what is
sent and a second press cannot start a second scan. The gate is checked once
more at that moment against the last `status` reply, so a client disarmed since
the panel was drawn is never asked to write.

What comes back is shown as what now exists — the sequence, and each created run
with the target position it actually carries — and the queue is re-read
immediately rather than at the next poll.

Failures are told apart, because they mean different things:

| what came back | what the page says |
| --- | --- |
| `usage`, `denied`, `db`, `unknown_command` | *Nothing was scheduled*, with the client's own words (`denied` also says the client is not armed) |
| no answer at all — `timeout`, `client_down`, `transport`, `bad_reply`, `too_large` | *The client did not answer. The runs may still have been queued. Check the queue before pressing again* — and the queue is re-read at once |
| an error carrying `created_anyway` | *Partially scheduled: runs … were created*, first and in bold, with what failed and a reminder to cancel what should not be there |

A write is never sent twice. The one-shot retry that reads use when a reply does
not fit is skipped for `schedule_five_point`, because a reply that went missing
says nothing about whether the runs were made — that is the reason the middle
row of that table exists rather than an automatic second attempt.

Everything the panel checks, the client checks again; the page's copies of the
bounds (1 … 10¹⁰ events, one configuration per type, no `target_position`, no
`do_not_use`) exist so it can say no without a round trip, not so it can decide.
The command line does the same thing:
`python -m pioneer.rundb.actions five-point --config-id ID --events N
--write-dsn … --confirm`.

### Not registered here

This directory contains files only. Nothing in the repo writes `/Custom/*`;
registration is a deliberate manual step.
