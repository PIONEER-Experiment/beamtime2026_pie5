//
// rundb.js -- the RunDB page itself: what it shows and how it says it.
//
// Rendering only. Everything that talks to mhttpd is in rundb-rpc.js, which
// must be loaded first (this file reads the RunDbRpc global).
//
// Three loops, on purpose:
//
//   strip   1 Hz, ODB only. It is the part a shifter glances at, and it keeps
//           working with the RunDBView client stopped -- mhttpd answers it.
//   slow    /RunDBView/Poll seconds (5 s): status + queue, one jrpc round trip
//           each into a python process.
//   log     /RunDBView/Runlog refresh seconds (30 s): runlog + sequences.
//
// Nothing here writes anything, anywhere. The page reads the run database and
// the ODB; it does not start runs, does not change the queue, and the action
// panel is inert in this version (see actionPanelHtml).
//
// The HTML builders are pure functions of the data: they take an envelope's
// `data` and give back a string. That is what makes them testable under
// `node --test` (rundb-rpc.test.js) without a browser or a database.
//

(function (root) {
"use strict";

const R = root.RunDbRpc;
const esc = R.esc;
const NO = R.NO_VALUE;

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------
//
// /RunDBView is seeded by the client when it connects. These are what the page
// uses until it has been -- so the page works on an experiment where the client
// has never run, which is exactly when somebody needs to be told why.

const CONFIG_ROOT = "/RunDBView";

const DEFAULTS = {
   "Client name": "RunDBView",
   "Poll seconds": 5.0,
   "Runlog rows": 50,
   "Runlog refresh seconds": 30,
   "Max reply kB": 256,
   "Stale seconds": 20,
   "Allow actions": false,
   "Database": ""
};

const RUNLOG_ROWS_MAX = 200;      // the client caps it there too
const RUNS_CAP = 400;             // most runs "Show older" will accumulate

// ODB paths, in the order the strip poll reads them.
const ODB_PATHS = [
   "/Runinfo/State",
   "/Runinfo/Run number",
   "/Runinfo/Run DB PK",
   "/PySequencer/State/Running",
   "/PySequencer/State/Finished",
   "/PySequencer/State/SFilename",
   "/PySequencer/Param/Value/nEv",
   CONFIG_ROOT + "/Client name",
   CONFIG_ROOT + "/Poll seconds",
   CONFIG_ROOT + "/Runlog rows",
   CONFIG_ROOT + "/Runlog refresh seconds",
   CONFIG_ROOT + "/Max reply kB",
   CONFIG_ROOT + "/Stale seconds",
   CONFIG_ROOT + "/Allow actions",
   CONFIG_ROOT + "/Database"
];

// midas.h:305-307
const STATE_STOPPED = 1;
const STATE_PAUSED = 2;
const STATE_RUNNING = 3;

function runStateWord(state) {
   const s = Number(state);
   if (s === STATE_RUNNING) return { word: "running", klass: "green" };
   if (s === STATE_PAUSED)  return { word: "paused",  klass: "yellow" };
   if (s === STATE_STOPPED) return { word: "stopped", klass: "gray" };
   return { word: "unknown state", klass: "gray" };
}

// ---------------------------------------------------------------------------
// Page state
// ---------------------------------------------------------------------------

const state = {
   config: Object.assign({}, DEFAULTS),
   odb: {},                  // last ODB read, by short name
   statuses: {},             // utils.status rows by name, from the status reply
   status: null,             // data of the last good `status`
   queue: null,              // data of the last good `queue`
   sequences: null,          // rows of the last good `sequences`
   runs: {},                 // runlog rows by database id, newest first when sorted
   nextBeforeId: null,       // paging cursor from the last runlog reply
   haveRunlog: false,
   paged: false,             // true once "Show older" has been used
   runsCapped: false,        // true once RUNS_CAP has been reached
   action: {                 // the action panel; only ever rendered when armed
      sig: null,             // what the panel was last drawn from
      options: [],           // configurations seen in the queue and the runlog
      selection: {},         // config_type -> the chosen entry
      events: 1000000,       // DEFAULT_ACTION_EVENTS
      extra: {},             // the free-text id field: {id, pending, info, error}
      preview: {},           // preview_five_point: {key, pending, data, error}
      busy: false,           // a schedule call is in flight
      result: null           // the last reply, success or refusal
   },
   expandedId: null,         // the run whose detail row is open
   detail: null,             // data of the last good `run {id}`
   detailError: null,
   lastGood: null,           // clock string of the last good client answer
   lastGoodMs: 0,
   clientError: null,        // last ok:false envelope with kind client_down
   dbError: null,            // ... with kind db
   otherError: null,         // ... anything else
   reducedTo: null           // a reply we had to ask for in fewer rows
};

// ---------------------------------------------------------------------------
// Small shared pieces
// ---------------------------------------------------------------------------

/**
 * A status, exactly as the run database stores it, coloured by its flags, with
 * the `utils.status` description as the tooltip.
 *
 * The name is not translated. Whoever reads this page also reads `psql`, the
 * sequencer's log and the nearline messages, and all of those say DONE.
 */
function statusCellHtml(name, tag) {
   const info = R.statusInfo(name, state.statuses);
   const el = tag || "td";
   if (!name) return "<" + el + ' class="rundb-st">' + NO + "</" + el + ">";
   return "<" + el + ' class="rundb-st ' + info.klass + '" title="' + esc(info.description || info.name) + '">' +
      esc(info.name) + "</" + el + ">";
}

/** The same, as an inline span for prose. */
function statusInline(name) {
   if (!name) return NO;
   const info = R.statusInfo(name, state.statuses);
   return '<span class="rundb-word ' + info.klass + '" title="' + esc(info.description || info.name) + '">' +
      esc(info.name) + "</span>";
}

// D6: the summary is a handful of fields, not a whole configuration row. Known
// keys get a human label; anything the client adds later still renders.
const SUMMARY_LABELS = {
   target_x: "target x", target_y: "target y",
   degrader_x: "degrader x", degrader_comment: "degrader",
   beam_config_id: "beam config", beam_config_type: "beam config type",
   config_id: "config", config_type: "type", comment: "comment"
};

function summaryText(summary) {
   if (summary === null || summary === undefined || summary === "") return "";
   if (typeof summary === "string") return summary;
   if (typeof summary !== "object") return String(summary);
   const parts = [];
   Object.keys(summary).forEach(function (k) {
      const v = summary[k];
      if (v === null || v === undefined || v === "") return;
      const label = SUMMARY_LABELS[k] || String(k).replace(/_/g, " ");
      parts.push(label + " " + v);
   });
   return parts.join(", ");
}

/**
 * The configuration cell of a runlog or queue row.
 *
 * A run with no configuration is not a broken run: every run taken before the
 * run database had configurations attached looks like this, and the client says
 * so in words through config_note. Print the sentence, not an empty cell.
 */
function configCellHtml(row) {
   const configs = row.configs || [];
   if (!configs.length) {
      const note = row.config_note || "no configuration recorded for this run";
      return '<td class="rundb-config"><span class="rundb-none">' + esc(note) + "</span></td>";
   }
   const parts = configs.map(function (c) {
      const text = summaryText(c.summary);
      const name = (c.config_type || "config") + " " + (c.config_id === undefined ? "" : c.config_id);
      const dnu = c.do_not_use ? ' <span class="rundb-warnword">do not use</span>' : "";
      return '<span class="rundb-cfg"><b>' + esc(name.trim()) + "</b>" + dnu +
         (text ? " " + esc(text) : "") + "</span>";
   });
   return '<td class="rundb-config">' + parts.join("<br>") + "</td>";
}

/**
 * The name of a file row. `fileext` is stored without its dot
 * (run00260 + mid.lz4), because open_file() splits on the first one.
 */
function fileName(f) {
   const base = (f && f.filebase) || "";
   const ext = (f && f.fileext) || "";
   return ext ? base + "." + ext : base;
}

/** files[] -> "3 files, worst DONE". */
function filesCellHtml(files) {
   const list = files || [];
   if (!list.length) return "<td>" + NO + "</td>";
   const worst = R.worstStatus(list.map(function (f) { return f.status; }), state.statuses);
   const names = list.map(function (f) {
      return fileName(f) + " " + (f.status || "");
   }).join("\n");
   return '<td class="rundb-st ' + R.statusInfo(worst, state.statuses).klass + '" title="' + esc(names) + '">' +
      list.length + (list.length === 1 ? " file" : " files") +
      (worst ? ", " + esc(worst) : "") + "</td>";
}

/** jobs[] (rolled up: job_type, status, count) -> the worst one, counted. */
function jobsCellHtml(jobs) {
   const list = jobs || [];
   if (!list.length) return "<td>" + NO + "</td>";
   let total = 0;
   const names = [];
   const detail = [];
   list.forEach(function (j) {
      const n = Number(j.count) || 1;
      total += n;
      names.push(j.status);
      detail.push(n + " x " + (j.job_type || "job") + " " + (j.status || ""));
   });
   const worst = R.worstStatus(names, state.statuses);
   return '<td class="rundb-st ' + R.statusInfo(worst, state.statuses).klass + '" title="' +
      esc(detail.join("\n")) + '">' + esc(worst) + " (" + total + ")</td>";
}

function sequenceCellHtml(seq) {
   if (!seq) return "<td>" + NO + "</td>";
   return "<td>#" + esc(seq.id) + " " + statusInline(seq.status) + "</td>";
}

// ---------------------------------------------------------------------------
// The live strip (ODB only)
// ---------------------------------------------------------------------------

function chip(klass, label, value, title) {
   return '<span class="rundb-chip ' + klass + '"' + (title ? ' title="' + esc(title) + '"' : "") +
      "><b>" + esc(label) + "</b> " + value + "</span>";
}

/**
 * The strip. `odb` is the object built by odbFromValues(), `health` says what the
 * client and the database last did, `counts` is the counts block of the last
 * good `status` reply (absent until the first one arrives).
 */
function stripHtml(odb, health, counts, queueCounts) {
   const run = runStateWord(odb.state);
   const chips = [];

   chips.push(chip(run.klass, "Run",
      esc(odb.runNumber === null || odb.runNumber === undefined ? "?" : odb.runNumber) +
      " &mdash; " + esc(run.word),
      "/Runinfo/State = " + odb.state));

   // /Runinfo/Run DB PK is created by the nearline daemon at start of run and
   // zeroed at end of run, so all three renderings are normal states.
   let pk;
   let pkClass = "gray";
   let pkTitle = "/Runinfo/Run DB PK";
   if (odb.runDbPk === null || odb.runDbPk === undefined) {
      pk = '<span class="rundb-none">key not present</span>';
      pkTitle = "/Runinfo/Run DB PK does not exist: the nearline daemon has not created it yet";
   } else if (Number(odb.runDbPk) === 0) {
      pk = '<span class="rundb-none">not attached</span>';
      pkTitle = "zero between runs: no run is attached to the database right now";
   } else {
      pk = esc(odb.runDbPk);
      pkClass = "blue";
   }
   chips.push(chip(pkClass, "DB run", pk, pkTitle));

   const seqRunning = truthy(odb.seqRunning);
   const seqFinished = truthy(odb.seqFinished);
   const script = R.basename(odb.seqFile);
   let seqWord;
   let seqClass;
   if (seqRunning) { seqWord = "running"; seqClass = "green"; }
   else if (seqFinished) { seqWord = "finished"; seqClass = "gray"; }
   else { seqWord = "not running"; seqClass = "yellow"; }
   chips.push(chip(seqClass, "Sequencer",
      esc(seqWord) + (script ? " &mdash; " + esc(script) : ""),
      "/PySequencer/State"));

   chips.push(chip("gray", "sequencer parameter nEv",
      odb.nEv === null || odb.nEv === undefined ? NO : esc(odb.nEv),
      "/PySequencer/Param/Value/nEv -- a wait time in the sequencer script, not a number of events"));

   // What is waiting and what has gone wrong, from the counts the status reply
   // already carries. A shifter should see a failed nearline job without having
   // to open the runlog and read down the column.
   if (counts) {
      const pending = Number(counts.queue_pending) || 0;
      const running = Number(counts.queue_running) || 0;
      const entries = R.countEntries(queueCounts, state.statuses);
      // By status name when the queue reply has been read; until then only the
      // total, because the status half of the counts is not a status name.
      const text = entries.length
         ? entries.map(function (e) { return e.count + " " + e.name; }).join(", ")
         : (pending + running) + " queued";
      chips.push(chip(running ? "green" : "gray", "Queue", esc(text),
         "of " + (Number(counts.runs_total) || 0) + " runs in the database"));
      const failed = Number(counts.jobs_failed) || 0;
      if (failed) {
         chips.push(chip("red", "Nearline", esc(failed + " failed"),
            (Number(counts.jobs_pending) || 0) + " jobs still pending"));
      }
   }

   let dbWord = "reachable";
   let dbClass = "green";
   if (health.clientError) { dbWord = "client not answering"; dbClass = "red"; }
   else if (health.dbError) { dbWord = "not answering"; dbClass = "red"; }
   else if (health.stale) { dbWord = "stale"; dbClass = "yellow"; }
   else if (!health.lastGood) { dbWord = "waiting for the first answer"; dbClass = "gray"; }
   chips.push(chip(dbClass, "Run database", esc(dbWord),
      health.lastGood ? "last good answer at " + health.lastGood : "nothing read yet"));

   return chips.join("");
}

function truthy(v) {
   if (v === null || v === undefined) return false;
   if (typeof v === "string") return v !== "" && v !== "0" && v.toLowerCase() !== "false" && v.toLowerCase() !== "n";
   return Boolean(Number(v)) || v === true;
}

/**
 * The one sentence that explains an idle queue. A shifter looking at eighteen
 * pending runs and nothing happening should not have to work out why.
 */
function sequencerNoteHtml(odb, queue) {
   if (truthy(odb.seqRunning)) return "";
   // Anything in the queue that has not started yet, whatever its status is
   // called: the flags say which those are.
   const counts = (queue && queue.counts) || {};
   let waiting = 0;
   Object.keys(counts).forEach(function (name) {
      if (R.isPendingStatus(name, state.statuses)) waiting += Number(counts[name]) || 0;
   });
   if (!waiting) return "";
   return '<div class="rundb-note yellow">The sequencer is not running, so nothing in the queue will start.</div>';
}

// ---------------------------------------------------------------------------
// Stale messages
// ---------------------------------------------------------------------------
//
// Two different failures, two different sentences, both naming when the data on
// screen was read. The tables stay up and go dim: the numbers may well still be
// right, they just must not read as live.

function staleHtml(st) {
   const out = [];
   const when = st.lastGood ? "The queue, runlog and sequences below were last read at " + esc(st.lastGood) + "."
                            : "Nothing has been read from the run database yet.";

   if (st.clientError) {
      out.push('<div class="rundb-alert red">' +
         "<b>The " + esc(R.getClientName()) + " client is not answering.</b> " + when +
         " Nothing is wrong with the run itself &mdash; this page only reads. " +
         "Start the client again from the Programs page; the page will pick it up on its own, with no reload." +
         '<div class="rundb-detailtext">' + esc(st.clientError.message || "") + "</div></div>");
   }
   if (st.dbError) {
      out.push('<div class="rundb-alert red">' +
         "<b>The run database did not answer.</b> " + when +
         " The client is running, so this is Postgres or the network to it." +
         '<div class="rundb-detailtext">' + esc(st.dbError.message || "") +
         (st.dbError.hint ? " &mdash; " + esc(st.dbError.hint) : "") + "</div></div>");
   }
   if (st.otherError && st.otherError.kind === "timeout") {
      // The client is there -- mhttpd reached it -- but it is not coming back.
      out.push('<div class="rundb-alert red">' +
         "<b>The " + esc(R.getClientName()) + " client is not coming back.</b> " + when +
         " It answered before, so it is running but stuck, most likely in a database query. " +
         "The page keeps asking; if this does not clear, restart the client from the Programs page." +
         '<div class="rundb-detailtext">' + esc(st.otherError.message || "") + "</div></div>");
   } else if (st.otherError) {
      out.push('<div class="rundb-alert yellow"><b>' + esc(st.otherError.kind || "error") + ".</b> " +
         esc(st.otherError.message || "") +
         (st.otherError.hint ? " &mdash; " + esc(st.otherError.hint) : "") + "</div>");
   }
   if (st.reducedTo) {
      out.push('<div class="rundb-note yellow">The reply did not fit in the buffer, so the runlog is showing ' +
         esc(st.reducedTo) + " rows. Raise /RunDBView/Max reply kB to see more at once.</div>");
   }
   return out.join("");
}

// ---------------------------------------------------------------------------
// Queue
// ---------------------------------------------------------------------------

function queueHtml(queue) {
   if (!queue) return '<div class="rundb-note">waiting for the first answer&hellip;</div>';
   const runs = queue.runs || [];
   if (!runs.length) return '<div class="rundb-note">Nothing is queued.</div>';

   // next_up is the *database id* of the run that would start next, or null
   // when nothing in the queue would start on its own (view.py sends an int).
   const nextId = (queue.next_up === null || queue.next_up === undefined)
      ? null : Number(queue.next_up);
   const rows = runs.map(function (row) {
      const isNext = nextId !== null && Number(row.id) === nextId;
      return '<tr class="' + (isNext ? "rundb-next" : "") + '">' +
         "<td>" + esc(row.position === undefined ? NO : row.position) + "</td>" +
         "<td>" + esc(row.id) + (isNext ? ' <span class="rundb-tag">next up</span>' : "") + "</td>" +
         "<td>" + esc(row.priority === null || row.priority === undefined ? NO : row.priority) + "</td>" +
         statusCellHtml(row.status) +
         '<td class="rundb-num">' + esc(R.formatCount(row.requested_events)) + "</td>" +
         configCellHtml(row) +
         sequenceCellHtml(row.sequence) +
         "</tr>";
   });

   const c = queue.counts || {};
   const sentence = queueCountSentence(c);
   return '<table class="mtable rundb-table"><tr>' +
      "<th>#</th><th>DB id</th><th>priority</th><th>status</th><th>requested events</th>" +
      "<th>configuration</th><th>sequence</th></tr>" +
      rows.join("") + "</table>" +
      (sentence ? '<div class="rundb-footnote">' + esc(sentence) + "</div>" : "");
}

/** counts keyed by status name -> "37 runs in the queue: 1 RUNNING, 35 PENDING, 1 HOLDING". */
function queueCountSentence(counts) {
   const entries = R.countEntries(counts, state.statuses);
   let total = 0;
   const parts = entries.map(function (e) { total += e.count; return e.count + " " + e.name; });
   if (!total) return "";
   return total + (total === 1 ? " run in the queue: " : " runs in the queue: ") + parts.join(", ") +
      ". The lowest priority goes first.";
}

// ---------------------------------------------------------------------------
// Runlog
// ---------------------------------------------------------------------------

const RUNLOG_COLUMNS = 11;

function runlogHtml(runs, expandedId, detail, detailError) {
   if (!runs || !runs.length) return '<div class="rundb-note">No runs in the database yet.</div>';

   const rows = runs.map(function (row) {
      const open = expandedId !== null && row.id === expandedId;
      let html = '<tr class="rundb-runrow' + (open ? " rundb-open" : "") + '" data-run-id="' + esc(row.id) + '"' +
         ' title="click for the full configuration, files and jobs of this run">' +
         "<td>" + (row.run_number === null || row.run_number === undefined
            ? '<span class="rundb-none">not taken yet</span>' : esc(row.run_number)) + "</td>" +
         "<td>" + esc(row.id) + "</td>" +
         statusCellHtml(row.status) +
         "<td>" + esc(R.formatStamp(row.started)) + "</td>" +
         "<td>" + esc(R.formatStamp(row.stopped)) + "</td>" +
         "<td>" + timesCell(row) + "</td>" +
         '<td class="rundb-num">' + esc(R.formatCount(row.requested_events)) + "</td>" +
         configCellHtml(row) +
         filesCellHtml(row.files) +
         jobsCellHtml(row.jobs) +
         sequenceCellHtml(row.sequence) +
         "</tr>";
      if (open) html += detailRowHtml(detail, detailError);
      return html;
   });

   return '<table class="mtable rundb-table"><tr>' +
      "<th>run</th><th>DB id</th><th>status</th><th>started</th><th>stopped</th><th>duration</th>" +
      "<th>requested events</th><th>configuration</th><th>files</th><th>nearline</th><th>sequence</th>" +
      "</tr>" + rows.join("") + "</table>";
}

function timesCell(row) {
   if (row.times_known === false) {
      return '<span class="rundb-none" title="start and stop come from the BOR/EOR rows in logs.slow_control">' +
         "no start/stop logged</span>";
   }
   return esc(R.formatDuration(row.duration_s));
}

// The expanded detail of one run: everything the summary left out.
function detailRowHtml(detail, detailError) {
   const open = '<tr class="rundb-detailrow"><td colspan="' + RUNLOG_COLUMNS + '">';
   if (detailError) {
      return open + '<div class="rundb-alert yellow">' + esc(detailError) + "</div></td></tr>";
   }
   if (!detail) return open + '<div class="rundb-note">reading the run&hellip;</div></td></tr>';
   return open + detailHtml(detail) + "</td></tr>";
}

function detailHtml(detail) {
   const run = detail.run || {};
   const out = [];

   out.push('<div class="rundb-detailhead">Run ' +
      (run.run_number === null || run.run_number === undefined ? "(not taken yet)" : esc(run.run_number)) +
      ", database id " + esc(run.id) + " &mdash; " + statusInline(run.status) + "</div>");

   const configs = detail.configs || [];
   if (!configs.length) {
      out.push('<div class="rundb-note">' + esc(run.config_note || "no configuration recorded for this run") + "</div>");
   } else {
      configs.forEach(function (c) {
         out.push('<div class="rundb-subhead">' + esc(c.config_type || "configuration") +
            " " + esc(c.config_id) +
            (c.do_not_use ? ' <span class="rundb-warnword">do not use</span>' : "") + "</div>");
         if (c.values === null || c.values === undefined) {
            out.push('<div class="rundb-note">the client does not know how to read a ' +
               esc(c.config_type || "configuration of this type") + " row, so its values are not shown</div>");
         } else {
            out.push(valuesTableHtml(c.values));
         }
      });
   }

   const files = detail.files || [];
   out.push('<div class="rundb-subhead">files</div>');
   if (!files.length) out.push('<div class="rundb-note">no files recorded for this run</div>');
   else {
      out.push('<table class="mtable rundb-table"><tr><th>file</th><th>producer</th><th>status</th></tr>' +
         files.map(function (f) {
            return "<tr><td>" + esc(fileName(f)) + "</td>" +
               "<td>" + esc(f.producer || NO) + "</td>" + statusCellHtml(f.status) + "</tr>";
         }).join("") + "</table>");
   }

   const jobs = detail.jobs || [];
   out.push('<div class="rundb-subhead">nearline jobs</div>');
   if (!jobs.length) out.push('<div class="rundb-note">no nearline jobs recorded for this run</div>');
   else {
      out.push('<table class="mtable rundb-table"><tr><th>job</th><th>status</th></tr>' +
         jobs.map(function (j) {
            return "<tr><td>" + esc(j.job_type || NO) + "</td>" + statusCellHtml(j.status) + "</tr>";
         }).join("") + "</table>");
   }

   const seq = detail.sequence;
   if (seq) {
      const members = countedSequence(seq);
      out.push('<div class="rundb-subhead">sequence ' + esc(seq.id) + " &mdash; " + statusInline(seq.status) +
         (seq.on_complete ? ", on complete " + esc(seq.on_complete) : "") +
         (members ? " &mdash; " + esc(members) : "") + "</div>");
      const rows = seq.runs || [];
      if (rows.length) {
         out.push('<table class="mtable rundb-table"><tr><th>DB id</th><th>run</th><th>status</th></tr>' +
            rows.map(function (m) {
               return "<tr" + (m.id === run.id ? ' class="rundb-next"' : "") + "><td>" + esc(m.id) + "</td>" +
                  "<td>" + esc(m.run_number === null || m.run_number === undefined ? NO : m.run_number) + "</td>" +
                  statusCellHtml(m.status) + "</tr>";
            }).join("") + "</table>");
      }
   }

   out.push('<div class="rundb-footnote">Same on the command line: ' +
      "<code>python -m pioneer.rundb.view run " + esc(run.id) + "</code></div>");
   return out.join("");
}

function valuesTableHtml(values) {
   const keys = Object.keys(values || {});
   if (!keys.length) return '<div class="rundb-note">no values</div>';
   const cells = keys.map(function (k) {
      const v = values[k];
      return '<div class="rundb-kv"><span class="rundb-k">' + esc(k) + '</span>' +
         '<span class="rundb-v">' + (v === null || v === undefined ? NO : esc(v)) + "</span></div>";
   });
   return '<div class="rundb-kvgrid">' + cells.join("") + "</div>";
}

// ---------------------------------------------------------------------------
// Sequences
// ---------------------------------------------------------------------------

/** The counts sentence of a `run.sequence` block, when it carries them. */
function countedSequence(seq) {
   if (!seq) return "";
   const n = Number(seq.n_runs);
   const counts = seq.counts || seq.by_status || seq.statuses;
   if (!counts || typeof counts !== "object") return "";
   const entries = R.countEntries(counts, state.statuses);
   if (!entries.length) return "";
   let total = isFinite(n) && n ? n : 0;
   if (!total) entries.forEach(function (e) { total += e.count; });
   return total + (total === 1 ? " run: " : " runs: ") +
      entries.map(function (e) { return e.count + " " + e.name; }).join(", ");
}

/**
 * One line per sequence, with the counts written out.
 *
 * This is the queued / running / done picture a shifter reads first, so the
 * numbers are a sentence rather than five columns of digits.
 */
function sequenceCountSentence(row, table) {
   const n = Number(row.n_runs) || 0;
   if (!n) return "no runs yet";
   // One count per member status, under the name the database stores.
   const counts = row.counts || row.by_status || row.statuses;
   const parts = (counts && typeof counts === "object")
      ? R.countEntries(counts, table).map(function (e) { return e.count + " " + e.name; })
      : [];
   return n + (n === 1 ? " run" : " runs") + (parts.length ? ": " + parts.join(", ") : "");
}

function sequencesHtml(rows) {
   if (!rows) return '<div class="rundb-note">waiting for the first answer&hellip;</div>';
   if (!rows.length) return '<div class="rundb-note">No sequences in the database.</div>';
   const body = rows.map(function (row) {
      let span = NO;
      if (row.first_run !== null && row.first_run !== undefined) {
         span = row.last_run !== null && row.last_run !== undefined && row.last_run !== row.first_run
            ? esc(row.first_run) + "&ndash;" + esc(row.last_run) : esc(row.first_run);
      }
      return "<tr><td>" + esc(row.id) + "</td>" +
         statusCellHtml(row.status) +
         "<td>" + esc(sequenceCountSentence(row, state.statuses)) + "</td>" +
         "<td>" + span + "</td>" +
         "<td>" + esc(row.on_complete || NO) + "</td></tr>";
   });
   return '<table class="mtable rundb-table"><tr>' +
      "<th>sequence</th><th>status</th><th>runs</th><th>run numbers</th><th>on complete</th></tr>" +
      body.join("") + "</table>" +
      '<div class="rundb-footnote">A sequence status can lag its runs until the database trigger fires, ' +
      "which is why the counts are shown beside it.</div>";
}

// ---------------------------------------------------------------------------
// Action panel
// ---------------------------------------------------------------------------
//
// Normally absent. The client has to have been started with --allow-actions
// *and* /RunDBView/Allow actions has to be true before there is anything here
// at all, and neither is the case on pinky.
//
// The numbers below are the ones actions.py enforces. They are duplicated here
// so the page can say no before a round trip, not so it can decide: every one
// of them is checked again in the client, which is what actually guards the
// database (actions.py:_check_events, _validate_configs).

const TARGET_SEQ_ID = 2;                        // actions.py TARGET_SEQ_ID
const TARGET_CONFIG_TYPE = "target_position";   // the five points; a caller may not set it
const DEFAULT_ACTION_EVENTS = 1000000;          // actions.py DEFAULT_REQUESTED_EVENTS
const MAX_ACTION_EVENTS = 1e10;                 // actions.py MAX_REQUESTED_EVENTS

/** A config type as a person would say it: pie5_epics -> "pie5 epics". */
function typeLabel(type) {
   return String(type || "configuration").replace(/_/g, " ");
}

/**
 * The configurations the pickers offer.
 *
 * They come from what the page has already read -- the queue and the runlog
 * carry `config_type`, `do_not_use` and the one-line `summary` for every
 * configuration attached to a run -- so opening the panel costs no round trips.
 * An id that has never appeared in either goes in the "other config id" field,
 * which looks it up with `config {id}`.
 *
 * target_position is left out on purpose: the five points *are* the scan, and
 * the client refuses a request that tries to set one.
 */
function configOptions(rowSets) {
   const byType = {};
   (rowSets || []).forEach(function (rows) {
      (rows || []).forEach(function (row) {
         ((row && row.configs) || []).forEach(function (cfg) {
            const type = cfg.config_type;
            if (!type || type === TARGET_CONFIG_TYPE) return;
            if (cfg.config_id === null || cfg.config_id === undefined) return;
            if (!byType[type]) byType[type] = {};
            byType[type][cfg.config_id] = {
               config_id: cfg.config_id,
               config_type: type,
               summary: cfg.summary,
               do_not_use: Boolean(cfg.do_not_use)
            };
         });
      });
   });
   return Object.keys(byType).sort().map(function (type) {
      const entries = Object.keys(byType[type])
         .map(function (id) { return byType[type][id]; })
         .sort(function (a, b) { return Number(a.config_id) - Number(b.config_id); });
      return { config_type: type, entries: entries };
   });
}

/** Is this a number of events the client would accept? */
function eventsValid(events) {
   const n = Number(events);
   return isFinite(n) && Math.floor(n) === n && n >= 1 && n <= MAX_ACTION_EVENTS;
}

function entryLabel(entry) {
   const text = summaryText(entry.summary) || ("#" + entry.config_id);
   return text + " (id " + entry.config_id + ")";
}

/**
 * The sentence above the button, and the one dlgConfirm repeats.
 *
 * It names what will exist afterwards rather than what is being sent, because
 * that is the thing a shifter can check before pressing -- and the number of
 * runs is the client's answer, not this page's guess: `preview_five_point`
 * makes every check the real call makes and counts the target positions it
 * finds. Until that has come back there is nothing truthful to promise, so the
 * button stays dead and the line says what is missing.
 */
function actionSummarySentence(selection, events, preview) {
   const types = Object.keys(selection || {}).sort();
   if (!types.length) return "Choose at least one configuration; the target positions come from the sequence itself.";
   if (!eventsValid(events)) return "Events per run must be a whole number between 1 and " + R.formatCount(MAX_ACTION_EVENTS) + ".";

   const view = preview || {};
   if (view.pending) return "Checking with the client what this would create\u2026";
   if (view.error) {
      if (view.error.kind === "denied") return "This client is not armed for actions, so it will not schedule anything.";
      if (view.error.kind === "unknown_command") {
         return "This client is too old to say what this would create, so the button stays disabled.";
      }
      return "The client could not check this request: " + (view.error.message || "no detail given") + ".";
   }
   if (!view.data) return "Waiting for the client to check this request\u2026";

   const parts = types.map(function (t) {
      return typeLabel(t) + " " + (summaryText(selection[t].summary) || ("#" + selection[t].config_id));
   });
   const withText = parts.length === 1 ? parts[0]
      : parts.slice(0, -1).join(", ") + " and " + parts[parts.length - 1];
   const n = Number(view.data.would_create_runs);
   const seq = view.data.target_seq_id === undefined ? TARGET_SEQ_ID : view.data.target_seq_id;
   return "This will schedule " + (isFinite(n) ? n : "?") + " runs (target positions of sequence " + seq +
      ") with " + withText + " at " + R.formatCount(events) + " events each, as one sequence.";
}

/** The args object for schedule_five_point, exactly as commands.py wants it. */
function actionArgs(selection, events) {
   const ids = Object.keys(selection || {}).sort().map(function (t) {
      return Number(selection[t].config_id);
   });
   return { config_ids: ids, requested_events: Number(events) };
}

function actionReady(selection, events, preview) {
   const view = preview || {};
   return Object.keys(selection || {}).length > 0 && eventsValid(events) &&
      !view.pending && !view.error && Boolean(view.data);
}

/**
 * The panel. `ui` is state.action: what is on offer, what is chosen, what the
 * free-text id field has come back with, and the last reply.
 */
function actionPanelHtml(statusData, ui) {
   const client = (statusData && statusData.client) || null;
   if (!client || !client.actions_allowed) {
      return '<div class="rundb-note">Actions are disabled on this client. ' +
         "This page reads the run database; it does not schedule runs, start runs or touch the sequencer.</div>";
   }

   const view = ui || {};
   const options = view.options || [];
   const selection = view.selection || {};
   const events = view.events === undefined ? DEFAULT_ACTION_EVENTS : view.events;
   const busy = Boolean(view.busy);

   const pickers = options.map(function (group) {
      const chosen = selection[group.config_type];
      const opts = ['<option value="">&mdash; not set &mdash;</option>'];
      group.entries.forEach(function (entry) {
         // A configuration somebody has marked do_not_use stays visible, so it
         // is clear it was not overlooked, but it cannot be picked: the client
         // refuses it anyway (actions.py:_validate_configs).
         const dnu = entry.do_not_use;
         opts.push('<option value="' + esc(entry.config_id) + '"' +
            (dnu ? ' disabled class="rundb-none"' : "") +
            (chosen && !dnu && String(chosen.config_id) === String(entry.config_id) ? " selected" : "") +
            ">" + esc(entryLabel(entry)) + (dnu ? " &mdash; do not use" : "") + "</option>");
      });
      return '<label>' + esc(typeLabel(group.config_type)) +
         ' <select data-config-type="' + esc(group.config_type) + '"' + (busy ? " disabled" : "") + ">" +
         opts.join("") + "</select></label>";
   });

   if (!pickers.length) {
      pickers.push('<span class="rundb-none">no configurations have appeared in the queue or the runlog yet &mdash; ' +
         "use the id field</span>");
   }

   const extra = view.extra || {};
   let extraNote = "";
   if (extra.pending) extraNote = '<span class="rundb-none">reading&hellip;</span>';
   else if (extra.error) extraNote = '<span class="rundb-warnword">' + esc(extra.error) + "</span>";
   else if (extra.info) {
      extraNote = '<span class="rundb-none">' + esc(typeLabel(extra.info.config_type) + ": " + entryLabel(extra.info)) +
         "</span>";
   }

   const ready = actionReady(selection, events, view.preview) && !busy;
   const sentence = actionSummarySentence(selection, events, view.preview);

   return '<div class="rundb-alert yellow"><b>This client is armed for actions.</b> ' +
      "It was started with <code>--allow-actions</code> and <code>/RunDBView/Allow actions</code> is true, " +
      "so the button below really does write to the run database. " +
      "The same thing on the command line: " +
      "<code>python -m pioneer.rundb.actions five-point --config-id ID --events N --write-dsn &hellip; --confirm</code></div>" +
      '<form class="rundb-actionform" onsubmit="return false;">' +
      pickers.join("") +
      '<label>other config id <input type="number" min="1" step="1" id="rundb-act-extra"' +
      (extra.id ? ' value="' + esc(extra.id) + '"' : "") + (busy ? " disabled" : "") +
      ' placeholder="id"></label>' + extraNote +
      '<label>events per run <input type="number" min="1" step="1" id="rundb-act-events" value="' +
      esc(events) + '"' + (busy ? " disabled" : "") + "></label>" +
      '<button type="button" id="rundb-act-go"' + (ready ? "" : " disabled") + ">" +
      (busy ? "scheduling&hellip;" : "Schedule five-point scan") + "</button>" +
      "</form>" +
      '<div class="rundb-note" id="rundb-act-summary">' + esc(sentence) + "</div>" +
      '<div id="rundb-act-result">' + actionResultHtml(view.result) + "</div>";
}

// Kinds that mean the client considered the request and turned it down before
// writing anything. Anything else -- no answer, an answer we could not read, an
// answer that did not fit -- says nothing about what happened at the far end.
const REFUSED_KINDS = { usage: true, denied: true, db: true, unknown_command: true };

function wasRefused(kind) {
   return Object.prototype.hasOwnProperty.call(REFUSED_KINDS, kind);
}

/** What came back from schedule_five_point: what exists now, or why nothing does. */
function actionResultHtml(env) {
   if (!env) return "";
   if (env.ok === false) {
      const err = env.error || {};
      const created = (err.data && err.data.created_anyway) || err.created_anyway || null;

      // Some of it was written before the failure. That is the one case a
      // shifter must not miss, so it goes first and in its own words.
      if (created && ((created.run_ids || []).length || (created.sequence_ids || []).length)) {
         const runs = created.run_ids || [];
         const seqs = created.sequence_ids || [];
         return '<div class="rundb-alert red"><b>Partially scheduled: ' +
            (runs.length ? "runs " + esc(runs.join(", ")) + " were created" : "part of the request was carried out") +
            (seqs.length ? ", in sequence " + esc(seqs.join(", ")) : "") + ".</b> " +
            "The rest failed: " + esc(err.message || "no detail given") + ". " +
            "Check the queue and cancel what should not be there before trying again." +
            (err.hint ? '<div class="rundb-detailtext">' + esc(err.hint) + "</div>" : "") + "</div>";
      }

      if (!wasRefused(err.kind)) {
         // timeout, transport, client_down, bad_reply, too_large: the request
         // may well have been carried out and only the answer went missing. A
         // write is never retried automatically and must not be re-pressed on
         // a guess.
         return '<div class="rundb-alert red"><b>The client did not answer.</b> ' +
            "The runs may still have been queued. Check the queue before pressing again." +
            '<div class="rundb-detailtext">' + esc(err.message || err.kind || "no detail given") + "</div></div>";
      }

      const denied = err.kind === "denied";
      return '<div class="rundb-alert red"><b>Nothing was scheduled.</b> ' +
         (denied ? "This client is not armed for actions, so it refused the request. "
                 : esc(err.message || "the client refused the request") + " ") +
         (denied ? '<div class="rundb-detailtext">' + esc(err.message || "") + "</div>"
                 : (err.hint ? '<div class="rundb-detailtext">' + esc(err.hint) + "</div>" : "")) +
         "</div>";
   }
   const data = env.data || {};
   const runs = data.runs || [];
   const rows = runs.map(function (run) {
      return "<tr><td>" + esc(run.run_id) + "</td><td>" + esc(run.priority) + "</td>" +
         statusCellHtml(run.status) +
         '<td class="rundb-num">' + esc(R.formatCount(run.requested_events)) + "</td>" +
         "<td>" + esc(position(run)) + "</td></tr>";
   });
   const applied = (data.configs_applied || []).map(function (c) {
      return typeLabel(c.config_type) + " #" + c.config_id;
   }).join(", ");
   return '<div class="rundb-alert yellow"><b>Scheduled.</b> ' + esc(data.message || "") +
      (applied ? " Applied: " + esc(applied) + "." : "") +
      (data.sequence_id ? " Sequence " + esc(data.sequence_id) + "." : "") + "</div>" +
      (rows.length
         ? '<table class="mtable rundb-table"><tr><th>run</th><th>priority</th><th>status</th>' +
           "<th>requested events</th><th>target position</th></tr>" + rows.join("") + "</table>"
         : "");
}

/** The target position of a created run, as the reply gives it. */
function position(run) {
   if (!run || run.xpos === null || run.xpos === undefined) return NO;
   return "x=" + run.xpos + " y=" + (run.ypos === null || run.ypos === undefined ? "?" : run.ypos);
}

// ---------------------------------------------------------------------------
// Reading the ODB
// ---------------------------------------------------------------------------

function odbFromValues(values) {
   return {
      state: values[0],
      runNumber: values[1],
      runDbPk: values[2],
      seqRunning: values[3],
      seqFinished: values[4],
      seqFile: values[5],
      nEv: values[6],
      clientName: values[7],
      pollSeconds: values[8],
      runlogRows: values[9],
      runlogRefreshSeconds: values[10],
      maxReplyKb: values[11],
      staleSeconds: values[12],
      allowActions: values[13],
      database: values[14]
   };
}

/** ODB values -> the page config, falling back to DEFAULTS key by key. */
function configFrom(odb) {
   const cfg = Object.assign({}, DEFAULTS);
   if (odb.clientName) cfg["Client name"] = String(odb.clientName);
   cfg["Poll seconds"] = positive(odb.pollSeconds, DEFAULTS["Poll seconds"]);
   cfg["Runlog rows"] = Math.min(RUNLOG_ROWS_MAX, positive(odb.runlogRows, DEFAULTS["Runlog rows"]));
   cfg["Runlog refresh seconds"] = positive(odb.runlogRefreshSeconds, DEFAULTS["Runlog refresh seconds"]);
   cfg["Max reply kB"] = positive(odb.maxReplyKb, DEFAULTS["Max reply kB"]);
   cfg["Stale seconds"] = positive(odb.staleSeconds, DEFAULTS["Stale seconds"]);
   cfg["Allow actions"] = truthy(odb.allowActions);
   if (odb.database) cfg["Database"] = String(odb.database);
   return cfg;
}

function positive(v, fallback) {
   const n = Number(v);
   return isFinite(n) && n > 0 ? n : fallback;
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

// Under node (the tests) there is no document: the render functions then do
// nothing, and the polling logic can still be exercised.
function el(id) {
   return (typeof document !== "undefined" && document.getElementById)
      ? document.getElementById(id) : null;
}

function put(id, html) {
   const node = el(id);
   if (node) node.innerHTML = html;
}

function maxBytes() { return Math.round(state.config["Max reply kB"] * 1024); }

function health() {
   const ageMs = state.lastGoodMs ? Date.now() - state.lastGoodMs : 0;
   return {
      clientError: state.clientError,
      dbError: state.dbError,
      lastGood: state.lastGood,
      stale: Boolean(state.lastGoodMs) && ageMs > state.config["Stale seconds"] * 1000
   };
}

function isStale() {
   const h = health();
   return Boolean(h.clientError || h.dbError || h.stale);
}

/**
 * Record what an envelope told us about the health of the chain.
 *
 * `isData` says whether this reply carried data from the database. It matters:
 * `status` answers ok:true with database.reachable false when Postgres is down
 * -- saying so is the whole job of that command -- so stamping "last read at"
 * from it would march the clock forward all through an outage and the tables
 * would never go dim. Only a reply that carried rows proves the chain answered.
 */
function note(env, isData) {
   if (env.ok) {
      state.clientError = null;
      state.otherError = null;
      if (isData) {
         state.dbError = null;
         state.lastGood = R.clockNow();
         state.lastGoodMs = Date.now();
      }
      return true;
   }
   const err = env.error || {};
   state.clientError = err.kind === "client_down" ? err : null;
   state.dbError = err.kind === "db" ? err : null;
   state.otherError = (err.kind === "client_down" || err.kind === "db") ? null : err;
   return false;
}

function renderStrip() {
   put("rundb-strip", stripHtml(state.odb, health(), state.status && state.status.counts,
                                state.queue && state.queue.counts));
   put("rundb-seqnote", sequencerNoteHtml(state.odb, state.queue));
}

function renderAlerts() {
   put("rundb-alerts", staleHtml(state));
   const stale = isStale();
   ["rundb-queue", "rundb-runlog", "rundb-sequences"].forEach(function (id) {
      const node = el(id);
      if (node) node.className = stale ? "rundb-body rundb-stale" : "rundb-body";
   });
   const when = el("rundb-lastread");
   if (when) {
      when.innerHTML = state.lastGood
         ? "last read at " + esc(state.lastGood) + (stale ? " &mdash; not being updated" : "")
         : "nothing read yet";
   }
}

function renderQueue() { put("rundb-queue", queueHtml(state.queue)); }
function renderSequences() { put("rundb-sequences", sequencesHtml(state.sequences)); }

function sortedRuns() {
   return Object.keys(state.runs)
      .map(function (k) { return state.runs[k]; })
      .sort(function (a, b) { return Number(b.id) - Number(a.id); });
}

function renderRunlog() {
   if (!state.haveRunlog) {
      put("rundb-runlog", '<div class="rundb-note">waiting for the first answer&hellip;</div>');
      return;
   }
   put("rundb-runlog", runlogHtml(sortedRuns(), state.expandedId, state.detail, state.detailError));
   const more = el("rundb-more");
   if (more) {
      more.innerHTML = moreHtml(state.nextBeforeId, state.runsCapped);
      const button = el("rundb-more-button");
      if (button) button.onclick = showOlder;
      const refresh = el("rundb-refresh-button");
      if (refresh) refresh.onclick = refreshNow;
   }
}

/** The Show older / Refresh line under the runlog. */
function moreHtml(nextBeforeId, capped) {
   const refresh = ' <button type="button" id="rundb-refresh-button">Refresh now</button>';
   if (capped) {
      return '<span class="rundb-none">showing the newest ' + RUNS_CAP +
         " runs &mdash; for older ones use <code>python -m pioneer.rundb.view runlog --before-id N</code></span>" +
         refresh;
   }
   if (nextBeforeId === null || nextBeforeId === undefined) {
      return '<span class="rundb-none">that is the whole runlog</span>' + refresh;
   }
   return '<button type="button" id="rundb-more-button">Show older runs</button>' + refresh;
}

function renderActions() {
   state.action.options = configOptions([queueRuns(), sortedRuns()]);
   state.action.sig = actionSignature();
   put("rundb-actions", actionPanelHtml(state.status, state.action));
   wireActionPanel();
}

/**
 * What the panel would be drawn from, as a string.
 *
 * The polls run every few seconds and must not redraw the panel underneath
 * somebody filling it in -- a select rebuilt mid-choice, or an events field
 * rewritten between two keystrokes, is how a shifter ends up scheduling the
 * wrong thing. So the polls redraw it only when this changes.
 */
function actionSignature() {
   const allowed = Boolean(state.status && state.status.client && state.status.client.actions_allowed);
   const ids = configOptions([queueRuns(), sortedRuns()]).map(function (group) {
      return group.config_type + ":" + group.entries.map(function (e) {
         return e.config_id + (e.do_not_use ? "x" : "");
      }).join(",");
   }).join("|");
   return (allowed ? "on" : "off") + " " + ids;
}

/** The poll's version: leave a panel somebody is using alone. */
function refreshActions() {
   if (state.action.busy) return;
   if (actionSignature() === state.action.sig) return;
   renderActions();
}

function queueRuns() {
   return (state.queue && state.queue.runs) || [];
}

/**
 * Hook the panel up after every render.
 *
 * The panel is small and re-rendered whole on every change, so there is one
 * place where what is on screen is decided (actionPanelHtml) and no second copy
 * of the state living in the DOM. The events field is the exception: rewriting
 * it under the cursor would eat what is being typed, so that one only updates
 * the sentence and the button.
 */
function wireActionPanel() {
   const root = el("rundb-actions");
   if (!root || !root.querySelectorAll) return;

   Array.prototype.forEach.call(root.querySelectorAll("select[data-config-type]"), function (sel) {
      sel.onchange = function () {
         chooseConfig(sel.getAttribute("data-config-type"), sel.value);
      };
   });

   const extra = el("rundb-act-extra");
   if (extra) extra.onchange = function () { lookupConfig(extra.value); };

   const events = el("rundb-act-events");
   if (events) {
      events.oninput = function () {
         state.action.events = events.value === "" ? "" : Number(events.value);
         // Rewriting the field under the cursor would eat what is being typed,
         // so only the sentence and the button move here.
         schedulePreview();
         const line = el("rundb-act-summary");
         if (line) line.textContent = actionSummarySentence(state.action.selection, state.action.events, state.action.preview);
         const go = el("rundb-act-go");
         if (go) go.disabled = !actionReady(state.action.selection, state.action.events, state.action.preview) || state.action.busy;
      };
   }

   const go = el("rundb-act-go");
   if (go) go.onclick = confirmSchedule;
}

/**
 * Choose (or clear) the configuration of one type.
 *
 * Keyed by type, so a second configuration of a type replaces the first rather
 * than being added beside it: `_validate_configs` refuses two of the same type,
 * and a form that can build a request the client will always reject is a form
 * that wastes a shifter's time.
 */
function chooseConfig(type, value) {
   const entry = findOption(type, value);
   if (entry) state.action.selection[type] = entry;
   else delete state.action.selection[type];
   state.action.result = null;
   schedulePreview();
   renderActions();
}

function findOption(type, value) {
   if (value === "" || value === null || value === undefined) return null;
   let found = null;
   state.action.options.forEach(function (group) {
      if (group.config_type !== type) return;
      group.entries.forEach(function (entry) {
         if (String(entry.config_id) === String(value) && !entry.do_not_use) found = entry;
      });
   });
   return found;
}

/**
 * Resolve an id the page has never seen, with `config {id}`.
 *
 * It is keyed into the selection by the type the client reports, so the "one
 * configuration per type" rule holds however it was chosen; a target_position
 * or a do_not_use row is refused here with the same words the client would use.
 */
async function lookupConfig(raw) {
   const id = Number(raw);
   if (!raw || !isFinite(id) || id <= 0) {
      state.action.extra = {};
      renderActions();
      return;
   }
   state.action.extra = { id: id, pending: true };
   renderActions();

   const env = await R.call("config", { id: id }, maxBytes());
   if (!note(env, true) || !env.data || !env.data.config) {
      state.action.extra = { id: id, error: (env.error && env.error.message) || "could not read that configuration" };
      renderActions();
      renderAlerts();
      return;
   }
   const cfg = env.data.config;
   if (cfg.config_type === TARGET_CONFIG_TYPE) {
      state.action.extra = { id: id,
         error: "configuration " + id + " is a " + TARGET_CONFIG_TYPE + ", which the five-point sequence sets itself" };
   } else if (cfg.do_not_use) {
      state.action.extra = { id: id, error: "configuration " + id + " is marked do not use" };
   } else {
      const entry = { config_id: cfg.config_id, config_type: cfg.config_type,
                      summary: summaryOfValues(cfg), do_not_use: false };
      state.action.extra = { id: id, info: entry };
      state.action.selection[cfg.config_type] = entry;    // one per type, this one wins
   }
   state.action.result = null;
   schedulePreview();
   renderActions();
   renderAlerts();
}

/** A one-line label for a `config {id}` reply, which carries values, not a summary. */
function summaryOfValues(cfg) {
   const values = cfg.values;
   if (!values) return typeLabel(cfg.config_type) + " #" + cfg.config_id;
   const keys = Object.keys(values).filter(function (k) { return k !== "id" && k !== "seq_id"; });
   return keys.slice(0, 3).map(function (k) { return k + "=" + values[k]; }).join(" ");
}

// Long enough that typing a six-digit number is one preview and not six.
const PREVIEW_DEBOUNCE_MS = 300;
let previewTimer = null;

/** Drop a preview that has not been sent yet (the panel is going away). */
function cancelPreview() {
   if (previewTimer) { clearTimeout(previewTimer); previewTimer = null; }
}

/**
 * Ask the client what this request would create, after a pause.
 *
 * `preview_five_point` is a read: it runs every check the write runs and counts
 * the target positions, without touching anything. The pause is so that a
 * shifter typing an events field does not send one per keystroke.
 */
function schedulePreview() {
   const selection = state.action.selection;
   const events = state.action.events;
   if (!Object.keys(selection).length || !eventsValid(events)) {
      cancelPreview();
      state.action.preview = {};
      return;
   }
   const key = JSON.stringify(actionArgs(selection, events));

   // Already asked, or already asking, about exactly this. Leave it alone --
   // in particular leave the timer armed: cancelling it here and returning
   // would strand the panel on "checking..." for ever, with the button dead.
   if (state.action.preview.key === key && (state.action.preview.data || state.action.preview.pending)) return;

   cancelPreview();
   state.action.preview = { key: key, pending: true };
   previewTimer = setTimeout(function () { previewTimer = null; runPreview(key); }, PREVIEW_DEBOUNCE_MS);
}

/** Whether a preview is waiting to be sent. For the tests. */
function previewArmed() { return previewTimer !== null; }

/** The preview call itself; `key` is what it was asked about. */
async function runPreview(key) {
   const args = JSON.parse(key);
   const env = await R.call("preview_five_point", args, maxBytes());
   if (state.action.preview.key !== key) return;        // the form moved on
   if (env.ok && env.data) state.action.preview = { key: key, data: env.data };
   else state.action.preview = { key: key, error: env.error || { kind: "internal", message: "no reply" } };
   renderActions();
}

/**
 * Ask, then schedule.
 *
 * The gate is checked again here, against the last status reply, so a client
 * that was disarmed since the panel was drawn is not asked to write anything --
 * it would refuse, but the page should not be sending it either.
 */
function confirmSchedule() {
   // A second press while the first is in flight would be a second scan.
   if (state.action.busy) return;

   const allowed = state.status && state.status.client && state.status.client.actions_allowed;
   if (!allowed) {
      state.action.result = { ok: false, error: { kind: "denied",
         message: "actions were switched off on this client since this panel was drawn" } };
      renderActions();
      return;
   }
   if (!actionReady(state.action.selection, state.action.events, state.action.preview)) return;

   const sentence = actionSummarySentence(state.action.selection, state.action.events, state.action.preview);
   const ask = esc(sentence) + "<br><br>The runs are queued, not started: the sequencer takes them when it reaches them.";

   // The form is frozen from the moment the question is on screen: what is
   // confirmed has to be what is sent.
   state.action.busy = true;
   renderActions();

   function answer(flag) {
      if (flag) { schedule(); return; }
      state.action.busy = false;
      renderActions();
   }

   if (typeof dlgConfirm === "function") dlgConfirm(ask, answer);
   else answer(typeof confirm !== "function" || confirm(sentence));
}

async function schedule() {
   state.action.busy = true;
   state.action.result = null;
   renderActions();

   const env = await R.call("schedule_five_point",
      actionArgs(state.action.selection, state.action.events), maxBytes());
   note(env, Boolean(env.ok));
   state.action.busy = false;
   state.action.result = env;
   state.action.preview = {};        // whatever was previewed has been used
   renderActions();
   renderAlerts();

   // Re-read the queue when something was created, and equally when we cannot
   // tell -- an unanswered write is exactly when a shifter needs to see what is
   // actually in the queue.
   const uncertain = env.ok === false && !wasRefused(env.error && env.error.kind);
   if ((env.ok || uncertain) && slowPoller) slowPoller.kick();
}

function renderFooter() {
   const cfg = state.config;
   put("rundb-footer",
      "Read-only view of the 2026 run database" + (cfg["Database"] ? " (" + esc(cfg["Database"]) + ")" : "") +
      ", through the " + esc(R.getClientName()) + " MIDAS client. " +
      "The same data on the command line: <code>python -m pioneer.rundb.view status|queue|runlog|sequences</code>.");
}

// ---- the three loops ----

async function pollOdb() {
   const values = await R.odb(ODB_PATHS);
   state.odb = odbFromValues(values);
   state.config = configFrom(state.odb);
   R.setClientName(state.config["Client name"]);
   slowPoller.setInterval(state.config["Poll seconds"] * 1000);
   logPoller.setInterval(state.config["Runlog refresh seconds"] * 1000);
   renderStrip();
   renderAlerts();
   renderFooter();          // it names the database and the client, both from the ODB
}

async function pollSlow() {
   const st = await R.call("status", {}, maxBytes());
   if (note(st, false) && st.data) {
      state.status = st.data;
      // An empty list is what `status` sends while the database is down; keeping
      // the last good table means the words beside the raw statuses do not
      // vanish along with it.
      const table = R.statusTable(st.data.statuses);
      if (Object.keys(table).length) state.statuses = table;
      state.dbError = databaseError(st.data);
      refreshActions();
   }
   // No point asking a client that just told us it is not there.
   if (!(st.ok === false && st.error && st.error.kind === "client_down")) {
      // The queue is short and self-limiting; the client's own default bounds it.
      const q = await R.call("queue", {}, maxBytes());
      if (note(q, true) && q.data) state.queue = q.data;
   }
   renderQueue();
   renderStrip();
   renderAlerts();
}

/**
 * The database half of a `status` reply, as an error or null.
 *
 * This is the only place an unreachable database shows up while the client
 * itself is fine: the client answers ok:true and says so in `database`.
 */
function databaseError(status) {
   const db = (status && status.database) || {};
   if (db.reachable !== false) return null;
   const client = (status && status.client) || {};
   return {
      kind: "db",
      message: "the client cannot reach " + (db.dsn || "the database"),
      hint: client.last_error || null
   };
}

async function pollLog() {
   const env = await R.call("runlog", { limit: state.config["Runlog rows"] }, maxBytes());
   if (note(env, true) && env.data) {
      mergeRuns(env.data.runs);
      // The cursor follows the newest page only until somebody has paged back:
      // after that the oldest page we hold is what "older" means.
      if (!state.paged) state.nextBeforeId = env.data.next_before_id;
      state.haveRunlog = true;
      state.reducedTo = env.limit_reduced_to || null;
   }
   const seq = await R.call("sequences", {}, maxBytes());
   if (note(seq, true) && seq.data) state.sequences = seq.data.sequences || seq.data.rows || [];
   renderRunlog();
   renderSequences();
   refreshActions();          // new configurations may have appeared in the runlog
   renderAlerts();
}

/**
 * Merge a page of runs into what is on screen, newest first, bounded.
 *
 * The whole table is re-rendered every Runlog refresh seconds, so an unbounded
 * "Show older" would make that render slower every time it was pressed. Past
 * the cap the oldest rows are dropped and the page says so.
 */
function mergeRuns(runs) {
   (runs || []).forEach(function (row) { if (row && row.id !== undefined) state.runs[row.id] = row; });
   const ids = Object.keys(state.runs).sort(function (a, b) { return Number(b) - Number(a); });
   state.runsCapped = ids.length > RUNS_CAP;
   ids.slice(RUNS_CAP).forEach(function (id) { delete state.runs[id]; });
}

async function showOlder() {
   const button = el("rundb-more-button");
   if (button) { button.disabled = true; button.textContent = "reading…"; }
   const env = await R.call("runlog",
      { limit: state.config["Runlog rows"], before_id: state.nextBeforeId }, maxBytes());
   if (note(env, true) && env.data) {
      mergeRuns(env.data.runs);
      state.nextBeforeId = env.data.next_before_id;
      state.paged = true;
      state.reducedTo = env.limit_reduced_to || null;
   }
   renderRunlog();
   renderAlerts();
}

/** The manual half of the 30 s refresh: read everything again, now. */
function refreshNow() {
   if (slowPoller) slowPoller.kick();
   if (logPoller) logPoller.kick();
}

async function toggleRun(id) {
   if (state.expandedId === id) {
      state.expandedId = null;
      state.detail = null;
      state.detailError = null;
      renderRunlog();
      return;
   }
   state.expandedId = id;
   state.detail = null;
   state.detailError = null;
   renderRunlog();
   const env = await R.call("run", { id: id }, maxBytes());
   if (state.expandedId !== id) return;          // clicked elsewhere meanwhile
   if (note(env, true) && env.data) state.detail = env.data;
   else state.detailError = "could not read run " + id + ": " +
      ((env.error && env.error.message) || "no detail");
   renderRunlog();
   renderAlerts();
}

let stripPoller = null;
let slowPoller = null;
let logPoller = null;

function init() {
   renderFooter();
   renderActions();
   renderQueue();
   renderSequences();
   renderRunlog();

   const log = el("rundb-runlog");
   if (log) {
      log.addEventListener("click", function (ev) {
         const tr = ev.target && ev.target.closest ? ev.target.closest("tr.rundb-runrow") : null;
         if (!tr) return;
         const id = Number(tr.getAttribute("data-run-id"));
         if (isFinite(id)) toggleRun(id);
      });
   }

   stripPoller = new R.Poller(pollOdb, 1000, "odb");
   slowPoller = new R.Poller(pollSlow, DEFAULTS["Poll seconds"] * 1000, "status");
   logPoller = new R.Poller(pollLog, DEFAULTS["Runlog refresh seconds"] * 1000, "runlog");

   // The ODB read is what gives us the configuration, so it goes first and the
   // other two follow one tick later with the right intervals.
   stripPoller.start();
   slowPoller.start();
   logPoller.start();
}

// ---------------------------------------------------------------------------
// Publish. `RUNDB` in a browser, module.exports under node --test.
// ---------------------------------------------------------------------------

// Lets a test watch the poller kick that follows a write. The page itself
// never calls this; `init()` builds the real pollers.
function __setSlowPollerForTests(poller) { slowPoller = poller; }

const RUNDB = {
   __setSlowPollerForTests,
   CONFIG_ROOT, DEFAULTS, ODB_PATHS, RUNLOG_ROWS_MAX, RUNLOG_COLUMNS,
   STATE_STOPPED, STATE_PAUSED, STATE_RUNNING,
   state, init, toggleRun, showOlder, refreshNow, renderActions, refreshActions,
   actionSignature, RUNS_CAP,
   // pure builders, all testable without a browser or a database
   runStateWord, summaryText, configCellHtml, filesCellHtml, jobsCellHtml, countedSequence,
   stripHtml, sequencerNoteHtml, staleHtml, queueHtml, queueCountSentence,
   runlogHtml, detailHtml, sequencesHtml, sequenceCountSentence, actionPanelHtml,
   odbFromValues, configFrom, truthy, statusCellHtml, statusInline, fileName,
   databaseError, note, moreHtml, mergeRuns,
   // the action panel
   configOptions, actionPanelHtml, actionSummarySentence, actionArgs, actionReady,
   actionResultHtml, eventsValid, typeLabel, confirmSchedule, schedule, lookupConfig,
   chooseConfig, findOption, schedulePreview, runPreview, cancelPreview, previewArmed, wasRefused,
   TARGET_SEQ_ID, TARGET_CONFIG_TYPE, DEFAULT_ACTION_EVENTS, MAX_ACTION_EVENTS,
   // loops, exported so a fixture page can drive them one step at a time
   pollOdb, pollSlow, pollLog
};

root.RUNDB = RUNDB;
if (typeof module !== "undefined" && module.exports) module.exports = RUNDB;

})(typeof globalThis !== "undefined" ? globalThis : this);
