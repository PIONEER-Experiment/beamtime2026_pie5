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
// Rendering is rundb.js. The one exception is the Clear queue sentences, which
// live here as plain strings so the tests can read them without a DOM. The pure
// helpers (statusInfo, retrySize, halveLimit, formatDuration, clearQueueArgs,
// ...) are exported on the same global so `node --test` can exercise them
// without a browser.
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
// queue, or a second round of cancellations. An action that comes back
// too_large is handed to the caller as it is, and the page says the write may
// or may not have happened.
const ACTION_CMDS = { schedule_five_point: true, clear_queue: true };

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
// What an action's error says about the database
// ---------------------------------------------------------------------------

// Kinds that mean the client considered the request and turned it down before
// writing anything. Anything else -- no answer, an answer we could not read, an
// answer that did not fit -- says nothing about what happened at the far end.
const REFUSED_KINDS = { usage: true, denied: true, db: true, unknown_command: true };

function wasRefused(kind) {
   return Object.prototype.hasOwnProperty.call(REFUSED_KINDS, kind);
}

// ---------------------------------------------------------------------------
// Clear queue
// ---------------------------------------------------------------------------
//
// The sentences of the Clear queue dialog and of the line it leaves under the
// queue. DOM-free, so the tests can read them; rundb.js escapes them and puts
// them on screen.
//
// The client decides everything that matters: which runs match, which one the
// sequencer may be about to take, and -- under the row lock -- which of the ids it
// is sent are still in a status it may cancel. These only put its answers into
// words.

// The statuses Clear queue may touch (actions.py). By name, not by flag:
// DEPENDING is pending too, and is never cancelled from here.
const CLEAR_STATUSES = ["PENDING", "HOLDING"];

// The operator name the client accepts (commands.py). It is the author of the
// annotation written beside every cancelled run, and goes in the MIDAS message.
const OPERATOR_MAX = 64;

/**
 * Is the Clear queue button worth showing?
 *
 * Only on a client armed for actions, and only when the last queue read has
 * something it could cancel: a button that can only ever answer "nothing to
 * do" is a button a shifter learns to ignore.
 */
function clearQueueOffered(client, queue) {
   if (!client || !client.actions_allowed) return false;
   const runs = (queue && queue.runs) || [];
   return runs.some(function (row) {
      const name = row && row.status ? String(row.status).toUpperCase() : "";
      return CLEAR_STATUSES.indexOf(name) >= 0;
   });
}

/** The operator name, trimmed, or "" when it would be refused. */
function operatorName(text) {
   const name = text === null || text === undefined ? "" : String(text).trim();
   return name.length > 0 && name.length <= OPERATOR_MAX ? name : "";
}

/**
 * The args object for clear_queue, from the preview the dialog is showing.
 *
 * The ids are the ones the dialog listed as going to CANCELLED, and only those.
 * A run it listed as kept is not sent: the dialog promised it stays PENDING,
 * and that holds even if the sequencer stops before OK is pressed -- the
 * shifter clears again for it. A run scheduled since the dialog opened is never
 * in this list either, so it is never cancelled. The client still re-checks
 * every id it is sent, and keeps one the sequencer may have reached since.
 * `sequencer_running` is not ours to send: the client reads it from the ODB and
 * refuses a caller that tries.
 *
 * null when there is nothing to send.
 */
function clearQueueArgs(data, includeHolding, operator) {
   if (!data) return null;
   const seen = {};
   const ids = [];
   (data.will_cancel || []).forEach(function (id) {
      const n = Number(id);
      if (!isFinite(n) || seen[n]) return;
      seen[n] = true;
      ids.push(n);
   });
   return { run_ids: ids, include_holding: Boolean(includeHolding), operator: operatorName(operator) };
}

function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

/** ["PENDING", "HOLDING"] -> "PENDING or HOLDING". */
function orList(names) {
   const list = (names && names.length ? names : ["PENDING"]).map(String);
   if (list.length === 1) return list[0];
   return list.slice(0, -1).join(", ") + " or " + list[list.length - 1];
}

/** [1, 2, 3, ...] -> "1, 2, 3", with "and N more" past `max`. */
function idList(ids, max) {
   const list = ids || [];
   const cap = max || 40;
   if (list.length <= cap) return list.join(", ");
   return list.slice(0, cap).join(", ") + " and " + (list.length - cap) + " more";
}

/** The label of the button that writes: "Cancel 12 runs". */
function clearQueueButtonLabel(n) {
   const k = Number(n) || 0;
   return "Cancel " + plural(k, "run", "runs");
}

/**
 * The dialog's first sentence: how many runs become CANCELLED, by the status
 * each one has now, under the name the database stores.
 */
function clearQueueSummary(data) {
   if (!data) return "";
   const which = orList(data.statuses);
   const will = (data.will_cancel || []).map(Number);
   const kept = data.kept_head || [];
   if (!will.length) {
      if (kept.length) return "Nothing would be cancelled: the only " + which + " runs are ones the sequencer may be about to take.";
      return "There are no " + which + " runs in the queue, so there is nothing to cancel.";
   }
   const byStatus = {};
   const order = [];
   (data.runs || []).forEach(function (run) {
      if (will.indexOf(Number(run.id)) < 0) return;
      const name = String(run.status || "?");
      if (!byStatus[name]) { byStatus[name] = 0; order.push(name); }
      byStatus[name]++;
   });
   const parts = order.map(function (name) { return byStatus[name] + " " + name; });
   return "This will set " + plural(will.length, "run", "runs") + " to CANCELLED" +
      (parts.length ? " (" + parts.join(", ") + ")" : "") + ". " +
      "CLAIMED and RUNNING runs, and runs that have finished, are not touched.";
}

/**
 * The line about the runs the client keeps for the sequencer, or "".
 *
 * The sequencer loads a run's configuration into the ODB -- moving devices --
 * while that run is still PENDING, so cancelling it under the sequencer gives
 * an untracked run or a refused start. The client cannot tell exactly which run
 * that is, so it keeps every one it might be (the lowest-priority PENDING runs,
 * and the run the nearline daemon has attached, whatever its priority). This
 * says which, without claiming more than that.
 */
function clearQueueKeptSentence(data) {
   const kept = (data && data.kept_head) || [];
   if (!kept.length) return "";
   const one = kept.length === 1;
   return "The sequencer is running, so " + (one ? "DB id " : "DB ids ") + idList(kept) +
      (one ? " stays PENDING: the sequencer may be about to take it."
           : " stay PENDING: the sequencer may be about to take one of them.") +
      " Stop the sequencer and clear again to remove " + (one ? "it." : "them.");
}

/** The line for a list the client cut short, or "". */
function clearQueueCappedSentence(data) {
   if (!data || !data.capped) return "";
   const shown = (data.runs || []).length;
   const total = Number(data.total);
   return "Only the first " + shown + (isFinite(total) && total > shown ? " of " + total : "") +
      " matching runs are listed, and only those are cancelled. Clear again for the rest.";
}

/**
 * Why the dialog's button is dead, or "" when it is not.
 *
 * `preview` is {pending, data, error} as the dialog holds it.
 */
function clearQueueBlocked(preview, operator) {
   const view = preview || {};
   if (view.pending) return "Waiting for the client to list the runs…";
   if (view.error) return "The client could not list the runs, so nothing can be cancelled from here.";
   if (!view.data) return "Waiting for the client to list the runs…";
   if (!(view.data.will_cancel || []).length) return "There is nothing to cancel.";
   if (!operatorName(operator)) {
      const raw = operator === null || operator === undefined ? "" : String(operator).trim();
      return raw.length > OPERATOR_MAX
         ? "The operator name is longer than " + OPERATOR_MAX + " characters."
         : "Type your name in Operator first: it is recorded with every cancelled run.";
   }
   return "";
}

// Error kinds this page makes up itself when no usable reply came back: the
// call timed out, mhttpd could not be reached or said the client is gone, or
// what came back was not an envelope.
const NO_REPLY_KINDS = { timeout: true, transport: true, client_down: true, bad_reply: true };

/**
 * Whether an error envelope is the client's own answer, as opposed to one
 * this page built because no answer came (`local`, or a no-reply kind).  Only
 * an answer that carries a message counts: without one there is nothing the
 * client said to show.
 */
function clientAnswered(env) {
   const err = (env && env.error) || {};
   return !env.local && !Object.prototype.hasOwnProperty.call(NO_REPLY_KINDS, err.kind) &&
      !!String(err.message || "").trim();
}

/**
 * What came back from clear_queue, as {level, head, lines}.
 *
 *   level "ok"       the client answered; head says how many were cancelled
 *   level "refused"  the client turned it down; nothing was written
 *   level "unknown"  some or all of it may have happened: either no usable
 *                    answer came back ("did not answer"), or the client answered
 *                    with an error it could not resolve, such as a failed commit
 *                    (`answered: true`, "could not confirm")
 *
 * The split is REFUSED_KINDS, the same as the five-point panel's: a request
 * that went out and did not come back may well have been carried out.
 */
function clearQueueResult(env) {
   if (!env) return null;
   if (env.ok === false) {
      const err = env.error || {};
      if (!wasRefused(err.kind) && clientAnswered(env)) {
         // The client did answer, with an error it could not resolve (a
         // commit that failed, say): it is not that nothing came back, it is
         // that the client itself does not know what the database kept.
         const message = String(err.message).replace(/[\s.]+$/, "");
         return { level: "unknown", answered: true,
            head: "The client could not confirm whether the runs were cancelled:",
            lines: [message + ". Look at the queue before pressing again."] };
      }
      if (!wasRefused(err.kind)) {
         return { level: "unknown", head: "The client did not answer.",
            lines: ["The runs may still have been cancelled. Look at the queue before pressing again.",
                    String(err.message || err.kind || "no detail given")] };
      }
      if (err.kind === "denied") {
         return { level: "refused", head: "Nothing was cancelled.",
            lines: ["This client is not armed for actions, so it refused the request.",
                    String(err.message || "")].filter(Boolean) };
      }
      return { level: "refused", head: "Nothing was cancelled.",
         lines: [String(err.message || "the client refused the request"),
                 String(err.hint || "")].filter(Boolean) };
   }
   const data = env.data || {};
   const cancelled = data.cancelled || [];
   const kept = data.kept_head || [];
   const skipped = data.skipped || [];
   const lines = [];
   if (cancelled.length) {
      lines.push((cancelled.length === 1 ? "DB id " : "DB ids ") + idList(cancelled) +
         (cancelled.length === 1 ? " is" : " are") + " now CANCELLED" +
         (data.operator ? ", recorded as cancelled by " + data.operator : "") + ".");
   }
   if (kept.length) {
      lines.push((kept.length === 1 ? "Kept DB id " : "Kept DB ids ") + idList(kept) +
         " PENDING: the sequencer may be about to take " + (kept.length === 1 ? "it" : "one of them") +
         ". Stop the sequencer and clear again to remove " + (kept.length === 1 ? "it." : "them."));
   }
   if (skipped.length) {
      lines.push("Skipped " + plural(skipped.length, "run", "runs") +
         " whose status changed after the dialog listed " + (skipped.length === 1 ? "it" : "them") + ": " +
         idList(skipped.map(function (s) {
            return s.id + " (" + (s.status ? s.status : "no longer in the database") + ")";
         })) + ".");
   }
   return { level: "ok",
      head: cancelled.length ? "Cancelled " + plural(cancelled.length, "run", "runs") + "." : "Nothing was cancelled.",
      lines: lines };
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
   basename, esc, normFlags, errorEnvelope, withTimeout,
   // what an action's error means, and the Clear queue sentences
   REFUSED_KINDS, wasRefused, NO_REPLY_KINDS, clientAnswered,
   CLEAR_STATUSES, OPERATOR_MAX, clearQueueOffered, operatorName, clearQueueArgs, clearQueueButtonLabel,
   clearQueueSummary, clearQueueKeptSentence, clearQueueCappedSentence, clearQueueBlocked,
   clearQueueResult, orList, idList
};

root.RunDbRpc = RunDbRpc;
if (typeof module !== "undefined" && module.exports) module.exports = RunDbRpc;

})(typeof globalThis !== "undefined" ? globalThis : this);
