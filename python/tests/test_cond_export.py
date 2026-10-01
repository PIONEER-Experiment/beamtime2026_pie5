"""The database -> JSON export (pioneer.conddb.cond_export, pg2json.py, condtool export).

The real containers are those of reco_testbeam/conditions: $PI_TB_CONDITIONS_DIR,
else the first main/reco_testbeam/conditions above this file (the workspace
layout, from the repository or from a worktree under scratch/). Without them
the round-trip tests skip.

Every test runs on SQLite. The round trips also run on PostgreSQL when
$PIONEER_CONDDB_TEST_DSN names a scratch database: its name must contain
"test" or "scratch", and every cond_* table in it is dropped first.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pioneer.conddb import cond_export, cond_loader, pg2json
from pioneer.conddb.cond_loader import LoaderError
from pioneer.conddb.merge_conditions import renumber

CONDDB = Path(cond_export.__file__).resolve().parent


def _real_dir():
    if os.environ.get("PI_TB_CONDITIONS_DIR"):
        return Path(os.environ["PI_TB_CONDITIONS_DIR"])
    for parent in Path(__file__).resolve().parents:
        d = parent / "main" / "reco_testbeam" / "conditions"
        if d.is_dir():
            return d
    return None


REAL = _real_dir()
needs_real = pytest.mark.skipif(
    REAL is None or not all((REAL / f).is_file() for f in pg2json.CONTAINERS),
    reason="reco_testbeam/conditions not found (set PI_TB_CONDITIONS_DIR)")

PG_DSN = os.environ.get("PIONEER_CONDDB_TEST_DSN", "")


def _pg_executor():
    name = re.search(r"dbname\s*=\s*'?([^\s']+)", PG_DSN)
    if not name or not ("test" in name.group(1) or "scratch" in name.group(1)):
        pytest.fail("PIONEER_CONDDB_TEST_DSN must name a database whose name contains "
                    "'test' or 'scratch'; its conditions tables are dropped")
    ex = cond_loader.make_executor(conninfo=PG_DSN)
    ex.script("DROP TABLE IF EXISTS cond_values, cond_iov, cond_tags, cond_tables, "
              "cond_schema CASCADE;")
    return ex


BACKENDS = ["sqlite", pytest.param("pg", marks=pytest.mark.skipif(
    not PG_DSN, reason="PIONEER_CONDDB_TEST_DSN not set"))]


@pytest.fixture(params=BACKENDS)
def db(request, tmp_path):
    """An empty conditions database on each backend."""
    if request.param == "sqlite":
        ex = cond_loader.make_executor(sqlite=str(tmp_path / "c.db"))
    else:
        ex = _pg_executor()
    yield ex
    ex.close()


def _load(ex, *tables):
    return cond_loader.load_tables(ex, [(f"t{i}", t) for i, t in enumerate(tables)])


def _without_inactive(table):
    """A container table as an export of it must look: active intervals, 1..N."""
    t = json.loads(json.dumps(table))
    t["iov"] = [r for r in t["iov"] if r.get("is_active", True)]
    ids = {str(r["row_id"]) for r in t["iov"]}
    tags = {r["tag"] for r in t["iov"]}
    if "values_by_iov" in t:
        t["values_by_iov"] = {k: v for k, v in t["values_by_iov"].items() if k in ids}
    if "values" in t:
        t["values"] = {k: v for k, v in t["values"].items() if k in tags}
    return renumber(t, 0)


# ---------------------------------------------------------------------------
# The five real containers

@needs_real
def test_real_containers_round_trip(db, tmp_path):
    """Load the git containers, export them with the git copy as the order hint:
    every table is the git table minus its inactive intervals, key order
    included; a file with no inactive interval in the repository's own format
    comes back byte for byte; and the export serves what the database serves."""
    files = [REAL / f for f in pg2json.CONTAINERS]
    cond_loader.load(db, files)
    texts, unnamed = pg2json.export_all(db, REAL)
    assert unnamed == []
    for fname, text in texts.items():
        src_text = (REAL / fname).read_text(encoding="utf-8")
        src = json.loads(src_text)
        out = json.loads(text)
        assert list(out) == list(src) == list(pg2json.CONTAINERS[fname])
        for name in src:
            assert json.dumps(out[name], indent=2) == json.dumps(_without_inactive(src[name]),
                                                                 indent=2), name
        canonical = pg2json.dump(src) == src_text
        inactive = any(not r.get("is_active", True) for t in src.values() for r in t["iov"])
        if canonical and not inactive:
            assert text == src_text, f"{fname}: an unchanged table must export byte for byte"
        (tmp_path / fname).write_text(text, encoding="utf-8")
    assert pg2json.check(db, [tmp_path / f for f in texts]) == []


@needs_real
def test_real_export_reloads_to_the_same_export(tmp_path):
    """export -> load into a fresh database -> export is the identity, with and
    without an order hint, and a reload of the same export changes no active
    interval's payload (it only moves them to new row ids)."""
    ex = cond_loader.make_executor(sqlite=str(tmp_path / "a.db"))
    cond_loader.load(ex, [REAL / f for f in pg2json.CONTAINERS])
    first, _ = pg2json.export_all(ex, REAL)
    bare, _ = pg2json.export_all(ex, None)
    d = tmp_path / "first"
    d.mkdir()
    for fname, text in first.items():
        (d / fname).write_text(text, encoding="utf-8")

    fresh = cond_loader.make_executor(sqlite=str(tmp_path / "b.db"))
    cond_loader.load(fresh, [d / f for f in first])
    assert pg2json.export_all(fresh, d)[0] == first
    assert pg2json.export_all(fresh, None)[0] == bare

    before = {t: cond_export.active_state(ex, t) for ts in pg2json.CONTAINERS.values() for t in ts}
    cond_loader.load(ex, [d / f for f in first])
    for t, state in before.items():
        assert cond_export.compare_states(t, state, cond_export.active_state(ex, t)) == []
    assert pg2json.export_all(ex, d)[0] == first
    ex.close()
    fresh.close()


@needs_real
def test_real_export_without_a_hint_is_a_fixed_point(tmp_path):
    """The first export with no hint at all (a fresh snapshot directory) must be
    what every later export hinted by it gives back: psm_geometry's rows carry
    `plane` and `rot_z_rad` on some channels only."""
    ex = cond_loader.make_executor(sqlite=str(tmp_path / "a.db"))
    cond_loader.load(ex, [REAL / f for f in pg2json.CONTAINERS])
    bare, _ = pg2json.export_all(ex, None)
    d = tmp_path / "bare"
    d.mkdir()
    for fname, text in bare.items():
        (d / fname).write_text(text, encoding="utf-8")
    assert pg2json.export_all(ex, d)[0] == bare
    fresh = cond_loader.make_executor(sqlite=str(tmp_path / "b.db"))
    cond_loader.load(fresh, [d / f for f in bare])
    assert pg2json.export_all(fresh, d)[0] == bare
    assert pg2json.export_all(fresh, None)[0] == bare
    ex.close()
    fresh.close()


@needs_real
def test_pg2json_cli_writes_and_checks(tmp_path, capsys):
    db = tmp_path / "c.db"
    ex = cond_loader.make_executor(sqlite=str(db))
    cond_loader.load(ex, [REAL / f for f in pg2json.CONTAINERS])
    ex.close()
    out = tmp_path / "out"
    out.mkdir()
    assert pg2json.main([f"sqlite:{db}", "--out-dir", str(out), "--order-from", str(REAL),
                         "--check"]) == 0
    text = capsys.readouterr().out
    assert "check: the 5 files" in text and text.count(": written") == 5
    # a second run finds nothing to change, and takes the order from --out-dir itself
    assert pg2json.main([f"sqlite:{db}", "--out-dir", str(out), "--check"]) == 0
    assert capsys.readouterr().out.count(": unchanged") == 5


# ---------------------------------------------------------------------------
# The mapping, on small tables

def _ps(name="p", tags=None, iov=None, values=None, values_by_iov=None):
    t = {"schema": name, "version": 1, "kind": "parameter_set",
         "tags": tags or [{"tag": "a", "is_default": True, "description": "d"}],
         "iov": iov or [{"row_id": 1, "tag": "a", "run_start": 0, "run_end": None,
                         "is_active": True, "created_by": "me", "comment": "c"}]}
    if values is not None:
        t["values"] = values
    if values_by_iov is not None:
        t["values_by_iov"] = values_by_iov
    return {name: t}


def test_types_nulls_arrays_and_open_intervals(db):
    rows = [{"key": "i", "value": 3}, {"key": "r", "value": 0.1}, {"key": "s", "value": "x'y"},
            {"key": "b", "value": True}, {"key": "n", "value": None},
            {"key": "arr", "value": [1, 2.5, "z", False, None]}, {"key": "e", "value": ""}]
    _load(db, _ps(values={"a": rows}))
    out = cond_export.export_table(db, "p")
    got = {r["key"]: r["value"] for r in out["values"]["a"]}
    assert got == {r["key"]: r["value"] for r in rows}
    assert type(got["b"]) is bool and type(got["i"]) is int and type(got["r"]) is float
    assert out["iov"] == [{"row_id": 1, "tag": "a", "run_start": 0, "run_end": None,
                           "is_active": True, "created_by": "me", "comment": "c"}]
    assert out["tags"] == [{"tag": "a", "is_default": True, "description": "d"}]
    assert "values_by_iov" not in out


def test_ordinal_gaps_come_back_as_one_row_per_cell(db):
    _load(db, _ps(values={"a": [{"key": "k", "ordinal": 3, "value": [7, 8]},
                               {"key": "k", "ordinal": 9, "value": 1}]}))
    out = cond_export.export_table(db, "p")
    assert out["values"]["a"] == [{"key": "k", "ordinal": 3, "value": 7},
                                  {"key": "k", "ordinal": 4, "value": 8},
                                  {"key": "k", "ordinal": 9, "value": 1}]
    # and they load back to the same cells
    other = cond_loader.make_executor(sqlite=":memory:")
    _load(other, {"p": out})
    assert cond_export.compare_states("p", cond_export.active_state(db, "p"),
                                      cond_export.active_state(other, "p")) == []


def test_one_element_arrays(db):
    """A single cell is a scalar unless the key is an array elsewhere in the
    table, is named in list_keys, or the hint spells it as an array."""
    iov = [{"row_id": 1, "tag": "a", "run_start": 0, "run_end": 5},
           {"row_id": 2, "tag": "a", "run_start": 5, "run_end": None}]
    _load(db, _ps(iov=iov, values={}, values_by_iov={
        "1": [{"key": "vid", "value": [1, 2]}, {"key": "one", "value": [9]}],
        "2": [{"key": "vid", "value": [3]}, {"key": "one", "value": [4]}]}))
    out = cond_export.export_table(db, "p")
    assert out["values_by_iov"]["2"] == [{"key": "one", "value": 4}, {"key": "vid", "value": [3]}]
    assert out["values_by_iov"]["1"][0] == {"key": "one", "value": 9}
    out = cond_export.export_table(db, "p", list_keys=("one",))
    assert out["values_by_iov"]["2"][0] == {"key": "one", "value": [4]}
    hint = _ps(iov=iov, values={}, values_by_iov={
        "1": [{"key": "vid", "value": [1, 2]}, {"key": "one", "value": [9]}],
        "2": [{"key": "vid", "value": [3]}, {"key": "one", "value": [4]}]})["p"]
    out = cond_export.export_table(db, "p", order_from=hint)
    assert out["values_by_iov"] == hint["values_by_iov"]


def test_active_only_renumbered_and_tag_wide_kept_apart(db):
    """Inactive intervals are left out, ids renumbered with values_by_iov; a tag
    with no active interval keeps its entry in tags (last, with no hint) but not
    its payload."""
    table = {"m": {
        "schema": "m", "version": 1, "kind": "channel_values",
        "tags": [{"tag": "old", "is_default": False, "description": "kept"},
                 {"tag": "new", "is_default": True, "description": ""},
                 {"tag": "wide", "is_default": False, "description": ""}],
        "iov": [{"row_id": 1, "tag": "old", "run_start": 0, "run_end": None, "is_active": False},
                {"row_id": 2, "tag": "new", "run_start": 0, "run_end": 10},
                {"row_id": 3, "tag": "new", "run_start": 10, "run_end": None},
                {"row_id": 4, "tag": "wide", "run_start": 3, "run_end": 4}],
        "values": {"old": [{"channel_id": 1, "vid": 5}],
                   "wide": [{"channel_id": 2, "vid": 6, "name": "S1"}]},
        "values_by_iov": {"2": [{"channel_id": 1, "vid": 7}], "3": [{"channel_id": 1, "vid": 8}]},
    }}
    _load(db, table)
    out = cond_export.export_table(db, "m")
    assert [(r["row_id"], r["tag"], r["run_start"]) for r in out["iov"]] == \
        [(1, "new", 0), (2, "new", 10), (3, "wide", 3)]
    assert [t["tag"] for t in out["tags"]] == ["new", "wide", "old"]
    assert out["values"] == {"wide": [{"channel_id": 2, "name": "S1", "vid": 6}]}
    assert out["values_by_iov"] == {"1": [{"channel_id": 1, "vid": 7}],
                                    "2": [{"channel_id": 1, "vid": 8}]}


def test_hint_sets_order_and_carries_the_description(db):
    table = _ps(values={"a": [{"key": "z", "value": 1}, {"key": "a", "value": 2}]})
    table["p"]["description"] = "a table-level note the database has no column for"
    table["p"] = {k: table["p"][k] for k in ("schema", "version", "kind", "tags",
                                             "description", "iov", "values")}
    _load(db, table)
    assert [r["key"] for r in cond_export.export_table(db, "p")["values"]["a"]] == ["a", "z"]
    out = cond_export.export_table(db, "p", order_from=table["p"])
    assert json.dumps(out) == json.dumps(table["p"])


def test_merge_orders():
    assert cond_export.merge_orders([["a", "x"], ["a", "p", "x", "r"]]) == ["a", "p", "x", "r"]
    assert cond_export.merge_orders([["b", "c"], ["a", "c"]]) == ["b", "a", "c"]
    # contradicting rows: no order honours both, first appearance decides
    assert cond_export.merge_orders([["a", "b"], ["b", "a"]]) == ["a", "b"]
    assert cond_export.merge_orders([]) == []


MIXED = [{"channel_id": 2001, "det_type": "MUTRIG", "name": "S1", "x_mm": 0.0, "hz_mm": 1.0},
         {"channel_id": 10011, "det_type": "MUPIX", "name": "q0", "plane": 1001, "x_mm": 10.4,
          "hz_mm": 0.0275},
         {"channel_id": 10013, "det_type": "MUPIX", "name": "q2", "plane": 1001, "x_mm": 10.4,
          "hz_mm": 0.0275, "rot_z_rad": 3.14}]


def _mixed_table(rows):
    return {"g": {"schema": "g", "version": 1, "kind": "channel_values",
                  "tags": [{"tag": "a", "is_default": True, "description": ""}],
                  "iov": [{"row_id": 1, "tag": "a", "run_start": 0, "run_end": None,
                           "is_active": True, "created_by": "", "comment": ""}],
                  "values": {"a": rows}}}


def test_rows_with_different_columns_keep_their_order(db):
    """Columns present on some rows only (plane, rot_z_rad): the hint's order is
    reproduced row by row, and an export with no hint is already a fixed point."""
    table = _mixed_table(MIXED)
    _load(db, table)
    out = cond_export.export_table(db, "g", order_from=table["g"])
    assert json.dumps(out["values"]) == json.dumps(table["g"]["values"])

    bare = cond_export.export_table(db, "g")
    assert [list(r) for r in bare["values"]["a"]][2] == \
        ["channel_id", "det_type", "hz_mm", "name", "plane", "rot_z_rad", "x_mm"]
    assert json.dumps(cond_export.export_table(db, "g", order_from=bare)) == json.dumps(bare)
    other = cond_loader.make_executor(sqlite=":memory:")
    _load(other, {"g": bare})
    assert json.dumps(cond_export.export_table(other, "g", order_from=bare)) == json.dumps(bare)
    assert json.dumps(cond_export.export_table(other, "g")) == json.dumps(bare)


def test_a_row_of_the_hint_that_contradicts_the_others_is_kept(db):
    """When the hint's rows disagree, each channel still gets its own row's order."""
    rows = [{"channel_id": 1, "a": 1, "b": 2}, {"channel_id": 2, "b": 3, "a": 4}]
    table = _mixed_table(rows)
    _load(db, table)
    out = cond_export.export_table(db, "g", order_from=table["g"])
    assert json.dumps(out["values"]["a"]) == json.dumps(rows)


def test_both_payload_kinds_on_an_active_interval_are_refused(tmp_path):
    ex = cond_loader.make_executor(sqlite=str(tmp_path / "c.db"))
    _load(ex, _ps(values={"a": [{"key": "k", "value": 1}]}))
    ex.script("INSERT INTO cond_values (table_name, tag, iov_row_id, key, column_name, ordinal, "
              "value_type, value_int) VALUES ('p', 'a', 1, 'k', 'value', 0, 'int', 2);")
    with pytest.raises(LoaderError, match="its own payload and the tag has a tag-wide one"):
        cond_export.export_table(ex, "p")


def test_check_notices_a_difference(tmp_path):
    ex = cond_loader.make_executor(sqlite=str(tmp_path / "c.db"))
    _load(ex, _ps(values={"a": [{"key": "k", "value": 1}]}))
    before = cond_export.active_state(ex, "p")
    ex.script("UPDATE cond_values SET value_int = 2 WHERE table_name = 'p';")
    problems = cond_export.compare_states("p", before, cond_export.active_state(ex, "p"))
    assert len(problems) == 1 and "1 only in the database" in problems[0]
    ex.script("UPDATE cond_tags SET description = 'x' WHERE table_name = 'p';")
    assert any("tags differ" in p for p in
               cond_export.compare_states("p", before, cond_export.active_state(ex, "p")))


def test_fingerprint_moves_with_every_interval_change(tmp_path):
    ex = cond_loader.make_executor(sqlite=str(tmp_path / "c.db"))
    _load(ex, _ps(values={"a": [{"key": "k", "value": 1}]}))
    assert cond_export.table_fingerprint(ex, "p") == (1, 1)
    _load(ex, _ps(values={"a": [{"key": "k", "value": 2}]}))
    assert cond_export.table_fingerprint(ex, "p") == (2, 1)
    ex.script("UPDATE cond_iov SET is_active = 0 WHERE table_name = 'p';")
    assert cond_export.table_fingerprint(ex, "p") == (2, 0)


def test_missing_table_is_an_error(db):
    _load(db, _ps())
    with pytest.raises(LoaderError, match="no table 'nope'"):
        cond_export.export_table(db, "nope")


def test_db_specs():
    assert cond_export.is_db_spec("service=pioneer-conditions-admin")
    assert cond_export.is_db_spec("host=h dbname=d")
    assert cond_export.is_db_spec("sqlite:/tmp/x.db")
    assert not cond_export.is_db_spec("add") and not cond_export.is_db_spec("study.json")
    assert cond_loader.describe_conninfo("service=pioneer-conditions password=s3cret") \
        == "service=pioneer-conditions"


# ---------------------------------------------------------------------------
# condtool export

def test_condtool_export(tmp_path):
    db = tmp_path / "c.db"
    ex = cond_loader.make_executor(sqlite=str(db))
    _load(ex, _ps(values={"a": [{"key": "z", "value": 1}, {"key": "a", "value": 2}]}))
    ex.close()
    out = tmp_path / "p.json"
    proc = subprocess.run([sys.executable, str(CONDDB / "condtool.py"), "--sqlite", str(db),
                           "export", "p", "--out", str(out)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(out.read_text())
    assert list(doc) == ["p"] and [r["key"] for r in doc["p"]["values"]["a"]] == ["a", "z"]
    proc = subprocess.run([sys.executable, str(CONDDB / "condtool.py"), "--sqlite", str(db),
                           "export", "p"], capture_output=True, text=True)
    assert proc.returncode == 0 and json.loads(proc.stdout) == doc
    proc = subprocess.run([sys.executable, str(CONDDB / "condtool.py"), "--sqlite", str(db),
                           "export", "nope"], capture_output=True, text=True)
    assert proc.returncode == 1 and "no table 'nope'" in proc.stderr


# ---------------------------------------------------------------------------
# Review fixes: unmapped tables, lost updates, float precision, -0.0, list keys

def _mapped_db(tmp_path, extra=True):
    """A SQLite database with every table pg2json maps (tiny stand-ins), plus
    one no container names when ``extra``."""
    path = tmp_path / "m.db"
    ex = cond_loader.make_executor(sqlite=str(path))
    for tables in pg2json.CONTAINERS.values():
        for t in tables:
            _load(ex, _ps(t, values={"a": [{"key": "k", "value": 1}]}))
    if extra:
        _load(ex, _ps("sma_new_offsets", values={"a": [{"key": "k", "value": 2}]}))
    ex.close()
    return path


def test_pg2json_strict_refuses_an_unmapped_table(tmp_path, capsys):
    db = _mapped_db(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    assert pg2json.main([f"sqlite:{db}", "--out-dir", str(out), "--strict"]) == 3
    err = capsys.readouterr().err
    assert "sma_new_offsets" in err and "nothing written" in err
    assert list(out.iterdir()) == []
    # without --strict the files are written, and --check still fails on it
    assert pg2json.main([f"sqlite:{db}", "--out-dir", str(out), "--check"]) == 1
    assert "sma_new_offsets" in capsys.readouterr().err
    assert len(list(out.iterdir())) == 5
    (tmp_path / "ok").mkdir()
    ok = _mapped_db(tmp_path / "ok", extra=False)
    assert pg2json.main([f"sqlite:{ok}", "--out-dir", str(out), "--strict", "--check"]) == 0


def test_list_keys_match_the_writers():
    from pioneer.conddb import mupix_mask, mupix_timewalk
    assert pg2json.LIST_KEYS["mupix_pixel_mask"] == mupix_mask.MASK_ARRAYS
    assert pg2json.LIST_KEYS["mupix_timewalk"] == mupix_timewalk.ARRAYS + ("comment",)


def test_first_snapshot_keeps_one_pixel_arrays(tmp_path):
    db = _mapped_db(tmp_path, extra=False)
    ex = cond_loader.make_executor(sqlite=str(db))
    _load(ex, _ps("mupix_pixel_mask", values={}, values_by_iov={"1": [
        {"key": "n_pixels", "value": 1}, {"key": "vid", "value": [10011]},
        {"key": "col", "value": [3]}, {"key": "row", "value": [4]}]}))
    texts, _ = pg2json.export_all(ex, None)
    ex.close()
    mask = json.loads(texts["bt2026_psm_readout_map.json"])["mupix_pixel_mask"]
    assert mask["values_by_iov"]["1"] == [{"key": "col", "value": [3]},
                                          {"key": "n_pixels", "value": 1},
                                          {"key": "row", "value": [4]},
                                          {"key": "vid", "value": [10011]}]


def _condtool(*argv):
    return subprocess.run([sys.executable, str(CONDDB / "condtool.py"), *map(str, argv)],
                          capture_output=True, text=True)


def _json2sqlite(*argv):
    return subprocess.run([sys.executable, str(CONDDB / "json2sqlite.py"), *map(str, argv)],
                          capture_output=True, text=True)


def test_condtool_export_records_the_fingerprint_and_a_stale_load_is_refused(tmp_path):
    """export -> (someone else loads) -> load of the edited export: refused, the
    other writer's interval survives; --force loads anyway."""
    db = tmp_path / "c.db"
    ex = cond_loader.make_executor(sqlite=str(db))
    _load(ex, _ps(values={"a": [{"key": "k", "value": 1}]}))
    ex.close()
    out = tmp_path / "p.json"
    proc = _condtool("--sqlite", db, "export", "p", "--out", out)
    assert proc.returncode == 0 and "highest row_id 1, 1 active" in proc.stderr
    doc = json.loads(out.read_text())
    assert doc["p"][cond_loader.EXPORT_KEY]["fingerprint"] == [1, 1]

    ex = cond_loader.make_executor(sqlite=str(db))
    _load(ex, _ps(values={"a": [{"key": "k", "value": 2}]}))        # the other writer
    ex.close()
    doc["p"]["values"]["a"][0]["value"] = 3                          # our edit
    out.write_text(json.dumps(doc))
    proc = _json2sqlite(db, out)
    assert proc.returncode == 1 and "changed since it was read" in proc.stderr
    ex = cond_loader.make_executor(sqlite=str(db))
    assert cond_export.export_table(ex, "p")["values"]["a"] == [{"key": "k", "value": 2}]
    ex.close()
    assert _json2sqlite(db, out, "--force").returncode == 0
    ex = cond_loader.make_executor(sqlite=str(db))
    got = cond_export.export_table(ex, "p")
    ex.close()
    assert got["values"]["a"] == [{"key": "k", "value": 3}]
    assert cond_loader.EXPORT_KEY not in got

    # a fresh export loads without --force, via json2pg's own flag parsing too
    proc = _condtool("--sqlite", db, "export", "p", "--out", out)
    assert _json2sqlite(db, out).returncode == 0


def test_json2pg_expect_fingerprint_flag(tmp_path, monkeypatch, capsys):
    from pioneer.conddb import json2pg
    db = tmp_path / "c.db"
    ex = cond_loader.make_executor(sqlite=str(db))
    _load(ex, _ps(values={"a": [{"key": "k", "value": 1}]}))
    ex.close()
    f = tmp_path / "p.json"
    f.write_text(json.dumps(_ps(values={"a": [{"key": "k", "value": 5}]})))
    # json2pg speaks psql only; drive its argument handling against SQLite
    monkeypatch.setattr(json2pg, "make_executor",
                        lambda **kw: cond_loader.make_executor(sqlite=str(db)))
    monkeypatch.setattr(sys, "argv", ["json2pg.py", "x=y", str(f),
                                      "--expect-fingerprint", "p=7,1"])
    assert json2pg.main() == 1
    assert "changed since it was read" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["json2pg.py", "x=y", str(f),
                                      "--expect-fingerprint", "p=1,1"])
    assert json2pg.main() == 0


@pytest.mark.skipif(not PG_DSN, reason="PIONEER_CONDDB_TEST_DSN not set")
def test_pg_keeps_negative_zero_and_full_precision(monkeypatch):
    """-0.0 survives a load (a bare -0.0 literal is numeric negation, +0), and a
    server set to extra_float_digits = 0 still exports every real exactly."""
    ex = _pg_executor()
    vals = [-0.0, 0.1234567890123456789, 1e-300, 2.0 / 3.0]
    _load(ex, _ps(values={"a": [{"key": "k", "value": vals}]}))
    monkeypatch.setenv("PGOPTIONS", "-c extra_float_digits=0")
    got = cond_export.export_table(ex, "p")["values"]["a"][0]["value"]
    ex.close()
    assert got == vals and [repr(v) for v in got] == [repr(v) for v in vals]
