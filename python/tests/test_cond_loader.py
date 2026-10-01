"""The append-only loader (pioneer.conddb.cond_loader), on scratch SQLite files.

A reload replaces the active intervals of every tag the container names. What
the retired intervals were serving must survive it: that is the history the
ConditionsHeader of an already-written file points at.
"""

import json

import pytest

from pioneer.conddb import cond_loader


def _table(values, iov):
    return {"t": {
        "schema": "t", "version": 1, "kind": "parameter_set",
        "tags": [{"tag": "a", "is_default": True, "description": ""}],
        "iov": iov,
        "values": {"a": [{"key": "k", "value": values}]},
    }}


def _served(con, row_id):
    """What an interval reads: its own cells, else the tag-wide ones."""
    own = con.execute("SELECT value_int FROM cond_values WHERE table_name = 't' "
                      "AND iov_row_id = ? ORDER BY ordinal", (row_id,)).fetchall()
    if own:
        return [r[0] for r in own]
    tag = con.execute("SELECT tag FROM cond_iov WHERE table_name = 't' AND row_id = ?",
                      (row_id,)).fetchone()[0]
    return [r[0] for r in con.execute(
        "SELECT value_int FROM cond_values WHERE table_name = 't' AND tag = ? "
        "AND iov_row_id IS NULL ORDER BY ordinal", (tag,)).fetchall()]


def _load(tmp_path, name, tables):
    path = tmp_path / name
    path.write_text(json.dumps(tables))
    ex = cond_loader.make_executor(sqlite=str(tmp_path / "c.db"))
    try:
        cond_loader.load(ex, [path])
    finally:
        ex.close()


def test_reload_pins_a_tag_wide_payload_onto_inactive_intervals(tmp_path):
    """An inactive interval the container itself carries is served by the tag-wide
    payload too; a reload with new values must not change what it reads."""
    first = _table([1, 2], [
        {"row_id": 1, "tag": "a", "run_start": 0, "run_end": 10, "is_active": False},
        {"row_id": 2, "tag": "a", "run_start": 0, "run_end": None, "is_active": True},
    ])
    _load(tmp_path, "first.json", first)
    con = cond_loader.sqlite3.connect(tmp_path / "c.db")
    assert _served(con, 1) == [1, 2] and _served(con, 2) == [1, 2]
    con.close()

    second = _table([7], [{"row_id": 1, "tag": "a", "run_start": 0, "run_end": None}])
    _load(tmp_path, "second.json", second)
    con = cond_loader.sqlite3.connect(tmp_path / "c.db")
    assert _served(con, 1) == [1, 2], "the inactive interval lost its history"
    assert _served(con, 2) == [1, 2], "the retired active interval lost its history"
    assert _served(con, 3) == [7]
    # every retired interval now owns its payload, so no interval mixes the two kinds
    assert con.execute("SELECT COUNT(*) FROM cond_values WHERE table_name = 't' "
                       "AND iov_row_id IS NULL").fetchone()[0] == 1
    con.close()


def test_intervals_already_pinned_are_not_pinned_twice(tmp_path):
    """A third load finds rows 1 and 2 with their own payload and leaves them alone
    (a second copy would violate the one-cell-per-slot index and abort the load)."""
    iov = [{"row_id": 1, "tag": "a", "run_start": 0, "run_end": None}]
    for n, v in enumerate(([1], [2], [3])):
        _load(tmp_path, f"l{n}.json", _table(v, iov))
    con = cond_loader.sqlite3.connect(tmp_path / "c.db")
    assert [_served(con, r) for r in (1, 2, 3)] == [[1], [2], [3]]
    con.close()


def test_load_tables_takes_containers_in_memory(tmp_path):
    ex = cond_loader.make_executor(sqlite=str(tmp_path / "m.db"))
    loaded = cond_loader.load_tables(ex, [("in-memory", _table([4, 5], [
        {"row_id": 1, "tag": "a", "run_start": 0, "run_end": None}]))])
    ex.close()
    assert loaded == [("in-memory", "t", 2, 1)]


def _count(path, sql):
    con = cond_loader.sqlite3.connect(path)
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def test_expected_fingerprint_refuses_a_stale_edit(tmp_path):
    """A writer that read the table at (max row_id, active) = (1, 1) may not load
    once another load has moved it on; force drops the expectation."""
    path = tmp_path / "c.db"
    iov = [{"row_id": 1, "tag": "a", "run_start": 0, "run_end": None}]
    _load(tmp_path, "one.json", _table([1], iov))
    _load(tmp_path, "two.json", _table([2], iov))
    ex = cond_loader.make_executor(sqlite=str(path))
    with pytest.raises(cond_loader.FingerprintMoved, match="changed since it was read"):
        cond_loader.load_tables(ex, [("stale", _table([3], iov))], expect={"t": (1, 1)})
    assert _count(path, "SELECT MAX(row_id) FROM cond_iov") == 2
    # the same expectation carried by the container itself (condtool export)
    stale = _table([3], iov)
    stale["t"][cond_loader.EXPORT_KEY] = {"fingerprint": [1, 1], "source": "x"}
    with pytest.raises(cond_loader.FingerprintMoved):
        cond_loader.load_tables(ex, [("stale", stale)])
    cond_loader.load_tables(ex, [("forced", stale)], force=True)
    assert _count(path, "SELECT MAX(row_id) FROM cond_iov") == 3
    # a current record loads, and stores nothing of the _export key
    stale["t"][cond_loader.EXPORT_KEY]["fingerprint"] = [3, 1]
    cond_loader.load_tables(ex, [("current", stale)])
    ex.close()


def test_a_writer_between_read_and_lock_aborts_the_load(tmp_path, monkeypatch):
    """The statements are computed from a read made before the transaction; a
    load that commits in between must make this one fail, not be overwritten."""
    path = tmp_path / "c.db"
    iov = [{"row_id": 1, "tag": "a", "run_start": 0, "run_end": None}]
    _load(tmp_path, "one.json", _table([1], iov))
    ex = cond_loader.make_executor(sqlite=str(path))
    real_script = ex.script

    def interloper(sql):
        other = cond_loader.make_executor(sqlite=str(path))
        cond_loader.load_tables(other, [("other", _table([9], iov))])
        other.close()
        real_script(sql)

    monkeypatch.setattr(ex, "script", interloper)
    with pytest.raises(cond_loader.FingerprintMoved, match="Another writer"):
        cond_loader.load_tables(ex, [("mine", _table([5], iov))])
    ex.close()
    con = cond_loader.sqlite3.connect(path)
    assert _served(con, 2) == [9], "the other writer's interval must survive"
    assert con.execute("SELECT COUNT(*) FROM cond_iov WHERE is_active = 1").fetchone()[0] == 1
    con.close()


def test_negative_zero_literal():
    assert cond_loader.lit(-0.0, "pg") == "'-0'::float8"
    assert cond_loader.lit(0.0, "pg") == "0.0" and cond_loader.lit(-1.5, "pg") == "-1.5"
