//
// Tests for the pure parts of the ConfigDB page, run with `node --test`.
//
//   docker run --rm --cpus=8 -m 4g -v "$PWD/beamtime2026_pie5/custom:/w" -w /w \
//       node:22-alpine node --test js/cfgdb.test.js
//
// Loaded the way the page loads them: rundb-rpc.js first, which publishes
// RunDbRpc on the global, then cfgdb.js, which reads it. No DOM: what is
// checked is the HTML the builders return and what readTable() keeps.
//

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const R = require("./rundb-rpc.js");
const CFGDB = require("./cfgdb.js");

// ---------------------------------------------------------------------------
// Which rows are machine-written: the same answers as the client
// ---------------------------------------------------------------------------

test("autoKind agrees with the examples the Python side checks too", () => {
   // python/tests/test_config_list.py reads the same file against
   // pioneer.rundb.view.auto_kind, so the page and the client cannot drift.
   const file = path.join(__dirname, "cfgdb-auto-kinds.json");
   const cases = JSON.parse(fs.readFileSync(file, "utf8")).cases;
   assert.ok(cases.length > 10);
   cases.forEach((c) => {
      assert.strictEqual(CFGDB.autoKind({ comment: c.comment }), c.kind,
         "comment " + JSON.stringify(c.comment));
   });
   assert.strictEqual(CFGDB.autoKind(null), "");
   assert.strictEqual(CFGDB.autoKind({}), "");
});

test("every kind has a label and the predicates follow autoKind", () => {
   CFGDB.AUTO_KINDS.forEach((k) => {
      assert.ok(k.kind && k.label && typeof k.test === "function");
   });
   assert.strictEqual(CFGDB.isRunplanConfig({ comment: "runplan p step 1" }), true);
   assert.strictEqual(CFGDB.isMysteryConfig({ comment: "Mystery Configuration" }), true);
   assert.strictEqual(CFGDB.isMysteryConfig({ comment: "runplan p step 1" }), false);
});

test("the count line names each kind there is and only those", () => {
   assert.strictEqual(CFGDB.autoCountText({ runplan: 614, mystery: 749 }),
                      "614 runplan + 749 mystery configurations");
   assert.strictEqual(CFGDB.autoCountText({ runplan: 0, mystery: 1 }), "1 mystery configuration");
   assert.strictEqual(CFGDB.autoTotal({ runplan: 2, mystery: 3, unknown: 100 }), 5);
   const counts = CFGDB.autoCounts([{ comment: "runplan a b" }, { comment: "x" },
                                    { comment: "Mystery Configuration" }, { comment: "runplan c d" }]);
   assert.strictEqual(counts.runplan, 2);
   assert.strictEqual(counts.mystery, 1);
});

test("the show/hide line says what it is doing", () => {
   const ts = CFGDB.tableState("pie5_epics");
   ts.counts = { runplan: 614, mystery: 749 };
   assert.ok(CFGDB.autoToggleText(ts, false).startsWith("614 runplan + 749 mystery configurations hidden"));
   assert.ok(CFGDB.autoToggleText(ts, false).includes("cfg-auto-switch"));
   ts.auto = "loading";
   assert.ok(CFGDB.autoToggleText(ts, true).startsWith("Loading 614 runplan"));
   ts.auto = "loaded";
   assert.ok(CFGDB.autoToggleText(ts, true).startsWith("Showing 614 runplan"));
   ts.auto = "error";
   ts.autoError = { kind: "too_large", message: "reply too large", needed: 211761, limit: 2048 };
   let text = CFGDB.autoToggleText(ts, true);
   assert.ok(text.includes("too_large: reply too large (the reply needed 207 kB, the buffer was 2 kB)"));
   ts.autoError = { kind: "db", message: "timeout", hint: "<check postgres>" };
   text = CFGDB.autoToggleText(ts, true);
   assert.ok(text.includes("db: timeout &mdash; &lt;check postgres&gt;"));
   assert.ok(text.includes("cfg-auto-retry"));
   assert.ok(text.includes("cfg-auto-switch"));
});

// ---------------------------------------------------------------------------
// Rows and tables
// ---------------------------------------------------------------------------

test("a beamline row without values has no data-values and shows its seq_id", () => {
   const html = CFGDB.configRowHtml("beamline", { config_id: 771, config_type: "pie5_epics",
      do_not_use: false, comment: "runplan p step 1", seq_id: 22 });
   assert.ok(html.includes('data-config="771"'));
   assert.ok(!html.includes("data-values"));
   assert.ok(html.includes("<td>22</td>"));
   assert.ok(html.includes("data-auto='runplan'"));
   assert.ok(html.includes('class="config-ckbx-beam" value="pie5_epics:771"'));
});

test("a target row carries its values, its id for the five-point box, and escapes", () => {
   const html = CFGDB.configRowHtml("target", { config_id: 5, config_type: "target_position",
      do_not_use: false, comment: "<b>centre</b>", values: { id: 5, seq_id: 2, xpos: 1.5, ypos: -2, note: "it's" } });
   assert.ok(html.includes("data-values='"));
   assert.ok(html.includes("it&#39;s"));
   assert.ok(html.includes('id="target_position:5"'));
   assert.ok(html.includes("&lt;b&gt;centre&lt;/b&gt;"));
   assert.ok(html.includes("<td>1.5</td><td>-2</td>"));
});

test("a do-not-use row has no checkbox and no go-to button", () => {
   const html = CFGDB.configRowHtml("degrader", { config_id: 9, config_type: "degrader_position",
      do_not_use: true, comment: "old", values: { seq_id: 1, xpos: 3 } });
   assert.ok(!html.includes("checkbox"));
   assert.ok(!html.includes("cfg-goto"));
});

test("an error box names kind, message, hint and the sizes of a too_large reply", () => {
   const html = CFGDB.configErrorHtml("beamline settings", { kind: "too_large", message: "reply too large",
      hint: "ask again with a larger buffer or a smaller limit", needed: 1296092, limit: 1048576 });
   assert.ok(html.includes("rundb-alert red"));
   assert.ok(html.includes("Could not read the beamline settings."));
   assert.ok(html.includes("too_large: reply too large"));
   assert.ok(html.includes("needed 1266 kB, the buffer was 1024 kB"));
   assert.ok(html.includes("raise /RunDBView/Max reply kB"));
   assert.ok(CFGDB.configErrorHtml("x", null).includes("no answer"));
});

test("a table that failed keeps its current-setting row; the others are untouched", () => {
   const tables = { target: CFGDB.tableState("target_position"),
                    degrader: CFGDB.tableState("degrader_position"),
                    beamline: CFGDB.tableState(null) };
   tables.beamline.error = CFGDB.noBeamlineError("PiM2");
   const html = CFGDB.configTableHtml({
      target_positions: [{ config_id: 1, config_type: "target_position", do_not_use: false, comment: "c",
                           values: { seq_id: 2, xpos: 0, ypos: 0 } }],
      degrader_positions: [],
      beamline_settings: [],
      beamline: { name: "PiM2", table: null }
   }, tables);
   assert.ok(html.includes("/RunDBView/Beamline is &quot;PiM2&quot;; expected PiM1 or PiE5"));
   assert.ok(html.includes("PiM2 Beamline"));
   ["target", "degrader", "beamline"].forEach((level) => {
      assert.ok(html.includes('data-level="' + level + '"'), level);
      assert.ok(html.includes('id="cfg-table-' + level + '"'), level);
   });
   assert.ok(html.includes('id="5p_with_merge"'));
   assert.ok(html.includes('id="submit_config"'));
   assert.strictEqual((html.match(/cfg-table-error/g) || []).length, 1);
});

test("no beamline in the ODB is said as such, and is not a table name", () => {
   assert.strictEqual(CFGDB.beamlineTable("PiE5"), "pie5_epics");
   assert.strictEqual(CFGDB.beamlineTable("PiM1"), "pim1_epics");
   assert.strictEqual(CFGDB.beamlineTable(null), null);
   assert.ok(CFGDB.noBeamlineError(null).message.endsWith("is not set; expected PiM1 or PiE5"));
});

test("the values dialog escapes names and values", () => {
   const html = CFGDB.valuesTableHtml({ "A<B": "<x>", seq_id: 1 });
   assert.ok(html.includes("A&lt;B"));
   assert.ok(html.includes("&lt;x&gt;"));
});

// ---------------------------------------------------------------------------
// readTable(): what one `config` call leaves behind
// ---------------------------------------------------------------------------

function replyWith(env) {
   const seen = [];
   globalThis.mjsonrpc_call = async (method, params) => {
      seen.push(params);
      return { result: { status: 1, reply: JSON.stringify(env) } };
   };
   return seen;
}

test("readTable asks for the slim reply and keeps rows and counts", async () => {
   const seen = replyWith({ ok: true, cmd: "config", data: {
      config_type: "pie5_epics", auto: "hide", values: false,
      rows: [{ config_id: 26, config_type: "pie5_epics", do_not_use: false, comment: "MEG II", seq_id: 0 }],
      auto_counts: { runplan: 614, mystery: 749 } } });
   await CFGDB.readTable("beamline", "pie5_epics");
   assert.strictEqual(seen.length, 1);
   assert.strictEqual(seen[0].args, '{"id":"pie5_epics","auto":"hide","values":false}');
   assert.strictEqual(CFGDB.state.configuration_tables.beamline_settings.length, 1);
   assert.strictEqual(CFGDB.state.tables.beamline.error, null);
   assert.strictEqual(CFGDB.state.tables.beamline.counts.mystery, 749);
});

test("readTable keeps the targets' values", async () => {
   const seen = replyWith({ ok: true, cmd: "config", data: { rows: [], auto_counts: {} } });
   await CFGDB.readTable("target", "target_position");
   assert.strictEqual(seen[0].args, '{"id":"target_position","auto":"hide","values":true}');
});

test("an error envelope leaves an empty table and the error, never a throw", async () => {
   // The crash this guards against: `config` too_large twice, an ok:false
   // envelope with no data, and `.data.map` on undefined.
   const seen = replyWith({ ok: false, cmd: "config", error: { kind: "too_large",
      message: "reply too large", needed: 1296092, limit: 1048576 } });
   await CFGDB.readTable("beamline", "pie5_epics");
   assert.strictEqual(seen.length, 2);      // the one bigger retry rundb-rpc.js makes
   assert.strictEqual(CFGDB.state.configuration_tables.beamline_settings.length, 0);
   assert.strictEqual(CFGDB.state.tables.beamline.error.kind, "too_large");
});

test("a client older than the page is told to be restarted", async () => {
   replyWith({ ok: false, cmd: "config", error: { kind: "usage",
      message: "config does not take auto, values", hint: "accepted: id" } });
   await CFGDB.readTable("degrader", "degrader_position");
   assert.strictEqual(CFGDB.state.tables.degrader.error.kind, "usage");
   assert.ok(CFGDB.state.tables.degrader.error.hint.includes("older than this page"));
});

test("an ok reply without rows is an error too", async () => {
   replyWith({ ok: true, cmd: "config", data: [] });
   await CFGDB.readTable("degrader", "degrader_position");
   assert.strictEqual(CFGDB.state.tables.degrader.error.kind, "bad_reply");
   assert.strictEqual(CFGDB.state.configuration_tables.degrader_positions.length, 0);
});

test("readTable with no table makes no call", async () => {
   const seen = replyWith({ ok: true, cmd: "config", data: { rows: [], auto_counts: {} } });
   await CFGDB.readTable("beamline", null);
   assert.strictEqual(seen.length, 0);
   assert.strictEqual(CFGDB.state.tables.beamline.table, null);
});

void R;
