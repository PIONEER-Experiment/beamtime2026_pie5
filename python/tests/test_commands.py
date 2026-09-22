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

    def status(self, actions_allowed=False, actions_built=False):
        return self._answer("status", actions_allowed=actions_allowed,
                            actions_built=actions_built)

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
    assert view.calls[-1][1] == {"actions_allowed": True, "actions_built": True}


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
