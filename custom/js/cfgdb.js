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
   CONFIG_ROOT + "/Client name",
   CONFIG_ROOT + "/Poll seconds",
   CONFIG_ROOT + "/Runlog rows",
   CONFIG_ROOT + "/Runlog refresh seconds",
   CONFIG_ROOT + "/Max reply kB",
   CONFIG_ROOT + "/Stale seconds",
   CONFIG_ROOT + "/Allow actions",
   CONFIG_ROOT + "/Database",
   CONFIG_ROOT + "/Beamline"
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
   configuration_tables : {'target_positions' : [], 'degrader_positions' : [], 'beamline_settings' : []},
   tables: {},               // per table (target, degrader, beamline): read error, machine-written
                             // counts, and whether those rows have been read (see tableState)
   showAuto: false,          // runplan + mystery configurations shown in the tables
   gotoWatch: null,          // token of the go-to arrival watch in progress, if any
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
// Configuration Table
// ---------------------------------------------------------------------------

// Some configurations are written by machines, not people, and are not meant
// to be picked by hand:
//  - the runplan backend writes one per plan step, with a comment
//    "runplan <plan> step <n> ...";
//  - scheduled sequences (five-point scans, the tuner) write theirs through
//    add_new_configuration() without a comment, so they get its default
//    "Mystery Configuration".
// They outnumber the hand-made ones many times over (pie5_epics: 1363 of 1403
// rows in October 2026), so the page asks the client to leave them out
// (`config` with auto: "hide"), shows how many there are, and fetches them only
// when someone asks to see them (auto: "only"). The choice is kept per
// browser; it is a convenience, so a browser without storage just hides them.
//
// The client sorts rows with the same rules (pioneer/rundb/view.py,
// AUTO_KINDS), and cfgdb-auto-kinds.json holds examples both test suites
// check. Whitespace is the six ASCII characters on both sides, spelled out,
// because Python's and JavaScript's ideas of Unicode whitespace differ. A new
// kind is one more entry here and one more line in view.py; the first rule
// that matches wins.
const MYSTERY_COMMENT = "Mystery Configuration";
const AUTO_KINDS = [
   { kind: "runplan", label: "runplan",
     test: function (c) { return /^runplan[ \t\n\r\f\v]/.test(c); } },
   { kind: "mystery", label: "mystery",
     test: function (c) { return c.replace(/^[ \t\n\r\f\v]+|[ \t\n\r\f\v]+$/g, "") === MYSTERY_COMMENT; } }
];
const SHOW_AUTO_KEY = "cfgdb.showAuto";

/** "runplan", "mystery", or "" for a configuration someone wrote by hand. */
function autoKind(row) {
   const comment = row && typeof row.comment === "string" ? row.comment : "";
   for (let i = 0; i < AUTO_KINDS.length; i++) {
      if (AUTO_KINDS[i].test(comment)) return AUTO_KINDS[i].kind;
   }
   return "";
}

function isRunplanConfig(row) { return autoKind(row) === "runplan"; }

function isMysteryConfig(row) { return autoKind(row) === "mystery"; }

function autoAttr(row) {
   const kind = autoKind(row);
   return kind ? " data-auto='" + kind + "'" : "";
}

/** {kind: count} of the machine-written rows in a list of rows. */
function autoCounts(rows) {
   const counts = {};
   AUTO_KINDS.forEach(function (k) { counts[k.kind] = 0; });
   (rows || []).forEach(function (row) {
      const kind = autoKind(row);
      if (kind) counts[kind] += 1;
   });
   return counts;
}

function autoTotal(counts) {
   return AUTO_KINDS.reduce(function (n, k) { return n + (Number((counts || {})[k.kind]) || 0); }, 0);
}

/** The "go to" button of one row; none for a row marked do_not_use. */
function gotoCell(row) {
   if (row.do_not_use) return " --- ";
   return '<button type="button" class="mbutton cfg-goto" data-config="' + Number(row.config_id) +
          '" title="Load this configuration into the ODB now, without starting a run">go to</button>';
}

// ---------------------------------------------------------------------------
// "Current setting": a level of a new sequence that is not changed
// ---------------------------------------------------------------------------
//
// The first row of each table. Ticked, the sequence carries no configuration
// for that table, so the sequencer leaves the equipment where it is; the row's
// configurations are unticked and locked meanwhile. Each table needs either
// configurations or this row, so a forgotten selection is still caught.

const CURRENT_LEVELS = {
   target:   { checkbox: ".config-ckbx-target",   name: "target positions" },
   degrader: { checkbox: ".config-ckbx-degrader", name: "degrader positions" },
   beamline: { checkbox: ".config-ckbx-beam",     name: "beamline settings" }
};

/** The "current setting" row of one table, `columns` wide. */
function currentRowHtml(level, columns) {
   return '<tr class="cfg-current-row"><td> --- </td>' +
          '<td><input type="checkbox" class="config-ckbx-current" data-level="' + level + '"></td>' +
          '<td colspan="' + (columns - 2) + '"><b>current setting</b> &mdash; do not change the ' +
          CURRENT_LEVELS[level].name + ' in this sequence</td></tr>';
}

/** Levels ticked "current setting", e.g. ["degrader"]. */
function currentLevels() {
   return Array.from(document.querySelectorAll(".config-ckbx-current:checked")).map(function (box) {
      return box.dataset.level;
   });
}

/** Untick and lock a level's configurations while its "current setting" is ticked. */
function currentToggled(box) {
   document.querySelectorAll(CURRENT_LEVELS[box.dataset.level].checkbox).forEach(function (cfg) {
      if (box.checked) cfg.checked = false;
      cfg.disabled = box.checked;
   });
   updateNumRuns();
}

// The three tables. `rows` is the key in state.configuration_tables, `values`
// whether the client sends each row's values: target and degrader rows are a
// handful of numbers and the page uses them (columns, the current-position
// highlight, the five-point selection); a beamline row is ~35 EPICS channels
// the list never shows, so it comes with seq_id only and a click on the row
// reads that one configuration (`config {id}`).
const TABLES = {
   target:   { rows: "target_positions",   values: true,  columns: 7, what: "target positions" },
   degrader: { rows: "degrader_positions", values: true,  columns: 6, what: "degrader positions" },
   beamline: { rows: "beamline_settings",  values: false, columns: 5, what: "beamline settings" }
};
const LEVELS = ["target", "degrader", "beamline"];

/** A fresh state.tables entry: what the page knows about one table besides its rows. */
function tableState(table) {
   return {
      table: table,          // the config.* table name, or null when there is none
      error: null,           // the error of the first read, if it failed
      counts: {},            // machine-written rows in the table, by kind
      auto: "none",          // none | loading | loaded | error: the hidden rows
      autoError: null
   };
}

/** One row of a table. Values are carried on the row when the client sent them. */
function configRowHtml(level, row) {
   const id = Number(row.config_id);
   const v = row.values;
   const has = v !== undefined && v !== null;
   const klass = CURRENT_LEVELS[level].checkbox.slice(1);
   const key = esc(row.config_type) + ":" + id;
   const box = row.do_not_use ? " --- "
      : '<input type="checkbox" class="' + klass + '" value="' + key + '"' +
        (level === "target" ? ' id="' + key + '"' : "") + ">";
   const seq = has ? v.seq_id : row.seq_id;
   let html = "<tr" + autoAttr(row) + ' id="cfg_row' + id + '" data-config="' + id + '"' +
              (has ? " data-values='" + JSON.stringify(v).replace(/&/g, "&amp;").replace(/'/g, "&#39;") + "'" : "") +
              ">" +
              "<td>" + id + "</td>" +
              "<td>" + box + "</td>" +
              "<td>" + gotoCell(row) + "</td>" +
              "<td>" + (seq === undefined || seq === null ? " --- " : esc(seq)) + "</td>" +
              "<td>" + (row.comment ? esc(row.comment) : " --- ") + "</td>";
   if (level === "target" || level === "degrader") html += "<td>" + (has ? esc(v.xpos) : "---") + "</td>";
   if (level === "target") html += "<td>" + (has ? esc(v.ypos) : "---") + "</td>";
   return html + "</tr>";
}

const TABLE_HEADERS = {
   target:   "<tr><th>config id</th><th>select</th><th>go to</th><th>seq_id</th><th>comment</th><th>xpos</th><th>ypos</th></tr>",
   degrader: "<tr><th>config id</th><th>select</th><th>go to</th><th>seq_id</th><th>comment</th><th>xpos</th></tr>",
   beamline: "<tr><th>config id</th><th>select</th><th>go to</th><th>seq_id</th><th>comment</th></tr>"
};

/**
 * "kind: message (sizes) -- hint" for an error envelope or the page's own error
 * object. A too_large reply names the sizes, and its hint is the knob that
 * actually helps here: `config` takes no row limit to ask for fewer rows with.
 */
function configErrorText(err) {
   err = err || {};
   let size = "";
   let hint = err.hint;
   if (err.kind === "too_large") {
      if (err.needed) {
         size = " (the reply needed " + Math.ceil(Number(err.needed) / 1024) + " kB" +
                (err.limit ? ", the buffer was " + Math.round(Number(err.limit) / 1024) + " kB" : "") + ")";
      }
      hint = "raise " + CONFIG_ROOT + "/Max reply kB (the page retries once at four times that)";
   }
   return esc(err.kind || "error") + ": " + esc(err.message || "no answer") + size +
          (hint ? " &mdash; " + esc(hint) : "");
}

/**
 * An error as a box above a table. The table stays, with its "current
 * setting" row, so a sequence that leaves this level alone can still be
 * scheduled.
 */
function configErrorHtml(what, err) {
   return '<div class="rundb-alert red cfg-table-error"><b>Could not read the ' + esc(what) + ".</b> " +
          configErrorText(err) + "</div>";
}

/** One table: heading link, error if any, header, "current setting" row, rows, show/hide line. */
function configSectionHtml(level, title, href, rows, ts) {
   return '<h3 class="rundb-h"><a href="' + href + '"> ' + esc(title) + " </a></h3>" +
          (level === "target" ? '<input type="checkbox" id="5p_with_merge"> Run 5 point sequence with merging.' : "") +
          (ts && ts.error ? configErrorHtml(TABLES[level].what, ts.error) : "") +
          '<table class="mtable rundb-table" id="cfg-table-' + level + '">' +
          TABLE_HEADERS[level] +
          currentRowHtml(level, TABLES[level].columns) +
          (rows || []).map(function (row) { return configRowHtml(level, row); }).join("") +
          "</table>" +
          '<div class="rundb-note cfg-auto-toggle" data-level="' + level + '"></div>';
}

function configTableHtml(configuration_tables, tables) {
   if (!configuration_tables) return '<div class="rundb-note">waiting for the first answer&hellip;</div>';
   tables = tables || {};
   const beamline = configuration_tables.beamline || {};

   // submit area
   const submit_area = '<table  class="mtable rundb-table">'+
         '<tr><td>Number of runs</td><td id="submit_num_runs">0</td></tr>'+
         '<tr><td>Description</td><td><textarea id="submit_description" cols="100" rows="10"></textarea></td></tr>' +
         '<tr><td>Number of events</td><td><input type="text" id="submit_events"></td></tr>' +
         '<tr><td>Operator Name</td><td><input type="text" id= "submit_operator_name"></input></td></tr>' +
         '<tr><td>Confirm number of runs</td><td><input type="text" id="submit_confirm_runs"></td></tr>' +
         '<tr><td>Schedule the Runs</td><td><button class="dlgButtonDefault" id="submit_config"> schedule </button></td></tr>' +
         '</table>';

   return '<div id="cfg-goto-status"></div>' +
          configSectionHtml("target", "Target Positions",
             "http://localhost:8080/?cmd=ODB&odb_path=%2FEquipment%2FXYTable%2FVariables",
             configuration_tables.target_positions, tables.target) +
          '<div id="target-add-line"></div>' +

          configSectionHtml("degrader", "Degrader Positions",
             "http://localhost:8080/?cmd=ODB&odb_path=%2FEquipment%2FDegrader%2FVariables",
             configuration_tables.degrader_positions, tables.degrader) +
          '<div id="degrader-add-line"></div>' +

          configSectionHtml("beamline", (beamline.name || "Unknown") + " Beamline",
             "http://localhost:8080/?cmd=ODB&odb_path=%2FEquipment%2FEPICS",
             configuration_tables.beamline_settings, tables.beamline) +
          '<div id="beam_add_line"></div>' +

          '<h3 class="rundb-h"> Submit new Sequences </h3>'+
          submit_area;
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
      clientName: values[6],
      pollSeconds: values[7],
      runlogRows: values[8],
      runlogRefreshSeconds: values[9],
      maxReplyKb: values[10],
      staleSeconds: values[11],
      allowActions: values[12],
      database: values[13],
      beamline: values[14]
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

function readShowAuto() {
   try { return root.localStorage.getItem(SHOW_AUTO_KEY) === "1"; }
   catch (err) { return false; }
}

function writeShowAuto(show) {
   try { root.localStorage.setItem(SHOW_AUTO_KEY, show ? "1" : "0"); }
   catch (err) { /* no storage: the choice lasts until the page is reloaded */ }
}

/** "3 runplan + 12 mystery configurations", leaving out a kind with none. */
function autoCountText(counts) {
   const parts = [];
   AUTO_KINDS.forEach(function (k) {
      const n = Number((counts || {})[k.kind]) || 0;
      if (n) parts.push(n + " " + k.label);
   });
   return parts.join(" + ") + " configuration" + (autoTotal(counts) === 1 ? "" : "s");
}

/**
 * The line under one table about its machine-written rows. `ts` is the
 * table's state.tables entry, `show` whether they are asked for.
 */
function autoToggleText(ts, show) {
   const what = autoCountText(ts.counts);
   if (!show) return what + ' hidden &mdash; <a href="#" class="cfg-auto-switch">show</a>';
   if (ts.auto === "loading") return "Loading " + what + "&hellip;";
   if (ts.auto === "error") {
      return '<span class="cfg-auto-error"><b>Could not load the ' + what + ".</b> " +
             configErrorText(ts.autoError) + "</span>" +
             ' &mdash; <a href="#" class="cfg-auto-retry">try again</a>' +
             ' &mdash; <a href="#" class="cfg-auto-switch">hide</a>';
   }
   return "Showing " + what + ' &mdash; <a href="#" class="cfg-auto-switch">hide</a>';
}

/**
 * Show or hide the machine-written rows that have been read, and redo the
 * lines under the tables. Hiding a row also unticks it: a run count that
 * includes rows nobody can see would be a trap.
 */
function applyAutoVisibility() {
   const show = state.showAuto;
   document.querySelectorAll("#rundb-configs tr[data-auto]").forEach(function (row) {
      row.style.display = show ? "" : "none";
      if (!show) row.querySelectorAll("input[type=checkbox]:checked").forEach(function (box) {
         box.checked = false;
      });
   });
   document.querySelectorAll("#rundb-configs .cfg-auto-toggle").forEach(function (node) {
      const ts = state.tables[node.dataset.level];
      const total = ts ? autoTotal(ts.counts) : 0;
      node.style.display = total ? "" : "none";
      // A failed read is an error box like the ones above the tables, not a note.
      const failed = Boolean(ts && show && ts.auto === "error");
      node.classList.toggle("rundb-note", !failed);
      node.classList.toggle("rundb-alert", failed);
      node.classList.toggle("red", failed);
      node.innerHTML = total ? autoToggleText(ts, show) : "";
   });
   updateNumRuns();
}

/**
 * Read the machine-written rows of one table, once, and put them into it in
 * id order. Rows already on the page keep their ticks; the new ones are
 * locked like the others when their level is "current setting" (or, for the
 * targets, while the five-point box is ticked). A failure says so under the
 * table and leaves everything else as it was.
 */
async function loadAutoRows(level) {
   const ts = state.tables[level];
   if (!ts || !ts.table || ts.error || !autoTotal(ts.counts)) return;
   if (ts.auto === "loading" || ts.auto === "loaded") return;
   ts.auto = "loading";
   ts.autoError = null;
   applyAutoVisibility();

   const env = await R.call("config", { id: ts.table, auto: "only", values: TABLES[level].values }, maxBytes());
   if (!env || !env.ok || !env.data || !Array.isArray(env.data.rows)) {
      ts.auto = "error";
      ts.autoError = olderClientHint((env && env.error) || { kind: "bad_reply", message: "the reply carried no rows" });
      applyAutoVisibility();
      return;
   }
   insertRows(level, env.data.rows);
   if (env.data.auto_counts) ts.counts = env.data.auto_counts;
   ts.auto = "loaded";
   applyAutoVisibility();
}

/** Merge rows into a table that is already on the page, in config id order. */
function insertRows(level, rows) {
   const key = TABLES[level].rows;
   const list = state.configuration_tables[key] || [];
   const seen = {};
   list.forEach(function (row) { seen[row.config_id] = true; });
   const fresh = rows.filter(function (row) { return !seen[row.config_id]; })
                     .sort(function (a, b) { return a.config_id - b.config_id; });
   state.configuration_tables[key] = list.concat(fresh).sort(function (a, b) { return a.config_id - b.config_id; });

   const table = el("cfg-table-" + level);
   if (!table) return;
   const body = table.tBodies[0] || table;
   const existing = Array.from(body.querySelectorAll("tr[data-config]"));
   let at = 0;
   fresh.forEach(function (row) {
      while (at < existing.length && Number(existing[at].dataset.config) < row.config_id) at++;
      const html = configRowHtml(level, row);
      if (at < existing.length) existing[at].insertAdjacentHTML("beforebegin", html);
      else body.insertAdjacentHTML("beforeend", html);
   });

   const current = document.querySelector('.config-ckbx-current[data-level="' + level + '"]');
   const fivePoint = level === "target" && el("5p_with_merge") && el("5p_with_merge").checked;
   if ((current && current.checked) || fivePoint) {
      body.querySelectorAll(CURRENT_LEVELS[level].checkbox).forEach(function (box) { box.disabled = true; });
   }
}

/** Turn the machine-written rows on or off; reading them the first time they are wanted. */
function setShowAuto(show) {
   state.showAuto = show;
   writeShowAuto(show);
   applyAutoVisibility();
   if (show) loadAllAutoRows();
}

async function loadAllAutoRows() {
   for (let i = 0; i < LEVELS.length; i++) await loadAutoRows(LEVELS[i]);
}

function updateNumRuns() {
   // A level kept at its current setting counts once; all three kept is no run at all.
   const current = currentLevels();
   let runs = current.length === Object.keys(CURRENT_LEVELS).length ? 0 : 1;
   Object.keys(CURRENT_LEVELS).forEach(function (level) {
      if (current.indexOf(level) < 0) {
         runs *= document.querySelectorAll(CURRENT_LEVELS[level].checkbox + ":checked").length;
      }
   });
   document.getElementById("submit_num_runs").textContent = runs;
}

// ---------------------------------------------------------------------------
// Go to: load one configuration into the ODB without starting a run
// ---------------------------------------------------------------------------
//
// Two calls to the client: goto_preview says what would change, the shifter
// confirms, goto_config writes the Demand values (the sequencer's own setters)
// and returns at once with the conditions the sequencer would have waited for.
// The page then watches those until the hardware is there. A run in progress
// or a running sequencer does not stop a load; it shows up as a warning.

const GOTO_POLL_MS = 1000;
const GOTO_SHOWN_CHANGES = 40;    // the confirm dialog lists at most this many

function gotoNumber(v) {
   const n = Number(v);
   return Number.isFinite(n) ? String(Math.round(n * 1e4) / 1e4) : esc(v);
}

/** The confirm dialog body for a goto_preview reply. */
function gotoConfirmHtml(p) {
   const changed = (p.changes || []).filter(function (c) { return c.changes; });
   const same = (p.changes || []).length - changed.length;
   let html = "<b>Load configuration " + Number(p.config_id) + " (" + esc(p.config_type) +
              ") into the ODB now?</b><br>No run is started.";
   (p.warnings || []).forEach(function (w) {
      html += '<div class="rundb-alert red" style="text-align:left">' +
              '<span class="rundb-warnword">Warning:</span> ' + esc(w) + "</div>";
   });
   if (!changed.length) {
      html += '<div class="rundb-note">Every setting is already at this configuration.</div>';
   } else {
      html += '<table class="mtable rundb-table" style="margin:8px auto">' +
              "<tr><th>setting</th><th>now</th><th>new</th></tr>";
      changed.slice(0, GOTO_SHOWN_CHANGES).forEach(function (c) {
         html += "<tr><td>" + esc(c.name) + "</td><td>" + gotoNumber(c.now) +
                 "</td><td><b>" + gotoNumber(c.new) + "</b></td></tr>";
      });
      html += "</table>";
      if (changed.length > GOTO_SHOWN_CHANGES) {
         html += '<div class="rundb-note">and ' + (changed.length - GOTO_SHOWN_CHANGES) +
                 " more changes</div>";
      }
   }
   if (same) html += '<div class="rundb-note">' + same + " setting" + (same === 1 ? "" : "s") +
                     " already there</div>";
   return html;
}

/** "/a/b[3]" -> ["/a/b", 3]; "/a/b" -> ["/a/b", null]. */
function splitIndex(path) {
   const m = /^(.*)\[(\d+)\]$/.exec(path);
   return m ? [m[1], Number(m[2])] : [path, null];
}

/** How many of the arrival conditions the ODB values meet. `read` maps base path to value. */
function arrivalCount(arrival, read) {
   let met = 0;
   (arrival || []).forEach(function (r) {
      const parts = splitIndex(r.path);
      let v = read[parts[0]];
      if (parts[1] !== null) v = Array.isArray(v) ? v[parts[1]] : undefined;
      if (v === undefined || v === null) return;
      const ok = r.op === "=="
         ? Number(v) === Number(r.target)
         : Number(r.target) <= Number(v) && Number(v) <= Number(r.upper);
      if (ok) met++;
   });
   return met;
}

function gotoStatus(klass, html) {
   put("cfg-goto-status", '<div class="rundb-alert ' + klass + '">' + html + "</div>");
}

/** Watch the conditions goto_config returned until they hold, or give up. */
async function watchArrival(done) {
   const token = {};
   state.gotoWatch = token;
   const what = "configuration " + Number(done.config_id) + " (" + esc(done.config_type) + ")";
   const arrival = done.arrival || [];
   const bases = Array.from(new Set(arrival.map(function (r) { return splitIndex(r.path)[0]; })));
   const stableMs = (Number(done.stable_for) || 0) * 1000;
   const timeoutMs = (Number(done.timeout) || 60) * 1000;
   const t0 = Date.now();
   let allSince = null;
   while (state.gotoWatch === token) {
      let read = {};
      try {
         const values = await R.odb(bases);
         bases.forEach(function (b, i) { read[b] = values[i]; });
      } catch (err) { read = {}; }
      if (state.gotoWatch !== token) return;
      const met = arrivalCount(arrival, read);
      const secs = Math.round((Date.now() - t0) / 1000);
      if (met === arrival.length) {
         if (allSince === null) allSince = Date.now();
         if (Date.now() - allSince >= stableMs) {
            gotoStatus("", "Reached " + what + " after " + secs + " s.");
            return;
         }
      } else {
         allSince = null;
      }
      if (Date.now() - t0 > timeoutMs && met < arrival.length) {
         gotoStatus("red", "Not at " + what + " after " + secs + " s: " + met + " of " + arrival.length +
                    " readbacks in tolerance. The Demand values are set; check the equipment pages.");
         return;
      }
      gotoStatus("yellow", "Going to " + what + "&hellip; " + met + " of " + arrival.length +
                 " readbacks in tolerance (" + secs + " s)");
      await new Promise(function (resolve) { setTimeout(resolve, GOTO_POLL_MS); });
   }
}

async function gotoClicked(configId) {
   const pre = await R.call("goto_preview", { config_id: configId }, maxBytes());
   if (!pre || !pre.ok) {
      dlgAlert("Cannot go to configuration " + configId + ": " +
               esc((pre && pre.error && pre.error.message) || "no answer"));
      return;
   }
   dlgConfirm(gotoConfirmHtml(pre.data), async function (yes) {
      if (!yes) return;
      const done = await R.call("goto_config", { config_id: configId }, maxBytes());
      if (!done || !done.ok) {
         state.gotoWatch = null;
         gotoStatus("red", "Loading configuration " + configId + " failed: " +
                    esc((done && done.error && done.error.message) || "no answer"));
         return;
      }
      pollOdb();      // the current-position highlight follows the new Demand
      watchArrival(done.data);
   });
}

/** The config.* table of the beamline the ODB names, or null. */
function beamlineTable(name) {
   if (name == "PiM1") return "pim1_epics";
   if (name == "PiE5") return "pie5_epics";
   return null;
}

/** Why there is no beamline table to read, as an error the section can show. */
function noBeamlineError(name) {
   return {
      kind: "odb",
      message: CONFIG_ROOT + "/Beamline is " +
               (name === null || name === undefined || name === "" ? "not set" : '"' + String(name) + '"') +
               "; expected PiM1 or PiE5",
      hint: "set it in the ODB and reload the page"
   };
}

/**
 * Read one table without its machine-written rows, into
 * state.configuration_tables and state.tables. Never throws: a failed read is
 * kept as the table's error and the page renders around it.
 */
async function readTable(level, table) {
   const ts = tableState(table);
   state.tables[level] = ts;
   state.configuration_tables[TABLES[level].rows] = [];
   if (!table) return;
   const env = await R.call("config", { id: table, auto: "hide", values: TABLES[level].values }, maxBytes());
   if (!env || !env.ok || !env.data || !Array.isArray(env.data.rows)) {
      ts.error = olderClientHint((env && env.error) || { kind: "bad_reply", message: "the reply carried no rows" });
      return;
   }
   state.configuration_tables[TABLES[level].rows] = env.data.rows;
   ts.counts = env.data.auto_counts || {};
}

/**
 * A client that predates `auto` and `values` refuses them as unknown
 * arguments; say what to do about it rather than what it said.
 */
function olderClientHint(err) {
   if (err && err.kind === "usage" && /does not take/.test(err.message || "")) {
      return Object.assign({}, err, {
         hint: "the " + R.getClientName() + " client is older than this page: restart it from the Programs page"
      });
   }
   return err;
}

/** The values of one configuration, four name/value pairs to a line. */
function valuesTableHtml(values) {
   const entries = Object.entries(values || {});
   let html = '<table  class="mtable rundb-table">';
   for (let i = 0; i < entries.length; i += 4) {
      html += "<tr>";
      for (let j = 0; j < 4; j++) {
         if (i + j < entries.length) {
            const key = entries[i + j][0];
            const value = entries[i + j][1];
            const padding = j > 0 ? "padding: 4px 8px 4px 30px" : "padding: 4px 8px";
            html += "<td style='" + padding + "'><b>" + esc(key) + "</b></td>";
            html += "<td style='padding: 4px 8px'>" + esc(value) + "</td>";
         } else {
            html += "<td></td><td></td>";
         }
      }
      html += "</tr>";
   }
   return html + "</table>";
}

/** A beamline row carries no values on the page; read them when it is clicked. */
async function showValuesOf(configId) {
   const env = await R.call("config", { id: configId }, maxBytes());
   if (!env || !env.ok || !env.data || !env.data.config) {
      const err = (env && env.error) || {};
      dlgAlert("Cannot read configuration " + configId + ": " + esc(err.message || "no answer") +
               (err.hint ? " &mdash; " + esc(err.hint) : ""));
      return;
   }
   const cfg = env.data.config;
   if (!cfg.values) {
      dlgAlert("Configuration " + configId + " (" + esc(cfg.config_type) + ") has no table of values.");
      return;
   }
   dlgAlert(valuesTableHtml(cfg.values));
}

// renderConfigurations is only called asyncronously upon loading the page.
// Updates are going to be rare enough such that reloading the page is acceptable.
async function renderConfigurations() {
   await pollOdb() // Poll ODB first to load configuration required here
   const beamline = state.odb.beamline;
   const beamtable = beamlineTable(beamline);
   state.configuration_tables = {
      "target_positions" : [],
      "degrader_positions" : [],
      "beamline_settings" : [],
      "beamline" : {"name" : beamline, "table" : beamtable }
   };
   await readTable("target", "target_position");
   await readTable("degrader", "degrader_position");
   await readTable("beamline", beamtable);
   if (!beamtable) state.tables.beamline.error = noBeamlineError(beamline);
   put("rundb-configs", configTableHtml(state.configuration_tables, state.tables));

   state.showAuto = readShowAuto();
   applyAutoVisibility();
   if (state.showAuto) loadAllAutoRows();

   // Delegated, so rows read later (the machine-written ones) need no wiring.
   el("rundb-configs").addEventListener("click", function (e) {
      const button = e.target.closest(".cfg-goto");
      if (button) { e.preventDefault(); gotoClicked(Number(button.dataset.config)); return; }
      if (e.target.matches(".cfg-auto-switch")) {
         e.preventDefault();
         setShowAuto(!state.showAuto);
         return;
      }
      if (e.target.matches(".cfg-auto-retry")) {
         e.preventDefault();
         loadAutoRows(e.target.closest(".cfg-auto-toggle").dataset.level);
      }
   });
   el("rundb-configs").addEventListener("change", function (e) {
      if (e.target.matches(".config-ckbx-current")) { currentToggled(e.target); return; }
      if (e.target.matches(".config-ckbx-target, .config-ckbx-degrader, .config-ckbx-beam")) updateNumRuns();
   });

   document.getElementById("5p_with_merge").addEventListener("change", function() {
      const enabled = this.checked;
      if (enabled) {
         for (let index = 0; index < state.configuration_tables.target_positions.length; index++) {
            const element = state.configuration_tables.target_positions[index];
            let el = document.getElementById(element.config_type + ":" + element.config_id)
            if (!el) continue;      // marked do_not_use: no checkbox
            if (element.values && element.values.seq_id == 2) {
               el.checked = true;
            } else {
               el.checked = false;
            }
         }
         document.querySelectorAll(".config-ckbx-target").forEach(function (checkbox) {
            checkbox.disabled = true;
         });
         document.querySelectorAll(".config-ckbx-current").forEach(function (box) {
            if (box.dataset.level == 'target') {
               box.disabled = true;
               box.checked = false;
            };
         });
      } else {
         document.querySelectorAll(".config-ckbx-target").forEach(function (checkbox) {
            checkbox.disabled = false;
         });
         document.querySelectorAll(".config-ckbx-current").forEach(function (box) {
            if (box.dataset.level == 'target') {
               box.disabled = false;
            };
         });
      }
   })

   document.getElementById("submit_config").addEventListener("click", async function() {
      const selected = Array.from(
         document.querySelectorAll(".config-ckbx-target:checked, .config-ckbx-degrader:checked, .config-ckbx-beam:checked")
      ).map(function(checkbox) {
         return checkbox.value;
      });

      // safety catch
      const numRuns = document.getElementById("submit_num_runs").textContent;
      const confirmedRuns = document.getElementById("submit_confirm_runs").value;
      const operator = document.getElementById("submit_operator_name").value;
      const description = document.getElementById("submit_description").value;
      const numEv = document.getElementById("submit_events").value
      const merge = document.getElementById("5p_with_merge").value

      // hard fail points, no recovery
      if (numRuns == "0") {
         dlgAlert("Can't schedule 0 runs. In each table select at least one configuration or tick \"current setting\"; " +
                  "at least one table must change.")
         return
      } else if (String(numRuns) != String(confirmedRuns)) {
         dlgAlert("Number of runs does not match confirmation.");
         return;
      } else if (!Number.isFinite(Number(numEv)) || Number(numEv) < 1) {
         dlgAlert("Number of events must be a positive number.");
         return;
      } else if (typeof operator !== "string" || operator.trim() === "") {
         dlgAlert("Please specify operator")
         return;
      } else if (typeof description !== "string" || description.trim() === "") {
         dlgAlert("Please provide a description")
         return;
      } else {
         dlgQuery("Confirm scheduling " + numRuns + " runs. Enter shifter password.</br></br> Password: ", "", async function(resp, param) {
            if (resp) {
               try {
                  const done = await R.call("generate_sequence", {
                     "config" : selected,
                     "current" : currentLevels(),
                     "events" : numEv,
                     "operator" : operator,
                     "description" : description,
                     "merge" : merge,
                     "password" : resp});
                  if (!done || !done.ok) {
                     dlgAlert("Scheduling failed: " + esc((done && done.error && done.error.message) || "no answer"));
                  } else {
                     dlgAlert("Scheduled " + ((done.data && done.data.runs) || []).length + " runs.");
                  }
               } catch(err) {
                  console.error(err);
               }
            }
         });
      }

   });
   document.addEventListener("click", function(e) {
      if (e.target.matches("input[type=checkbox]") || e.target.closest(".cfg-goto")) return;
      const row = e.target.closest("#rundb-configs tr[data-config]");
      if (!row) return;
      if (row.dataset.values !== undefined) {
         dlgAlert(valuesTableHtml(JSON.parse(row.dataset.values)));
      } else {
         showValuesOf(Number(row.dataset.config));
      }
   });
   await pollOdb() // Poll ODB again to check against RPC loaded configurations.
}

function renderFooter() {
   const cfg = state.config;
   put("rundb-footer",
      "Read-only view of the 2026 run database" + (cfg["Database"] ? " (" + esc(cfg["Database"]) + ")" : "") +
      ", through the " + esc(R.getClientName()) + " MIDAS client. " +
      "The same data on the command line: <code>python -m pioneer.rundb.view status|queue|runlog|sequences</code>.");
}

// ---- the three loops ----

async function check_xy_table() {
   const xydemand = await R.odb(["/Equipment/XYTable/Variables/Demand"])
   let found_match = null
   for (let index = 0; index < state.configuration_tables.target_positions.length; index++) {
      const element = state.configuration_tables.target_positions[index];
      const row = document.getElementById("cfg_row" + element.config_id);
      if (element.values &&
          element.values.xpos == xydemand[0][0] &&
          element.values.ypos == xydemand[0][1] &&
          element.do_not_use == false)
      {
         if (row) {
            row.classList.add('marked-row');
            found_match = element.config_id;
         }
      } else {
         if (row) {
            row.classList.remove('marked-row');
         }
      }
   }
   if (found_match) {
      put("target-add-line", "Currently in ODB: " + found_match)
   } else {
      put("target-add-line", "The option to add a line should appear here")
   }
}


async function check_degrader() {
   const demand = await R.odb(["/Equipment/Degrader/Variables/Demand"])
   let found_match = null;
   for (let index = 0; index < state.configuration_tables.degrader_positions.length; index++) {
      const element = state.configuration_tables.degrader_positions[index];
      const row = document.getElementById("cfg_row" + element.config_id);
      if (element.values && element.values.xpos == demand[0] && element.do_not_use == false) {
         if (row) {
            row.classList.add('marked-row');
            found_match = element.config_id;
         }
      } else {
         if (row) {
            row.classList.remove('marked-row');
         }
      }
   }
   if (found_match) {
      put("degrader-add-line", "Currently in ODB: " + found_match)
   } else {
      put("degrader-add-line", "The option to add a line should appear here")
   }
}

async function pollOdb() {
   const values = await R.odb(ODB_PATHS);
   state.odb = odbFromValues(values);
   state.config = configFrom(state.odb);
   R.setClientName(state.config["Client name"]);
   renderStrip();
   renderAlerts();
   renderFooter();
   check_xy_table();
   check_degrader();
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


let stripPoller = null;

function init() {
   renderFooter();
   renderConfigurations();


   stripPoller = new R.Poller(pollOdb, 1000, "odb");

   // The ODB read is what gives us the configuration, so it goes first and the
   // other two follow one tick later with the right intervals.
   stripPoller.start();
}

// ---------------------------------------------------------------------------
// Publish. `CFGDB` in a browser, module.exports under node --test.
// ---------------------------------------------------------------------------

const CFGDB = {
   CONFIG_ROOT, DEFAULTS, ODB_PATHS,
   // pure builders, all testable without a browser or a database
   runStateWord, stripHtml, sequencerNoteHtml, staleHtml,
   databaseError, note, init, isRunplanConfig, isMysteryConfig, autoCountText,
   AUTO_KINDS, autoKind, autoCounts, autoTotal, autoToggleText, tableState,
   configRowHtml, configErrorHtml, configTableHtml, beamlineTable, noBeamlineError, valuesTableHtml,
   state, readTable,
   gotoConfirmHtml, arrivalCount, splitIndex,
   // loops, exported so a fixture page can drive them one step at a time
   pollOdb
};

root.CFGDB = CFGDB;
if (typeof module !== "undefined" && module.exports) module.exports = CFGDB;

})(typeof globalThis !== "undefined" ? globalThis : this);
