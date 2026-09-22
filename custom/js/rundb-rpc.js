//
// rundb-rpc.js -- transport for the RunDB custom page.
//
// Two ways into the machine, kept deliberately apart:
//
//   odb(paths)           mjsonrpc_db_get_values, answered by mhttpd itself, so
//                        the live strip at the top of the page keeps working
//                        when the RunDBView client is stopped.
//   call(cmd, args, max) mjsonrpc_call("jrpc", ...), answered by the RunDBView
//                        python client, which always replies with the JSON
//                        envelope from the plan:
//                          {ok:true,  cmd, generated, query_ms, data:{...}}
//                          {ok:false, cmd, error:{kind, message, hint}}
//
// call() never throws and never hands back half a reply. Everything that can go
// wrong -- client stopped, database down, reply longer than we asked for -- comes
// back as an ok:false envelope carrying a kind the page can put into words.
//
// Nothing in this file knows what a run, a queue or a sequence is; that is
// rundb.js. The pure helpers at the bottom (statusInfo, retrySize, halveLimit,
// formatDuration, ...) are exported on the same global so `node --test` can
// exercise them without a browser.
//
// No build step, no bundler, no modules: plain <script src>, one global, the
// way every other MIDAS custom page in this repo does it.
//

(function (root) {
"use strict";

// db_get_values reports one status per path. 1 = SUCCESS, 312 = DB_NO_KEY
// (midas.h:643) for a key that does not exist -- which is a normal answer here:
// /Runinfo/Run DB PK only exists while a run is attached to the database.
const DB_SUCCESS = 1;
const DB_NO_KEY = 312;

// The MIDAS client we talk to. /RunDBView/Client name may rename it.
let clientName = "RunDBView";

// Commands that write to the run database (commands.py ACTION_COMMANDS).
//
// They are never sent twice. Every retry in this file exists because a reply
// did not fit in the buffer we asked for -- which says nothing about whether
// the work was done -- so asking again would risk a second set of runs in the
// queue. An action that comes back too_large is handed to the caller as it is,
// and the panel says the runs may or may not have been created.
const ACTION_CMDS = { schedule_five_point: true };

function isAction(cmd) { return Object.prototype.hasOwnProperty.call(ACTION_CMDS, cmd); }

// How long to wait for one command before giving up on it.
//
// mhttpd's jrpc is a synchronous round trip into a python process, and a client
// stuck in a query that will not finish never answers at all. Without this the
// poller parks on that promise for ever and the page goes quietly still --
// worse than an error, because nothing on screen says so. We cannot cancel the
// request, only stop waiting for it; the poller re-arms and the next answer is
// taken normally.
const CALL_TIMEOUT_MS = 15000;

function setClientName(name) { if (name) clientName = String(name); }
function getClientName() { return clientName; }

// ---------------------------------------------------------------------------
// Statuses
// ---------------------------------------------------------------------------
//
// A status is shown exactly as the run database stores it -- DONE, PENDING,
// HOLDING, RUNSDONE -- and never translated into a word of our own. Two people
// looking at the page and at `psql` have to be looking at the same thing, and a
// second vocabulary for the same eleven names is a second thing to learn and to
// get wrong.
//
// What the flags in `utils.status` are used for is the colour and the ordering:
// which of several statuses is the one worth showing in a rolled-up cell. The
// description from that table is the tooltip.

// Only for the first second, before the `status` command has answered with the
// real table: the rows utils.status seeds in db_config.sql.
const FALLBACK_STATUS = {
   HOLDING:   { ispending: true, isuser: true },
   PENDING:   { ispending: true },
   DEPENDING: { ispending: true },
   CLAIMED:   { isrunning: true },
   RUNNING:   { isrunning: true },
   RUNSDONE:  { isrunning: true },
   PPROC:     { isrunning: true },
   DONE:      { issuccess: true },
   FAILED:    { isfailure: true },
   BLOCKED:   { isfailure: true },
   ERROR:     { isfailure: true },
   CANCELLED: { isfailure: true, isuser: true }
};

// psycopg gives lower-case column names, but a hand-written fixture or a future
// serialiser may use isSuccess or is_success. Normalise once and stop caring.
function normFlags(row) {
   const out = {};
   if (!row || typeof row !== "object") return out;
   Object.keys(row).forEach(function (k) {
      out[String(k).toLowerCase().replace(/_/g, "")] = row[k];
   });
   return out;
}

/**
 * The colour a status is shown in, from its flags.
 *
 * Failure is red unless a person caused it (CANCELLED), which is grey; pending
 * is grey unless a person caused it (HOLDING), which is yellow, because a run
 * somebody stopped by hand is the one that will sit there for ever otherwise.
 */
function statusClass(row) {
   const f = normFlags(row);
   if (f.isrunning) return "green";
   if (f.issuccess) return "green";
   if (f.isfailure) return f.isuser ? "gray" : "red";
   if (f.ispending) return f.isuser ? "yellow" : "gray";
   return "gray";
}

/** How loudly a status shouts, for picking one out of several. */
function statusSeverity(row) {
   const f = normFlags(row);
   if (f.isfailure) return f.isuser ? 2 : 6;
   if (f.ispending) return f.isuser ? 5 : 3;
   if (f.isrunning) return 4;
   if (f.issuccess) return 1;
   return 0;
}

/** Does this status mean "has not started yet"? */
function isPendingStatus(name, table) {
   return Boolean(normFlags(flagsOf(name, table)).ispending);
}

function flagsOf(name, table) {
   const key = (name === null || name === undefined ? "" : String(name)).toUpperCase();
   return (table && table[key]) || FALLBACK_STATUS[key] || null;
}

/**
 * Everything the page needs to show one status: the name as stored, the colour
 * its flags give it, the description for the tooltip, and its severity.
 *
 * Never null, even for a name nobody has seen before -- an unknown status must
 * still render, in grey, under its own name.
 */
function statusInfo(name, table) {
   const given = name === null || name === undefined ? "" : String(name);
   const row = flagsOf(given, table);
   return {
      name: given,
      klass: statusClass(row),
      severity: statusSeverity(row),
      description: (row && row.description) || ""
   };
}

/** Build the name -> row table from the `statuses` array of the status reply. */
function statusTable(rows) {
   const out = {};
   if (!Array.isArray(rows)) return out;
   rows.forEach(function (r) {
      if (r && r.name) out[String(r.name).toUpperCase()] = r;
   });
   return out;
}

/**
 * The status worth showing when several rows are rolled into one cell: the
 * loudest one, by name, as stored. "" for an empty list.
 */
function worstStatus(names, table) {
   let best = "";
   let rank = -1;
   (names || []).forEach(function (name) {
      if (!name) return;
      const r = statusInfo(name, table).severity;
      if (r > rank) { rank = r; best = String(name); }
   });
   return best;
}

/**
 * Status-keyed counts, ordered for reading: loudest first, then alphabetically,
 * and zeroes dropped. Returns [{name, count}].
 *
 * The keys are raw status names. Older clients keyed these by a word instead;
 * such a key has no flags, sorts last and is still printed as it came, so the
 * page stays readable against either.
 */
function countEntries(counts, table) {
   const out = [];
   Object.keys(counts || {}).forEach(function (key) {
      const n = Number(counts[key]) || 0;
      if (n) out.push({ name: key, count: n, severity: statusInfo(key, table).severity });
   });
   out.sort(function (a, b) {
      if (b.severity !== a.severity) return b.severity - a.severity;
      return a.name < b.name ? -1 : (a.name > b.name ? 1 : 0);
   });
   return out;
}

// ---------------------------------------------------------------------------
// Retry arithmetic
// ---------------------------------------------------------------------------
//
// mhttpd's jrpc mallocs max_reply_length on every single call, so the page asks
// for a modest buffer (/RunDBView/Max reply kB) and the client answers a short
// `too_large` envelope naming the size it needed. We then ask again, once, for
// that size -- capped at four times the configured buffer so a runaway `needed`
// cannot talk mhttpd into a huge allocation per poll.

/** needed (bytes) + the configured buffer -> the size to ask for next. */
function retrySize(needed, base) {
   const b = Math.max(4096, Number(base) || 0);
   const cap = 4 * b;
   const n = Number(needed);
   if (!isFinite(n) || n <= 0) return cap;
   return Math.max(b, Math.min(Math.ceil(n) + 1024, cap));
}

/**
 * Halve the row limit of an args object; null when there is nothing to halve.
 * Used only after the capped retry was still too large: fewer rows is better
 * than a page that cannot show the runlog at all.
 */
function halveLimit(args) {
   if (!args || typeof args !== "object") return null;
   const n = Number(args.limit);
   if (!isFinite(n) || n <= 1) return null;
   const out = Object.assign({}, args);
   out.limit = Math.max(1, Math.floor(n / 2));
   return out;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

const NO_VALUE = "—";      // em dash, for "we do not know"

/** Seconds -> "45 s" / "12 m 04 s" / "1 h 03 m". */
function formatDuration(seconds) {
   const s = Number(seconds);
   if (seconds === null || seconds === undefined || !isFinite(s) || s < 0) return NO_VALUE;
   const total = Math.round(s);
   if (total < 60) return total + " s";
   const m = Math.floor(total / 60);
   if (m < 60) return m + " m " + pad2(total % 60) + " s";
   const h = Math.floor(m / 60);
   return h + " h " + pad2(m % 60) + " m";
}

function pad2(n) { return (n < 10 ? "0" : "") + n; }

/**
 * ISO-8601 with offset -> "2026-09-22 14:03:11".
 *
 * Cut out of the string rather than parsed through Date on purpose: the times
 * come from Postgres with the experiment's offset already in them, and pushing
 * them through the browser's timezone is a way to make a run look an hour late.
 */
function formatStamp(iso) {
   if (!iso) return NO_VALUE;
   const m = String(iso).match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/);
   if (!m) return String(iso);
   return m[1] + " " + m[2];
}

/** Same, clock part only, for "last read at 14:03:11". */
function formatClock(iso) {
   if (!iso) return NO_VALUE;
   const m = String(iso).match(/[T ](\d{2}:\d{2}:\d{2})/);
   return m ? m[1] : String(iso);
}

/** Now, as a clock string, for stamping the last good read. */
function clockNow() {
   const d = new Date();
   return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
}

/** 1234567 -> "1 234 567". Thin spaces, so a column of them still lines up. */
function formatCount(n) {
   if (n === null || n === undefined || n === "") return NO_VALUE;
   const v = Number(n);
   if (!isFinite(v)) return String(n);
   return String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

/** Filename -> basename, for the sequencer script. */
function basename(path) {
   if (!path) return "";
   const s = String(path);
   const i = s.lastIndexOf("/");
   return i < 0 ? s : s.substring(i + 1);
}

/** HTML-escape. Every string from the database goes through this. */
function esc(text) {
   if (text === null || text === undefined) return "";
   return String(text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// ---------------------------------------------------------------------------
// The jrpc call
// ---------------------------------------------------------------------------

function errorEnvelope(cmd, kind, message, hint) {
   return {
      ok: false,
      cmd: cmd,
      local: true,                  // built here, not by the client
      error: { kind: kind, message: message, hint: hint || null }
   };
}

/** Reject with a marked error if `promise` has not settled in `ms`. */
function withTimeout(promise, ms, cmd) {
   return new Promise(function (resolve, reject) {
      const timer = setTimeout(function () {
         const e = new Error(cmd + " timed out");
         e.rundbTimeout = true;
         reject(e);
      }, ms);
      promise.then(
         function (v) { clearTimeout(timer); resolve(v); },
         function (e) { clearTimeout(timer); reject(e); }
      );
   });
}

function describeError(e) {
   if (!e) return "no detail";
   if (e.request && e.error) return String(e.error);
   if (e.message) return String(e.message);
   return String(e);
}

/**
 * One command to the RunDBView client.
 *
 * Resolves with the parsed envelope, always -- ok:true with data, or ok:false
 * with an error kind:
 *
 *   client_down    mhttpd answered but there was no reply field: jrpc_old
 *                  returns only {status} when it cannot reach the client
 *   transport      the browser could not reach mhttpd at all
 *   bad_reply      a reply that is not the envelope (should not happen)
 *   timeout        no answer at all within CALL_TIMEOUT_MS
 *   usage|db|too_large|denied|unknown_command|internal   from the client
 *
 * A too_large reply is retried, once bigger and once with fewer rows -- unless
 * the command writes (ACTION_CMDS), which is never retried at all.
 *
 * maxLen is the configured buffer in bytes (/RunDBView/Max reply kB x 1024);
 * it is also what the 4x retry cap is computed from.
 */
async function call(cmd, args, maxLen) {
   const base = Math.max(4096, Number(maxLen) || 256 * 1024);
   let max = base;
   let sendArgs = Object.assign({}, args || {});
   let reducedTo = null;

   for (let attempt = 0; attempt < 3; attempt++) {
      let rpc;
      try {
         rpc = await withTimeout(mjsonrpc_call("jrpc", {
            client_name: clientName,
            cmd: cmd,
            args: JSON.stringify(sendArgs),
            max_reply_length: max
         }), CALL_TIMEOUT_MS, cmd);
      } catch (e) {
         if (e && e.rundbTimeout) {
            return errorEnvelope(cmd, "timeout",
               clientName + " did not answer " + cmd + " within " + Math.round(CALL_TIMEOUT_MS / 1000) + " s",
               "the client is up but stuck, most likely in a database query");
         }
         return errorEnvelope(cmd, "transport",
            "the browser could not reach mhttpd (" + describeError(e) + ")",
            "reload the page; if that fails, mhttpd itself is down");
      }

      const result = rpc && rpc.result;
      if (!result || result.reply === undefined || result.reply === null) {
         const st = result && result.status !== undefined ? result.status : "?";
         return errorEnvelope(cmd, "client_down",
            clientName + " did not answer (mhttpd status " + st + ")",
            "start " + clientName + " from the Programs page");
      }
      if (result.reply === "") {
         return errorEnvelope(cmd, "bad_reply",
            clientName + " answered " + cmd + " with an empty reply", null);
      }

      let env;
      try {
         env = JSON.parse(result.reply);
      } catch (e) {
         return errorEnvelope(cmd, "bad_reply",
            "could not read the reply to " + cmd + " (" + describeError(e) + ")",
            "the reply may have been truncated; check /RunDBView/Max reply kB");
      }
      if (!env || typeof env !== "object") {
         return errorEnvelope(cmd, "bad_reply", "the reply to " + cmd + " is not an envelope", null);
      }
      if (!env.cmd) env.cmd = cmd;

      if (env.ok !== false) {
         // Only ever reached with a complete reply: a truncated one fails to
         // parse above, and a too_large envelope is handled below. Partial data
         // is never rendered.
         if (reducedTo !== null) env.limit_reduced_to = reducedTo;
         return env;
      }

      const kind = env.error && env.error.kind;
      if (kind !== "too_large") return env;
      if (isAction(cmd)) return env;          // never ask a writer twice

      if (attempt === 0) {
         max = retrySize(env.error.needed, base);
         continue;
      }
      if (attempt === 1) {
         const smaller = halveLimit(sendArgs);
         if (!smaller) return env;
         sendArgs = smaller;
         reducedTo = smaller.limit;
         continue;
      }
      return env;
   }
   return errorEnvelope(cmd, "too_large", "the reply to " + cmd + " stayed too long after two retries",
      "raise /RunDBView/Max reply kB or ask for fewer rows");
}

// ---------------------------------------------------------------------------
// ODB
// ---------------------------------------------------------------------------

/**
 * Read several ODB paths at once. Returns one value per path, null where the
 * key does not exist (status 312) or the read failed -- a missing key is a
 * normal answer here, not an error, so the caller gets a value it can test.
 *
 * Rejects only when the browser cannot reach mhttpd.
 */
async function odb(paths) {
   const rpc = await mjsonrpc_db_get_values(paths);
   const result = (rpc && rpc.result) || {};
   const data = result.data || [];
   const status = result.status || [];
   return paths.map(function (p, i) {
      const st = status[i];
      if (st !== undefined && st !== null && st !== DB_SUCCESS) return null;
      const v = data[i];
      return v === undefined ? null : v;
   });
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

/**
 * Serialised, visibility-aware polling. One request in flight at a time, and
 * nothing at all while the tab is hidden.
 *
 * The serialisation is the point: a setInterval against a reply that sometimes
 * takes longer than the interval stacks requests up behind each other until
 * mhttpd is the bottleneck, and every jrpc call is a synchronous round trip
 * into a python process. Re-arming from the answer cannot stack.
 *
 * Adapted from wavedream-midas-dqm/pages/js/dqm-brpc.js (AutoUpdater).
 */
class Poller {
   constructor(update, intervalMs, name) {
      this.update = update;
      this.intervalMs = intervalMs || 1000;
      this.name = name || "poll";
      this.running = false;
      this.onError = null;
      this._timer = null;
      this._busy = false;
      const self = this;
      this._onVisible = function () {
         if (!isHidden() && self.running && !self._timer && !self._busy) self._tick();
      };
      if (typeof document !== "undefined" && document.addEventListener) {
         document.addEventListener("visibilitychange", this._onVisible);
      }
   }

   start() {
      if (this.running) return;
      this.running = true;
      this._tick();
   }

   stop() {
      this.running = false;
      if (this._timer) { clearTimeout(this._timer); this._timer = null; }
   }

   /** Change the interval; takes effect after the call in flight. */
   setInterval(ms) { if (ms > 0) this.intervalMs = ms; }

   /** Run one update now, unless one is already in flight. */
   kick() {
      if (!this._busy) {
         if (this._timer) { clearTimeout(this._timer); this._timer = null; }
         this._tick();
      }
   }

   async _tick() {
      this._timer = null;
      if (!this.running || isHidden()) return;
      this._busy = true;
      let delay = this.intervalMs;
      try {
         await this.update();
      } catch (e) {
         // Back off rather than hammering something that is not answering.
         delay = Math.max(5000, this.intervalMs);
         if (typeof console !== "undefined") console.error("rundb " + this.name + " failed:", e);
         if (this.onError) this.onError(e);
      }
      this._busy = false;
      const self = this;
      if (this.running) this._timer = setTimeout(function () { self._tick(); }, delay);
   }
}

function isHidden() {
   return typeof document !== "undefined" && document.hidden === true;
}

// ---------------------------------------------------------------------------
// Publish. `RunDbRpc` in a browser, module.exports under node --test.
// ---------------------------------------------------------------------------

const RunDbRpc = {
   DB_SUCCESS, DB_NO_KEY, NO_VALUE, FALLBACK_STATUS, CALL_TIMEOUT_MS, ACTION_CMDS, isAction,
   setClientName, getClientName,
   call, odb, Poller,
   // pure helpers, all testable without a browser
   statusClass, statusSeverity, statusInfo, statusTable, worstStatus, countEntries, isPendingStatus,
   retrySize, halveLimit,
   formatDuration, formatStamp, formatClock, formatCount, clockNow,
   basename, esc, normFlags, errorEnvelope, withTimeout
};

root.RunDbRpc = RunDbRpc;
if (typeof module !== "undefined" && module.exports) module.exports = RunDbRpc;

})(typeof globalThis !== "undefined" ? globalThis : this);
