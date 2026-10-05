"""The command layer, with no database and no MIDAS anywhere near it.

Every reply the page can ever receive is built here, so this is where the
envelope, the argument rules and the two action gates are checked.  The view is
a stand-in that records what it was asked for: none of this needs PostgreSQL,
which is the point of keeping the layer separate.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from pioneer.rundb import commands


class FakeView:
    """Answers like a view, records the arguments, raises when told to."""

    def __init__(self, raises=None):
        self.calls = []
        self.raises = raises

    def _answer(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if self.raises is not None:
            raise self.raises
        return {"name": name, "args": kwargs}

    def status(self, actions_allowed=False, actions_built=False, five_point_offered=False):
        return self._answer("status", actions_allowed=actions_allowed,
                            actions_built=actions_built,
                            five_point_offered=five_point_offered)

    def runlog(self, limit, before_id):
        return self._answer("runlog", limit=limit, before_id=before_id)

    def queue(self, limit):
        return self._answer("queue", limit=limit)

    def run(self, run_id):
        return self._answer("run", id=run_id)

    def sequences(self, limit):
        return self._answer("sequences", limit=limit)

    def config(self, config_id):
        return self._answer("config", id=config_id)


class FakeActions:
    """Stands in for the action module, which is supplied separately."""

    def __init__(self):
        self.calls = []

    def schedule_five_point(self, **kwargs):
        self.calls.append(kwargs)
        return {"sequence_id": 1, "run_ids": [1, 2, 3, 4, 5]}


class FakeViewError(Exception):
    """What a view raises: an exception that carries a kind."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def call(cmd, args=None, view=None, actions=None, max_len=None, allowed=False):
    return json.loads(commands.dispatch(view or FakeView(), actions, cmd, args,
                                        max_len=max_len, actions_allowed=allowed))


def test_ok_envelope():
    envelope = call("status")
    assert envelope["ok"] is True
    assert envelope["cmd"] == "status"
    assert isinstance(envelope["query_ms"], int)
    assert envelope["data"]["name"] == "status"
    # An offset makes the time unambiguous wherever the page is opened.
    assert envelope["generated"][-6] in "+-" or envelope["generated"].endswith("Z")


def test_defaults_and_clamping():
    view = FakeView()
    call("runlog", view=view)
    assert view.calls[-1][1] == {"limit": commands.DEFAULT_RUNLOG_ROWS, "before_id": None}

    call("runlog", '{"limit": 5000}', view=view)
    assert view.calls[-1][1]["limit"] == commands.MAX_ROWS

    call("runlog", '{"limit": 0}', view=view)
    assert view.calls[-1][1]["limit"] == 1

    call("runlog", '{"before_id": 42}', view=view)
    assert view.calls[-1][1]["before_id"] == 42


def test_arguments_may_be_a_dict_or_empty():
    view = FakeView()
    call("queue", {"limit": 7}, view=view)
    assert view.calls[-1][1]["limit"] == 7
    call("queue", "", view=view)
    assert view.calls[-1][1]["limit"] == commands.DEFAULT_QUEUE_ROWS


def test_unknown_command():
    error = call("drop_everything")["error"]
    assert error["kind"] == "unknown_command"
    assert "schedule_five_point" in error["hint"]


def test_unknown_argument_is_refused():
    error = call("runlog", '{"limit": 5, "wibble": 1}')["error"]
    assert error["kind"] == "usage"
    assert "wibble" in error["message"]


def test_missing_and_malformed_arguments():
    assert call("run")["error"]["kind"] == "usage"
    assert call("run", '{"id": "not a number"}')["error"]["kind"] == "usage"
    assert call("run", "{not json}")["error"]["kind"] == "usage"
    assert call("run", "[1, 2]")["error"]["kind"] == "usage"


def test_view_errors_keep_their_kind():
    view = FakeView(raises=FakeViewError("db", "connection refused\nDETAIL: secrets"))
    error = call("queue", view=view)["error"]
    assert error["kind"] == "db"
    assert error["message"] == "connection refused"

    view = FakeView(raises=RuntimeError("something came apart"))
    assert call("queue", view=view)["error"]["kind"] == "internal"


def test_password_never_reaches_the_envelope():
    view = FakeView(raises=FakeViewError(
        "db", "cannot connect to host=pinky password=hunter2: no route"))
    message = call("status", view=view)["error"]["message"]
    assert "hunter2" not in message
    assert "password=***" in message


class BigView(FakeView):
    """A view whose reply is far larger than any buffer asked for here."""

    def runlog(self, limit, before_id):
        return {"runs": [{"id": n, "note": "x" * 100} for n in range(limit)]}

    def queue(self, limit):
        return self.runlog(limit, None)


class PaddedView(FakeView):
    """A view whose reply can be made any length, to the byte."""

    def __init__(self, pad):
        super().__init__()
        self.pad = pad

    def queue(self, limit):
        return {"pad": "x" * self.pad}


def view_replying_exactly(size):
    """A view whose `queue` envelope encodes to exactly `size` bytes."""
    pad = size
    for _ in range(5):
        view = PaddedView(max(pad, 0))
        length = len(commands.dispatch(view, None, "queue", None).encode("utf-8"))
        if length == size:
            return view
        pad += size - length
    raise AssertionError(f"could not build a reply of exactly {size} bytes")


def test_too_large_reply():
    envelope_text = commands.dispatch(BigView(), None, "runlog", None, max_len=160)
    envelope = json.loads(envelope_text)

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "too_large"
    assert envelope["error"]["needed"] > envelope["error"]["limit"] == 160
    assert len(envelope_text.encode("utf-8")) < 160


def test_a_reply_exactly_the_size_of_the_buffer_is_refused():
    """The boundary: MIDAS keeps one byte for the terminator.

    A reply the same length as the buffer comes back one byte short, so it has
    to be refused here -- otherwise a page retrying with `max_reply_length =
    needed` would be one byte short on every attempt, for ever.
    """
    size = 400
    view = view_replying_exactly(size)

    assert json.loads(commands.dispatch(view, None, "queue", None, max_len=size + 1))["ok"]

    envelope = json.loads(commands.dispatch(view, None, "queue", None, max_len=size))
    assert envelope["error"]["kind"] == "too_large"
    assert envelope["error"]["needed"] == size


def test_replies_are_ascii_so_bytes_and_characters_agree():
    """Non-ASCII data is escaped, so the size arithmetic counts what MIDAS counts."""
    class OddView(FakeView):
        def queue(self, limit):
            return {"comment": "degrader 4.5 mm \u00b1 0.1"}

    text = commands.dispatch(OddView(), None, "queue", None)
    assert text.isascii()
    assert len(text) == len(text.encode("utf-8"))


def test_too_large_reply_fits_even_a_tiny_buffer():
    """A buffer too small for the explanation still gets JSON, not a fragment."""
    envelope_text = commands.dispatch(BigView(), None, "queue", None, max_len=115)
    assert len(envelope_text.encode("utf-8")) < 115
    assert json.loads(envelope_text)["error"]["kind"] == "too_large"


def test_a_buffer_too_small_for_anything_gets_the_floor():
    """Nothing smaller than this exists, and it is still valid JSON."""
    envelope_text = commands.dispatch(BigView(), None, "queue", None, max_len=20)
    assert json.loads(envelope_text) == {"ok": False}


def test_dispatch_envelope_gives_back_what_was_sent():
    """The envelope a caller is handed describes the text it is holding."""
    envelope, text = commands.dispatch_envelope(BigView(), None, "queue", None,
                                                max_len=160)
    assert envelope == json.loads(text)
    assert envelope["error"]["kind"] == "too_large"


def test_actions_are_denied_without_both_gates():
    args = '{"config_ids": [1, 2], "requested_events": 1000}'
    actions = FakeActions()

    # No action module at all.
    assert call("schedule_five_point", args, allowed=True)["error"]["kind"] == "denied"
    # Module present, ODB flag off.
    error = call("schedule_five_point", args, actions=actions, allowed=False)["error"]
    assert error["kind"] == "denied"
    assert actions.calls == []


def test_actions_run_when_both_gates_are_open():
    actions = FakeActions()
    envelope = call("schedule_five_point", '{"config_ids": [3, 4]}',
                    actions=actions, allowed=True)

    assert envelope["ok"] is True
    assert actions.calls == [{"config_ids": [3, 4], "requested_events": 1_000_000}]


def test_action_arguments_are_checked_before_anything_is_armed():
    actions = FakeActions()
    error = call("schedule_five_point", '{"config_ids": []}',
                 actions=actions, allowed=True)["error"]
    assert error["kind"] == "usage"
    assert actions.calls == []


def test_status_is_told_about_the_gates():
    view = FakeView()
    call("status", view=view, actions=FakeActions(), allowed=True)
    # FakeActions has no `five_point_offered`, so the five-point button is not
    # offered: not knowing means not offering.
    assert view.calls[-1][1] == {"actions_allowed": True, "actions_built": True,
                                 "five_point_offered": False}


class OfferingActions(FakeActions):
    """An action module that says whether its database would take a scan."""

    def __init__(self, offered):
        super().__init__()
        self.offered = offered

    def five_point_offered(self):
        return self.offered


def test_status_says_whether_five_point_is_offered():
    """Built *and* writing to a scratch database; the module is asked which."""
    for actions, expected in ((None, False),
                              (OfferingActions(False), False),
                              (OfferingActions(True), True)):
        view = FakeView()
        call("status", view=view, actions=actions)
        assert view.calls[-1][1]["five_point_offered"] is expected


def test_read_and_action_commands_are_separate():
    assert set(commands.READ_COMMANDS) & set(commands.ACTION_COMMANDS) == set()
    assert "schedule_five_point" in commands.ACTION_COMMANDS


def test_module_imports_neither_psycopg_nor_midas():
    """The command layer stays testable on a machine with neither installed."""
    probe = (
        "import sys; import pioneer.rundb.commands; "
        "bad = [n for n in sys.modules if n.split('.')[0] in ('psycopg', 'midas')]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH", "")])
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                            text=True, env=env)
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------
# clearing the queue
# --------------------------------------------------------------------------

class ClearingActions(FakeActions):
    """Records what the two clear-queue commands were handed."""

    def preview_clear_queue(self, **kwargs):
        self.calls.append(("preview_clear_queue", kwargs))
        return {"runs": []}

    def clear_queue(self, **kwargs):
        self.calls.append(("clear_queue", kwargs))
        return {"cancelled": kwargs["run_ids"]}


CLEAR = {"run_ids": [5, 6], "operator": "  A. Shifter  "}


def test_clear_queue_is_an_action_and_its_preview_a_read():
    assert "clear_queue" in commands.ACTION_COMMANDS
    assert "preview_clear_queue" in commands.READ_COMMANDS
    # Like the five-point preview, it needs the action module's connection, so
    # the read command line does not offer it.
    assert "preview_clear_queue" not in commands.CLI_COMMANDS


def test_clear_queue_is_denied_without_both_gates():
    actions = ClearingActions()
    assert call("clear_queue", CLEAR, allowed=True)["error"]["kind"] == "denied"
    assert call("clear_queue", CLEAR, actions=actions,
                allowed=False)["error"]["kind"] == "denied"
    assert actions.calls == []


def test_clear_queue_arguments_and_the_server_side_extra():
    """The operator is trimmed, the flag defaults to false, and the server's
    `sequencer_running` is handed on beside what the caller sent."""
    actions = ClearingActions()
    envelope = json.loads(commands.dispatch(
        FakeView(), actions, "clear_queue", CLEAR, actions_allowed=True,
        server_args={"sequencer_running": False}))

    assert envelope["ok"] is True, envelope
    assert actions.calls == [("clear_queue", {
        "run_ids": [5, 6], "include_holding": False, "operator": "A. Shifter",
        "sequencer_running": False})]


def test_a_caller_cannot_say_whether_the_sequencer_is_running():
    """That is the server's to read from the ODB, never the page's to claim."""
    actions = ClearingActions()
    for cmd, args in (("clear_queue", {**CLEAR, "sequencer_running": False}),
                      ("preview_clear_queue", {"sequencer_running": False})):
        error = call(cmd, args, actions=actions, allowed=True)["error"]
        assert error["kind"] == "usage"
        assert "sequencer_running" in error["message"]
    assert actions.calls == []


def test_clear_queue_refuses_what_a_dialog_would_never_send():
    actions = ClearingActions()
    too_many = list(range(1, commands.MAX_CLEAR_IDS + 2))
    for args in ({"operator": "me"},                                  # no ids
                 {"run_ids": [], "operator": "me"},
                 {"run_ids": too_many, "operator": "me"},
                 {"run_ids": [1, "x"], "operator": "me"},
                 {"run_ids": [1]},                                    # no operator
                 {"run_ids": [1], "operator": "   "},
                 {"run_ids": [1], "operator": "x" * (commands.MAX_TEXT_LENGTH + 1)},
                 {"run_ids": [1], "operator": "two\nlines"},
                 {"run_ids": [1], "operator": 42},
                 {"run_ids": [1], "operator": "me", "include_holding": "false"},
                 {"run_ids": [1], "operator": "me", "include_holding": 1}):
        error = call("clear_queue", args, actions=actions, allowed=True)["error"]
        assert error["kind"] == "usage", args
    assert actions.calls == []

    # The bounds are inclusive.
    envelope = call("clear_queue",
                    {"run_ids": too_many[:-1], "operator": "x" * commands.MAX_TEXT_LENGTH},
                    actions=actions, allowed=True)
    assert envelope["ok"] is True


def test_the_clear_preview_needs_the_module_but_not_the_flag():
    error = call("preview_clear_queue", {})["error"]
    assert error["kind"] == "denied"
    assert "--allow-actions" in error["hint"]

    actions = ClearingActions()
    envelope = call("preview_clear_queue", {"include_holding": True},
                    actions=actions, allowed=False)
    assert envelope["ok"] is True
    assert actions.calls == [("preview_clear_queue", {"include_holding": True})]


def test_status_is_not_armed_by_the_flag_alone():
    """No module, nothing to carry an action out: the page must not show one."""
    view = FakeView()
    call("status", view=view, actions=None, allowed=True)
    assert view.calls[-1][1]["actions_allowed"] is False


def test_ids_with_a_fraction_are_refused_not_truncated():
    actions = ClearingActions()
    for args in ({"run_ids": [2.7], "operator": "me"},
                 {"run_ids": [True], "operator": "me"}):
        assert call("clear_queue", args, actions=actions,
                    allowed=True)["error"]["kind"] == "usage"
    assert call("run", {"id": 2.5})["error"]["kind"] == "usage"
    # A float that is a whole number is still a whole number.
    assert call("clear_queue", {"run_ids": [3.0], "operator": "me"},
                actions=actions, allowed=True)["ok"] is True
    assert actions.calls[-1][1]["run_ids"] == [3]


def test_an_operator_must_be_printable():
    actions = ClearingActions()
    for name in ("tab\there", "zero​width", "bell\x07"):
        assert call("clear_queue", {"run_ids": [1], "operator": name}, actions=actions,
                    allowed=True)["error"]["kind"] == "usage"
    assert call("clear_queue", {"run_ids": [1], "operator": "Jürgen Müller"},
                actions=actions, allowed=True)["ok"] is True
