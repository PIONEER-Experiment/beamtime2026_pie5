"""The MuPix pixel-mask CLI (pioneer.conddb.mupix_mask), on scratch containers.

Every test writes into tmp_path only. The container is a small stand-in for
bt2026_psm_readout_map.json with the two things the tool reads from it: a
mupix_chip_map whose raw-id mapping changes between two intervals (as the FEB
Mapping did at run 200) and the mupix_pixel_mask table as it ships, an empty
mask on [0, open). One test also reads the real container, when the offline
tree is checked out beside this repository: the shipped file, or the one
PI_TB_READOUT_MAP_FILE names (a scratch copy with a mask added, say). It
checks invariants only, so adding a mask to the shipped file keeps it passing.
"""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from pioneer.conddb import cond_loader, mupix_mask
from pioneer.conddb.mupix_mask import MaskError, main

SHIPPED = Path(os.environ.get("PI_TB_READOUT_MAP_FILE") or (
    Path(__file__).resolve().parents[3]
    / "main" / "reco_testbeam" / "conditions" / "bt2026_psm_readout_map.json"))

# raw chip id -> detector id before run 200 (Mapping shifted by one) and after
EARLY = {1: 10011, 2: 10012, 3: 10013, 4: 10014, 5: 10021, 6: 10022, 7: 10023, 0: 10024}
LATE = {0: 10011, 1: 10012, 2: 10013, 3: 10014, 4: 10021, 5: 10022, 6: 10023, 7: 10024}


def _container():
    return {
        "mupix_chip_map": {
            "schema": "psm_raw_channel_map", "version": 1, "kind": "channel_values",
            "tags": [{"tag": "febmap", "is_default": True, "description": ""}],
            "iov": [
                {"row_id": 1, "tag": "febmap", "run_start": 0, "run_end": 200, "is_active": True},
                {"row_id": 2, "tag": "febmap", "run_start": 200, "run_end": None, "is_active": True},
            ],
            "values": {},
            "values_by_iov": {
                "1": [{"channel_id": r, "vid": v} for r, v in EARLY.items()],
                "2": [{"channel_id": r, "vid": v} for r, v in LATE.items()],
            },
        },
        "sma_coarse_shift": {
            "schema": "sma_coarse_shift", "version": 1, "kind": "parameter_set",
            "tags": [{"tag": "m", "is_default": True, "description": ""}],
            "iov": [{"row_id": 1, "tag": "m", "run_start": 0, "run_end": None, "is_active": True}],
            "values": {}, "values_by_iov": {"1": [{"key": "CoarseShift", "value": 14}]},
        },
        "mupix_pixel_mask": {
            "schema": "mupix_pixel_mask", "version": 1, "kind": "parameter_set",
            "tags": [{"tag": "hot", "is_default": True, "description": "hot pixels"}],
            "description": "test",
            "iov": [{"row_id": 1, "tag": "hot", "run_start": 0, "run_end": None,
                     "is_active": True, "created_by": "test", "comment": "empty"}],
            "values": {},
            "values_by_iov": {"1": [{"key": "n_pixels", "value": 0}]},
        },
    }


@pytest.fixture
def conditions(tmp_path):
    """A conditions directory holding the stand-in container."""
    d = tmp_path / "conditions"
    d.mkdir()
    (d / mupix_mask.CONTAINER).write_text(json.dumps(_container(), indent=2) + "\n")
    return d


def _doc(conditions):
    return json.loads((conditions / mupix_mask.CONTAINER).read_text())


def _write(path, obj):
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return path


def _study(tmp_path, pixels, run=459):
    """The shape of noisy_pixels.json: the recommended mask and the run's hot records."""
    return _write(tmp_path / "noisy_pixels.json", {
        "primary_run": run,
        "recommended": {"run": run, "pixel_mask": {"pixels": [list(p) for p in pixels], "n": len(pixels)}},
        "runs": {str(run): {"hot": {"pixels": [
            {"chip": c, "col": x, "row": y, "hits_no_burst": 100 + i, "expected_no_burst": 0.1}
            for i, (c, x, y) in enumerate(pixels)]}}},
    })


def _add(conditions, *argv):
    return main(["--conditions", str(conditions), "add", *map(str, argv)])


def _params(table, row_id):
    return {r["key"]: r["value"] for r in table["values_by_iov"][str(row_id)]}


# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(conditions, tmp_path, capsys):
    before = (conditions / mupix_mask.CONTAINER).read_text()
    study = _study(tmp_path, [(5, 0, 22), (4, 137, 2)])
    assert _add(conditions, study, "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "c") == 0
    out = capsys.readouterr().out
    assert "dry run: nothing written" in out
    assert "+" in out and "@@" in out, "the unified diff is printed"
    assert (conditions / mupix_mask.CONTAINER).read_text() == before


def test_overlap_refused_without_split(conditions, tmp_path, capsys):
    study = _study(tmp_path, [(5, 0, 22)])
    assert _add(conditions, study, "--run-start", 459, "--last-run", 459,
                "--comment", "c", "--write") == 1
    err = capsys.readouterr().err
    assert "overlaps active interval" in err and "row 1 [0, open)" in err and "--split" in err
    assert _doc(conditions) == _container()


def test_split_carves_the_open_interval(conditions, tmp_path):
    before = _doc(conditions)
    study = _study(tmp_path, [(5, 0, 22), (4, 137, 2), (1, 0, 63)])
    assert _add(conditions, study, "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "hot pixels of 459", "--created-by", "tester", "--write") == 0
    doc = _doc(conditions)
    table = doc["mupix_pixel_mask"]
    rows = {r["row_id"]: r for r in table["iov"]}
    assert not rows[1]["is_active"] and rows[1]["run_start"] == 0 and rows[1]["run_end"] is None
    assert (rows[2]["run_start"], rows[2]["run_end"], rows[2]["is_active"]) == (0, 459, True)
    assert (rows[3]["run_start"], rows[3]["run_end"], rows[3]["is_active"]) == (460, None, True)
    assert (rows[4]["run_start"], rows[4]["run_end"], rows[4]["is_active"]) == (459, 460, True)
    assert rows[4]["created_by"] == "tester" and rows[4]["comment"].startswith("hot pixels of 459")
    # the parts outside keep the old (empty) payload, with no arrays
    assert table["values_by_iov"]["2"] == [{"key": "n_pixels", "value": 0}]
    assert table["values_by_iov"]["3"] == [{"key": "n_pixels", "value": 0}]
    # raw ids converted at run 459 (identity Mapping), sorted by (vid, col, row)
    p = _params(table, 4)
    assert p["n_pixels"] == 3
    assert list(zip(p["vid"], p["col"], p["row"])) == [(10012, 0, 63), (10021, 137, 2), (10022, 0, 22)]
    assert len(p["reason"]) == 3 and "hot in run 459" in p["reason"][0]
    # the other tables are untouched, byte for byte
    for name in ("mupix_chip_map", "sma_coarse_shift"):
        assert doc[name] == before[name]
    text = (conditions / mupix_mask.CONTAINER).read_text()
    assert text == json.dumps(doc, indent=2) + "\n"

    # every run resolves to exactly one interval, the right one
    for run, n in ((0, 0), (458, 0), (459, 3), (460, 0), (100000, 0)):
        _row, payload = mupix_mask.resolve(table, "mupix_pixel_mask", run)
        assert len(mupix_mask.mask_pixels(payload)) == n


def test_second_split_inside_a_remnant(conditions, tmp_path):
    study = _study(tmp_path, [(5, 0, 22)])
    assert _add(conditions, study, "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "a", "--write") == 0
    other = _write(tmp_path / "l.csv", "vid,col,row\n10011,3,4\n")
    assert _add(conditions, other, "--run-start", 500, "--split", "--comment", "b", "--write") == 0
    table = _doc(conditions)["mupix_pixel_mask"]
    active = sorted((r["run_start"], r["run_end"]) for r in table["iov"] if r["is_active"])
    assert active == [(0, 459), (459, 460), (460, 500), (500, None)]
    _row, payload = mupix_mask.resolve(table, "mupix_pixel_mask", 777)
    assert [(p.vid, p.col, p.row) for p in mupix_mask.mask_pixels(payload)] == [(10011, 3, 4)]


def test_raw_ids_follow_the_chip_map_of_the_run(conditions, tmp_path):
    """The same raw chip is another sensor before run 200: the mask names the sensor."""
    study = _study(tmp_path, [(0, 10, 20)], run=150)
    assert _add(conditions, study, "--run-start", 150, "--last-run", 150, "--split",
                "--comment", "c", "--write") == 0
    table = _doc(conditions)["mupix_pixel_mask"]
    _row, payload = mupix_mask.resolve(table, "mupix_pixel_mask", 150)
    assert [p.vid for p in mupix_mask.mask_pixels(payload)] == [EARLY[0]] == [10024]


def test_map_run_overrides_the_study_run(conditions, tmp_path):
    study = _study(tmp_path, [(0, 10, 20)], run=150)
    assert _add(conditions, study, "--run-start", 150, "--last-run", 150, "--split",
                "--map-run", 459, "--comment", "c", "--write") == 0
    table = _doc(conditions)["mupix_pixel_mask"]
    _row, payload = mupix_mask.resolve(table, "mupix_pixel_mask", 150)
    assert [p.vid for p in mupix_mask.mask_pixels(payload)] == [LATE[0]]


def test_per_run_study_file(conditions, tmp_path):
    per_run = _write(tmp_path / "noisy_pixels_run00459.json", {
        "run": 459, "hits": 1,
        "hot": {"pixels": [{"chip": 6, "col": 164, "row": 4, "hits_no_burst": 189,
                            "expected_no_burst": 0.2}]}})
    assert _add(conditions, per_run, "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "c", "--write") == 0
    p = _params(_doc(conditions)["mupix_pixel_mask"], 4)
    assert (p["vid"], p["col"], p["row"]) == ([10023], [164], [4])
    assert p["reason"] == ["hot in run 459, 189 hits outside bursts, 0.2 expected"]


@pytest.mark.parametrize("text, extra, expect", [
    ("vid,col,row,reason\n10021,1,2,loud\n10011,3,4,\n", [], [(10011, 3, 4), (10021, 1, 2)]),
    ("# a comment\nchip,col,row\n4,1,2\n", ["--map-run", 459], [(10021, 1, 2)]),
    ("10014,7,8\n", ["--chip-ids", "vid"], [(10014, 7, 8)]),
    ("3,7,8\n", ["--chip-ids", "raw", "--map-run", 459], [(10014, 7, 8)]),
])
def test_csv_lists(conditions, tmp_path, text, extra, expect):
    lst = _write(tmp_path / "list.csv", text)
    assert _add(conditions, lst, "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "c", "--write", *extra) == 0
    p = _params(_doc(conditions)["mupix_pixel_mask"], 4)
    assert list(zip(p["vid"], p["col"], p["row"])) == expect


def test_json_lists(conditions, tmp_path):
    lst = _write(tmp_path / "list.json", [{"vid": 10022, "col": 0, "row": 1, "reason": "r"},
                                          {"vid": 10022, "col": 0, "row": 0}])
    assert _add(conditions, lst, "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "c", "--write") == 0
    p = _params(_doc(conditions)["mupix_pixel_mask"], 4)
    assert list(zip(p["vid"], p["col"], p["row"], p["reason"])) == [(10022, 0, 0, ""), (10022, 0, 1, "r")]

    triples = _write(tmp_path / "t.json", [[10011, 5, 5]])
    assert _add(conditions, triples, "--run-start", 600, "--split", "--comment", "c") == 1, \
        "a bare list of triples needs --chip-ids"


def test_no_reason_array_without_reasons(conditions, tmp_path):
    lst = _write(tmp_path / "l.csv", "vid,col,row\n10011,1,1\n")
    assert _add(conditions, lst, "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "c", "--write") == 0
    assert "reason" not in _params(_doc(conditions)["mupix_pixel_mask"], 4)


@pytest.mark.parametrize("text, why", [
    ("vid,col,row\n10011,1,250\n", "row outside 0-249"),
    ("vid,col,row\n10011,256,1\n", "column outside 0-255"),
    ("vid,col,row\n10011,-1,1\n", "column outside 0-255"),
    ("vid,col,row\n10011,1,2\n10011,1,2\n", "listed twice"),
    ("vid,col,row\n2001,1,2\n", "is no chip of mupix_chip_map"),
    ("chip,col,row\n9,1,2\n", "raw chip 9 has no entry in mupix_chip_map at run 459"),
    ("chip,col,row\n1,1,2\n", "--map-run"),
    ("vid,chip,col,row\n1,1,1,2\n", "must name 'vid' or 'chip'"),
    ("vid,col,row\n10011,x,2\n", "not an integer"),
])
def test_refused_pixels(conditions, tmp_path, capsys, text, why):
    lst = _write(tmp_path / "bad.csv", text)
    extra = ["--map-run", 459] if "has no entry" in why else []
    assert _add(conditions, lst, "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "c", "--write", *extra) == 1
    assert why in capsys.readouterr().err
    assert _doc(conditions) == _container()


def test_refused_intervals(conditions, tmp_path, capsys):
    lst = _write(tmp_path / "l.csv", "vid,col,row\n10011,1,1\n")
    assert _add(conditions, lst, "--run-start", 10, "--run-end", 10, "--split", "--comment", "c") == 1
    assert "is empty" in capsys.readouterr().err
    assert _add(conditions, lst, "--run-start", -1, "--split", "--comment", "c") == 1
    assert "negative" in capsys.readouterr().err


def test_new_tag(conditions, tmp_path, capsys):
    lst = _write(tmp_path / "l.csv", "vid,col,row\n10011,1,1\n")
    assert _add(conditions, lst, "--run-start", 0, "--tag", "trial", "--comment", "c") == 1
    assert "--tag-description" in capsys.readouterr().err
    assert _add(conditions, lst, "--run-start", 0, "--tag", "trial", "--tag-description", "a trial",
                "--comment", "c", "--write") == 0
    table = _doc(conditions)["mupix_pixel_mask"]
    assert {"tag": "trial", "is_default": False, "description": "a trial"} in table["tags"]
    # no overlap across tags, and the default tag still resolves to the empty mask
    assert [r["is_active"] for r in table["iov"]] == [True, True]
    _row, payload = mupix_mask.resolve(table, "mupix_pixel_mask", 5)
    assert mupix_mask.mask_pixels(payload) == []
    _row, payload = mupix_mask.resolve(table, "mupix_pixel_mask", 5, "trial")
    assert len(mupix_mask.mask_pixels(payload)) == 1


def test_table_out_loads_into_sqlite(conditions, tmp_path):
    """The table alone, through the ordinary loader, resolves like the JSON."""
    study = _study(tmp_path, [(5, 0, 22), (4, 137, 2)])
    out = tmp_path / "mask_only.json"
    assert _add(conditions, study, "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "c", "--write", "--table-out", out) == 0
    only = json.loads(out.read_text())
    assert list(only) == ["mupix_pixel_mask"]
    assert only["mupix_pixel_mask"] == _doc(conditions)["mupix_pixel_mask"]

    db = tmp_path / "c.db"
    ex = cond_loader.make_executor(sqlite=str(db))
    cond_loader.load(ex, [out])
    ex.close()
    con = sqlite3.connect(db)
    active = con.execute("SELECT row_id, run_start, run_end FROM cond_iov WHERE table_name = "
                         "'mupix_pixel_mask' AND is_active = 1 ORDER BY run_start").fetchall()
    assert [(a[1], a[2]) for a in active] == [(0, 459), (459, 460), (460, None)]
    mask_row = next(a[0] for a in active if a[1] == 459)
    cells = con.execute("SELECT key, ordinal, value_int FROM cond_values WHERE table_name = "
                        "'mupix_pixel_mask' AND iov_row_id = ? AND value_type = 'int' "
                        "ORDER BY key, ordinal", (mask_row,)).fetchall()
    assert ("n_pixels", 0, 2) in cells
    assert [c[2] for c in cells if c[0] == "vid"] == [10021, 10022]
    # the empty intervals hold n_pixels alone: an empty array would have no cell
    empty_row = next(a[0] for a in active if a[1] == 0)
    assert con.execute("SELECT key, value_int FROM cond_values WHERE table_name = "
                       "'mupix_pixel_mask' AND iov_row_id = ?", (empty_row,)).fetchall() \
        == [("n_pixels", 0)]


def test_show(conditions, tmp_path, capsys):
    study = _study(tmp_path, [(5, 0, 22)])
    _add(conditions, study, "--run-start", 459, "--last-run", 459, "--split", "--comment", "c", "--write")
    capsys.readouterr()
    assert main(["--conditions", str(conditions), "show", "--run", "459"]) == 0
    out = capsys.readouterr().out
    assert "run 459 reads row 4 [459, 460)" in out and "1 masked pixel(s)" in out and "10022" in out


def test_payload_rules():
    assert mupix_mask.payload([]) == [{"key": "n_pixels", "value": 0}]
    with pytest.raises(MaskError):
        mupix_mask.mask_pixels([{"key": "n_pixels", "value": 2}, {"key": "vid", "value": [1]},
                                {"key": "col", "value": [1]}, {"key": "row", "value": [1]}])
    with pytest.raises(MaskError):
        mupix_mask.mask_pixels([{"key": "vid", "value": [1]}])


@pytest.mark.skipif(not SHIPPED.is_file(), reason="offline tree not checked out beside this repo")
def test_real_container_invariants(tmp_path):
    """The container the nearline reads, whatever masks it holds: every run resolves to
    exactly one active interval of the default tag, every mask passes the decoder's rules
    against the chip map of its runs, the tool round-trips the file byte for byte, and a
    study still goes into a copy of it (--union, so an existing mask is kept)."""
    text = SHIPPED.read_text(encoding="utf-8")
    doc = json.loads(text)
    table = doc["mupix_pixel_mask"]
    assert mupix_mask.validate(doc) == []

    default = mupix_mask.select_tag(table, None, "mupix_pixel_mask")
    spans = [r for r in table["iov"] if r["tag"] == default and r.get("is_active", True)]
    for r in spans:
        last = r["run_start"] + 100000 if r["run_end"] is None else r["run_end"] - 1
        for run in (r["run_start"], last):
            row, payload = mupix_mask.resolve(table, "mupix_pixel_mask", run)
            assert row["row_id"] == r["row_id"]
            mupix_mask.mask_pixels(payload)
    assert mupix_mask.dump(doc) == text, \
        "the tool rewrites the file with json.dumps(indent=2, ensure_ascii=False); it must round-trip"

    d = tmp_path / "c"
    d.mkdir()
    (d / mupix_mask.CONTAINER).write_text(text, encoding="utf-8")
    _row, before = mupix_mask.resolve(table, "mupix_pixel_mask", 459)
    old = {(p.vid, p.col, p.row) for p in mupix_mask.mask_pixels(before)}
    study = _study(tmp_path, [(5, 0, 22), (4, 137, 2), (1, 0, 63)])
    assert _add(d, study, "--run-start", 459, "--last-run", 459, "--split", "--union",
                "--comment", "c", "--write") == 0
    after_doc = json.loads((d / mupix_mask.CONTAINER).read_text(encoding="utf-8"))
    assert mupix_mask.validate(after_doc) == []
    _row, payload = mupix_mask.resolve(after_doc["mupix_pixel_mask"], "mupix_pixel_mask", 459)
    got = {(p.vid, p.col, p.row) for p in mupix_mask.mask_pixels(payload)}
    assert got == old | {(10022, 0, 22), (10021, 137, 2), (10012, 0, 63)}


# ---------------------------------------------------------------------------
# --split over an interval that already masks pixels

def _mask_at(conditions, run):
    table = _doc(conditions)["mupix_pixel_mask"]
    _row, payload = mupix_mask.resolve(table, "mupix_pixel_mask", run)
    return {(p.vid, p.col, p.row) for p in mupix_mask.mask_pixels(payload)}


def _csv(tmp_path, name, *pixels):
    return _write(tmp_path / name, "vid,col,row,reason\n"
                  + "".join(f"{v},{c},{r},{name}\n" for v, c, r in pixels))


A = [(10011, 1, 1), (10012, 2, 2)]
B = [(10012, 2, 2), (10021, 3, 3)]


def test_split_over_a_mask_is_refused(conditions, tmp_path, capsys):
    assert _add(conditions, _csv(tmp_path, "a.csv", *A), "--run-start", 459, "--last-run", 459,
                "--split", "--comment", "a", "--write") == 0
    before = _doc(conditions)
    capsys.readouterr()
    assert _add(conditions, _csv(tmp_path, "b.csv", *B), "--run-start", 455, "--run-end", 465,
                "--split", "--comment", "b", "--write") == 1
    err = capsys.readouterr().err
    assert "already mask pixels" in err and "row 4 [459, 460) (2 pixel(s))" in err
    assert "--union" in err and "--replace-mask" in err
    assert _doc(conditions) == before


def test_split_over_an_empty_mask_needs_no_flag(conditions, tmp_path):
    """The shipped empty interval is carved without --union or --replace-mask."""
    assert _add(conditions, _csv(tmp_path, "a.csv", *A), "--run-start", 459, "--last-run", 459,
                "--split", "--comment", "a", "--write") == 0
    assert _mask_at(conditions, 459) == set(A)


def test_replace_mask_drops_and_says_how_many(conditions, tmp_path, capsys):
    assert _add(conditions, _csv(tmp_path, "a.csv", *A), "--run-start", 459, "--last-run", 459,
                "--split", "--comment", "a", "--write") == 0
    capsys.readouterr()
    assert _add(conditions, _csv(tmp_path, "b.csv", *B), "--run-start", 455, "--run-end", 465,
                "--split", "--replace-mask", "--comment", "b", "--write") == 0
    out = capsys.readouterr().out
    # A has two pixels, B lists one of them: one is dropped over [459, 460)
    assert "row 4: --replace-mask drops 1 of its 2 pixel(s) over [459, 460)" in out
    assert [line.split(":")[0].strip() for line in out.splitlines()
            if "--replace-mask drops" in line] == ["row 4"], \
        "only the overlapped row with a mask is reported; the empty rows 2 and 3 drop nothing"
    for run in (455, 459, 464):
        assert _mask_at(conditions, run) == set(B)
    assert _mask_at(conditions, 454) == set() and _mask_at(conditions, 465) == set()
    assert mupix_mask.validate(_doc(conditions)) == []


def test_union_keeps_the_old_pixels_and_splits_where_they_differ(conditions, tmp_path, capsys):
    assert _add(conditions, _csv(tmp_path, "a.csv", *A), "--run-start", 459, "--last-run", 459,
                "--split", "--comment", "a", "--write") == 0
    capsys.readouterr()
    assert _add(conditions, _csv(tmp_path, "b.csv", *B), "--run-start", 455, "--run-end", 465,
                "--split", "--union", "--comment", "b", "--write") == 0
    table = _doc(conditions)["mupix_pixel_mask"]
    active = sorted((r["run_start"], r["run_end"]) for r in table["iov"] if r["is_active"])
    assert active == [(0, 455), (455, 459), (459, 460), (460, 465), (465, None)]
    assert _mask_at(conditions, 455) == _mask_at(conditions, 458) == set(B)
    assert _mask_at(conditions, 459) == set(A) | set(B)
    assert _mask_at(conditions, 460) == _mask_at(conditions, 464) == set(B)
    assert _mask_at(conditions, 454) == _mask_at(conditions, 465) == set()
    # the old reason survives on the pixel only the old mask had
    row459 = next(r for r in table["iov"] if r["is_active"] and r["run_start"] == 459)
    p = _params(table, row459["row_id"])
    reasons = dict(zip(zip(p["vid"], p["col"], p["row"]), p["reason"]))
    assert reasons[(10011, 1, 1)] == "a.csv" and reasons[(10012, 2, 2)] == "b.csv"
    assert "Union with the mask of row(s) 4: 1 of their pixel(s) added." in row459["comment"]
    assert mupix_mask.validate(_doc(conditions)) == []


def test_union_merges_stretches_with_equal_masks(conditions, tmp_path):
    """Two adjacent intervals with the same mask: the union over both is one row."""
    a = _csv(tmp_path, "a.csv", *A)
    assert _add(conditions, a, "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "a", "--write") == 0
    assert _add(conditions, a, "--run-start", 460, "--last-run", 460, "--split",
                "--comment", "a", "--write") == 0
    assert _add(conditions, _csv(tmp_path, "b.csv", *B), "--run-start", 459, "--last-run", 460,
                "--split", "--union", "--comment", "b", "--write") == 0
    table = _doc(conditions)["mupix_pixel_mask"]
    active = sorted((r["run_start"], r["run_end"]) for r in table["iov"] if r["is_active"])
    assert active == [(0, 459), (459, 461), (461, None)]
    assert _mask_at(conditions, 459) == _mask_at(conditions, 460) == set(A) | set(B)


def test_union_and_replace_need_split_and_exclude_each_other(conditions, tmp_path, capsys):
    b = _csv(tmp_path, "b.csv", *B)
    assert _add(conditions, b, "--run-start", 459, "--union", "--comment", "b") == 1
    assert "need --split" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        _add(conditions, b, "--run-start", 459, "--split", "--union", "--replace-mask",
             "--comment", "b")


def test_remnant_comments_stay_flat(conditions, tmp_path):
    """A remnant of a remnant names its parent once and the original comment once."""
    lst = _csv(tmp_path, "a.csv", *A)
    for start in (459, 500, 480, 470):
        assert _add(conditions, lst, "--run-start", start, "--last-run", start, "--split",
                    "--union", "--comment", "c", "--write") == 0
    table = _doc(conditions)["mupix_pixel_mask"]
    remnants = [r for r in table["iov"] if r["comment"].startswith("Split from row ")]
    assert remnants, "the splits left remnants"
    for r in remnants:
        assert r["comment"].count("Split from") == 1, r["comment"]
        assert r["comment"].count("deactivated by mupix_mask") == (0 if r["is_active"] else 1)
        if r["is_active"] and mupix_mask.mask_pixels(table["values_by_iov"][str(r["row_id"])]) == []:
            assert r["comment"].endswith("): empty"), r["comment"]
    rows = {r["row_id"]: r for r in table["iov"]}
    last = next(r for r in remnants if r["is_active"] and r["run_start"] == 471)
    parent = int(last["comment"].split()[3])
    assert last["comment"] == (f"Split from row {parent} [{rows[parent]['run_start']}, "
                               f"{rows[parent]['run_end']}): empty")
    assert mupix_mask.original_comment("Split from row 7 [460, open): x") == "x"
    assert mupix_mask.original_comment("plain") == "plain"


# ---------------------------------------------------------------------------
# validate() and check: the decoder's rules over every run

def _check(conditions):
    return main(["--conditions", str(conditions), "check"])


def test_check_passes_the_fixture_and_a_split(conditions, tmp_path, capsys):
    assert _check(conditions) == 0
    assert _add(conditions, _csv(tmp_path, "a.csv", *A), "--run-start", 150, "--last-run", 250,
                "--split", "--comment", "a", "--write") == 0
    assert _check(conditions) == 0
    assert "every run resolves to exactly one interval" in capsys.readouterr().out


@pytest.mark.parametrize("breakage, why", [
    ("overlap", "overlap"),
    ("gap", "leave a gap"),
    ("start", "runs [0, 10) have no interval"),
    ("closed", "not open-ended"),
    ("unknown_vid", "detector id 2001 is no chip"),
    ("two_raws", "raw chip ids [0, 7] at run 200"),
    ("off_sensor", "off the 256 x 250 sensor"),
    ("count", "does not match the array lengths"),
])
def test_validate_catches(conditions, breakage, why, capsys):
    doc = _doc(conditions)
    t = doc["mupix_pixel_mask"]
    iov, byv = t["iov"], t["values_by_iov"]
    mask = [{"key": "n_pixels", "value": 1}, {"key": "vid", "value": [10011]},
            {"key": "col", "value": [1]}, {"key": "row", "value": [1]}]
    if breakage == "overlap":
        iov.append({"row_id": 2, "tag": "hot", "run_start": 5, "run_end": 6, "is_active": True})
        byv["2"] = mask
    elif breakage == "gap":
        iov[0]["run_end"] = 10
        iov.append({"row_id": 2, "tag": "hot", "run_start": 11, "run_end": None, "is_active": True})
        byv["2"] = mask
    elif breakage == "start":
        iov[0]["run_start"] = 10
    elif breakage == "closed":
        iov[0]["run_end"] = 10
    elif breakage == "unknown_vid":
        byv["1"] = mask
        byv["1"][1]["value"] = [2001]
    elif breakage == "two_raws":
        byv["1"] = mask
        cmap = doc["mupix_chip_map"]["values_by_iov"]["2"]
        next(r for r in cmap if r["channel_id"] == 7)["vid"] = 10011
    elif breakage == "off_sensor":
        byv["1"] = mask
        byv["1"][3]["value"] = [250]
    elif breakage == "count":
        byv["1"] = mask
        byv["1"][0]["value"] = 2
    problems = mupix_mask.validate(doc)
    assert any(why in p for p in problems), problems
    (conditions / mupix_mask.CONTAINER).write_text(json.dumps(doc, indent=2) + "\n")
    assert _check(conditions) == 1
    assert why in capsys.readouterr().out


def test_add_refuses_a_result_the_decoder_would_reject(conditions, tmp_path, capsys):
    """A chip map giving one detector id to two raw ids: masking that sensor is refused."""
    doc = _doc(conditions)
    cmap = doc["mupix_chip_map"]["values_by_iov"]["2"]
    next(r for r in cmap if r["channel_id"] == 7)["vid"] = 10011
    (conditions / mupix_mask.CONTAINER).write_text(json.dumps(doc, indent=2) + "\n")
    assert _add(conditions, _csv(tmp_path, "a.csv", (10011, 1, 1)), "--run-start", 459,
                "--last-run", 459, "--split", "--comment", "a", "--write") == 1
    assert "would fail the decoder" in capsys.readouterr().err


def test_non_ascii_is_written_as_is(conditions, tmp_path):
    """dump() writes UTF-8, not \\u escapes, so a hand edit and the tool agree."""
    assert _add(conditions, _csv(tmp_path, "a.csv", *A), "--run-start", 459, "--last-run", 459,
                "--split", "--comment", "5 µs bursts", "--write") == 0
    text = (conditions / mupix_mask.CONTAINER).read_text(encoding="utf-8")
    assert "5 µs bursts" in text and "\\u00b5" not in text
    assert mupix_mask.dump(json.loads(text)) == text
