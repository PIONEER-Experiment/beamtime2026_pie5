"""The MIDAS side of the client, without a MIDAS experiment.

What matters here is the contract with mhttpd and with an operator: the
callback keeps its signature and always reports SUCCESS, the ODB flag that arms
actions is read again for every single call, and seeding leaves whatever is
already in `/RunDBView` alone.  A stand-in client records what was asked of it,
so none of this needs a running experiment -- only the `midas` python package
on the path, which is where the status codes come from.
"""

import json

import pytest

midas = pytest.importorskip("midas", reason="needs /software/midas/python on PYTHONPATH")

from pioneer.rundb import commands, rpc_server       # noqa: E402


class StubClient:
    """A MIDAS client that only remembers what it was told.

    `odb_get` counts its reads, which is how the test can tell a value that is
    re-read from one that was cached at startup.
    """

    def __init__(self, allow_actions=False):
        self.odb = {}
        self.messages = []
        self.reads = []
        self.allow_actions = allow_actions

    def odb_exists(self, path):
        return path in self.odb

    def odb_set(self, path, value):
        self.odb[path] = value

    def odb_get(self, path):
        self.reads.append(path)
        if path == f"{rpc_server.ROOT}/Allow actions":
            return self.allow_actions
        if path not in self.odb:
            raise KeyError(path)
        return self.odb[path]

    def msg(self, message, is_error=False, facility="midas"):
        self.messages.append(message)


class StubView:
    """Enough of a view for the callback to have something to answer with."""

    client_name = "RunDBView"

    def status(self, actions_allowed=False, actions_built=False, five_point_offered=False):
        return {"client": {"actions_allowed": actions_allowed,
                           "actions_built": actions_built,
                           "five_point_offered": five_point_offered}}

    def queue(self, limit):
        return {"runs": [], "counts": {}, "next_up": None}


class StubActions:
    def __init__(self):
        self.calls = []

    def schedule_five_point(self, **kwargs):
        self.calls.append(kwargs)
        return {"sequence_id": 7, "run_ids": [1, 2, 3, 4, 5]}


def test_seeding_creates_what_is_missing():
    client = StubClient()
    created = rpc_server.seed(client, "host=example dbname=pioneer password=hunter2")

    assert created == len(rpc_server.DEFAULTS)
    assert client.odb[f"{rpc_server.ROOT}/Allow actions"] is False
    assert "hunter2" not in client.odb[f"{rpc_server.ROOT}/Database"]


def test_seeding_never_overwrites_an_operator_edit():
    client = StubClient()
    rpc_server.seed(client, "host=example dbname=pioneer")

    client.odb[f"{rpc_server.ROOT}/Poll seconds"] = 60.0
    client.odb[f"{rpc_server.ROOT}/Allow actions"] = True
    created = rpc_server.seed(client, "host=elsewhere dbname=pioneer")

    assert created == 0
    assert client.odb[f"{rpc_server.ROOT}/Poll seconds"] == 60.0
    assert client.odb[f"{rpc_server.ROOT}/Allow actions"] is True
    # The database description is not a setting: it says where this client is
    # pointed now, so it is rewritten on every connect.
    assert "host=elsewhere" in client.odb[f"{rpc_server.ROOT}/Database"]


def test_callback_signature_and_status():
    server = rpc_server.Server(StubView())
    result = server.serve(StubClient(), "status", "{}", 100_000)

    assert isinstance(result, tuple) and len(result) == 2
    status, reply = result
    assert status == midas.status_codes["SUCCESS"]
    assert isinstance(reply, str)
    assert json.loads(reply)["ok"] is True


def test_errors_are_still_success():
    """mhttpd drops the reply of anything that is not SUCCESS."""
    server = rpc_server.Server(StubView())

    for cmd, args in [("nonsense", "{}"), ("queue", "{not json}"),
                      ("schedule_five_point", '{"config_ids": [1]}')]:
        status, reply = server.serve(StubClient(), cmd, args, 100_000)
        assert status == midas.status_codes["SUCCESS"]
        assert json.loads(reply)["ok"] is False


def test_a_reply_that_does_not_fit_is_still_json():
    server = rpc_server.Server(StubView())
    status, reply = server.serve(StubClient(), "status", "{}", 60)

    assert status == midas.status_codes["SUCCESS"]
    assert len(reply.encode("utf-8")) < 60
    assert json.loads(reply)["ok"] is False


def test_an_exception_inside_dispatch_is_still_success(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("the command layer came apart")

    monkeypatch.setattr(commands, "dispatch_envelope", explode)
    server = rpc_server.Server(StubView())
    status, reply = server.serve(StubClient(), "status", "{}", 100_000)

    assert status == midas.status_codes["SUCCESS"]
    envelope = json.loads(reply)
    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "internal"


def test_the_action_flag_is_read_again_every_time():
    actions = StubActions()
    server = rpc_server.Server(StubView(), actions)
    client = StubClient(allow_actions=False)
    args = '{"config_ids": [1, 2]}'

    status, reply = server.serve(client, "schedule_five_point", args, 100_000)
    assert json.loads(reply)["error"]["kind"] == "denied"
    assert actions.calls == []

    # An operator ticks the box, without restarting anything.
    client.allow_actions = True
    status, reply = server.serve(client, "schedule_five_point", args, 100_000)
    assert json.loads(reply)["ok"] is True
    assert actions.calls == [{"config_ids": [1, 2], "requested_events": 1_000_000}]

    # And unticks it again.
    client.allow_actions = False
    status, reply = server.serve(client, "schedule_five_point", args, 100_000)
    assert json.loads(reply)["error"]["kind"] == "denied"
    assert len(actions.calls) == 1

    flag = f"{rpc_server.ROOT}/Allow actions"
    assert client.reads.count(flag) == 3


def test_reads_do_not_look_at_the_action_flag_at_all():
    server = rpc_server.Server(StubView(), StubActions())
    client = StubClient(allow_actions=True)
    server.serve(client, "queue", "{}", 100_000)

    assert client.reads == []


def test_every_action_attempt_leaves_a_message():
    actions = StubActions()
    server = rpc_server.Server(StubView(), actions)
    client = StubClient(allow_actions=True)

    server.serve(client, "schedule_five_point", '{"config_ids": [1]}', 100_000)
    server.serve(client, "schedule_five_point", '{"config_ids": []}', 100_000)

    assert len(client.messages) == 2
    assert "accepted" in client.messages[0]
    assert "refused" in client.messages[1]


def test_an_attempt_that_never_gets_through_the_gates_is_logged_too():
    """Pressing a disabled button has to leave a trace, and say which gate.

    Without this, a shifter clicking "schedule" on a client that is not armed
    sees a message on the page and nothing anywhere else, and whoever is asked
    about it afterwards has nothing to look at.
    """
    unarmed = rpc_server.Server(StubView())
    client = StubClient(allow_actions=True)
    unarmed.serve(client, "schedule_five_point", '{"config_ids": [1]}', 100_000)

    assert len(client.messages) == 1
    assert "refused action schedule_five_point" in client.messages[0]
    assert "not built" in client.messages[0]

    armed_but_not_allowed = rpc_server.Server(StubView(), StubActions())
    client = StubClient(allow_actions=False)
    armed_but_not_allowed.serve(client, "schedule_five_point", '{"config_ids": [1]}',
                                100_000)

    assert len(client.messages) == 1
    assert "refused action schedule_five_point" in client.messages[0]
    assert f"{rpc_server.ROOT}/Allow actions" in client.messages[0]


def test_a_read_command_leaves_no_message():
    """Only actions are logged; a page polling every five seconds is not news."""
    server = rpc_server.Server(StubView(), StubActions())
    client = StubClient(allow_actions=True)
    server.serve(client, "queue", "{}", 100_000)

    assert client.messages == []


def test_an_unreadable_flag_means_no():
    class Broken(StubClient):
        def odb_get(self, path):
            raise RuntimeError("no such key")

    assert rpc_server.actions_allowed(Broken()) is False


def test_a_padded_command_name_is_still_an_action():
    """The gate and the dispatcher have to be deciding about the same command.

    `dispatch` strips what it is given, so `" schedule_five_point "` runs as an
    action; the callback therefore has to strip before it decides whether this
    is one, or a name with a space in it would slip past the gate check and the
    audit line both.
    """
    actions = StubActions()
    server = rpc_server.Server(StubView(), actions)
    client = StubClient(allow_actions=False)

    status, reply = server.serve(client, "  schedule_five_point  ",
                                 '{"config_ids": [1]}', 100_000)

    assert json.loads(reply)["error"]["kind"] == "denied"
    assert actions.calls == []
    assert client.reads.count(f"{rpc_server.ROOT}/Allow actions") == 1
    assert "refused action schedule_five_point" in client.messages[0]


def test_a_successful_action_logs_what_it_created():
    """"Who scheduled these runs" has to be answerable from the Messages page."""
    server = rpc_server.Server(StubView(), StubActions())
    client = StubClient(allow_actions=True)

    server.serve(client, "schedule_five_point", '{"config_ids": [1]}', 100_000)

    assert "accepted" in client.messages[0]
    assert "sequence 7" in client.messages[0]
    assert "[1, 2, 3, 4, 5]" in client.messages[0]


# --------------------------------------------------------------------------
# clearing the queue: the sequencer state comes from the ODB, not the caller
# --------------------------------------------------------------------------

class ClearStubActions(StubActions):
    def preview_clear_queue(self, **kwargs):
        self.calls.append(("preview_clear_queue", kwargs))
        return {"runs": [], "sequencer_running": kwargs["sequencer_running"]}

    def clear_queue(self, **kwargs):
        self.calls.append(("clear_queue", kwargs))
        return {"cancelled": [3, 4], "kept_head": [2], "skipped": [{"id": 9, "status": None}],
                "statuses": ["PENDING"], "sequencer_running": kwargs["sequencer_running"],
                "operator": kwargs["operator"]}


CLEAR_ARGS = '{"run_ids": [2, 3, 4, 9], "operator": "A. Shifter"}'


def test_the_sequencer_state_is_read_on_every_clear_call():
    actions = ClearStubActions()
    server = rpc_server.Server(StubView(), actions)
    client = StubClient(allow_actions=True)
    key = rpc_server.SEQUENCER_RUNNING

    client.odb[key] = False
    server.serve(client, "preview_clear_queue", "{}", 100_000)
    client.odb[key] = True
    server.serve(client, "clear_queue", CLEAR_ARGS, 100_000)

    assert [kwargs["sequencer_running"] for _, kwargs in actions.calls] == [False, True]
    assert client.reads.count(key) == 2


def test_a_bool_key_read_as_an_int_is_understood():
    """The MIDAS python client hands a BOOL key back as the int 0 or 1.

    A stopped sequencer read as 0 has to count as stopped, or a clear with the
    sequencer stopped would still keep the head of the queue.
    """
    client = StubClient(allow_actions=True)
    for value, running in ((0, False), (1, True), (False, False), (True, True),
                           ("n", True), (None, True)):
        client.odb[rpc_server.SEQUENCER_RUNNING] = value
        assert rpc_server.sequencer_running(client) is running, value


def test_an_unreadable_sequencer_state_means_running():
    """A failed read keeps the head of the queue: it is the safe side."""
    class Broken(StubClient):
        def odb_get(self, path):
            if path == rpc_server.SEQUENCER_RUNNING:
                raise RuntimeError("ODB went away")
            return super().odb_get(path)

    actions = ClearStubActions()
    server = rpc_server.Server(StubView(), actions)

    server.serve(Broken(allow_actions=True), "clear_queue", CLEAR_ARGS, 100_000)
    # And a key that is simply missing (StubClient raises KeyError for it).
    server.serve(StubClient(allow_actions=True), "preview_clear_queue", "{}", 100_000)

    assert [kwargs["sequencer_running"] for _, kwargs in actions.calls] == [True, True]
    assert rpc_server.sequencer_running(Broken()) is True


def test_other_commands_do_not_read_the_sequencer_state():
    server = rpc_server.Server(StubView(), ClearStubActions())
    client = StubClient(allow_actions=True)
    server.serve(client, "schedule_five_point", '{"config_ids": [1]}', 100_000)
    server.serve(client, "queue", "{}", 100_000)

    assert rpc_server.SEQUENCER_RUNNING not in client.reads


def test_a_caller_sending_the_sequencer_state_is_refused():
    actions = ClearStubActions()
    server = rpc_server.Server(StubView(), actions)
    client = StubClient(allow_actions=True)
    client.odb[rpc_server.SEQUENCER_RUNNING] = True

    status, reply = server.serve(
        client, "clear_queue",
        '{"run_ids": [2], "operator": "me", "sequencer_running": false}', 100_000)

    assert status == midas.status_codes["SUCCESS"]
    assert json.loads(reply)["error"]["kind"] == "usage"
    assert actions.calls == []
    assert "refused" in client.messages[0]


def test_a_clear_leaves_its_own_audit_line():
    """Who, how many, which, which were kept and how many were skipped."""
    server = rpc_server.Server(StubView(), ClearStubActions())
    client = StubClient(allow_actions=True)
    client.odb[rpc_server.SEQUENCER_RUNNING] = True

    server.serve(client, "clear_queue", CLEAR_ARGS, 100_000)

    line = client.messages[0]
    assert "action clear_queue accepted" in line
    assert "'A. Shifter'" in line
    assert "cancelled 2 run(s) [3, 4]" in line
    assert "kept next run [2] (sequencer running)" in line
    assert "skipped 1" in line
    # The five-point wording is not borrowed.
    assert "sequence" not in line.replace("sequencer", "")


def test_a_refused_clear_is_logged_without_echoing_every_id():
    unarmed = rpc_server.Server(StubView())
    client = StubClient(allow_actions=True)
    many = json.dumps({"run_ids": list(range(1, 1001)), "operator": "me"})

    unarmed.serve(client, "clear_queue", many, 100_000)

    line = client.messages[0]
    assert "refused action clear_queue (actions not built" in line
    assert "1000 run id(s)" in line
    assert "999" not in line


def test_a_long_clear_is_cut_in_the_audit_line():
    class Many(ClearStubActions):
        def clear_queue(self, **kwargs):
            return {"cancelled": list(range(1, 501)), "kept_head": [], "skipped": [],
                    "statuses": ["PENDING"], "sequencer_running": False, "operator": "me"}

    server = rpc_server.Server(StubView(), Many())
    client = StubClient(allow_actions=True)
    client.odb[rpc_server.SEQUENCER_RUNNING] = False
    server.serve(client, "clear_queue", CLEAR_ARGS, 100_000)

    line = client.messages[0]
    assert "cancelled 500 run(s)" in line
    assert f"and {500 - rpc_server.AUDIT_IDS_SHOWN} more" in line
    assert "kept next run" not in line


# --------------------------------------------------------------------------
# the status reply tells the page whether it is armed
# --------------------------------------------------------------------------

def status_client_flags(server, client):
    status, reply = server.serve(client, "status", "{}", 100_000)
    return json.loads(reply)["data"]["client"]


def test_status_reports_armed_only_with_the_flag_and_the_module():
    """The page shows its write buttons from this; the flag has to be read."""
    armed = rpc_server.Server(StubView(), ClearStubActions())
    unarmed = rpc_server.Server(StubView())

    flags = status_client_flags(armed, StubClient(allow_actions=True))
    assert flags["actions_allowed"] is True and flags["actions_built"] is True

    flags = status_client_flags(unarmed, StubClient(allow_actions=True))
    assert flags["actions_allowed"] is False and flags["actions_built"] is False

    flags = status_client_flags(armed, StubClient(allow_actions=False))
    assert flags["actions_allowed"] is False and flags["actions_built"] is True


def test_status_reads_the_flag_every_time_and_logs_nothing():
    server = rpc_server.Server(StubView(), ClearStubActions())
    client = StubClient(allow_actions=False)
    assert status_client_flags(server, client)["actions_allowed"] is False
    client.allow_actions = True
    assert status_client_flags(server, client)["actions_allowed"] is True

    assert client.reads.count(f"{rpc_server.ROOT}/Allow actions") == 2
    assert client.messages == []


# --------------------------------------------------------------------------
# the run the sequencer has loaded
# --------------------------------------------------------------------------

def test_the_loaded_run_is_read_while_the_sequencer_runs():
    actions = ClearStubActions()
    server = rpc_server.Server(StubView(), actions)
    client = StubClient(allow_actions=True)
    client.odb[rpc_server.SEQUENCER_RUNNING] = True
    client.odb[rpc_server.LOADED_RUN] = 42

    server.serve(client, "clear_queue", CLEAR_ARGS, 100_000)
    server.serve(client, "preview_clear_queue", "{}", 100_000)
    assert [kwargs.get("loaded_run_id") for _, kwargs in actions.calls] == [42, 42]

    # 0 is what the daemon leaves between runs: nothing extra to keep.
    client.odb[rpc_server.LOADED_RUN] = 0
    server.serve(client, "preview_clear_queue", "{}", 100_000)
    assert "loaded_run_id" not in actions.calls[-1][1]


def test_the_loaded_run_is_not_read_when_the_sequencer_is_stopped():
    actions = ClearStubActions()
    server = rpc_server.Server(StubView(), actions)
    client = StubClient(allow_actions=True)
    client.odb[rpc_server.SEQUENCER_RUNNING] = False
    client.odb[rpc_server.LOADED_RUN] = 42

    server.serve(client, "clear_queue", CLEAR_ARGS, 100_000)
    assert "loaded_run_id" not in actions.calls[-1][1]
    assert rpc_server.LOADED_RUN not in client.reads


def test_an_unreadable_loaded_run_protects_nothing_extra():
    class Broken(StubClient):
        def odb_get(self, path):
            if path == rpc_server.LOADED_RUN:
                raise RuntimeError("no such key")
            return super().odb_get(path)

    actions = ClearStubActions()
    server = rpc_server.Server(StubView(), actions)
    client = Broken(allow_actions=True)
    client.odb[rpc_server.SEQUENCER_RUNNING] = True

    status, reply = server.serve(client, "clear_queue", CLEAR_ARGS, 100_000)
    assert json.loads(reply)["ok"] is True
    assert actions.calls[-1][1]["sequencer_running"] is True
    assert "loaded_run_id" not in actions.calls[-1][1]
    for odd in (True, "42", -3, 2.5):
        client = StubClient()
        client.odb[rpc_server.LOADED_RUN] = odd
        assert rpc_server.loaded_run_id(client) is None


def test_a_caller_cannot_name_the_loaded_run_either():
    actions = ClearStubActions()
    server = rpc_server.Server(StubView(), actions)
    status, reply = server.serve(
        StubClient(allow_actions=True), "clear_queue",
        '{"run_ids": [2], "operator": "me", "loaded_run_id": 2}', 100_000)
    assert json.loads(reply)["error"]["kind"] == "usage"
    assert actions.calls == []


def test_a_refused_clear_line_is_short_whatever_was_sent():
    server = rpc_server.Server(StubView(), ClearStubActions())
    client = StubClient(allow_actions=True)
    noise = json.dumps({"run_ids": "x" * 5000, "operator": "y" * 5000,
                        "include_holding": "z" * 5000})
    server.serve(client, "clear_queue", noise, 100_000)

    line = client.messages[0]
    assert "refused" in line
    assert len(line) < 600
