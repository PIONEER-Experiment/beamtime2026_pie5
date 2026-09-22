"""The only code in this package that writes, and the two gates in front of it.

The question every test here asks is the same one an operator asks: can this
client put runs into the queue, and under what circumstances.  So each test
counts the rows in `state.midas_run`, `state.run_sequence` and
`state.runs_in_sequence` before and after, and a refusal has to leave all three
exactly as they were.  A message saying "denied" over a queue that grew by five
runs would be worse than no message at all.

These tests need their own database.  They schedule runs, and the database the
read-side tests share is asserted on down to the number of waiting runs in the
queue, so writing into it would make those tests fail for a reason that has
nothing to do with them.  `write_db` therefore builds a second scratch database
on the same server, with the same seed data, and only this module touches it.
"""

import functools
import json

import pytest

import psycopg

from pioneer.rundb import actions, commands
from conftest import SCHEMA_FILE

# The database this module builds, drops and rebuilds, and which nothing else
# uses.  Not `pioneer_rundb_scratch`: that one belongs to the standalone
# experiment under scratch/rundb-page-standalone, and a test run would wipe
# what its page is showing.  All three names are ones the action's own command
# line will write to.
ACTION_DB_NAME = "pioneer_rundb_actions"


class Armed:
    """The action module with a write connection string bound to it.

    The same thing `rpc_server.ActionAdapter` is, written out here so that the
    database tests do not need the `midas` package.  The test that checks the
    real adapter is further down and skips without it.
    """

    def __init__(self, module, write_dsn):
        self.module = module
        self.write_dsn = write_dsn

    def __getattr__(self, name):
        return functools.partial(getattr(self.module, name), write_dsn=self.write_dsn)


@pytest.fixture(scope="module")
def write_db(scratch_dsn):
    """A scratch database of this module's own, seeded like the shared one."""
    dsn = psycopg.conninfo.make_conninfo(scratch_dsn, dbname=ACTION_DB_NAME)
    admin_dsn = psycopg.conninfo.make_conninfo(scratch_dsn, dbname="postgres")

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{ACTION_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{ACTION_DB_NAME}"')
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SCHEMA_FILE.read_text())
    return dsn


@pytest.fixture(scope="module")
def write_seed(write_db):
    """The seeded shape, and the ids the tests pick their configurations from."""
    from rundb_seed import seed

    info = seed(write_db)
    with psycopg.connect(write_db) as conn:
        info["degrader_ids"] = [
            row[0] for row in conn.execute(
                "SELECT id FROM config.degrader_position ORDER BY id LIMIT 2").fetchall()
        ]
        info["beam_config_id"] = conn.execute(
            "SELECT id FROM config.pim1_epics ORDER BY id LIMIT 1").fetchone()[0]
        info["target_config_id"] = conn.execute(
            "SELECT id FROM config.target_position ORDER BY id LIMIT 1").fetchone()[0]
        info["unused_id"] = conn.execute(
            "SELECT max(id) + 1000 FROM config.configuration").fetchone()[0]
    return info


@pytest.fixture
def armed(write_db):
    """The real action module, with the scratch database bound to it."""
    return Armed(actions, write_db)


def counts(dsn: str) -> dict:
    """Everything an action can add to, counted."""
    with psycopg.connect(dsn) as conn:
        return {
            "runs": conn.execute("SELECT count(*) FROM state.midas_run").fetchone()[0],
            "sequences": conn.execute(
                "SELECT count(*) FROM state.run_sequence").fetchone()[0],
            "memberships": conn.execute(
                "SELECT count(*) FROM state.runs_in_sequence").fetchone()[0],
            "configurations": conn.execute(
                "SELECT count(*) FROM config.configuration").fetchone()[0],
        }


def max_pending_priority(dsn: str):
    """The priority the next scheduled run continues from.

    `interface.schedule_new_run` takes the maximum over `PENDING` rows only, so
    that is what a new run's priority follows -- not the maximum over the whole
    table.
    """
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT max(priority) FROM state.midas_run WHERE status = 'PENDING'"
        ).fetchone()[0]


def refused(dsn, actions_module, payload, kind="usage"):
    """Send one action through the command layer and insist nothing changed."""
    before = counts(dsn)
    envelope = json.loads(commands.dispatch(
        None, actions_module, "schedule_five_point", payload, actions_allowed=True))
    assert envelope["ok"] is False, envelope
    assert envelope["error"]["kind"] == kind, envelope
    assert counts(dsn) == before
    return envelope


# --------------------------------------------------------------------------
# the gates
# --------------------------------------------------------------------------

def test_a_client_without_the_action_module_refuses_even_when_allowed(write_db, write_seed):
    """One gate open is not enough: there is nothing behind it to call."""
    before = counts(write_db)
    envelope = json.loads(commands.dispatch(
        None, None, "schedule_five_point",
        {"config_ids": [write_seed["degrader_ids"][0]]},
        actions_allowed=True))

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "denied"
    assert "actions are disabled" in envelope["error"]["message"]
    assert counts(write_db) == before


def test_a_client_with_the_module_refuses_while_the_flag_is_false(write_db, write_seed, armed):
    """The other gate: armed on the command line, not allowed in the ODB."""
    before = counts(write_db)
    envelope = json.loads(commands.dispatch(
        None, armed, "schedule_five_point",
        {"config_ids": [write_seed["degrader_ids"][0]]},
        actions_allowed=False))

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "denied"
    assert counts(write_db) == before


# --------------------------------------------------------------------------
# the action itself
# --------------------------------------------------------------------------

def test_scheduling_creates_five_runs_and_one_sequence(write_db, write_seed, armed):
    before = counts(write_db)
    first_priority = (max_pending_priority(write_db) or 0) + 1

    envelope = json.loads(commands.dispatch(
        None, armed, "schedule_five_point",
        {"config_ids": [write_seed["degrader_ids"][0], write_seed["beam_config_id"]],
         "requested_events": 12345},
        actions_allowed=True))
    assert envelope["ok"] is True, envelope
    data = envelope["data"]

    after = counts(write_db)
    assert after["runs"] == before["runs"] + 5
    assert after["sequences"] == before["sequences"] + 1
    assert after["memberships"] == before["memberships"] + 5
    # The configurations the caller chose already exist and the five target
    # positions come from the seeded sequence: a scan invents no new ones.
    assert after["configurations"] == before["configurations"]

    run_ids = data["run_ids"]
    assert len(run_ids) == 5 and len(set(run_ids)) == 5

    with psycopg.connect(write_db) as conn:
        rows = conn.execute(
            "SELECT id, status, priority, requested_events, midas_run_number "
            "FROM state.midas_run WHERE id = ANY(%s) ORDER BY id", (run_ids,)
        ).fetchall()
        members = conn.execute(
            "SELECT DISTINCT seq_id FROM state.runs_in_sequence "
            "WHERE midas_run_id = ANY(%s)", (run_ids,)
        ).fetchall()
        sequence_status = conn.execute(
            "SELECT status, on_complete FROM state.run_sequence WHERE id = %s",
            (data["sequence_id"],)
        ).fetchone()

    assert [row[1] for row in rows] == ["PENDING"] * 5
    # A queued run has no MIDAS run number until it actually starts.
    assert [row[4] for row in rows] == [None] * 5
    assert [row[3] for row in rows] == [12345] * 5
    assert sorted(row[2] for row in rows) == list(range(first_priority, first_priority + 5))
    assert [row[0] for row in members] == [data["sequence_id"]]
    assert sequence_status[0] == "PENDING"
    assert sequence_status[1] == "merge mt_add"


def test_the_reply_says_what_was_created(write_db, write_seed, armed):
    envelope = json.loads(commands.dispatch(
        None, armed, "schedule_five_point",
        {"config_ids": [write_seed["degrader_ids"][0]], "requested_events": 4242},
        actions_allowed=True))
    data = envelope["data"]

    assert set(data) >= {"sequence_id", "run_ids", "target_seq_id", "requested_events",
                         "configs_applied", "runs", "message"}
    assert data["target_seq_id"] == actions.TARGET_SEQ_ID
    assert data["requested_events"] == 4242
    assert data["configs_applied"] == [
        {"config_id": write_seed["degrader_ids"][0], "config_type": "degrader_position"}]

    # `interface.load_config_sequence` has no ORDER BY, so which run got which
    # point is not predictable -- the reply has to say, and the five points it
    # names are the five of the sequence.
    assert [run["run_id"] for run in data["runs"]] == data["run_ids"]
    positions = sorted((run["xpos"], run["ypos"]) for run in data["runs"])
    assert positions == sorted([(-17.0, -17.0), (-17.0, 17.0), (0.0, 0.0),
                                (17.0, -17.0), (17.0, 17.0)])
    assert all(run["status"] == "PENDING" for run in data["runs"])
    assert str(data["sequence_id"]) in envelope["data"]["message"]


def test_the_reply_is_json_and_says_nothing_secret(write_db, write_seed, armed):
    """Neither a success nor a failure may echo the connection string.

    The page shows these replies to whoever is on shift and they end up in
    screenshots, so nothing about how this client reaches the database belongs
    in one -- not the password, and not the host, user or database name either.
    """
    params = psycopg.conninfo.conninfo_to_dict(write_db)
    secrets = [params["password"], params["host"], params["user"], params["dbname"]]

    good = commands.dispatch(
        None, armed, "schedule_five_point",
        {"config_ids": [write_seed["degrader_ids"][0]]}, actions_allowed=True)
    # A database error, which is the reply most likely to quote the connection.
    bad = commands.dispatch(
        None, Armed(actions, "host=127.0.0.1 port=1 dbname=pioneer_rundb_actions "
                             "user=nobody password=hunter2"),
        "schedule_five_point", {"config_ids": [write_seed["degrader_ids"][0]]},
        actions_allowed=True)

    assert json.loads(good)["ok"] is True
    assert json.loads(bad)["error"]["kind"] == "db"
    for reply in (good, bad):
        assert "password" not in reply
        for secret in secrets + ["hunter2", "nobody", "127.0.0.1"]:
            assert str(secret) not in reply, secret


# --------------------------------------------------------------------------
# everything that is refused, and leaves the queue alone
# --------------------------------------------------------------------------

def test_a_configuration_marked_do_not_use_is_refused(write_db, write_seed, armed):
    blocked = write_seed["degrader_ids"][1]
    with psycopg.connect(write_db, autocommit=True) as conn:
        conn.execute("UPDATE config.configuration SET do_not_use = true WHERE id = %s",
                     (blocked,))
    try:
        envelope = refused(write_db, armed, {"config_ids": [blocked]})
        assert "do not use" in envelope["error"]["message"]
    finally:
        with psycopg.connect(write_db, autocommit=True) as conn:
            conn.execute("UPDATE config.configuration SET do_not_use = false WHERE id = %s",
                         (blocked,))


def test_two_configurations_of_one_type_are_refused(write_db, write_seed, armed):
    envelope = refused(write_db, armed, {"config_ids": write_seed["degrader_ids"]})
    assert "degrader_position" in envelope["error"]["message"]


def test_a_target_position_is_refused(write_db, write_seed, armed):
    """The five-point sequence owns that axis; a sixth point would replace it."""
    envelope = refused(write_db, armed,
                       {"config_ids": [write_seed["target_config_id"]]})
    assert "target_position" in envelope["error"]["message"]


def test_an_id_that_is_not_in_the_database_is_refused(write_db, write_seed, armed):
    envelope = refused(write_db, armed, {"config_ids": [write_seed["unused_id"]]})
    assert "no such configuration" in envelope["error"]["message"]


@pytest.mark.parametrize("events", [0, -1, actions.MAX_REQUESTED_EVENTS + 1])
def test_an_impossible_number_of_events_is_refused(write_db, write_seed, armed, events):
    refused(write_db, armed,
            {"config_ids": [write_seed["degrader_ids"][0]], "requested_events": events})


def test_events_that_are_not_a_number_are_refused(write_db, write_seed, armed):
    """The command layer stops this one first; the action stops it on its own too."""
    refused(write_db, armed,
            {"config_ids": [write_seed["degrader_ids"][0]], "requested_events": "lots"})

    before = counts(write_db)
    with pytest.raises(actions.ActionError) as caught:
        actions.schedule_five_point(config_ids=[write_seed["degrader_ids"][0]],
                                    requested_events="lots", write_dsn=write_db)
    assert caught.value.kind == "usage"
    assert counts(write_db) == before


def test_an_empty_or_repeated_id_list_is_refused(write_db, write_seed, armed):
    refused(write_db, armed, {"config_ids": []})

    before = counts(write_db)
    with pytest.raises(actions.ActionError) as caught:
        actions.schedule_five_point(
            config_ids=[write_seed["degrader_ids"][0], write_seed["degrader_ids"][0]],
            write_dsn=write_db)
    assert caught.value.kind == "usage"
    assert counts(write_db) == before


def test_a_client_with_no_write_connection_string_writes_nothing(write_db, write_seed):
    """`--allow-actions` without `--write-dsn` cannot happen, and if it did."""
    before = counts(write_db)
    with pytest.raises(actions.ActionError) as caught:
        actions.schedule_five_point(config_ids=[write_seed["degrader_ids"][0]],
                                    write_dsn=None)
    assert caught.value.kind == "internal"
    assert counts(write_db) == before


# --------------------------------------------------------------------------
# the dry run, and what it leaves behind
# --------------------------------------------------------------------------

def test_a_preview_checks_everything_and_writes_nothing(write_db, write_seed):
    before = counts(write_db)
    preview = actions.preview_five_point(
        config_ids=[write_seed["degrader_ids"][0]], requested_events=99,
        write_dsn=write_db)

    assert preview["would_create_runs"] == 5
    assert preview["requested_events"] == 99
    assert len(preview["target_positions"]) == 5
    assert counts(write_db) == before

    with pytest.raises(actions.ActionError):
        actions.preview_five_point(config_ids=[write_seed["target_config_id"]],
                                   write_dsn=write_db)
    assert counts(write_db) == before


def test_the_command_line_refuses_a_database_that_is_not_scratch(write_db, write_seed):
    """A connection string for the experiment's own database is not obeyed.

    It is refused before anything is connected to, so this test never opens a
    connection to the name it passes.
    """
    before = counts(write_db)
    code = actions.main(["five-point", "--config-id", str(write_seed["degrader_ids"][0]),
                         "--write-dsn", "host=example.invalid dbname=pioneer", "--confirm"])
    assert code == 2
    assert counts(write_db) == before


def test_the_command_line_writes_nothing_without_confirm(write_db, write_seed, capsys):
    before = counts(write_db)
    code = actions.main(["five-point", "--config-id", str(write_seed["degrader_ids"][0]),
                         "--events", "500", "--write-dsn", write_db])

    assert code == 2
    assert counts(write_db) == before
    out = capsys.readouterr()
    assert "would create 5 runs" in out.out
    assert "--confirm" in out.err


def test_the_command_line_creates_the_runs_with_confirm(write_db, write_seed):
    before = counts(write_db)
    code = actions.main(["five-point", "--config-id", str(write_seed["degrader_ids"][0]),
                         "--events", "500", "--write-dsn", write_db, "--confirm"])

    assert code == 0
    after = counts(write_db)
    assert after["runs"] == before["runs"] + 5
    assert after["sequences"] == before["sequences"] + 1


def test_the_read_path_is_not_left_pointing_at_the_write_database(write_db, write_seed, armed):
    """The action patches `pioneer.rundb.config`, and must put it back."""
    from pioneer.rundb import config

    before = (config.DB_HOST, config.DB_PORT, config.DB_NAME, config.connect.__defaults__)

    commands.dispatch(None, armed, "schedule_five_point",
                      {"config_ids": [write_seed["degrader_ids"][0]]},
                      actions_allowed=True)
    assert (config.DB_HOST, config.DB_PORT, config.DB_NAME,
            config.connect.__defaults__) == before

    # And after a failure, which is where a missing `finally` would show.
    commands.dispatch(None, armed, "schedule_five_point",
                      {"config_ids": [write_seed["unused_id"]]},
                      actions_allowed=True)
    assert (config.DB_HOST, config.DB_PORT, config.DB_NAME,
            config.connect.__defaults__) == before


# --------------------------------------------------------------------------
# the whole way in, from the RPC callback
# --------------------------------------------------------------------------

def test_the_rpc_path_reaches_the_action_and_leaves_a_message(monkeypatch):
    """Flag true plus a real `ActionAdapter` really does call the action.

    The action itself is replaced here: what is being checked is that the
    callback arms it, that the connection string comes from the client rather
    than from the caller, and that the write leaves a line in the MIDAS message
    log -- none of which needs a database.
    """
    pytest.importorskip("midas", reason="needs /software/midas/python on PYTHONPATH")
    from pioneer.rundb import rpc_server
    from test_rpc_server import StubClient, StubView

    seen = []

    def fake_schedule(**kwargs):
        seen.append(kwargs)
        return {"sequence_id": 3, "run_ids": [1, 2, 3, 4, 5]}

    monkeypatch.setattr(actions, "schedule_five_point", fake_schedule)

    adapter = rpc_server.ActionAdapter(actions, "host=nowhere dbname=pioneer_rundb_actions")
    server = rpc_server.Server(StubView(), adapter)
    client = StubClient(allow_actions=True)

    status, reply = server.serve(
        client, "schedule_five_point",
        '{"config_ids": [11, 12], "requested_events": 4321}', 100_000)

    assert json.loads(reply)["ok"] is True
    assert seen == [{"config_ids": [11, 12], "requested_events": 4321,
                     "write_dsn": "host=nowhere dbname=pioneer_rundb_actions"}]
    # The caller cannot choose the database: it is bound by the command line.
    assert client.reads.count(f"{rpc_server.ROOT}/Allow actions") == 1
    assert len(client.messages) == 1
    assert "schedule_five_point" in client.messages[0]
    assert "accepted" in client.messages[0]


def test_the_rpc_path_really_schedules_runs(write_db, write_seed):
    """The same callback, the real action module, the scratch database."""
    pytest.importorskip("midas", reason="needs /software/midas/python on PYTHONPATH")
    from pioneer.rundb import rpc_server
    from test_rpc_server import StubClient, StubView

    server = rpc_server.Server(StubView(), rpc_server.ActionAdapter(actions, write_db))
    client = StubClient(allow_actions=False)
    payload = json.dumps({"config_ids": [write_seed["beam_config_id"]],
                          "requested_events": 777})

    before = counts(write_db)
    status, reply = server.serve(client, "schedule_five_point", payload, 100_000)
    assert json.loads(reply)["error"]["kind"] == "denied"
    assert counts(write_db) == before
    # A shifter who presses the button and gets nothing has to be able to find
    # out why from the Messages page, so a refusal is logged too.
    assert len(client.messages) == 1
    assert "refused action schedule_five_point" in client.messages[-1]
    assert "Allow actions" in client.messages[-1]

    client.allow_actions = True
    status, reply = server.serve(client, "schedule_five_point", payload, 100_000)
    envelope = json.loads(reply)
    assert envelope["ok"] is True, envelope

    after = counts(write_db)
    assert after["runs"] == before["runs"] + 5
    assert after["sequences"] == before["sequences"] + 1
    assert after["memberships"] == before["memberships"] + 5
    assert "accepted" in client.messages[-1]
    # And the log says which runs, so the Messages page answers "what are
    # these?" without anybody opening psql.
    assert f"sequence {json.loads(reply)['data']['sequence_id']}" in client.messages[-1]


# --------------------------------------------------------------------------
# a configuration that is only half there
# --------------------------------------------------------------------------

def test_a_type_with_no_table_is_refused(write_db, write_seed, armed):
    """The seeded configuration whose `config_type` names nothing.

    Left to itself this schedules *zero* runs and an empty sequence, and says
    it worked: `interface.load_config` finds nothing, the product of the
    configuration lists is empty, and `schedule()` returns `[]`.
    """
    envelope = refused(write_db, armed,
                       {"config_ids": [write_seed["unknown_config_id"]]})
    assert "no table" in envelope["error"]["message"]


def test_a_parent_row_with_no_settings_is_refused(write_db, write_seed, armed):
    """A `config.configuration` row whose typed row was never written."""
    with psycopg.connect(write_db, autocommit=True) as conn:
        orphan = conn.execute(
            "INSERT INTO config.configuration (config_type) VALUES "
            "('degrader_position') RETURNING id").fetchone()[0]

    envelope = refused(write_db, armed, {"config_ids": [orphan]})
    assert str(orphan) in envelope["error"]["message"]
    assert "no row in config.degrader_position" in envelope["error"]["message"]


def test_scheduling_nothing_is_never_reported_as_success(write_db, write_seed,
                                                         armed, monkeypatch):
    """If the product ever does come out empty, it is an error, not an ok.

    Validation now catches every way that was reachable, so the only way to
    exercise this is to make the scheduling call return nothing.
    """
    monkeypatch.setattr(actions, "_schedule", lambda *args, **kwargs: [])

    envelope = refused(write_db, armed,
                       {"config_ids": [write_seed["degrader_ids"][0]]},
                       kind="internal")
    assert "nothing was scheduled" in envelope["error"]["message"]


# --------------------------------------------------------------------------
# a five-point sequence that cannot be used
# --------------------------------------------------------------------------

def test_a_target_point_marked_do_not_use_stops_the_whole_scan(write_db, write_seed,
                                                               armed):
    """Four points are not a five-point scan, so it is refused, not trimmed."""
    with psycopg.connect(write_db) as conn:
        point = conn.execute(
            "SELECT id FROM config.target_position WHERE seq_id = %s ORDER BY id "
            "LIMIT 1", (actions.TARGET_SEQ_ID,)).fetchone()[0]

    with psycopg.connect(write_db, autocommit=True) as conn:
        conn.execute("UPDATE config.configuration SET do_not_use = true WHERE id = %s",
                     (point,))
    try:
        envelope = refused(write_db, armed,
                           {"config_ids": [write_seed["degrader_ids"][0]]}, kind="db")
        assert str(point) in envelope["error"]["message"]
        assert "do not use" in envelope["error"]["message"]

        # The preview says the same thing rather than promising five runs.
        with pytest.raises(actions.ActionError) as caught:
            actions.preview_five_point(
                config_ids=[write_seed["degrader_ids"][0]], write_dsn=write_db)
        assert str(point) in caught.value.message
    finally:
        with psycopg.connect(write_db, autocommit=True) as conn:
            conn.execute("UPDATE config.configuration SET do_not_use = false "
                         "WHERE id = %s", (point,))


# --------------------------------------------------------------------------
# a write that gives up half way
# --------------------------------------------------------------------------

TRIP_ON_THIRD_ROW = """
CREATE TABLE IF NOT EXISTS public.insert_counter (n INT);
DELETE FROM public.insert_counter;
INSERT INTO public.insert_counter VALUES (0);

CREATE OR REPLACE FUNCTION public.trip_on_third() RETURNS trigger AS $$
DECLARE counted INT;
BEGIN
    UPDATE public.insert_counter SET n = n + 1 RETURNING n INTO counted;
    IF counted >= 3 THEN
        RAISE EXCEPTION 'the run database gave up on row %', counted;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trip_on_third BEFORE INSERT ON state.midas_run
FOR EACH ROW EXECUTE FUNCTION public.trip_on_third();
"""

UNTRIP = """
DROP TRIGGER IF EXISTS trip_on_third ON state.midas_run;
DROP FUNCTION IF EXISTS public.trip_on_third();
DROP TABLE IF EXISTS public.insert_counter;
"""


def test_a_half_finished_scan_names_the_runs_it_created(write_db, write_seed, armed):
    """`schedule()` commits run by run, so a failure leaves some behind.

    A trigger that refuses the third run stands in for anything that can go
    wrong half way -- a constraint, a lost connection, the database being
    restarted.  What matters is that the reply does not say "nothing happened"
    while two runs sit in the queue.
    """
    with psycopg.connect(write_db, autocommit=True) as conn:
        conn.execute(TRIP_ON_THIRD_ROW)
    try:
        before = counts(write_db)
        envelope = json.loads(commands.dispatch(
            None, armed, "schedule_five_point",
            {"config_ids": [write_seed["degrader_ids"][0]]}, actions_allowed=True))
    finally:
        with psycopg.connect(write_db, autocommit=True) as conn:
            conn.execute(UNTRIP)

    assert envelope["ok"] is False
    error = envelope["error"]
    assert error["kind"] == "db"

    created = error["created_anyway"]["run_ids"]
    assert len(created) == 2
    assert error["created_anyway"]["sequence_ids"] == []
    assert "check the queue" in error["hint"]
    assert "without a sequence" in error["hint"]
    for run_id in created:
        assert str(run_id) in error["hint"]

    after = counts(write_db)
    assert after["runs"] == before["runs"] + 2
    assert after["sequences"] == before["sequences"]

    with psycopg.connect(write_db) as conn:
        rows = conn.execute(
            "SELECT id FROM state.midas_run WHERE id = ANY(%s)", (created,)).fetchall()
    assert len(rows) == 2


# --------------------------------------------------------------------------
# the preview, which is a read
# --------------------------------------------------------------------------

def test_the_preview_is_a_read_command(write_db, write_seed, armed):
    assert "preview_five_point" in commands.READ_COMMANDS
    assert "preview_five_point" not in commands.ACTION_COMMANDS

    before = counts(write_db)
    # actions_allowed is false: the ODB flag has nothing to do with a read.
    envelope = json.loads(commands.dispatch(
        None, armed, "preview_five_point",
        {"config_ids": [write_seed["degrader_ids"][0]], "requested_events": 4096},
        actions_allowed=False))

    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert data["would_create_runs"] == 5
    assert len(data["target_positions"]) == 5
    assert data["requested_events"] == 4096
    assert data["configs_applied"] == [
        {"config_id": write_seed["degrader_ids"][0], "config_type": "degrader_position"}]
    assert counts(write_db) == before


def test_the_preview_checks_what_the_action_checks(write_db, write_seed, armed):
    for payload in ({"config_ids": [write_seed["target_config_id"]]},
                    {"config_ids": [write_seed["unused_id"]]},
                    {"config_ids": write_seed["degrader_ids"]}):
        refused(write_db, armed, payload)
        envelope = json.loads(commands.dispatch(
            None, armed, "preview_five_point", payload, actions_allowed=False))
        assert envelope["ok"] is False
        assert envelope["error"]["kind"] == "usage"


def test_a_client_without_actions_cannot_preview_either(write_db, write_seed):
    """Nothing to read the write database with, so it says so and offers the fix."""
    envelope = json.loads(commands.dispatch(
        None, None, "preview_five_point",
        {"config_ids": [write_seed["degrader_ids"][0]]}, actions_allowed=True))

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "denied"
    assert "--allow-actions" in envelope["error"]["hint"]


def test_the_rpc_path_previews_without_the_odb_flag(write_db, write_seed):
    """A read, so: no flag read, no audit line, and it still answers."""
    pytest.importorskip("midas", reason="needs /software/midas/python on PYTHONPATH")
    from pioneer.rundb import rpc_server
    from test_rpc_server import StubClient, StubView

    server = rpc_server.Server(StubView(), rpc_server.ActionAdapter(actions, write_db))
    client = StubClient(allow_actions=False)

    before = counts(write_db)
    status, reply = server.serve(
        client, "preview_five_point",
        json.dumps({"config_ids": [write_seed["beam_config_id"]]}), 100_000)

    assert json.loads(reply)["data"]["would_create_runs"] == 5
    assert client.reads == []
    assert client.messages == []
    assert counts(write_db) == before


# --------------------------------------------------------------------------
# arguments that are nearly numbers, and lists that are too long
# --------------------------------------------------------------------------

def test_a_number_with_a_fraction_is_refused(write_db, write_seed):
    """2.5 events is not a number anybody meant; truncating it silently is worse."""
    chosen = write_seed["degrader_ids"][0]
    before = counts(write_db)

    for kwargs in ({"config_ids": [chosen + 0.5]},
                   {"config_ids": [chosen], "requested_events": 2.5},
                   {"config_ids": [chosen], "requested_events": True},
                   {"config_ids": [True]}):
        with pytest.raises(actions.ActionError) as caught:
            actions.schedule_five_point(write_dsn=write_db, **kwargs)
        assert caught.value.kind == "usage"
    assert counts(write_db) == before

    # A float that is a whole number is still a whole number.
    preview = actions.preview_five_point(config_ids=[float(chosen)],
                                         requested_events=1000.0, write_dsn=write_db)
    assert preview["requested_events"] == 1000
    assert counts(write_db) == before


def test_more_configurations_than_a_run_can_carry_is_refused(write_db, write_seed):
    before = counts(write_db)
    with pytest.raises(actions.ActionError) as caught:
        actions.schedule_five_point(
            config_ids=list(range(1, actions.MAX_CONFIG_IDS + 2)), write_dsn=write_db)

    assert caught.value.kind == "usage"
    assert str(actions.MAX_CONFIG_IDS) in caught.value.message
    assert counts(write_db) == before


def test_the_preview_is_not_offered_by_the_read_command_line():
    """It needs ids and a write connection string; `view` has neither."""
    assert "preview_five_point" in commands.READ_COMMANDS
    assert "preview_five_point" not in commands.CLI_COMMANDS
    assert set(commands.CLI_COMMANDS) < set(commands.READ_COMMANDS)
