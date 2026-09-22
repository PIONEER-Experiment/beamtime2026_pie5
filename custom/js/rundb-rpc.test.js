//
// Tests for the pure parts of the RunDB page, run with `node --test`.
//
//   docker run --rm --cpus=8 -v "$PWD/beamtime2026_pie5/custom:/w" -w /w \
//       node:22-alpine node --test js/rundb-rpc.test.js
//
// Node is not a dependency of this repo and must not become one: the page has
// no build step and loads plain <script src>. These tests exist so the helpers
// that decide what a shifter reads -- the status words, the retry arithmetic,
// the sentences about missing data -- are checked somewhere other than in a
// browser at 3 a.m.
//
// The two files are loaded the way the page loads them: rundb-rpc.js first,
// which publishes RunDbRpc on the global, then rundb.js, which reads it.
//

const test = require("node:test");
const assert = require("node:assert");

const R = require("./rundb-rpc.js");
const RUNDB = require("./rundb.js");

// ---------------------------------------------------------------------------
// Status words
// ---------------------------------------------------------------------------

test("a status is coloured by its flags and never renamed", () => {
   // The name is what the run database stores; the flags only pick the colour.
   assert.strictEqual(R.statusClass({ isrunning: true }), "green");
   assert.strictEqual(R.statusClass({ issuccess: true }), "green");
   assert.strictEqual(R.statusClass({ isfailure: true }), "red");
   assert.strictEqual(R.statusClass({ isfailure: true, isuser: true }), "gray");
   assert.strictEqual(R.statusClass({ ispending: true }), "gray");
   assert.strictEqual(R.statusClass({ ispending: true, isuser: true }), "yellow");
   assert.strictEqual(R.statusClass({}), "gray");
   assert.strictEqual(R.statusClass(null), "gray");
});

test("flag spellings do not matter", () => {
   assert.strictEqual(R.statusClass({ isSuccess: true }), "green");
   assert.strictEqual(R.statusClass({ is_success: true }), "green");
});

test("statusInfo gives back the name it was given, untouched", () => {
   const seeded = ["HOLDING", "PENDING", "DEPENDING", "CLAIMED", "RUNNING", "RUNSDONE",
                   "PPROC", "DONE", "FAILED", "BLOCKED", "ERROR", "CANCELLED"];
   seeded.forEach((name) => {
      const info = R.statusInfo(name, {});
      assert.strictEqual(info.name, name);
      assert.ok(["green", "red", "yellow", "gray"].indexOf(info.klass) >= 0);
   });
   // and a status nobody has seen before still renders, in grey, under its name
   const unknown = R.statusInfo("SOMETHING_NEW", {});
   assert.strictEqual(unknown.name, "SOMETHING_NEW");
   assert.strictEqual(unknown.klass, "gray");
});

test("the table the client sends supplies the colour and the tooltip", () => {
   const table = R.statusTable([
      { name: "PENDING", description: "Waiting for resources", ispending: true, isuser: true }
   ]);
   assert.strictEqual(R.statusInfo("PENDING", table).klass, "yellow", "flags from the table win");
   assert.strictEqual(R.statusInfo("PENDING", table).description, "Waiting for resources");
   assert.strictEqual(R.statusInfo("PENDING", table).name, "PENDING");
   assert.strictEqual(R.statusInfo("DONE", {}).description, "", "no table yet, no tooltip text");
});

test("the client dropping status_word and word changes nothing here", () => {
   // The page never read them: it prints the name and colours it from the flags.
   const table = R.statusTable([{ name: "DONE", description: "ok", issuccess: true }]);
   assert.strictEqual(R.statusInfo("DONE", table).name, "DONE");
   assert.strictEqual(R.statusInfo("DONE", table).klass, "green");
});

test("pending is a question about the flags, not about the name", () => {
   assert.ok(R.isPendingStatus("PENDING", {}));
   assert.ok(R.isPendingStatus("HOLDING", {}));
   assert.ok(R.isPendingStatus("DEPENDING", {}));
   assert.ok(!R.isPendingStatus("RUNNING", {}));
   assert.ok(!R.isPendingStatus("DONE", {}));
});

test("status-keyed counts come back loudest first", () => {
   const entries = R.countEntries({ DONE: 4, FAILED: 1, PENDING: 35, RUNNING: 1, HOLDING: 1, ERROR: 0 }, {});
   assert.deepStrictEqual(entries.map((e) => e.name), ["FAILED", "HOLDING", "RUNNING", "PENDING", "DONE"]);
   assert.deepStrictEqual(entries.map((e) => e.count), [1, 1, 1, 35, 4]);
   assert.deepStrictEqual(R.countEntries({}, {}), [], "zeroes and empties drop out");
   // an older client keyed these by a word; it is still printed as it came
   assert.deepStrictEqual(R.countEntries({ waiting: 3 }, {}).map((e) => e.name), ["waiting"]);
});

test("the worst status of a set is what a rolled-up cell shows", () => {
   assert.strictEqual(R.worstStatus(["DONE", "FAILED", "RUNNING"], {}), "FAILED");
   assert.strictEqual(R.worstStatus(["DONE", "RUNNING"], {}), "RUNNING");
   assert.strictEqual(R.worstStatus(["DONE", "HOLDING"], {}), "HOLDING");
   assert.strictEqual(R.worstStatus(["DONE", "CANCELLED"], {}), "CANCELLED");
   assert.strictEqual(R.worstStatus(["DONE"], {}), "DONE");
   assert.strictEqual(R.worstStatus([], {}), "");
});

// ---------------------------------------------------------------------------
// Retry arithmetic
// ---------------------------------------------------------------------------

test("the retry asks for what was needed, capped at four buffers", () => {
   const base = 256 * 1024;
   assert.strictEqual(R.retrySize(300 * 1024, base), 300 * 1024 + 1024);
   assert.strictEqual(R.retrySize(99 * 1024 * 1024, base), 4 * base);   // capped
   assert.strictEqual(R.retrySize(undefined, base), 4 * base);
   assert.strictEqual(R.retrySize(0, base), 4 * base);
   assert.ok(R.retrySize(10, base) >= base, "never ask for less than the buffer");
});

test("halving the limit stops at one row", () => {
   assert.deepStrictEqual(R.halveLimit({ limit: 200 }), { limit: 100 });
   assert.deepStrictEqual(R.halveLimit({ limit: 3, before_id: 7 }), { limit: 1, before_id: 7 });
   assert.strictEqual(R.halveLimit({ limit: 1 }), null);
   assert.strictEqual(R.halveLimit({ id: 12 }), null, "a run detail has nothing to halve");
   assert.strictEqual(R.halveLimit(null), null);
   const args = { limit: 50 };
   R.halveLimit(args);
   assert.strictEqual(args.limit, 50, "the caller's args are not modified");
});

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

test("durations read as a shifter would say them", () => {
   assert.strictEqual(R.formatDuration(0), "0 s");
   assert.strictEqual(R.formatDuration(45.4), "45 s");
   assert.strictEqual(R.formatDuration(724), "12 m 04 s");
   assert.strictEqual(R.formatDuration(3780), "1 h 03 m");
   assert.strictEqual(R.formatDuration(null), R.NO_VALUE);
   assert.strictEqual(R.formatDuration(undefined), R.NO_VALUE);
   assert.strictEqual(R.formatDuration(-1), R.NO_VALUE);
});

test("timestamps keep the offset they were written with", () => {
   // Not parsed through Date: an hour of drift would make a run look late.
   assert.strictEqual(R.formatStamp("2026-09-22T14:03:11+02:00"), "2026-09-22 14:03:11");
   assert.strictEqual(R.formatClock("2026-09-22T14:03:11+02:00"), "14:03:11");
   assert.strictEqual(R.formatStamp(null), R.NO_VALUE);
   assert.strictEqual(R.formatStamp("nonsense"), "nonsense");
});

test("counts, basenames and escaping", () => {
   assert.strictEqual(R.formatCount(1234567), "1 234 567");
   assert.strictEqual(R.formatCount(null), R.NO_VALUE);
   assert.strictEqual(R.basename("/home/pinky/bt2026/seq/scan.py"), "scan.py");
   assert.strictEqual(R.basename(""), "");
   assert.strictEqual(R.esc('<b>"x"</b>'), "&lt;b&gt;&quot;x&quot;&lt;/b&gt;");
});

// ---------------------------------------------------------------------------
// call(): the jrpc envelope
// ---------------------------------------------------------------------------

function stubCall(replies) {
   const seen = [];
   globalThis.mjsonrpc_call = async (method, params) => {
      seen.push({ method, params });
      const next = replies[Math.min(seen.length - 1, replies.length - 1)];
      if (typeof next === "function") return next(params);
      return next;
   };
   return seen;
}

const okReply = (data) => ({ result: { status: 1, reply: JSON.stringify({ ok: true, cmd: "runlog", generated: "2026-09-22T14:03:11+02:00", query_ms: 7, data: data }) } });
const errReply = (kind, extra) => ({ result: { status: 1, reply: JSON.stringify(Object.assign({ ok: false, cmd: "runlog", error: Object.assign({ kind: kind, message: kind + " happened" }, extra || {}) }, {})) } });

test("a good reply comes back parsed", async () => {
   const seen = stubCall([okReply({ runs: [] })]);
   const env = await R.call("runlog", { limit: 50 }, 256 * 1024);
   assert.strictEqual(env.ok, true);
   assert.deepStrictEqual(env.data, { runs: [] });
   assert.strictEqual(seen[0].params.cmd, "runlog");
   assert.strictEqual(seen[0].params.args, '{"limit":50}');
   assert.strictEqual(seen[0].params.max_reply_length, 256 * 1024);
   assert.strictEqual(seen[0].params.client_name, R.getClientName());
});

test("no reply field means the client is not running", async () => {
   // jrpc_old drops the reply and answers only {status} when it cannot reach
   // the client, so this -- not an exception -- is what a stopped client is.
   stubCall([{ result: { status: 503 } }]);
   const env = await R.call("status", {}, 4096);
   assert.strictEqual(env.ok, false);
   assert.strictEqual(env.error.kind, "client_down");
   assert.match(env.error.message, /503/);
   assert.ok(env.error.hint, "a client-down envelope says what to do");
});

test("too_large is retried once at the size the client asked for", async () => {
   const seen = stubCall([
      errReply("too_large", { needed: 400 * 1024, limit: 50 }),
      okReply({ runs: [{ id: 1 }] })
   ]);
   const env = await R.call("runlog", { limit: 50 }, 256 * 1024);
   assert.strictEqual(env.ok, true);
   assert.strictEqual(seen.length, 2);
   assert.strictEqual(seen[1].params.max_reply_length, 400 * 1024 + 1024);
   assert.strictEqual(seen[1].params.args, '{"limit":50}', "same rows, bigger buffer");
});

test("still too large: halve the rows, once, and say so", async () => {
   const seen = stubCall([
      errReply("too_large", { needed: 400 * 1024 }),
      errReply("too_large", { needed: 40 * 1024 * 1024 }),
      okReply({ runs: [{ id: 1 }] })
   ]);
   const env = await R.call("runlog", { limit: 50 }, 256 * 1024);
   assert.strictEqual(env.ok, true);
   assert.strictEqual(seen.length, 3);
   assert.strictEqual(seen[2].params.args, '{"limit":25}');
   assert.strictEqual(env.limit_reduced_to, 25, "the page can tell the shifter it is showing fewer rows");
});

test("a reply that never fits comes back as an error, never as partial data", async () => {
   const seen = stubCall([errReply("too_large", { needed: 9e9 })]);
   const env = await R.call("run", { id: 5 }, 256 * 1024);
   assert.strictEqual(env.ok, false);
   assert.strictEqual(env.error.kind, "too_large");
   assert.strictEqual(seen.length, 2, "nothing to halve in a run detail, so one retry only");
   assert.strictEqual(env.data, undefined);
});

test("a database error is passed through untouched", async () => {
   stubCall([errReply("db", { hint: "is postgres running?" })]);
   const env = await R.call("queue", { limit: 50 }, 4096);
   assert.strictEqual(env.ok, false);
   assert.strictEqual(env.error.kind, "db");
   assert.strictEqual(env.error.hint, "is postgres running?");
});

test("a truncated or non-JSON reply is an error, not a crash", async () => {
   stubCall([{ result: { status: 1, reply: '{"ok":true,"data":{"runs":[{"id' } }]);
   const env = await R.call("runlog", { limit: 50 }, 4096);
   assert.strictEqual(env.ok, false);
   assert.strictEqual(env.error.kind, "bad_reply");
});

test("mhttpd itself being unreachable is an error, not a rejection", async () => {
   globalThis.mjsonrpc_call = async () => { throw new Error("network down"); };
   const env = await R.call("status", {}, 4096);
   assert.strictEqual(env.ok, false);
   assert.strictEqual(env.error.kind, "transport");
});

// ---------------------------------------------------------------------------
// odb()
// ---------------------------------------------------------------------------

test("a missing ODB key reads as null, not as an error", async () => {
   globalThis.mjsonrpc_db_get_values = async () => ({
      result: { status: [1, 312, 1], data: [3, null, "scan.py"] }
   });
   const v = await R.odb(["/Runinfo/State", "/Runinfo/Run DB PK", "/PySequencer/State/SFilename"]);
   assert.deepStrictEqual(v, [3, null, "scan.py"]);
   assert.strictEqual(R.DB_NO_KEY, 312);
});

test("a key that exists and is zero is not confused with a missing one", async () => {
   globalThis.mjsonrpc_db_get_values = async () => ({ result: { status: [1], data: [0] } });
   const v = await R.odb(["/Runinfo/Run DB PK"]);
   assert.strictEqual(v[0], 0);
});

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

// The page learns the real utils.status table from the `status` reply; without
// it the status words still work (from the flags) but the cell title falls back
// to the raw name. Give the renderer the table, as the page would have it.
RUNDB.state.statuses = R.statusTable([
   { name: "DONE", description: "Job completed successfully (return code 0)", issuccess: true },
   { name: "PENDING", description: "Waiting for resources to become available", ispending: true },
   { name: "HOLDING", description: "Put on hold by user interaction", ispending: true, isuser: true },
   { name: "RUNNING", description: "Execution started and termination was not registered yet", isrunning: true },
   { name: "FAILED", description: "Job terminated with any return code other than 0", isfailure: true }
]);

const RUN_WITH_CONFIG = {
   id: 63, run_number: 260, status: "DONE", priority: null, requested_events: 200000,
   started: "2026-09-22T14:00:00+02:00", stopped: "2026-09-22T14:12:04+02:00",
   duration_s: 724, times_known: true,
   configs: [{ config_id: 12, config_type: "target_position", do_not_use: false, seq_id: 2,
               summary: { target_x: 5.0, target_y: -2.0 } }],
   config_note: null,
   files: [{ id: 1, filebase: "run00260", fileext: "mid.lz4", producer: "fe", status: "DONE" }],
   jobs: [{ job_type: "nearline", status: "DONE", count: 2 }],
   sequence: { id: 2, status: "RUNNING", on_complete: "nothing" }
};

const RUN_WITHOUT_CONFIG = {
   id: 4, run_number: 114, status: "DONE", requested_events: null,
   started: null, stopped: null, duration_s: null, times_known: false,
   configs: [], config_note: "no configuration recorded for this run",
   files: [], jobs: [], sequence: null
};

const RUN_HOLDING = {
   id: 70, run_number: null, status: "HOLDING", priority: 3, position: 1,
   requested_events: 500000, configs: [], config_note: "no configuration recorded for this run",
   files: [], jobs: [], sequence: null
};

test("a status is printed exactly as the database stores it", () => {
   const html = RUNDB.runlogHtml([RUN_WITH_CONFIG], null, null, null);
   assert.match(html, /<td class="rundb-st green" title="Job completed successfully \(return code 0\)">DONE<\/td>/);
   assert.doesNotMatch(html, /finished|waiting|on hold|cancelled/,
      "no word of our own anywhere in a rendered row");
});

test("a run with no configuration says so in words", () => {
   const html = RUNDB.runlogHtml([RUN_WITHOUT_CONFIG], null, null, null);
   assert.match(html, /no configuration recorded for this run/);
   assert.match(html, /no start\/stop logged/, "times_known false is explained, not left blank");
});

test("a run with a configuration shows the summary, not an id alone", () => {
   const html = RUNDB.runlogHtml([RUN_WITH_CONFIG], null, null, null);
   assert.match(html, /target_position 12/);
   assert.match(html, /target x 5/);
   assert.match(html, /12 m 04 s/);
   assert.match(html, /1 file, DONE/);
});

test("a configuration summary is whatever the client wrote", () => {
   // view.py builds it as one line of text; an object is accepted too, so a
   // later change of shape shows up as different words, not as an empty cell.
   assert.strictEqual(RUNDB.summaryText("target x=5 y=-2 mm"), "target x=5 y=-2 mm");
   assert.strictEqual(RUNDB.summaryText({ target_x: 5, target_y: -2 }), "target x 5, target y -2");
   assert.strictEqual(RUNDB.summaryText(null), "");
   const html = RUNDB.runlogHtml([Object.assign({}, RUN_WITH_CONFIG, {
      configs: [{ config_id: 12, config_type: "target_position", summary: "target x=5 y=-2 mm" }]
   })], null, null, null);
   assert.match(html, /target x=5 y=-2 mm/);
});

test("a configuration marked do_not_use says so", () => {
   const html = RUNDB.runlogHtml([Object.assign({}, RUN_WITH_CONFIG, {
      configs: [{ config_id: 8, config_type: "pie5_epics", do_not_use: true, summary: "pie5_epics #8" }]
   })], null, null, null);
   assert.match(html, /do not use/);
});

test("a run that has not been taken yet has no run number to show", () => {
   const html = RUNDB.runlogHtml([RUN_HOLDING], null, null, null);
   assert.match(html, /not taken yet/);
   assert.match(html, /<td class="rundb-st yellow"[^>]*>HOLDING<\/td>/);
});

test("the queue marks what goes next and counts itself in a sentence", () => {
   // next_up is the database id of the run that would start next (view.py sends
   // an int, not an object), and the tag has to land on that row and no other.
   const html = RUNDB.queueHtml({
      runs: [RUN_HOLDING, Object.assign({}, RUN_WITH_CONFIG, { position: 2, status: "PENDING" })],
      counts: { PENDING: 1, RUNNING: 0, HOLDING: 1 },
      next_up: 63
   });
   assert.match(html, /next up/);
   const rows = html.split("<tr").filter((r) => r.indexOf("next up") >= 0);
   assert.strictEqual(rows.length, 1, "exactly one row is tagged");
   assert.match(rows[0], /data-next-check|63/, "and it is the row whose id is next_up");
   assert.doesNotMatch(RUNDB.queueHtml({ runs: [RUN_HOLDING], counts: {}, next_up: null }), /next up/);
   // and the counts of an older client, keyed by a word, still read sensibly
   assert.match(RUNDB.queueHtml({ runs: [RUN_HOLDING], counts: { waiting: 2 } }), /2 runs in the queue: 2 waiting/);
   assert.match(html, /2 runs in the queue: 1 HOLDING, 1 PENDING/);
   assert.match(html, /lowest priority goes first/);
});

test("an empty queue says nothing is queued", () => {
   assert.match(RUNDB.queueHtml({ runs: [], counts: {} }), /Nothing is queued/);
});

test("the sequencer note appears only when it would explain something", () => {
   const idle = { seqRunning: 0 };
   const queue = { counts: { PENDING: 18, RUNNING: 0 } };
   assert.match(RUNDB.sequencerNoteHtml(idle, queue), /sequencer is not running, so nothing in the queue will start/);
   assert.strictEqual(RUNDB.sequencerNoteHtml({ seqRunning: 1 }, queue), "");
   assert.strictEqual(RUNDB.sequencerNoteHtml(idle, { counts: { RUNNING: 2 } }), "",
      "a queue with nothing pending in it is not waiting on the sequencer");
});

test("the three renderings of /Runinfo/Run DB PK", () => {
   const health = { lastGood: "14:03:11" };
   assert.match(RUNDB.stripHtml({ state: 3, runNumber: 260, runDbPk: 63 }, health), /DB run<\/b> 63/);
   assert.match(RUNDB.stripHtml({ state: 3, runNumber: 260, runDbPk: 0 }, health), /not attached/);
   assert.match(RUNDB.stripHtml({ state: 3, runNumber: 260, runDbPk: null }, health), /key not present/);
});

test("the strip names the run state and the sequencer parameter", () => {
   const html = RUNDB.stripHtml({ state: 1, runNumber: 260, runDbPk: 0, nEv: 5,
                                  seqRunning: 0, seqFile: "/home/pinky/seq/scan.py" }, { lastGood: "14:03:11" });
   assert.match(html, /stopped/);
   assert.match(html, /sequencer parameter nEv/);
   assert.match(html, /scan\.py/);
   assert.match(html, /not running/);
});

test("run states follow midas.h, not a guess", () => {
   assert.strictEqual(RUNDB.runStateWord(RUNDB.STATE_STOPPED).word, "stopped");
   assert.strictEqual(RUNDB.runStateWord(RUNDB.STATE_PAUSED).word, "paused");
   assert.strictEqual(RUNDB.runStateWord(RUNDB.STATE_RUNNING).word, "running");
   assert.strictEqual(RUNDB.STATE_STOPPED, 1);
   assert.strictEqual(RUNDB.STATE_PAUSED, 2);
   assert.strictEqual(RUNDB.STATE_RUNNING, 3);
});

test("the two stale messages are different and both name the last read", () => {
   const clientDown = RUNDB.staleHtml({
      clientError: { kind: "client_down", message: "RunDBView did not answer (mhttpd status 503)" },
      dbError: null, otherError: null, lastGood: "14:03:11"
   });
   const dbDown = RUNDB.staleHtml({
      clientError: null, dbError: { kind: "db", message: "could not connect to server" },
      otherError: null, lastGood: "14:03:11"
   });
   assert.match(clientDown, /client is not answering/);
   assert.match(clientDown, /last read at 14:03:11/);
   assert.match(clientDown, /Programs page/);
   assert.match(dbDown, /run database did not answer/);
   assert.match(dbDown, /last read at 14:03:11/);
   assert.notStrictEqual(clientDown, dbDown);
   assert.doesNotMatch(dbDown, /Programs page/, "the two states get different advice");
});

test("with nothing read yet the stale message says that instead of a time", () => {
   const html = RUNDB.staleHtml({ clientError: { kind: "client_down", message: "x" }, lastGood: null });
   assert.match(html, /Nothing has been read from the run database yet/);
});

test("sequences count their members by status name", () => {
   const html = RUNDB.sequencesHtml([{
      id: 2, status: "RUNNING", on_complete: "nothing", n_runs: 9,
      counts: { DONE: 4, RUNNING: 1, PENDING: 4 },
      first_run: 252, last_run: 260
   }]);
   assert.match(html, /9 runs: 1 RUNNING, 4 PENDING, 4 DONE/);
   assert.match(html, /252&ndash;260/);
   assert.match(RUNDB.sequencesHtml([]), /No sequences in the database/);
});

test("a sequence with no counts says only how many runs it has", () => {
   assert.strictEqual(RUNDB.sequenceCountSentence({ n_runs: 9 }, {}), "9 runs");
   assert.strictEqual(RUNDB.sequenceCountSentence({ n_runs: 1, counts: {} }, {}), "1 run");
   assert.strictEqual(RUNDB.sequenceCountSentence({ n_runs: 0 }, {}), "no runs yet");
});

test("the run detail repeats the counts of the sequence it belongs to", () => {
   const html = RUNDB.detailHtml({
      run: { id: 63, run_number: 260, status: "DONE" },
      configs: [], files: [], jobs: [],
      sequence: { id: 2, status: "RUNNING", on_complete: "nothing", n_runs: 9,
                  counts: { DONE: 4, RUNNING: 1, PENDING: 4 },
                  runs: [{ id: 63, run_number: 260, status: "DONE" }] }
   });
   assert.match(html, /9 runs: 1 RUNNING, 4 PENDING, 4 DONE/);
   assert.strictEqual(RUNDB.countedSequence({ id: 2 }), "", "no counts, no sentence");
   assert.strictEqual(RUNDB.countedSequence({ counts: { DONE: 2 } }), "2 runs: 2 DONE",
      "the total is added up when the row does not carry one");
});

// ---------------------------------------------------------------------------
// The action panel
// ---------------------------------------------------------------------------

const ARMED = { client: { actions_allowed: true }, database: { reachable: true } };

// Two runs' worth of configurations, as the queue and the runlog carry them.
const CONFIG_ROWS = [
   { id: 1, configs: [
      { config_id: 2, config_type: "target_position", summary: "target x=0 y=0 mm" },
      { config_id: 16, config_type: "degrader_position", summary: "degrader x=3.5 mm (4 mm before L1)" },
      { config_id: 26, config_type: "pie5_epics", summary: "pie5_epics #26" } ] },
   { id: 2, configs: [
      { config_id: 17, config_type: "degrader_position", do_not_use: true, summary: "degrader x=9 mm" },
      { config_id: 18, config_type: "degrader_position", summary: "degrader x=12 mm" },
      { config_id: 26, config_type: "pie5_epics", summary: "pie5_epics #26" } ] }
];

const PREVIEWED = { key: "x", data: { would_create_runs: 5, target_seq_id: 2 } };

const DEGRADER = { config_id: 16, config_type: "degrader_position",
                   summary: "degrader x=3.5 mm (4 mm before L1)", do_not_use: false };
const BEAM = { config_id: 26, config_type: "pie5_epics", summary: "pie5_epics #26", do_not_use: false };

test("the action panel is absent unless the client is armed", () => {
   const off = RUNDB.actionPanelHtml({ client: { actions_allowed: false } }, { options: [] });
   assert.match(off, /Actions are disabled on this client/);
   assert.doesNotMatch(off, /<button/, "no button at all when actions are not allowed");
   assert.doesNotMatch(off, /<select/);
   assert.strictEqual(RUNDB.actionPanelHtml(null, { options: [] }).indexOf("<button"), -1);
});

test("the pickers come from the configurations already on screen", () => {
   const groups = RUNDB.configOptions([CONFIG_ROWS]);
   assert.deepStrictEqual(groups.map((g) => g.config_type), ["degrader_position", "pie5_epics"],
      "target_position is not offered: the five points are the scan");
   assert.deepStrictEqual(groups[0].entries.map((e) => e.config_id), [16, 17, 18]);
   assert.strictEqual(groups[1].entries.length, 1, "the same id seen twice is one option");
   assert.strictEqual(groups[0].entries[1].do_not_use, true);
   assert.deepStrictEqual(RUNDB.configOptions([]), []);
});

test("a do-not-use configuration is shown but cannot be chosen", () => {
   const html = RUNDB.actionPanelHtml(ARMED, { options: RUNDB.configOptions([CONFIG_ROWS]),
                                               selection: {}, events: 1000000 });
   assert.match(html, /degrader x=9 mm \(id 17\)[^<]*&mdash; do not use/);
   const option17 = html.split("<option").filter((o) => o.indexOf('value="17"') >= 0)[0];
   assert.match(option17, /disabled/);
   const option16 = html.split("<option").filter((o) => o.indexOf('value="16"') >= 0)[0];
   assert.doesNotMatch(option16, /disabled/);
});

test("the button is dead until something is chosen and the events make sense", () => {
   const options = RUNDB.configOptions([CONFIG_ROWS]);
   const empty = RUNDB.actionPanelHtml(ARMED, { options: options, selection: {}, events: 1000000 });
   assert.match(empty.split("rundb-act-go")[1], /disabled/);
   assert.match(empty, /Choose at least one configuration/);

   const chosen = RUNDB.actionPanelHtml(ARMED,
      { options: options, selection: { degrader_position: DEGRADER }, events: 1000000,
        preview: PREVIEWED });
   assert.doesNotMatch(chosen.split("rundb-act-go")[1].split(">")[0], /disabled/);

   const silly = RUNDB.actionPanelHtml(ARMED,
      { options: options, selection: { degrader_position: DEGRADER }, events: 0, preview: PREVIEWED });
   assert.match(silly.split("rundb-act-go")[1], /disabled/);
   assert.match(silly, /whole number between 1 and/);
});

test("events are bounded the way the client bounds them", () => {
   assert.ok(RUNDB.eventsValid(1));
   assert.ok(RUNDB.eventsValid(1000000));
   assert.ok(RUNDB.eventsValid(RUNDB.MAX_ACTION_EVENTS));
   assert.ok(!RUNDB.eventsValid(0));
   assert.ok(!RUNDB.eventsValid(-5));
   assert.ok(!RUNDB.eventsValid(1.5));
   assert.ok(!RUNDB.eventsValid(RUNDB.MAX_ACTION_EVENTS + 1));
   assert.ok(!RUNDB.eventsValid(""));
   assert.strictEqual(RUNDB.DEFAULT_ACTION_EVENTS, 1000000);
});

test("the summary sentence says what will exist afterwards, on the client's count", () => {
   // The number of runs is preview_five_point's answer, not a constant here:
   // it is however many target positions the sequence actually has.
   const sentence = RUNDB.actionSummarySentence(
      { degrader_position: DEGRADER, pie5_epics: BEAM }, 1000000, PREVIEWED);
   assert.match(sentence, /^This will schedule 5 runs \(target positions of sequence 2\) with /);
   assert.match(sentence, /degrader position degrader x=3\.5 mm \(4 mm before L1\)/);
   assert.match(sentence, /and pie5 epics pie5_epics #26/);
   assert.match(sentence, /events each, as one sequence\.$/);
   assert.match(RUNDB.actionSummarySentence({ degrader_position: DEGRADER }, 5, PREVIEWED),
      /with degrader position [^,]*at 5 events each/, "one choice, no stray 'and'");

   const seven = RUNDB.actionSummarySentence({ degrader_position: DEGRADER }, 5,
      { data: { would_create_runs: 7, target_seq_id: 3 } });
   assert.match(seven, /schedule 7 runs \(target positions of sequence 3\)/);
});

test("nothing is promised until the client has checked the request", () => {
   const sel = { degrader_position: DEGRADER };
   assert.ok(!RUNDB.actionReady(sel, 1000, {}), "no preview yet, no button");
   assert.match(RUNDB.actionSummarySentence(sel, 1000, {}), /Waiting for the client/);

   assert.ok(!RUNDB.actionReady(sel, 1000, { pending: true }));
   assert.match(RUNDB.actionSummarySentence(sel, 1000, { pending: true }), /Checking with the client/);

   assert.ok(!RUNDB.actionReady(sel, 1000, { error: { kind: "denied", message: "no" } }));
   assert.match(RUNDB.actionSummarySentence(sel, 1000, { error: { kind: "denied" } }),
      /not armed for actions/);
   assert.match(RUNDB.actionSummarySentence(sel, 1000, { error: { kind: "unknown_command" } }),
      /too old to say what this would create/);
   assert.match(RUNDB.actionSummarySentence(sel, 1000,
      { error: { kind: "usage", message: "configuration marked do not use: 17" } }),
      /could not check this request: configuration marked do not use: 17/);

   assert.ok(RUNDB.actionReady(sel, 1000, PREVIEWED));
});

test("the preview is asked for, and its answer drives the button", async () => {
   const sent = [];
   globalThis.mjsonrpc_call = async (method, params) => {
      sent.push(params);
      return { result: { status: 1, reply: JSON.stringify({ ok: true, cmd: "preview_five_point",
         data: { would_create_runs: 5, target_seq_id: 2, requested_events: 1000,
                 configs_applied: [{ config_id: 16, config_type: "degrader_position" }],
                 message: "would create 5 runs" } }) } };
   };
   RUNDB.state.status = { client: { actions_allowed: true } };
   RUNDB.state.action.selection = { degrader_position: DEGRADER };
   RUNDB.state.action.events = 1000;
   RUNDB.state.action.preview = { key: JSON.stringify(RUNDB.actionArgs({ degrader_position: DEGRADER }, 1000)),
                                  pending: true };
   await RUNDB.runPreview(RUNDB.state.action.preview.key);
   assert.strictEqual(sent[0].cmd, "preview_five_point");
   assert.deepStrictEqual(JSON.parse(sent[0].args), { config_ids: [16], requested_events: 1000 },
      "the preview asks about exactly what would be sent");
   assert.strictEqual(RUNDB.state.action.preview.data.would_create_runs, 5);
   assert.ok(RUNDB.actionReady(RUNDB.state.action.selection, RUNDB.state.action.events,
                               RUNDB.state.action.preview));
   RUNDB.cancelPreview();
});

test("asking for the same preview twice does not strand the panel", async () => {
   // Two <select> changes that leave the same request behind used to cancel the
   // armed timer and then return, so the preview never went out and the button
   // stayed dead on "checking with the client".
   RUNDB.state.action.selection = { degrader_position: DEGRADER };
   RUNDB.state.action.events = 1000;
   RUNDB.state.action.preview = {};
   RUNDB.schedulePreview();
   assert.ok(RUNDB.state.action.preview.pending);
   assert.ok(RUNDB.previewArmed(), "a preview is on its way");
   RUNDB.schedulePreview();
   assert.ok(RUNDB.previewArmed(), "and it still is after an identical second ask");
   RUNDB.cancelPreview();
   assert.ok(!RUNDB.previewArmed());

   // a different request does replace it
   RUNDB.state.action.preview = {};
   RUNDB.schedulePreview();
   const first = RUNDB.state.action.preview.key;
   RUNDB.state.action.events = 2000;
   RUNDB.schedulePreview();
   assert.notStrictEqual(RUNDB.state.action.preview.key, first);
   assert.ok(RUNDB.previewArmed());
   RUNDB.cancelPreview();

   // and nothing to preview clears it
   RUNDB.state.action.selection = {};
   RUNDB.schedulePreview();
   assert.deepStrictEqual(RUNDB.state.action.preview, {});
   assert.ok(!RUNDB.previewArmed());
});

test("a preview answer for a request that has moved on is dropped", async () => {
   globalThis.mjsonrpc_call = async () => ({ result: { status: 1, reply: JSON.stringify({
      ok: true, cmd: "preview_five_point", data: { would_create_runs: 99 } }) } });
   RUNDB.state.action.preview = { key: "the form has moved on", pending: true };
   await RUNDB.runPreview('{"config_ids":[16],"requested_events":1000}');
   assert.strictEqual(RUNDB.state.action.preview.data, undefined, "a stale answer is not shown");
   RUNDB.cancelPreview();
});

test("the args object is exactly what commands.py accepts", () => {
   const args = RUNDB.actionArgs({ degrader_position: DEGRADER, pie5_epics: BEAM }, 250000);
   assert.deepStrictEqual(args, { config_ids: [16, 26], requested_events: 250000 });
   assert.deepStrictEqual(Object.keys(args).sort(), ["config_ids", "requested_events"],
      "no extra keys: the client refuses unknown ones");
   args.config_ids.forEach((id) => assert.strictEqual(typeof id, "number"));
   assert.deepStrictEqual(RUNDB.actionArgs({}, 10), { config_ids: [], requested_events: 10 });
});

test("the picker replaces the configuration of its type, and never adds a second", () => {
   // Driven through the handler the <select> is wired to, not through a local
   // object: _validate_configs refuses two configurations of one type, so the
   // form must not be able to build that request in the first place.
   // The options are rebuilt from the queue and the runlog on every render, so
   // the test supplies those rather than the derived list.
   RUNDB.state.queue = { runs: CONFIG_ROWS, counts: {} };
   RUNDB.state.runs = {};
   RUNDB.state.action.selection = {};
   RUNDB.state.action.events = 1000;
   RUNDB.renderActions();

   RUNDB.chooseConfig("degrader_position", "16");
   assert.deepStrictEqual(RUNDB.actionArgs(RUNDB.state.action.selection, 1).config_ids, [16]);

   RUNDB.chooseConfig("degrader_position", "18");
   assert.deepStrictEqual(RUNDB.actionArgs(RUNDB.state.action.selection, 1).config_ids, [18],
      "a second degrader replaces the first");

   RUNDB.chooseConfig("pie5_epics", "26");
   assert.deepStrictEqual(RUNDB.actionArgs(RUNDB.state.action.selection, 1).config_ids, [18, 26],
      "a different type is added beside it");

   RUNDB.chooseConfig("degrader_position", "17");
   assert.deepStrictEqual(RUNDB.actionArgs(RUNDB.state.action.selection, 1).config_ids, [26],
      "a do-not-use row cannot be chosen even if the value reaches the handler");

   RUNDB.chooseConfig("pie5_epics", "");
   assert.deepStrictEqual(RUNDB.state.action.selection, {}, "back to nothing chosen");
   assert.strictEqual(RUNDB.findOption("degrader_position", "999"), null, "an id not on offer is not a choice");
   RUNDB.cancelPreview();
   RUNDB.state.queue = null;
});

test("a hand-typed id replaces the picker's choice of the same type", async () => {
   const reply = (cfg) => async () => ({ result: { status: 1, reply: JSON.stringify({
      ok: true, cmd: "config", data: { config: cfg } }) } });
   RUNDB.state.queue = { runs: CONFIG_ROWS, counts: {} };
   RUNDB.state.runs = {};
   RUNDB.state.action.selection = {};
   RUNDB.state.action.events = 1000;
   RUNDB.renderActions();

   RUNDB.chooseConfig("pie5_epics", "26");
   globalThis.mjsonrpc_call = reply({ config_id: 45, config_type: "pie5_epics", do_not_use: false,
                                      values: { id: 45, "QTA11:SOL:2": -20 } });
   await RUNDB.lookupConfig("45");
   assert.deepStrictEqual(RUNDB.actionArgs(RUNDB.state.action.selection, 1).config_ids, [45],
      "one pie5_epics, the typed one");
   RUNDB.cancelPreview();
   RUNDB.state.queue = null;
});

test("a scheduled scan is reported as what now exists", () => {
   const html = RUNDB.actionResultHtml({ ok: true, cmd: "schedule_five_point", data: {
      sequence_id: 9, run_ids: [41, 42, 43, 44, 45], target_seq_id: 2, requested_events: 1000000,
      configs_applied: [{ config_id: 16, config_type: "degrader_position" }],
      runs: [{ run_id: 41, priority: 7, status: "PENDING", requested_events: 1000000,
               target_config_id: 6, xpos: -17, ypos: -17 },
             { run_id: 42, priority: 8, status: "PENDING", requested_events: 1000000,
               target_config_id: 7, xpos: 0, ypos: 0 }],
      message: "queued 5 runs of 1000000 events as sequence 9; they start when the sequencer reaches them"
   } });
   assert.match(html, /Scheduled\./);
   assert.match(html, /queued 5 runs/);
   assert.match(html, /Sequence 9/);
   assert.match(html, /degrader position #16/);
   assert.match(html, /x=-17 y=-17/, "the position each run actually carries, not the order it was asked in");
   assert.match(html, /<td class="rundb-st gray"[^>]*>PENDING<\/td>/);
});

test("a write is never sent twice, whatever comes back", async () => {
   // The retry exists because a reply did not fit. That says nothing about
   // whether the work was done, so a writer is asked exactly once.
   const sent = [];
   globalThis.mjsonrpc_call = async (method, params) => {
      sent.push(params);
      return { result: { status: 1, reply: JSON.stringify({ ok: false, cmd: params.cmd,
         error: { kind: "too_large", message: "reply too long", needed: 900000, limit: 50 } }) } };
   };
   const env = await R.call("schedule_five_point", { config_ids: [16], requested_events: 10 }, 4096);
   assert.strictEqual(sent.length, 1, "one attempt only");
   assert.strictEqual(env.ok, false);
   assert.strictEqual(env.error.kind, "too_large");
   assert.ok(R.isAction("schedule_five_point"));
   assert.ok(!R.isAction("runlog"));
   assert.ok(!R.isAction("preview_five_point"), "the preview writes nothing and may be retried");

   // ... while a read with the same reply is retried as before
   sent.length = 0;
   let first = true;
   globalThis.mjsonrpc_call = async (method, params) => {
      sent.push(params);
      if (first) {
         first = false;
         return { result: { status: 1, reply: JSON.stringify({ ok: false, cmd: params.cmd,
            error: { kind: "too_large", needed: 9000, limit: 50 } }) } };
      }
      return { result: { status: 1, reply: '{"ok":true,"cmd":"runlog","data":{"runs":[]}}' } };
   };
   await R.call("runlog", { limit: 50 }, 4096);
   assert.strictEqual(sent.length, 2);
});

test("an unanswered write says the runs may exist, and does not say nothing happened", () => {
   ["timeout", "transport", "client_down", "bad_reply", "too_large"].forEach((kind) => {
      const html = RUNDB.actionResultHtml({ ok: false, error: { kind: kind, message: "no answer" } });
      assert.match(html, /The client did not answer/, kind);
      assert.match(html, /may still have been queued. Check the queue before pressing again/, kind);
      assert.doesNotMatch(html, /Nothing was scheduled/, kind + " must not claim nothing happened");
      assert.strictEqual(RUNDB.wasRefused(kind), false, kind);
   });
   ["usage", "denied", "db", "unknown_command"].forEach((kind) => {
      assert.strictEqual(RUNDB.wasRefused(kind), true, kind);
      assert.match(RUNDB.actionResultHtml({ ok: false, error: { kind: kind, message: "no" } }),
         /Nothing was scheduled/, kind);
   });
});

test("a partial write is the first thing the panel says", () => {
   const html = RUNDB.actionResultHtml({ ok: false, error: {
      kind: "db", message: "the run database refused the new runs: deadlock detected",
      hint: "check the queue", data: { created_anyway: { run_ids: [41, 42], sequence_ids: [9] } } } });
   assert.match(html, /^<div class="rundb-alert red"><b>Partially scheduled: runs 41, 42 were created/);
   assert.match(html, /in sequence 9/);
   assert.match(html, /The rest failed: the run database refused the new runs/);
   assert.match(html, /cancel what should not be there/);
   assert.doesNotMatch(html, /Nothing was scheduled/);
   // an empty created_anyway is not a partial write
   assert.match(RUNDB.actionResultHtml({ ok: false, error: { kind: "usage", message: "no",
      data: { created_anyway: { run_ids: [], sequence_ids: [] } } } }), /Nothing was scheduled/);
});

test("an unanswered write re-reads the queue instead of guessing", async () => {
   let kicked = 0;
   globalThis.mjsonrpc_call = async () => ({ result: { status: 503 } });   // client gone
   RUNDB.state.status = { client: { actions_allowed: true } };
   RUNDB.state.action.selection = { degrader_position: DEGRADER };
   RUNDB.state.action.events = 1000;
   RUNDB.state.action.preview = PREVIEWED;
   const poller = { kick: () => { kicked++; } };
   const saved = RUNDB.state.__poller;
   RUNDB.__setSlowPollerForTests(poller);
   await RUNDB.schedule();
   assert.strictEqual(RUNDB.state.action.result.error.kind, "client_down");
   assert.match(RUNDB.actionResultHtml(RUNDB.state.action.result), /may still have been queued/);
   assert.strictEqual(kicked, 1, "the queue is re-read so the shifter can see what is really there");
   RUNDB.__setSlowPollerForTests(null);
   void saved;
});

test("the form is frozen from the moment the question is asked", () => {
   let answer = null;
   globalThis.dlgConfirm = (text, cb) => { answer = cb; };   // leave the dialog open
   globalThis.mjsonrpc_call = async () => ({ result: { status: 1, reply: '{"ok":true,"cmd":"x","data":{}}' } });
   RUNDB.state.status = { client: { actions_allowed: true } };
   RUNDB.state.action.selection = { degrader_position: DEGRADER };
   RUNDB.state.action.events = 1000;
   RUNDB.state.action.preview = PREVIEWED;
   RUNDB.state.action.busy = false;

   RUNDB.confirmSchedule();
   assert.strictEqual(RUNDB.state.action.busy, true, "busy while the question is on screen");
   const html = RUNDB.actionPanelHtml(ARMED, RUNDB.state.action);
   assert.match(html.split("rundb-act-go")[1], /disabled/);
   assert.match(html, /scheduling/);

   let second = false;
   globalThis.dlgConfirm = () => { second = true; };
   RUNDB.confirmSchedule();
   assert.strictEqual(second, false, "a second press while busy does nothing");

   answer(false);                       // cancelled
   assert.strictEqual(RUNDB.state.action.busy, false, "and the form comes back");
});

test("a refusal says nothing was scheduled, and denied says why", () => {
   const denied = RUNDB.actionResultHtml({ ok: false, error: { kind: "denied",
      message: "actions are not allowed on this client" } });
   assert.match(denied, /Nothing was scheduled/);
   assert.match(denied, /not armed for actions/);

   const usage = RUNDB.actionResultHtml({ ok: false, error: { kind: "usage",
      message: "configuration marked do not use: 17", hint: "pick another configuration" } });
   assert.match(usage, /Nothing was scheduled/);
   assert.match(usage, /configuration marked do not use: 17/);
   assert.match(usage, /pick another configuration/);
   assert.doesNotMatch(usage, /not armed/);
   assert.strictEqual(RUNDB.actionResultHtml(null), "");
});

test("the panel never sends anything when the client is not armed", async () => {
   let called = 0;
   globalThis.mjsonrpc_call = async () => { called++; return { result: { status: 1, reply: '{"ok":true}' } }; };
   globalThis.dlgConfirm = (text, cb) => cb(true);
   RUNDB.state.status = { client: { actions_allowed: false } };
   RUNDB.state.action.selection = { degrader_position: DEGRADER };
   RUNDB.state.action.events = 1000;
   RUNDB.confirmSchedule();
   assert.strictEqual(called, 0, "not even the confirm dialog leads to a call");
   assert.strictEqual(RUNDB.state.action.result.error.kind, "denied");
   assert.match(RUNDB.actionResultHtml(RUNDB.state.action.result), /not armed for actions/);
});

test("armed, it asks first and then sends exactly the chosen ids", async () => {
   const sent = [];
   globalThis.mjsonrpc_call = async (method, params) => {
      sent.push(params);
      return { result: { status: 1, reply: JSON.stringify({ ok: true, cmd: "schedule_five_point",
         data: { sequence_id: 9, run_ids: [41], runs: [], configs_applied: [], message: "queued 5 runs" } }) } };
   };
   let asked = null;
   globalThis.dlgConfirm = (text, cb) => { asked = text; cb(true); };
   RUNDB.state.status = { client: { actions_allowed: true } };
   RUNDB.state.action.selection = { degrader_position: DEGRADER, pie5_epics: BEAM };
   RUNDB.state.action.events = 250000;
   RUNDB.state.action.result = null;
   RUNDB.state.action.preview = PREVIEWED;
   await RUNDB.schedule();
   assert.strictEqual(sent.length, 1);
   assert.strictEqual(sent[0].cmd, "schedule_five_point");
   assert.deepStrictEqual(JSON.parse(sent[0].args), { config_ids: [16, 26], requested_events: 250000 });
   assert.ok(RUNDB.state.action.result.ok);
   assert.strictEqual(RUNDB.state.action.busy, false, "the button comes back");

   RUNDB.state.action.preview = PREVIEWED;
   RUNDB.state.action.busy = false;
   RUNDB.confirmSchedule();
   assert.ok(asked, "the shifter is asked before anything is written");
   assert.match(asked, /This will schedule 5 runs/);
   assert.doesNotMatch(asked, /<script/, "the sentence is escaped into the dialog");
});

test("an id the page has never seen is resolved and keyed by its type", async () => {
   globalThis.mjsonrpc_call = async (method, params) => ({ result: { status: 1, reply: JSON.stringify({
      ok: true, cmd: "config", data: { config: { config_id: 44, config_type: "pim1_epics",
         do_not_use: false, known_type: true, values: { id: 44, seq_id: 1, "QTA11:SOL:2": -21.23 } } }
   }) } });
   RUNDB.state.action.selection = {};
   RUNDB.state.action.extra = {};
   await RUNDB.lookupConfig("44");
   assert.strictEqual(RUNDB.state.action.extra.info.config_type, "pim1_epics");
   assert.deepStrictEqual(RUNDB.actionArgs(RUNDB.state.action.selection, 1).config_ids, [44]);
});

test("a target_position or a do-not-use id typed by hand is refused here", async () => {
   const reply = (cfg) => async () => ({ result: { status: 1, reply: JSON.stringify({
      ok: true, cmd: "config", data: { config: cfg } }) } });
   RUNDB.state.action.selection = {};
   globalThis.mjsonrpc_call = reply({ config_id: 6, config_type: "target_position", do_not_use: false });
   await RUNDB.lookupConfig("6");
   assert.match(RUNDB.state.action.extra.error, /five-point sequence sets itself/);
   assert.deepStrictEqual(RUNDB.state.action.selection, {});

   globalThis.mjsonrpc_call = reply({ config_id: 17, config_type: "degrader_position", do_not_use: true });
   await RUNDB.lookupConfig("17");
   assert.match(RUNDB.state.action.extra.error, /marked do not use/);
   assert.deepStrictEqual(RUNDB.state.action.selection, {});
});

test("the polls leave a panel somebody is filling in alone", () => {
   RUNDB.state.status = { client: { actions_allowed: true } };
   RUNDB.state.queue = { runs: CONFIG_ROWS };
   RUNDB.state.runs = {};
   RUNDB.renderActions();
   const sig = RUNDB.state.action.sig;
   RUNDB.refreshActions();
   assert.strictEqual(RUNDB.state.action.sig, sig, "nothing changed, nothing redrawn");
   RUNDB.state.queue = { runs: [] };
   RUNDB.refreshActions();
   assert.notStrictEqual(RUNDB.state.action.sig, sig, "a new set of configurations does redraw it");
   RUNDB.state.status = null;
   RUNDB.state.queue = null;
   RUNDB.state.action.selection = {};
});

test("the detail of a run shows the full values and the shell equivalent", () => {
   const html = RUNDB.detailHtml({
      run: RUN_WITH_CONFIG,
      configs: [{ config_id: 12, config_type: "target_position", do_not_use: false,
                  values: { x: 5.0, y: -2.0, comment: "centre" } }],
      files: RUN_WITH_CONFIG.files,
      jobs: [{ job_type: "nearline", status: "DONE" }],
      sequence: { id: 2, status: "RUNNING", on_complete: "nothing",
                  runs: [{ id: 63, run_number: 260, status: "DONE" }] }
   });
   assert.match(html, /centre/);
   assert.match(html, /run00260\.mid\.lz4/);
   assert.match(html, /python -m pioneer\.rundb\.view run 63/);
});

test("a config type the client cannot read is said, not shown empty", () => {
   const html = RUNDB.detailHtml({
      run: RUN_WITH_CONFIG,
      configs: [{ config_id: 9, config_type: "isel_config", values: null }],
      files: [], jobs: [], sequence: null
   });
   assert.match(html, /does not know how to read a isel_config row/);
});

test("database text is escaped, not injected", () => {
   const html = RUNDB.detailHtml({
      run: { id: 1, run_number: 1, status: "DONE" },
      configs: [{ config_id: 1, config_type: "x", values: { note: "<script>alert(1)</script>" } }],
      files: [], jobs: [], sequence: null
   });
   assert.doesNotMatch(html, /<script>alert/);
   assert.match(html, /&lt;script&gt;/);
});

// ---------------------------------------------------------------------------
// Things the first review caught
// ---------------------------------------------------------------------------

test("a file name puts the dot back", () => {
   // open_file() splits run00260.mid.lz4 on the FIRST dot, so fileext is stored
   // as "mid.lz4" with no leading dot of its own.
   assert.strictEqual(RUNDB.fileName({ filebase: "run09004", fileext: "mid.lz4" }), "run09004.mid.lz4");
   assert.strictEqual(RUNDB.fileName({ filebase: "run09004", fileext: "" }), "run09004");
   assert.strictEqual(RUNDB.fileName({}), "");
   const html = RUNDB.runlogHtml([RUN_WITH_CONFIG], null, null, null);
   assert.match(html, /run00260\.mid\.lz4/);
   assert.doesNotMatch(html, /run00260mid/);
});

test("an unreachable database is read out of a status reply that is itself ok", () => {
   // `status` answers ok:true and says reachable:false -- that is its job.
   const down = RUNDB.databaseError({
      client: { last_error: "connection refused" },
      database: { reachable: false, dsn: "host=pinky dbname=pioneer user=readonly password=***" }
   });
   assert.strictEqual(down.kind, "db");
   assert.match(down.message, /cannot reach host=pinky/);
   assert.strictEqual(down.hint, "connection refused");
   assert.doesNotMatch(down.message, /readonly@/);
   assert.strictEqual(RUNDB.databaseError({ database: { reachable: true } }), null);
   assert.strictEqual(RUNDB.databaseError(null), null);
});

test("an ok status does not stamp the last-read time", () => {
   // Otherwise "last read at" marches forward all through a database outage and
   // the tables never go dim.
   RUNDB.state.lastGood = null;
   RUNDB.state.lastGoodMs = 0;
   RUNDB.note({ ok: true, data: {} }, false);
   assert.strictEqual(RUNDB.state.lastGood, null, "a status reply proves nothing about the database");
   RUNDB.note({ ok: true, data: {} }, true);
   assert.ok(RUNDB.state.lastGood, "a reply that carried rows does");
});

test("the database outage survives the status poll and goes stale", async () => {
   // The whole path: status ok + reachable false -> prose, dimmed tables, and a
   // last-read time that stops moving.
   RUNDB.state.lastGood = "14:03:11";
   RUNDB.state.lastGoodMs = Date.now() - 60000;
   RUNDB.state.dbError = null;
   RUNDB.state.clientError = null;
   globalThis.mjsonrpc_call = async (method, params) => {
      if (params.cmd === "status") {
         return { result: { status: 1, reply: JSON.stringify({ ok: true, cmd: "status", data: {
            client: { name: "RunDBView", actions_allowed: false, last_error: "connection refused" },
            database: { reachable: false, dsn: "host=pinky dbname=pioneer" },
            counts: null, statuses: []
         } }) } };
      }
      return { result: { status: 1, reply: JSON.stringify({ ok: false, cmd: params.cmd,
         error: { kind: "db", message: "could not connect to server" } }) } };
   };
   const before = RUNDB.state.statuses;
   await RUNDB.pollSlow();
   assert.ok(RUNDB.state.dbError, "the outage is recorded");
   assert.strictEqual(RUNDB.state.lastGood, "14:03:11", "the last-read time did not move");
   assert.strictEqual(RUNDB.state.statuses, before, "an empty statuses list does not wipe the table");
   const html = RUNDB.staleHtml(RUNDB.state);
   assert.match(html, /run database did not answer/);
   assert.match(html, /last read at 14:03:11/);
   RUNDB.state.dbError = null;
});

test("a hung client becomes a sentence, not a stalled page", async () => {
   globalThis.mjsonrpc_call = () => new Promise(() => { /* never settles */ });
   const started = Date.now();
   const env = await R.withTimeout(Promise.resolve("x"), 50, "probe");   // the happy path
   assert.strictEqual(env, "x");
   await assert.rejects(R.withTimeout(new Promise(() => {}), 20, "runlog"),
      (e) => e.rundbTimeout === true);
   assert.ok(Date.now() - started < 5000);
   const html = RUNDB.staleHtml({
      otherError: { kind: "timeout", message: "RunDBView did not answer runlog within 15 s" },
      lastGood: "14:03:11"
   });
   assert.match(html, /not coming back/);
   assert.match(html, /last read at 14:03:11/);
   assert.ok(R.CALL_TIMEOUT_MS >= 5000 && R.CALL_TIMEOUT_MS <= 60000);
});

test("the runlog keeps a bounded number of rows", () => {
   RUNDB.state.runs = {};
   RUNDB.state.runsCapped = false;
   const page = [];
   for (let i = 1; i <= RUNDB.RUNS_CAP + 25; i++) page.push({ id: i, run_number: i, status: "DONE" });
   RUNDB.mergeRuns(page);
   assert.strictEqual(Object.keys(RUNDB.state.runs).length, RUNDB.RUNS_CAP);
   assert.ok(RUNDB.state.runsCapped);
   assert.ok(RUNDB.state.runs[RUNDB.RUNS_CAP + 25], "the newest run is kept");
   assert.ok(!RUNDB.state.runs[1], "the oldest is dropped");
   RUNDB.state.runs = {};
   RUNDB.state.runsCapped = false;
});

test("the line under the runlog says which of the three things is true", () => {
   assert.match(RUNDB.moreHtml(17, false), /Show older runs/);
   assert.match(RUNDB.moreHtml(null, false), /that is the whole runlog/);
   const capped = RUNDB.moreHtml(17, true);
   assert.match(capped, /showing the newest 400 runs/);
   assert.match(capped, /--before-id N/);
   assert.doesNotMatch(capped, /Show older runs/);
   // and every one of them offers the manual refresh
   [RUNDB.moreHtml(17, false), RUNDB.moreHtml(null, false), capped].forEach((html) => {
      assert.match(html, /id="rundb-refresh-button"/);
   });
});

test("the strip shows what the status counts already say", () => {
   const health = { lastGood: "14:03:11" };
   const odb = { state: 3, runNumber: 260, runDbPk: 63 };
   const counts = { runs_total: 200, queue_pending: 6, queue_running: 1, jobs_pending: 1, jobs_failed: 2 };
   const busy = RUNDB.stripHtml(odb, health, counts, { PENDING: 5, HOLDING: 1, RUNNING: 1 });
   assert.match(busy, /Queue<\/b> 1 HOLDING, 1 RUNNING, 5 PENDING/);
   assert.match(busy, /Nearline<\/b> 2 failed/);
   // before the queue has been read there is a total but no status half
   assert.match(RUNDB.stripHtml(odb, health, counts), /Queue<\/b> 7 queued/);
   const quiet = RUNDB.stripHtml(odb, health, { runs_total: 200, queue_pending: 0, queue_running: 0,
                                                jobs_pending: 0, jobs_failed: 0 }, {});
   assert.doesNotMatch(quiet, /Nearline/, "no failed jobs, no chip");
   assert.doesNotMatch(RUNDB.stripHtml(odb, health), /Queue<\/b>/, "nothing claimed before status answers");
});

test("the database chip follows the status reply", () => {
   const odb = { state: 3, runNumber: 260, runDbPk: 63 };
   assert.match(RUNDB.stripHtml(odb, { dbError: { kind: "db" }, lastGood: "14:03:11" }), /not answering/);
   assert.match(RUNDB.stripHtml(odb, { clientError: { kind: "client_down" }, lastGood: "14:03:11" }),
                /client not answering/);
   assert.match(RUNDB.stripHtml(odb, { stale: true, lastGood: "14:03:11" }), /stale/);
});

test("a run-number span is escaped like everything else from the database", () => {
   const html = RUNDB.sequencesHtml([{ id: 1, status: "DONE", n_runs: 1, n_done: 1,
                                       first_run: "<script>", last_run: "9" }]);
   assert.doesNotMatch(html, /<script>/);
   assert.match(html, /&lt;script&gt;/);
});

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

test("the page works before /RunDBView has been seeded", () => {
   const odb = RUNDB.odbFromValues(new Array(RUNDB.ODB_PATHS.length).fill(null));
   const cfg = RUNDB.configFrom(odb);
   assert.deepStrictEqual(cfg, RUNDB.DEFAULTS);
   assert.strictEqual(cfg["Allow actions"], false);
});

test("the ODB overrides what it sets, and the row limit is capped", () => {
   const values = new Array(RUNDB.ODB_PATHS.length).fill(null);
   values[9] = 5000;        // Runlog rows
   values[12] = 45;         // Stale seconds
   values[13] = true;       // Allow actions
   const cfg = RUNDB.configFrom(RUNDB.odbFromValues(values));
   assert.strictEqual(cfg["Runlog rows"], RUNDB.RUNLOG_ROWS_MAX);
   assert.strictEqual(cfg["Stale seconds"], 45);
   assert.strictEqual(cfg["Allow actions"], true);
});

test("the ODB paths are the ones the plan names", () => {
   ["/Runinfo/State", "/Runinfo/Run number", "/Runinfo/Run DB PK",
    "/PySequencer/State/Running", "/PySequencer/State/Finished", "/PySequencer/State/SFilename",
    "/PySequencer/Param/Value/nEv", "/RunDBView/Allow actions"].forEach((p) => {
      assert.ok(RUNDB.ODB_PATHS.indexOf(p) >= 0, p + " is not read");
   });
});
