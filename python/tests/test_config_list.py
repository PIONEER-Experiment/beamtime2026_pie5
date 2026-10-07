"""`config` on a whole table: every row, or only the hand-made ones, with or without values.

The ConfigDB page asks for one table at a time.  Most rows of the beamline
table are written by machines (runplan steps, scheduled scans), and with their
values the reply outgrew the page's buffer, so the page now asks for
`auto: "hide"` and reads the machine-written rows only when someone asks to
see them.  The plain request, with no `auto` and no `values`, must keep giving
exactly what it always gave: other callers use it.

The command-layer tests need no database.  The view tests use the seeded
scratch database of conftest.py and skip without one.
"""

import json
from pathlib import Path

import pytest
from psycopg import sql

from pioneer.rundb import commands
from pioneer.rundb.view import AUTO_CHOICES, AUTO_KINDS, auto_kind

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "custom" / "js" / "cfgdb-auto-kinds.json"


# ------------------------------------------------------------- classification


def test_auto_kind_agrees_with_the_page():
    # custom/js/cfgdb.test.js checks autoKind() in cfgdb.js against the same
    # file, so the client and the page sort every row the same way.
    cases = json.loads(EXAMPLES.read_text())["cases"]
    assert len(cases) > 10
    for case in cases:
        assert auto_kind(case["comment"]) == case["kind"], repr(case["comment"])


def test_every_kind_in_the_examples_is_a_kind_here():
    kinds = {kind for kind, _ in AUTO_KINDS}
    cases = json.loads(EXAMPLES.read_text())["cases"]
    assert {case["kind"] for case in cases} - {""} == kinds


def test_command_layer_and_view_agree_on_the_choices():
    # commands.py does not import the view, so the two words are written twice.
    assert commands.AUTO_ROWS == AUTO_CHOICES


# -------------------------------------------------------------- command layer


class ListView:
    """Records how `config` reached the view."""

    def __init__(self):
        self.calls = []

    def config(self, config_id):
        self.calls.append(("config", (config_id,), {}))
        return {"config": {"config_id": config_id}}

    def config_list(self, config_type, *args, **kwargs):
        self.calls.append(("config_list", (config_type,) + args, kwargs))
        return []


def call(args, view=None):
    view = view or ListView()
    return json.loads(commands.dispatch(view, None, "config", json.dumps(args))), view


def test_plain_table_request_is_unchanged():
    envelope, view = call({"id": "pie5_epics"})
    assert envelope["ok"] is True
    # Called exactly as before: one positional argument, nothing else.
    assert view.calls == [("config_list", ("pie5_epics",), {})]


def test_auto_and_values_are_passed_on():
    _, view = call({"id": "pie5_epics", "auto": "hide", "values": False})
    assert view.calls == [("config_list", ("pie5_epics",), {"auto": "hide", "values": False})]
    _, view = call({"id": "pie5_epics", "auto": "only"})
    assert view.calls == [("config_list", ("pie5_epics",), {"auto": "only", "values": True})]
    _, view = call({"id": "pie5_epics", "values": False})
    assert view.calls == [("config_list", ("pie5_epics",), {"auto": None, "values": False})]


@pytest.mark.parametrize("args, words", [
    ({"id": "pie5_epics", "auto": "all"}, "auto must be one of hide, only"),
    ({"id": "pie5_epics", "auto": True}, "auto must be one of hide, only"),
    ({"id": "pie5_epics", "values": "false"}, "values must be true or false"),
    ({"id": "pie5_epics", "values": 0}, "values must be true or false"),
    ({"id": 42, "auto": "hide"}, "not to one configuration id"),
    ({"id": 42, "values": False}, "not to one configuration id"),
    ({"id": "pie5_epics", "hide_auto": True}, "does not take hide_auto"),
])
def test_bad_arguments_are_refused(args, words):
    envelope, view = call(args)
    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "usage"
    assert words in envelope["error"]["message"]
    assert view.calls == []


def test_one_configuration_by_id_is_unchanged():
    envelope, view = call({"id": 42})
    assert envelope["data"] == {"config": {"config_id": 42}}
    assert view.calls == [("config", (42,), {})]


# ----------------------------------------------------------------------- view

# Comments of the rows added to config.pie5_epics, which the seed leaves empty.
ROWS = [
    ("PSM nominal 65 MeV/c", False),
    ("runplan scan-a step 1", False),
    ("runplan scan-a step 2", False),
    ("Mystery Configuration", False),
    (None, False),
    ("hand-made, do not use", True),
    ("runplan scan-a step 3", True),
]


@pytest.fixture(scope="module")
def pie5(fresh_db, seeded):
    """Seven pie5_epics configurations: three hand-made, three runplan, one mystery."""
    import psycopg

    ids = []
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        columns = [row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'config' AND table_name = 'pie5_epics' "
            "AND column_name NOT IN ('id', 'seq_id') ORDER BY ordinal_position").fetchall()]
        for n, (comment, do_not_use) in enumerate(ROWS):
            config_id = conn.execute(
                "INSERT INTO config.configuration (config_type, comment, do_not_use) "
                "VALUES ('pie5_epics', %s, %s) RETURNING id", (comment, do_not_use)).fetchone()[0]
            conn.execute(sql.SQL("INSERT INTO config.pie5_epics (id, seq_id, {cols}) VALUES ({vals})").format(
                cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
                vals=sql.SQL(", ").join([sql.Placeholder()] * (len(columns) + 2))),
                [config_id, 10 + n] + [float(n)] * len(columns))
            ids.append(config_id)
    return dict(zip([comment for comment, _ in ROWS], ids)) | {"_all": ids}


def roundtrip(data):
    return json.loads(json.dumps(data))


def test_default_is_every_row_with_values(view, pie5):
    rows = roundtrip(view.config_list("pie5_epics"))
    assert isinstance(rows, list)
    assert [row["config_id"] for row in rows] == pie5["_all"]
    assert set(rows[0]) == {"config_id", "config_type", "do_not_use", "comment", "values"}
    assert rows[0]["values"]["seq_id"] == 10
    assert "QSF41:SOL:2" in rows[0]["values"]


def test_hide_leaves_out_the_machine_written_rows_and_counts_them(view, pie5):
    data = roundtrip(view.config_list("pie5_epics", auto="hide"))
    assert data["config_type"] == "pie5_epics"
    assert data["auto"] == "hide"
    assert data["values"] is True
    assert [row["comment"] for row in data["rows"]] == [
        "PSM nominal 65 MeV/c", None, "hand-made, do not use"]
    assert data["auto_counts"] == {"runplan": 3, "mystery": 1}
    assert data["rows"][2]["do_not_use"] is True
    assert "QSF41:SOL:2" in data["rows"][0]["values"]


def test_only_gives_the_machine_written_rows(view, pie5):
    data = roundtrip(view.config_list("pie5_epics", auto="only"))
    assert [row["config_id"] for row in data["rows"]] == [
        pie5["runplan scan-a step 1"], pie5["runplan scan-a step 2"],
        pie5["Mystery Configuration"], pie5["runplan scan-a step 3"]]
    assert data["auto_counts"] == {"runplan": 3, "mystery": 1}


def test_without_values_rows_keep_seq_id_only(view, pie5):
    data = roundtrip(view.config_list("pie5_epics", auto="hide", values=False))
    assert data["values"] is False
    for row in data["rows"]:
        assert set(row) == {"config_id", "config_type", "do_not_use", "comment", "seq_id"}
    assert [row["seq_id"] for row in data["rows"]] == [10, 14, 15]

    plain = roundtrip(view.config_list("pie5_epics", values=False))
    assert isinstance(plain, list) and len(plain) == len(ROWS)
    assert "values" not in plain[0]


def test_a_type_with_no_table(view, seeded):
    from rundb_seed import UNKNOWN_CONFIG_TYPE

    rows = roundtrip(view.config_list(UNKNOWN_CONFIG_TYPE))
    assert rows and rows[0]["values"] is None
    data = roundtrip(view.config_list(UNKNOWN_CONFIG_TYPE, auto="hide", values=False))
    assert data["rows"][0]["seq_id"] is None
    assert data["auto_counts"] == {"runplan": 0, "mystery": 0}


def test_hide_fits_where_the_whole_table_does_not(view, pie5):
    # The failure on the page: the whole table does not fit the buffer the
    # page offers and comes back as too_large.  Pick a buffer between the two.
    whole = commands.dispatch(view, None, "config", {"id": "pie5_epics"})
    slim = commands.dispatch(view, None, "config", {"id": "pie5_epics", "auto": "hide", "values": False})
    assert len(slim) < len(whole) / 3
    buffer = (len(slim) + len(whole)) // 2

    refused = json.loads(commands.dispatch(view, None, "config", {"id": "pie5_epics"}, max_len=buffer))
    assert refused["ok"] is False and refused["error"]["kind"] == "too_large"
    fits = json.loads(commands.dispatch(view, None, "config",
                                        {"id": "pie5_epics", "auto": "hide", "values": False},
                                        max_len=buffer))
    assert fits["ok"] is True and len(fits["data"]["rows"]) == 3


def test_cli_prints_the_slim_list(fresh_db, pie5, capsys):
    from pioneer.rundb import view as view_module

    assert view_module.main(["config", "pie5_epics", "--auto", "hide", "--no-values",
                             "--dsn", fresh_db]) == 0
    out = capsys.readouterr().out
    assert "machine-written rows in pie5_epics: 3 runplan, 1 mystery (left out)" in out
    assert "PSM nominal 65 MeV/c" in out
    assert "runplan scan-a" not in out
