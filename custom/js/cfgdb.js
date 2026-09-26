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
   configurations : null,
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

function configTableHtml(configuration_tables) {
   if (!configuration_tables) return '<div class="rundb-note">waiting for the first answer&hellip;</div>';

   // Render target positions
   const target_header = "<tr><th>config id</th><th>comment</th><th>select</th><th>seq_id</th><th>xpos</th><th>ypos</th></tr>"
   const target_body = configuration_tables.target_positions.map(function(row) {
      const do_not_use_cell = row.do_not_use
            ? " --- " : '<input type="checkbox" class="config-ckbx-target" value="' + row.config_type + ":" + row.config_id + '">';
      return "<tr>" +
              "<td>" + row.config_id + "</td>" +
              "<td>" + (row.comment ? row.comment : " --- ") + "</td>" +
              "<td>" + do_not_use_cell + "</td>" +
              "<td>" + (row.values ? row.values.seq_id : "---") + "</td>" +
              "<td>" + (row.values ? row.values.xpos : "---") + "</td>" +
              "<td>" + (row.values ? row.values.ypos : "---") + "</td>" +
              "</tr>"

   });

   // Render target positions
   const degrader_header = '<tr><th>config id</th><th>comment</th><th>select</th><th>seq_id</th><th>xpos</th></tr>'
   const degrader_body = configuration_tables.degrader_positions.map(function(row) {
      const do_not_use_cell = row.do_not_use
            ? " --- " : '<input type="checkbox" class="config-ckbx-degrader"  value="' + row.config_type + ":" + row.config_id + '">';
       return "<tr>" +
              "<td>" + row.config_id + "</td>" +
              "<td>" + (row.comment ? row.comment : " --- ") + "</td>" +
              "<td>" + do_not_use_cell + "</td>" +
              "<td>" + (row.values ? row.values.seq_id : "---") + "</td>" +
              "<td>" + (row.values ? row.values.xpos : "---") + "</td>" +
              "</tr>"

   });

   const beamline_header = '<tr><th>config id</th><th>select</th><th>seq_id</th><th>comment</th></tr>'
   const beamline_body   = configuration_tables.beamline_settings.map(function(row) {
      const do_not_use_cell = row.do_not_use
            ? " --- " : '<input type="checkbox" class="config-ckbx-beam"  value="' + row.config_type + ":" + row.config_id + '">';
       return "<tr>" +
              "<td>" + row.config_id + "</td>" +
              "<td>" + do_not_use_cell + "</td>" +
              "<td>" + (row.values ? row.values.seq_id : " ---" ) + "</td>" +
              "<td>" + (row.comment ? row.comment : " --- ") + "</td>" +
              "</tr>"

   });

   // submit area
   //const submit_area = 'Number of Events: <input type="text" id="submit_events></input> <button id="submit_config"> schedule </button>';
   const submit_area = '<table  class="mtable rundb-table">'+
         '<tr><td>Number of runs</td><td id="submit_num_runs"> 0 </td></tr>'+
         '<tr><td>Number of events</td><td><input type="text" id="submit_events"></td></tr>' +
         '<tr><td>Confirm number of runs</td><td><input type="text" id="submit_confirm_runs"></input></td></tr>' +
         '<tr><td>Schedule the Runs</td><td><button id="submit_config"> schedule </button></td></tr>' +
         '</table>';

   return '<h3 class="rundb-h"> Target Positions </h3>' +
          '<table class="mtable rundb-table">' +
          target_header +
          target_body.join("") +
          "</table>" +

         '<h3 class="rundb-h"> Degrader Positions </h3>'+
         '<table class="mtable rundb-table">' +
         degrader_header +
         degrader_body.join("") +
         "</table>" +

         '<h3 class="rundb-h">' + configuration_tables.beamline.name + ' Beamline </h3>'+
         '<table class="mtable rundb-table">' +
         beamline_header +
         beamline_body.join("") +
         "</table>" +

         '<table class="mtable rundb-table">' +
         '<h3 class="rundb-h"> Submit new Sequences </h3>'+
         submit_area
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

function updateNumRuns() {
   const num_targets = document.querySelectorAll( ".config-ckbx-target:checked" ).length;
   const num_degraders = document.querySelectorAll( ".config-ckbx-degrader:checked" ).length;
   const num_beams = document.querySelectorAll( ".config-ckbx-beam:checked" ).length;
   document.getElementById("submit_num_runs").textContent = num_targets * num_degraders * num_beams;
}

// renderConfigurations is only called asyncronously upon loading the page.
// Updates are going to be rare enough such that reloading the page is acceptable.
async function renderConfigurations() {
   await pollOdb()
   const beamline = state.odb.beamline
   let beamtable = null;
   if (beamline == "PiM1") {
      beamtable = "pim1_epics";
   } else if (beamline == "PiE5") {
      beamtable = "pie5_epics";
   }
   const target_positions   = await R.call("config", {"id" : "target_position"}, maxBytes());
   const degrader_positions = await R.call("config", {"id" : "degrader_position"}, maxBytes());
   const beamline_settings = await R.call("config", {"id" : beamtable}, maxBytes());
   state.config_tables = {
      "target_positions" : target_positions.data,
      "degrader_positions" : degrader_positions.data,
      "beamline_settings" : beamline_settings.data,
      "beamline" : {"name" : beamline, "table" : beamtable }
   };
   put("rundb-configs", configTableHtml(state.config_tables))

   document.querySelectorAll( ".config-ckbx-target, .config-ckbx-degrader, .config-ckbx-beam" ).forEach(function(checkbox) {
      checkbox.addEventListener("change", updateNumRuns);
   });

   document.getElementById("submit_config").addEventListener("click", async function() {
      const selected = Array.from(
         document.querySelectorAll(".config-ckbx-target:checked, .config-ckbx-degrader:checked, .config-ckbx-beam:checked")
      ).map(function(checkbox) {
         return checkbox.value;
      });

      // safety catch
      const numRuns = document.getElementById("submit_num_runs").textContent;
      const confirmedRuns = document.getElementById("submit_confirm_runs").value;

      if (String(numRuns) !== String(confirmedRuns)) {
         dlgAlert("Number of runs does not match confirmation.");
         return;
      } else {
         dlgQuery("Confirm scheduling " + numRuns + " runs. Enter shifter password.</br></br> Password: ", "", async function(resp, param) {
            if (resp) {
               try {
                  await R.call("generate_sequence", {"config" : selected, "events" : document.getElementById("submit_events").value, "password" : resp})
               } catch(err) {
                  console.error(err);
               }
            }
         });
      }

   });
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
   renderStrip();
   renderAlerts();
   renderFooter();          // it names the database and the client, both from the ODB
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
   databaseError, note, init,
   // loops, exported so a fixture page can drive them one step at a time
   pollOdb
};

root.CFGDB = CFGDB;
if (typeof module !== "undefined" && module.exports) module.exports = CFGDB;

})(typeof globalThis !== "undefined" ? globalThis : this);
