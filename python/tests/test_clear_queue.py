"""Clearing the queue: which runs are cancelled, which are left, and what is written.

The question an operator asks of "Clear queue" is the same one these tests
ask: after pressing OK, which runs are `CANCELLED`, which are still waiting and
why, and what does the run database say about who did it.  Every test builds
the queue it needs from nothing -- a handful of `state.midas_run` rows with
chosen statuses and priorities -- so that each answer can be checked run by
run.

The rows are inserted directly rather than through `interface`: the clear only
looks at `status` and `priority`, and a queue shaped by hand (ties, NULL
priorities, a HOLDING run at the head's priority) is the point.

These tests rebuild `pioneer_rundb_actions`, the action tests' own database,
for the same reason those tests do: the shared read-side database is asserted
on down to the number of waiting runs.
"""

import functools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import psycopg

from pioneer.rundb import actions, commands
from conftest import SCHEMA_FILE

ACTION_DB_NAME = "pioneer_rundb_actions"
PACKAGE_DIR = Path(__file__).resolve().parents[1]

# The role pinky's client writes as, created by db_config.sql with this
# development password.  One test clears the queue as it, so that a grant the
# action needs and the role lacks shows up here and not on the DAQ machine.
SHIFTER = {"user": "shifter", "password": "12345"}


class Armed:
    """The action module with a write connection string bound to it.

    What `rpc_server.ActionAdapter` is, without the `midas` package.
    """

    def __init__(self, module, write_dsn):
        self.module = module
        self.write_dsn = write_dsn

    def __getattr__(self, name):
        return functools.partial(getattr(self.module, name), write_dsn=self.write_dsn)


@pytest.fixture(scope="module")
def clear_db(scratch_dsn):
    """An empty run database of this module's own: the schema and nothing else."""
    dsn = psycopg.conninfo.make_conninfo(scratch_dsn, dbname=ACTION_DB_NAME)
    admin_dsn = psycopg.conninfo.make_conninfo(scratch_dsn, dbname="postgres")

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{ACTION_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{ACTION_DB_NAME}"')
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SCHEMA_FILE.read_text())
    return dsn


class Queue:
    """A queue built by hand, and the questions a test asks of it."""

    def __init__(self, dsn):
        self.dsn = dsn

    def add(self, status, priority, events=1000):
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            return conn.execute(
                "INSERT INTO state.midas_run (status, priority, requested_events) "
                "VALUES (%s, %s, %s) RETURNING id", (status, priority, events)
            ).fetchone()[0]

    def set_status(self, run_id, status):
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("UPDATE state.midas_run SET status = %s WHERE id = %s",
                         (status, run_id))

    def statuses(self):
        with psycopg.connect(self.dsn) as conn:
            return dict(conn.execute("SELECT id, status FROM state.midas_run").fetchall())

    def annotations(self):
        with psycopg.connect(self.dsn) as conn:
            return conn.execute(
                "SELECT run_id, author, note FROM logs.run_annotations ORDER BY run_id"
            ).fetchall()


@pytest.fixture
def queue(clear_db):
    """An empty queue for every test; the runs and their annotations go."""
    with psycopg.connect(clear_db, autocommit=True) as conn:
        conn.execute("TRUNCATE state.midas_run CASCADE")
    return Queue(clear_db)


@pytest.fixture
def armed(clear_db):
    return Armed(actions, clear_db)


def dispatch(armed, cmd, args, running, allowed=True, loaded=None):
    """One command through the command layer, as the RPC server sends it."""
    extra = {"sequencer_running": running}
    if loaded is not None:
        extra["loaded_run_id"] = loaded
    return json.loads(commands.dispatch(
        None, armed, cmd, args, actions_allowed=allowed, server_args=extra))


def preview(armed, running, include_holding=False, loaded=None):
    envelope = dispatch(armed, "preview_clear_queue",
                        {"include_holding": include_holding}, running, loaded=loaded)
    assert envelope["ok"] is True, envelope
    return envelope["data"]


def clear(armed, run_ids, running, include_holding=False, operator="A. Shifter",
          loaded=None):
    envelope = dispatch(armed, "clear_queue",
                        {"run_ids": run_ids, "include_holding": include_holding,
                         "operator": operator}, running, loaded=loaded)
    assert envelope["ok"] is True, envelope
    return envelope["data"]


def a_mixed_queue(queue):
    """Two waiting runs, one on hold, and one each of taken and finished."""
    return {
        "first": queue.add("PENDING", 1),
        "second": queue.add("PENDING", 2),
        "held": queue.add("HOLDING", 3),
        "claimed": queue.add("CLAIMED", 4),
        "running": queue.add("RUNNING", 5),
        "done": queue.add("DONE", 6),
    }


# --------------------------------------------------------------------------
# which statuses
# --------------------------------------------------------------------------

def test_pending_only_by_default(queue, armed):
    runs = a_mixed_queue(queue)

    data = preview(armed, running=False)
    assert data["statuses"] == ["PENDING"]
    assert data["sequencer_running"] is False
    assert [run["id"] for run in data["runs"]] == [runs["first"], runs["second"]]
    assert data["will_cancel"] == [runs["first"], runs["second"]]
    assert data["kept_head"] == [] and data["head_reason"] is None
    assert data["total"] == 2 and data["capped"] is False
    assert set(data["runs"][0]) == {"id", "status", "priority", "midas_run_number",
                                    "requested_events"}

    # Even when every id in the table is named, only PENDING ones go.
    result = clear(armed, sorted(runs.values()), running=False)
    assert result["cancelled"] == [runs["first"], runs["second"]]
    assert result["kept_head"] == []
    assert result["skipped"] == [
        {"id": runs["held"], "status": "HOLDING"},
        {"id": runs["claimed"], "status": "CLAIMED"},
        {"id": runs["running"], "status": "RUNNING"},
        {"id": runs["done"], "status": "DONE"},
    ]
    assert result["statuses"] == ["PENDING"]
    assert result["operator"] == "A. Shifter"

    after = queue.statuses()
    assert after[runs["first"]] == after[runs["second"]] == "CANCELLED"
    assert after[runs["held"]] == "HOLDING"
    assert after[runs["claimed"]] == "CLAIMED"
    assert after[runs["running"]] == "RUNNING"
    assert after[runs["done"]] == "DONE"


def test_holding_too_when_asked(queue, armed):
    runs = a_mixed_queue(queue)

    data = preview(armed, running=False, include_holding=True)
    assert data["statuses"] == ["PENDING", "HOLDING"]
    assert data["will_cancel"] == [runs["first"], runs["second"], runs["held"]]

    result = clear(armed, sorted(runs.values()), running=False, include_holding=True)
    assert result["cancelled"] == [runs["first"], runs["second"], runs["held"]]
    # Taken and finished runs are never in the chosen statuses.
    assert [entry["status"] for entry in result["skipped"]] == ["CLAIMED", "RUNNING", "DONE"]

    after = queue.statuses()
    assert after[runs["held"]] == "CANCELLED"
    assert [after[runs[name]] for name in ("claimed", "running", "done")] == \
        ["CLAIMED", "RUNNING", "DONE"]


# --------------------------------------------------------------------------
# the head of the queue
# --------------------------------------------------------------------------

def test_the_head_is_kept_while_the_sequencer_runs_ties_included(queue, armed):
    """Every PENDING run at the lowest priority stays; a HOLDING run at that
    priority is not the head, because the sequencer never takes one."""
    tie_a = queue.add("PENDING", 4)
    tie_b = queue.add("PENDING", 4)
    later = queue.add("PENDING", 7)
    held_at_head = queue.add("HOLDING", 4)
    queue.add("RUNNING", 1)          # a lower priority, but not PENDING: not the head

    data = preview(armed, running=True, include_holding=True)
    assert data["sequencer_running"] is True
    assert data["kept_head"] == [tie_a, tie_b]
    assert data["head_reason"] == actions.HEAD_REASON
    assert sorted(data["will_cancel"]) == sorted([later, held_at_head])

    result = clear(armed, [tie_a, tie_b, later, held_at_head], running=True,
                   include_holding=True)
    assert result["cancelled"] == sorted([later, held_at_head])
    assert result["kept_head"] == [tie_a, tie_b]
    assert result["skipped"] == []
    assert result["sequencer_running"] is True

    after = queue.statuses()
    assert after[tie_a] == after[tie_b] == "PENDING"

    # The annotations say why the head is still there, the same words on
    # every run of the batch.
    notes = {run_id: note for run_id, _, note in queue.annotations()}
    assert set(notes) == {later, held_at_head}
    assert set(notes.values()) == {
        f"cancelled from the RunDB page (Clear queue); sequencer running, "
        f"next run(s) {tie_a}, {tie_b} left PENDING"}


def test_the_loaded_run_is_kept_even_when_the_head_has_moved(queue, armed):
    """The sequencer picked X; then a run with a lower priority number was
    queued, so X is no longer the head -- but it is still the run being set up."""
    loaded = queue.add("PENDING", 5)
    other = queue.add("PENDING", 6)
    jumped_in = queue.add("PENDING", 1)

    data = preview(armed, running=True, loaded=loaded)
    assert data["kept_head"] == sorted([loaded, jumped_in])
    assert data["will_cancel"] == [other]
    assert data["head_reason"] == actions.HEAD_REASON

    # The page sends only what will be cancelled; the CLI may send everything.
    result = clear(armed, [loaded, other, jumped_in], running=True, loaded=loaded)
    assert result["cancelled"] == [other]
    assert result["kept_head"] == sorted([loaded, jumped_in])
    assert queue.statuses()[loaded] == "PENDING"


def test_the_loaded_run_protects_nothing_once_the_sequencer_is_stopped(queue, armed):
    loaded = queue.add("PENDING", 5)
    head = queue.add("PENDING", 1)

    data = preview(armed, running=False, loaded=loaded)
    assert data["kept_head"] == []
    result = clear(armed, [loaded, head], running=False, loaded=loaded)
    assert result["cancelled"] == sorted([loaded, head])


def test_a_loaded_run_that_is_no_longer_pending_is_not_kept(queue, armed):
    """The ODB keeps the id after the run has started; it then protects nothing."""
    started = queue.add("RUNNING", 5)
    head = queue.add("PENDING", 1)
    later = queue.add("PENDING", 2)

    data = preview(armed, running=True, loaded=started)
    assert data["kept_head"] == [head]
    result = clear(armed, [head, later], running=True, loaded=started)
    assert result["cancelled"] == [later] and result["kept_head"] == [head]


def test_a_kept_run_the_page_did_not_send_is_named_in_the_note_only(queue, armed):
    """The page sends `will_cancel`, so a kept run is never cancelled by that
    OK -- even if the sequencer stopped in between -- and is not in the reply's
    `kept_head` either, which is about the ids sent."""
    head = queue.add("PENDING", 1)
    later = queue.add("PENDING", 2)
    shown = preview(armed, running=True)
    assert shown["will_cancel"] == [later]

    result = clear(armed, shown["will_cancel"], running=False)
    assert result["cancelled"] == [later]
    assert result["kept_head"] == []
    assert queue.statuses()[head] == "PENDING"


def test_a_null_priority_head_is_kept_too(queue, armed):
    """`ORDER BY priority ASC` puts NULL last, so a NULL run is the head only
    when every PENDING run has one -- and then they all are."""
    only_a = queue.add("PENDING", None)
    only_b = queue.add("PENDING", None)

    data = preview(armed, running=True)
    assert data["kept_head"] == [only_a, only_b]
    assert data["will_cancel"] == []
    result = clear(armed, [only_a, only_b], running=True)
    assert result["cancelled"] == [] and result["kept_head"] == [only_a, only_b]

    # With a numbered run in the queue, that one is the head and NULL is not.
    numbered = queue.add("PENDING", 9)
    data = preview(armed, running=True)
    assert data["kept_head"] == [numbered]
    # Listed in the queue's own order: priority, NULLs last, then id.
    assert [run["id"] for run in data["runs"]] == [numbered, only_a, only_b]
    result = clear(armed, [numbered, only_a, only_b], running=True)
    assert result["cancelled"] == [only_a, only_b]
    assert result["kept_head"] == [numbered]


def test_the_head_is_cancelled_when_the_sequencer_is_stopped(queue, armed):
    head = queue.add("PENDING", 1)
    tie = queue.add("PENDING", 1)
    later = queue.add("PENDING", 2)

    data = preview(armed, running=False)
    assert data["kept_head"] == []
    assert data["will_cancel"] == [head, tie, later]

    result = clear(armed, [head, tie, later], running=False)
    assert result["cancelled"] == [head, tie, later]
    assert result["kept_head"] == []
    notes = [note for _, _, note in queue.annotations()]
    assert notes == [actions.CLEAR_NOTE.format(origin=actions.ORIGIN_PAGE)] * 3


def test_without_a_server_value_the_sequencer_counts_as_running(queue, armed):
    """A caller of the action that does not say keeps the head."""
    head = queue.add("PENDING", 1)
    later = queue.add("PENDING", 2)

    result = actions.clear_queue(run_ids=[head, later], operator="me",
                                 write_dsn=armed.write_dsn)
    assert result["cancelled"] == [later]
    assert result["kept_head"] == [head]
    assert result["sequencer_running"] is True


# --------------------------------------------------------------------------
# between the preview and OK
# --------------------------------------------------------------------------

def test_a_run_scheduled_after_the_preview_is_left_alone(queue, armed):
    first = queue.add("PENDING", 1)
    second = queue.add("PENDING", 2)
    shown = preview(armed, running=False)

    newcomer = queue.add("PENDING", 3)
    result = clear(armed, [run["id"] for run in shown["runs"]], running=False)

    assert result["cancelled"] == [first, second]
    assert queue.statuses()[newcomer] == "PENDING"
    assert newcomer not in [run_id for run_id, _, _ in queue.annotations()]


def test_a_run_that_changed_since_the_preview_is_skipped(queue, armed):
    taken = queue.add("PENDING", 1)
    held = queue.add("PENDING", 2)
    stays = queue.add("PENDING", 3)
    shown = [run["id"] for run in preview(armed, running=False)["runs"]]
    gone = max(shown) + 100_000            # an id the database does not have

    # Between the dialog opening and OK: the sequencer took one, a person put
    # another on hold.
    queue.set_status(taken, "RUNNING")
    queue.set_status(held, "HOLDING")

    result = clear(armed, shown + [gone], running=False)
    assert result["cancelled"] == [stays]
    assert result["skipped"] == [{"id": taken, "status": "RUNNING"},
                                 {"id": held, "status": "HOLDING"},
                                 {"id": gone, "status": None}]
    after = queue.statuses()
    assert after[taken] == "RUNNING" and after[held] == "HOLDING"


# --------------------------------------------------------------------------
# what is written beside the status
# --------------------------------------------------------------------------

def test_each_cancelled_run_gets_an_annotation_by_the_operator(queue, armed):
    runs = [queue.add("PENDING", priority) for priority in (1, 2, 3)]
    held = queue.add("HOLDING", 4)

    clear(armed, runs + [held], running=False, operator="  Jane Doe ")

    rows = queue.annotations()
    assert [row[0] for row in rows] == runs           # none for the skipped run
    assert {row[1] for row in rows} == {"Jane Doe"}    # trimmed
    assert {row[2] for row in rows} == {"cancelled from the RunDB page (Clear queue)"}


def test_a_failed_annotation_cancels_nothing(queue, armed, clear_db):
    """The UPDATE and the annotations are one transaction."""
    runs = [queue.add("PENDING", priority) for priority in (1, 2)]
    with psycopg.connect(clear_db, autocommit=True) as conn:
        conn.execute("""
            CREATE OR REPLACE FUNCTION public.no_notes() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'annotations are switched off'; END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER no_notes BEFORE INSERT ON logs.run_annotations
            FOR EACH ROW EXECUTE FUNCTION public.no_notes();
        """)
    try:
        envelope = dispatch(armed, "clear_queue",
                            {"run_ids": runs, "operator": "me"}, running=False)
    finally:
        with psycopg.connect(clear_db, autocommit=True) as conn:
            conn.execute("DROP TRIGGER IF EXISTS no_notes ON logs.run_annotations; "
                         "DROP FUNCTION IF EXISTS public.no_notes();")

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "db"
    assert "nothing was cancelled" in envelope["error"]["message"]
    assert [queue.statuses()[run_id] for run_id in runs] == ["PENDING", "PENDING"]
    assert queue.annotations() == []


def test_a_locked_row_gives_up_after_the_lock_timeout(queue, armed, clear_db):
    """Somebody else holding a row means "try again", not a page that hangs."""
    locked = queue.add("PENDING", 1)
    other = queue.add("PENDING", 2)

    holder = psycopg.connect(clear_db)
    try:
        holder.execute("SELECT id FROM state.midas_run WHERE id = %s FOR UPDATE", (locked,))
        envelope = dispatch(armed, "clear_queue",
                            {"run_ids": [locked, other], "operator": "me"}, running=False)
    finally:
        holder.rollback()
        holder.close()

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "db"
    assert "busy" in envelope["error"]["message"]
    assert [queue.statuses()[run_id] for run_id in (locked, other)] == ["PENDING", "PENDING"]


def test_the_shifter_role_can_clear(queue, clear_db):
    """pinky's client writes as `shifter`; the grants it needs are these.

    The cancelled run is in a sequence, so the status trigger
    (`state.trg_mr_status_change`) runs as `shifter` too and has to be able to
    mark the sequence FAILED -- which is what a cancelled member does to it.
    """
    run_id = queue.add("PENDING", 1)
    with psycopg.connect(clear_db, autocommit=True) as conn:
        seq_id = conn.execute(
            "INSERT INTO state.run_sequence (status, on_complete) "
            "VALUES ('PENDING', NULL) RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO state.runs_in_sequence (seq_id, midas_run_id) "
                     "VALUES (%s, %s)", (seq_id, run_id))
    as_shifter = psycopg.conninfo.make_conninfo(clear_db, **SHIFTER)

    result = clear(Armed(actions, as_shifter), [run_id], running=False)
    assert result["cancelled"] == [run_id]
    assert queue.annotations()[0][1] == "A. Shifter"
    with psycopg.connect(clear_db) as conn:
        status = conn.execute("SELECT status FROM state.run_sequence WHERE id = %s",
                              (seq_id,)).fetchone()[0]
    assert status == "FAILED"


def test_a_failed_commit_is_reported_as_unknown(queue, armed, clear_db):
    """A deferred check that fires at COMMIT stands in for the connection
    dropping at that moment: the reply must not claim nothing happened."""
    run_id = queue.add("PENDING", 1)
    with psycopg.connect(clear_db, autocommit=True) as conn:
        conn.execute("""
            CREATE OR REPLACE FUNCTION public.fail_at_commit() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'refused at commit'; END;
            $$ LANGUAGE plpgsql;
            CREATE CONSTRAINT TRIGGER fail_at_commit AFTER INSERT ON logs.run_annotations
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.fail_at_commit();
        """)
    try:
        envelope = dispatch(armed, "clear_queue", {"run_ids": [run_id], "operator": "me"},
                            running=False)
    finally:
        with psycopg.connect(clear_db, autocommit=True) as conn:
            conn.execute("DROP TRIGGER IF EXISTS fail_at_commit ON logs.run_annotations; "
                         "DROP FUNCTION IF EXISTS public.fail_at_commit();")

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "internal"
    assert "not known" in envelope["error"]["message"]
    assert "look at the queue" in envelope["error"]["hint"]


# --------------------------------------------------------------------------
# refusals, which change nothing
# --------------------------------------------------------------------------

def test_both_gates_are_closed_by_default(queue, armed):
    run_id = queue.add("PENDING", 1)
    payload = {"run_ids": [run_id], "operator": "me"}

    # No action module, and a module with the ODB flag false.
    for module, allowed in ((None, True), (armed, False)):
        envelope = dispatch(module, "clear_queue", payload, running=False, allowed=allowed)
        assert envelope["ok"] is False
        assert envelope["error"]["kind"] == "denied"
    assert queue.statuses()[run_id] == "PENDING"


def test_the_rpc_server_is_closed_by_default_and_keeps_the_head_without_midas(queue,
                                                                             clear_db):
    """The real callback and adapter: the flag starts false, and a sequencer
    state the ODB does not have counts as running."""
    pytest.importorskip("midas", reason="needs /software/midas/python on PYTHONPATH")
    from pioneer.rundb import rpc_server
    from test_rpc_server import StubClient, StubView

    head = queue.add("PENDING", 1)
    later = queue.add("PENDING", 2)
    server = rpc_server.Server(StubView(), rpc_server.ActionAdapter(actions, clear_db))
    client = StubClient()                                  # Allow actions: false
    payload = json.dumps({"run_ids": [head, later], "operator": "me"})

    status, reply = server.serve(client, "clear_queue", payload, 100_000)
    assert json.loads(reply)["error"]["kind"] == "denied"
    assert "refused action clear_queue" in client.messages[-1]
    assert set(queue.statuses().values()) == {"PENDING"}

    client.allow_actions = True                            # no /PySequencer key at all
    status, reply = server.serve(client, "clear_queue", payload, 100_000)
    data = json.loads(reply)["data"]
    assert data["sequencer_running"] is True
    assert data["cancelled"] == [later] and data["kept_head"] == [head]
    assert "accepted" in client.messages[-1]
    assert "kept next run" in client.messages[-1]


def test_a_caller_supplied_sequencer_state_is_refused(queue, armed):
    run_id = queue.add("PENDING", 1)
    envelope = json.loads(commands.dispatch(
        None, armed, "clear_queue",
        {"run_ids": [run_id], "operator": "me", "sequencer_running": False},
        actions_allowed=True))
    assert envelope["error"]["kind"] == "usage"
    assert queue.statuses()[run_id] == "PENDING"


@pytest.mark.parametrize("operator", ["", "   ", "x" * (actions.MAX_TEXT_LENGTH + 1), None])
def test_an_operator_that_is_missing_or_too_long_is_refused(queue, armed, operator):
    run_id = queue.add("PENDING", 1)
    payload = {"run_ids": [run_id]}
    if operator is not None:
        payload["operator"] = operator

    assert dispatch(armed, "clear_queue", payload, running=False)["error"]["kind"] == "usage"
    # And the action refuses on its own, for callers that skip the command layer.
    with pytest.raises(actions.ActionError) as caught:
        actions.clear_queue(run_ids=[run_id], operator=operator, sequencer_running=False,
                            write_dsn=armed.write_dsn)
    assert caught.value.kind == "usage"
    assert queue.statuses()[run_id] == "PENDING"


def test_too_many_run_ids_are_refused(queue, armed):
    run_id = queue.add("PENDING", 1)
    too_many = [run_id] + list(range(10**6, 10**6 + actions.MAX_CLEAR_IDS))

    envelope = dispatch(armed, "clear_queue", {"run_ids": too_many, "operator": "me"},
                        running=False)
    assert envelope["error"]["kind"] == "usage"
    with pytest.raises(actions.ActionError) as caught:
        actions.clear_queue(run_ids=too_many, operator="me", sequencer_running=False,
                            write_dsn=armed.write_dsn)
    assert caught.value.kind == "usage"
    assert queue.statuses()[run_id] == "PENDING"


def test_the_preview_is_capped(queue, armed, clear_db, monkeypatch):
    """A queue longer than the cap is listed up to it and says so."""
    monkeypatch.setattr(actions, "MAX_CLEAR_IDS", 3)
    runs = [queue.add("PENDING", priority) for priority in range(1, 6)]

    data = actions.preview_clear_queue(sequencer_running=False, write_dsn=clear_db)
    assert [run["id"] for run in data["runs"]] == runs[:3]
    assert data["will_cancel"] == runs[:3]
    assert data["total"] == 5 and data["capped"] is True


# --------------------------------------------------------------------------
# the command line gives the page's answer
# --------------------------------------------------------------------------

def run_cli(*arguments):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(PACKAGE_DIR), env.get("PYTHONPATH", "")])
    return subprocess.run([sys.executable, "-m", "pioneer.rundb.actions", *arguments],
                          capture_output=True, text=True, env=env, timeout=120)


def normalise(envelope):
    envelope = dict(envelope)
    envelope.pop("generated", None)
    envelope.pop("query_ms", None)
    return envelope


def test_the_command_line_and_the_rpc_give_the_same_reply(queue, armed, clear_db):
    head = queue.add("PENDING", 1)
    later = queue.add("PENDING", 2)
    held = queue.add("HOLDING", 3)

    # The preview: the CLI treats the sequencer as running unless told.
    result = run_cli("clear-queue", "--operator", "me", "--write-dsn", clear_db,
                     "--include-holding", "--json")
    assert result.returncode == actions.PREVIEW_EXIT == 3, result.stderr
    assert "--yes" in result.stderr
    expected = dispatch(armed, "preview_clear_queue", {"include_holding": True},
                        running=True)
    assert normalise(json.loads(result.stdout)) == normalise(expected)
    assert set(queue.statuses().values()) == {"PENDING", "HOLDING"}

    # The clear, with the sequencer treated as stopped.
    result = run_cli("clear-queue", "--operator", "me", "--write-dsn", clear_db,
                     "--include-holding", "--include-head", "--yes", "--json")
    assert result.returncode == 0, result.stderr
    from_cli = json.loads(result.stdout)
    assert from_cli["data"]["cancelled"] == [head, later, held]
    notes = {note for _, _, note in queue.annotations()}
    assert notes == {actions.CLEAR_NOTE.format(origin=actions.ORIGIN_CLI)}

    # Put the queue back and do the same through the command layer.
    with psycopg.connect(clear_db, autocommit=True) as conn:
        conn.execute("DELETE FROM logs.run_annotations")
    for run_id, status in ((head, "PENDING"), (later, "PENDING"), (held, "HOLDING")):
        queue.set_status(run_id, status)
    from_rpc = dispatch(armed, "clear_queue",
                        {"run_ids": [head, later, held], "include_holding": True,
                         "operator": "me"}, running=False)
    assert normalise(from_cli) == normalise(from_rpc)


def test_the_command_line_previews_in_words(queue, clear_db):
    head = queue.add("PENDING", 1)
    queue.add("PENDING", 2)

    result = run_cli("clear-queue", "--operator", "me", "--write-dsn", clear_db)
    assert result.returncode == actions.PREVIEW_EXIT
    assert "would cancel 1 of 2 run(s)" in result.stdout
    assert f"keep   run id {head:6d}" in result.stdout
    assert actions.HEAD_REASON in result.stdout
    assert set(queue.statuses().values()) == {"PENDING"}


def test_the_command_line_refuses_an_empty_operator(queue, clear_db):
    run_id = queue.add("PENDING", 1)
    result = run_cli("clear-queue", "--operator", "  ", "--write-dsn", clear_db, "--yes",
                     "--json")
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["kind"] == "usage"
    assert queue.statuses()[run_id] == "PENDING"


def test_the_command_line_does_not_need_a_scratch_database():
    """Only five-point keeps the scratch-only rule; a clear of a database with
    any other name gets as far as connecting (here: to a port nothing listens
    on, so nothing is reached)."""
    result = run_cli("clear-queue", "--operator", "me", "--write-dsn",
                     "host=127.0.0.1 port=1 dbname=not_a_scratch_db user=nobody "
                     "connect_timeout=2")
    assert result.returncode == 1
    assert result.stderr.startswith("error (db)")
    assert "refusing" not in result.stderr


def test_the_command_line_clears_exactly_the_ids_it_is_given(queue, clear_db):
    """`--run-id` is the dialog's list: a run queued after it is left alone."""
    first = queue.add("PENDING", 1)
    second = queue.add("PENDING", 2)
    newcomer = queue.add("PENDING", 3)

    result = run_cli("clear-queue", "--operator", "me", "--write-dsn", clear_db,
                     "--include-head", "--yes", "--json",
                     "--run-id", str(first), "--run-id", str(second))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data"]["cancelled"] == [first, second]
    assert queue.statuses()[newcomer] == "PENDING"


def test_the_command_line_keeps_the_run_it_is_told_is_loaded(queue, clear_db):
    head = queue.add("PENDING", 1)
    loaded = queue.add("PENDING", 4)
    other = queue.add("PENDING", 5)

    result = run_cli("clear-queue", "--operator", "me", "--write-dsn", clear_db,
                     "--keep-run-id", str(loaded), "--yes", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)["data"]
    # Without --run-id, --yes clears a fresh preview's will_cancel list.
    assert data["cancelled"] == [other]
    assert queue.statuses()[head] == queue.statuses()[loaded] == "PENDING"

    result = run_cli("clear-queue", "--operator", "me", "--write-dsn", clear_db,
                     "--keep-run-id", str(loaded), "--include-head")
    assert result.returncode == 2              # argparse: the two contradict
