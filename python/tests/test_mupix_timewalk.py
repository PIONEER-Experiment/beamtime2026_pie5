"""The MuPix timewalk CLI (pioneer.conddb.mupix_timewalk), on scratch containers.

Every test writes into tmp_path only. The container is a small stand-in for
bt2026_psm_readout_map.json: a mupix_chip_map whose raw-id mapping changes
at run 200 and the mupix_timewalk table as it ships, empty on [0, open). The
fit tests build nearline-layout ROOT files from synthetic dt-vs-ToT
histograms of known walk curves (they need numpy, scipy, iminuit and uproot
and skip without them). One test also reads the real container when the
offline tree is checked out beside this repository: the shipped file, or the
one PI_TB_READOUT_MAP_FILE names. It checks invariants only, so adding
constants to the shipped file keeps it passing.
"""

import json
import math
import os
import sqlite3
from pathlib import Path

import pytest

from pioneer.conddb import cond_loader, mupix_timewalk as tw
from pioneer.conddb.mupix_timewalk import TimewalkError, main

SHIPPED = Path(os.environ.get("PI_TB_READOUT_MAP_FILE") or (
    Path(__file__).resolve().parents[3]
    / "main" / "reco_testbeam" / "conditions" / "bt2026_psm_readout_map.json"))

EARLY = {1: 10011, 2: 10012, 3: 10013, 4: 10014, 5: 10021, 6: 10022, 7: 10023, 0: 10024}
LATE = {0: 10011, 1: 10012, 2: 10013, 3: 10014, 4: 10021, 5: 10022, 6: 10023, 7: 10024}
TAG = "bt2026-timewalk"


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
        "mupix_timewalk": {
            "schema": "mupix_timewalk", "version": 1, "kind": "parameter_set",
            "tags": [{"tag": TAG, "is_default": True, "description": "timewalk"}],
            "description": "test",
            "iov": [{"row_id": 1, "tag": TAG, "run_start": 0, "run_end": None,
                     "is_active": True, "created_by": "test", "comment": "empty"}],
            "values": {},
            "values_by_iov": {"1": [{"key": "n_chips", "value": 0}]},
        },
    }


@pytest.fixture
def conditions(tmp_path):
    d = tmp_path / "conditions"
    d.mkdir()
    (d / tw.CONTAINER).write_text(json.dumps(_container(), indent=2) + "\n")
    return d


def _doc(conditions):
    return json.loads((conditions / tw.CONTAINER).read_text(encoding="utf-8"))


def _chip(p=(-71.0, 526.1, -1.31), tot_min=2, tot_max=23, **kw):
    return {"params": list(p), "errors": [1.2, 6.8, 0.05], "tot_min": tot_min, "tot_max": tot_max,
            "fit_tot_min": 4, "fit_tot_max": tot_max, "chi2_ndf": 2.19, "valid": True, **kw}


def _fit(tmp_path, chips=None, form="lin_inv", name="fit.json"):
    """A fit JSON in this tool's format."""
    if chips is None:
        chips = {"10011": _chip(), "10021": _chip((-64.1, 555.5, -0.89), 2, 26),
                 "10022": {"entries": 12, "refused": "0 good columns from ToT 4 (need 5)"}}
    path = tmp_path / name
    path.write_text(json.dumps({"tool": "pioneer.conddb.mupix_timewalk fit", "form": form,
                                "files": ["a_hists.root", "b_hists.root"], "chips": chips}))
    return path


def _add(conditions, *argv):
    return main(["--conditions", str(conditions), "add", *map(str, argv)])


def _params(table, row_id):
    return {r["key"]: r["value"] for r in table["values_by_iov"][str(row_id)]}


def _chips_at(conditions, run):
    table = _doc(conditions)["mupix_timewalk"]
    _row, payload = tw.resolve(table, "mupix_timewalk", run)
    return tw.timewalk_chips(payload)


# ---------------------------------------------------------------------------
# The curve (PIMuPixTimewalk.hh Evaluate / CheckCurve)

def test_evaluate_and_clamps():
    p = (-71.0, 526.0, -1.3)
    w = lambda x: p[0] + p[1] / x + p[2] * x  # noqa: E731
    c = tw.Chip(10011, "lin_inv", *p, 2, 23)
    assert c.w(10) == pytest.approx(w(10))
    assert c.w(0) == c.w(1) == c.w(2) == pytest.approx(w(2)), "held at W(tot_min) below it"
    assert c.w(31) == c.w(24) == pytest.approx(w(23)), "held at W(tot_max) above it"
    assert c.w(float("nan")) == pytest.approx(w(2)), "a NaN ToT is taken as tot_min"
    assert tw.w_unclamped("inverse", 1, 2, 3, 5) == pytest.approx(1 + 2 / 2)
    assert tw.w_unclamped("power", 1, 2, 0.5, 4) == pytest.approx(1 + 2 / 2)
    assert tw.w_unclamped("exp", 1, 2, 4, 4) == pytest.approx(1 + 2 * math.exp(-1))
    assert math.isinf(tw.w_unclamped("lin_inv", 1, 2, 3, 0))
    assert tw.check_curve("lin_inv", *p, 2, 23) == ""


@pytest.mark.parametrize("form, p, lo, hi, why", [
    ("inverse", (0, 100, 2.0), 2, 20, "pole p2 = 2.0 is not below tot_min 2"),
    ("power", (0, 100, 1.0), 0, 20, "power form needs tot_min >= 1"),
    ("exp", (0, 100, 0.0), 2, 20, "decay length p2 = 0.0 is not positive"),
    ("lin_inv", (0, 100, -1.0), 0, 20, "lin_inv form needs tot_min >= 1"),
    ("lin_inv", (0, float("nan"), -1.0), 2, 20, "not all finite"),
    ("lin_inv", (0, 100, -1.0), 5, 4, "tot_min 5 is above tot_max 4"),
    ("exp", (1.7e308, 1.7e308, 5.0), 2, 20, "W overflows"),
])
def test_check_curve_refuses(form, p, lo, hi, why):
    assert why in tw.check_curve(form, *p, lo, hi)


def test_choose_tot_min_is_relative_to_the_curve():
    """The sanity limit on W(tot_min) is taken against W(fit_tot_max), so a
    chip's constant offset from S1 (p0) does not move tot_min."""
    p = (-71.0, 526.1, -1.31)
    assert tw.choose_tot_min("lin_inv", p, 22) == (2, "")
    # W(2) = +389 ns: over an absolute 250 ns, but only 265 ns above W(22)
    assert tw.choose_tot_min("lin_inv", (p[0] + 200, *p[1:]), 22) == (2, "")
    # a steeper curve: W(2) - W(22) = 390 ns > 330, W(3) - W(22) = 255 ns
    t, why = tw.choose_tot_min("lin_inv", (-71.0, 800.0, -1.31), 22)
    assert t == 3 and "W(2) - W(22) = 389.8 ns > 330 ns" in why
    # nothing sane up to fit_tot_min: held there
    assert tw.choose_tot_min("lin_inv", (-71.0, 5000.0, -1.31), 22)[0] == 4
    # the inverse form keeps 0.5 clear of its pole
    t, why = tw.choose_tot_min("inverse", (-70.0, 300.0, 1.8), 22)
    assert t == 3 and "pole" in why


# ---------------------------------------------------------------------------
# add

def test_add_warns_about_fits_at_a_limit(conditions, tmp_path, capsys):
    fit = _fit(tmp_path, {"10011": _chip(at_limit=["p2"]),
                          "10021": _chip(tot_min=3, tot_min_raised="from 2 to 3: W(2) ..."),
                          "10012": _chip()})
    assert _add(conditions, fit, "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "c") == 0
    out = capsys.readouterr().out
    assert "WARNING vid 10011: parameter(s) p2 at a limit of the fit" in out
    assert "WARNING vid 10021: tot_min raised from 2 to 3" in out
    assert "WARNING vid 10012" not in out


def test_dry_run_writes_nothing(conditions, tmp_path, capsys):
    before = (conditions / tw.CONTAINER).read_text()
    assert _add(conditions, _fit(tmp_path), "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "c") == 0
    out = capsys.readouterr().out
    assert "dry run: nothing written" in out and "@@" in out
    assert "vid 10022: refused by the fit" in out, "a refused chip is named"
    assert (conditions / tw.CONTAINER).read_text() == before


def test_overlap_refused_without_split(conditions, tmp_path, capsys):
    assert _add(conditions, _fit(tmp_path), "--run-start", 459, "--last-run", 459,
                "--comment", "c", "--write") == 1
    err = capsys.readouterr().err
    assert "overlaps active interval" in err and "row 1 [0, open)" in err and "--split" in err
    assert _doc(conditions) == _container()


def test_split_carves_the_open_interval(conditions, tmp_path):
    before = _doc(conditions)
    assert _add(conditions, _fit(tmp_path), "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "walk of 459", "--created-by", "tester", "--write") == 0
    doc = _doc(conditions)
    table = doc["mupix_timewalk"]
    rows = {r["row_id"]: r for r in table["iov"]}
    assert not rows[1]["is_active"]
    assert "deactivated by mupix_timewalk: split around [459, 460)" in rows[1]["comment"]
    assert (rows[2]["run_start"], rows[2]["run_end"], rows[2]["is_active"]) == (0, 459, True)
    assert (rows[3]["run_start"], rows[3]["run_end"], rows[3]["is_active"]) == (460, None, True)
    assert (rows[4]["run_start"], rows[4]["run_end"], rows[4]["is_active"]) == (459, 460, True)
    assert rows[2]["comment"] == "Split from row 1 [0, open): empty"
    assert rows[4]["created_by"] == "tester" and rows[4]["comment"].startswith("walk of 459 Source: fit.json")
    assert "2 chip(s)" in rows[4]["comment"]
    assert table["values_by_iov"]["2"] == table["values_by_iov"]["3"] == [{"key": "n_chips", "value": 0}]
    p = _params(table, 4)
    assert p["n_chips"] == 2
    assert p["vid"] == [10011, 10021] and p["form"] == ["lin_inv", "lin_inv"]
    assert p["p0"] == [-71.0, -64.1] and p["p2"] == [-1.31, -0.89]
    assert p["tot_min"] == [2, 2] and p["tot_max"] == [23, 26]
    assert p["comment"] == ["fit ToT 4-23, chi2/ndf 2.19", "fit ToT 4-26, chi2/ndf 2.19"]
    assert list(p) == ["n_chips", "vid", "form", "p0", "p1", "p2", "tot_min", "tot_max", "comment"]
    assert doc["mupix_chip_map"] == before["mupix_chip_map"]
    assert (conditions / tw.CONTAINER).read_text() == json.dumps(doc, indent=2) + "\n"
    for run, n in ((0, 0), (458, 0), (459, 2), (460, 0), (100000, 0)):
        assert len(_chips_at(conditions, run)) == n
    assert tw.validate(doc) == []


def test_split_over_constants_needs_replace(conditions, tmp_path, capsys):
    assert _add(conditions, _fit(tmp_path), "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "a", "--write") == 0
    before = _doc(conditions)
    capsys.readouterr()
    other = _fit(tmp_path, {"10011": _chip((-70.0, 520.0, -1.2))}, name="b.json")
    assert _add(conditions, other, "--run-start", 455, "--run-end", 465, "--split",
                "--comment", "b", "--write") == 1
    err = capsys.readouterr().err
    assert "already hold timewalk constants" in err and "row 4 [459, 460) (2 chip(s))" in err
    assert "--replace" in err
    assert _doc(conditions) == before

    assert _add(conditions, other, "--run-start", 455, "--run-end", 465, "--replace",
                "--comment", "b") == 1
    assert "needs --split" in capsys.readouterr().err

    assert _add(conditions, other, "--run-start", 455, "--run-end", 465, "--split", "--replace",
                "--comment", "b", "--write") == 0
    out = capsys.readouterr().out
    assert ("row 4: --replace drops the constants of its 2 chip(s) over [459, 460); chip(s) "
            "10021 are then uncorrected there") in out
    assert [ln.split(":")[0].strip() for ln in out.splitlines() if "--replace drops" in ln] \
        == ["row 4"], "only the overlapped row with constants is reported"
    for run in (455, 459, 464):
        assert [(c.vid, c.p0) for c in _chips_at(conditions, run)] == [(10011, -70.0)]
    assert _chips_at(conditions, 454) == [] and _chips_at(conditions, 465) == []
    assert tw.validate(_doc(conditions)) == []


def test_split_over_an_empty_interval_needs_no_replace(conditions, tmp_path):
    assert _add(conditions, _fit(tmp_path), "--run-start", 300, "--split",
                "--comment", "a", "--write") == 0
    table = _doc(conditions)["mupix_timewalk"]
    active = sorted((r["run_start"], r["run_end"]) for r in table["iov"] if r["is_active"])
    assert active == [(0, 300), (300, None)]
    assert len(_chips_at(conditions, 5000)) == 2


def test_remnant_comments_stay_flat(conditions, tmp_path):
    fit = _fit(tmp_path)
    for start in (459, 500, 480, 470):
        assert _add(conditions, fit, "--run-start", start, "--last-run", start, "--split",
                    "--comment", "c", "--write") == 0
    table = _doc(conditions)["mupix_timewalk"]
    remnants = [r for r in table["iov"] if r["comment"].startswith("Split from row ")]
    assert remnants
    for r in remnants:
        assert r["comment"].count("Split from") == 1, r["comment"]
        assert r["comment"].count("deactivated by mupix_timewalk") == (0 if r["is_active"] else 1)
    assert tw.validate(_doc(conditions)) == []


def test_skip_vid_and_nothing_left(conditions, tmp_path, capsys):
    assert _add(conditions, _fit(tmp_path), "--run-start", 459, "--last-run", 459, "--split",
                "--skip-vid", 10021, "--comment", "c", "--write") == 0
    assert "vid 10021: left out (--skip-vid)" in capsys.readouterr().out
    assert [c.vid for c in _chips_at(conditions, 459)] == [10011]
    only_refused = _fit(tmp_path, {"10022": {"refused": "sparse"}}, name="r.json")
    assert _add(conditions, only_refused, "--run-start", 600, "--split", "--comment", "c") == 1
    err = capsys.readouterr().err
    assert "no chip with usable constants" in err and "vid 10022: refused" in err


def test_invalid_walk_fit_is_left_out(conditions, tmp_path, capsys):
    fit = _fit(tmp_path, {"10011": _chip(), "10012": _chip(valid=False)})
    assert _add(conditions, fit, "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "c", "--write") == 0
    assert "vid 10012: the walk fit is not valid" in capsys.readouterr().out
    assert [c.vid for c in _chips_at(conditions, 459)] == [10011]


def test_walk_fit_json_input(conditions, tmp_path):
    """psm-analysis walk_fit.py's JSON: several forms per chip, --form picks one."""
    wf = {"run": 459, "files": ["x.npz"], "chips": {
        "10011": {"fit_tot_min": 4, "fit_tot_max": 23, "forms": {
            "inv": {"params": [-120.2, 1142.9, -2.84], "tot_min": 2, "tot_max": 23,
                    "fit_tot_max": 23, "chi2_ndf": 2.5, "valid": True},
            "lin_inv": {"params": [-71.0, 526.1, -1.31], "tot_min": 2, "tot_max": 23,
                        "fit_tot_max": 23, "chi2_ndf": 2.19, "valid": True}}},
        "10012": {"refused": "few columns", "forms": {}}}}
    path = tmp_path / "walk_fit_run00459.json"
    path.write_text(json.dumps(wf))
    assert _add(conditions, path, "--run-start", 459, "--last-run", 459, "--split",
                "--form", "inv", "--comment", "c", "--write") == 0
    [c] = _chips_at(conditions, 459)
    assert (c.vid, c.form, c.p0, c.p2, c.tot_min, c.tot_max) == (10011, "inverse", -120.2, -2.84, 2, 23)
    assert _add(conditions, path, "--run-start", 500, "--split", "--comment", "c", "--write") == 0
    assert _chips_at(conditions, 500)[0].form == "lin_inv", "lin_inv is the default"


def test_form_mismatch_refused(conditions, tmp_path, capsys):
    assert _add(conditions, _fit(tmp_path), "--run-start", 459, "--split", "--form", "exp",
                "--comment", "c") == 1
    assert "holds form 'lin_inv'" in capsys.readouterr().err


def test_refused_intervals(conditions, tmp_path, capsys):
    fit = _fit(tmp_path)
    assert _add(conditions, fit, "--run-start", 10, "--run-end", 10, "--split", "--comment", "c") == 1
    assert "is empty" in capsys.readouterr().err
    assert _add(conditions, fit, "--run-start", -1, "--split", "--comment", "c") == 1
    assert "negative" in capsys.readouterr().err


def test_new_tag(conditions, tmp_path, capsys):
    fit = _fit(tmp_path)
    assert _add(conditions, fit, "--run-start", 0, "--tag", "trial", "--comment", "c") == 1
    assert "--tag-description" in capsys.readouterr().err
    assert _add(conditions, fit, "--run-start", 0, "--tag", "trial", "--tag-description", "a trial",
                "--comment", "c", "--write") == 0
    table = _doc(conditions)["mupix_timewalk"]
    assert {"tag": "trial", "is_default": False, "description": "a trial"} in table["tags"]
    _row, payload = tw.resolve(table, "mupix_timewalk", 5)
    assert tw.timewalk_chips(payload) == []
    _row, payload = tw.resolve(table, "mupix_timewalk", 5, "trial")
    assert len(tw.timewalk_chips(payload)) == 2


def test_add_refuses_a_result_the_correction_would_reject(conditions, tmp_path, capsys):
    bad = _fit(tmp_path, {"2001": _chip()}, name="bad.json")
    assert _add(conditions, bad, "--run-start", 459, "--split", "--comment", "c", "--write") == 1
    err = capsys.readouterr().err
    assert "would fail the correction" in err and "detector id 2001 is no chip" in err
    lo = _fit(tmp_path, {"10011": _chip(tot_min=0)}, name="lo.json")
    assert _add(conditions, lo, "--run-start", 459, "--split", "--comment", "c", "--write") == 1
    assert "lin_inv form needs tot_min >= 1" in capsys.readouterr().err
    assert _doc(conditions) == _container()


def test_table_out_loads_into_sqlite(conditions, tmp_path):
    out = tmp_path / "twc_only.json"
    assert _add(conditions, _fit(tmp_path), "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "c", "--write", "--table-out", out) == 0
    only = json.loads(out.read_text())
    assert list(only) == ["mupix_timewalk"]
    assert only["mupix_timewalk"] == _doc(conditions)["mupix_timewalk"]
    db = tmp_path / "c.db"
    ex = cond_loader.make_executor(sqlite=str(db))
    cond_loader.load(ex, [out])
    ex.close()
    con = sqlite3.connect(db)
    active = con.execute("SELECT row_id, run_start, run_end FROM cond_iov WHERE table_name = "
                         "'mupix_timewalk' AND is_active = 1 ORDER BY run_start").fetchall()
    assert [(a[1], a[2]) for a in active] == [(0, 459), (459, 460), (460, None)]
    row = next(a[0] for a in active if a[1] == 459)
    cells = con.execute("SELECT key, ordinal, value_type, value_int, value_real, value_text FROM "
                        "cond_values WHERE table_name = 'mupix_timewalk' AND iov_row_id = ? "
                        "ORDER BY key, ordinal", (row,)).fetchall()
    by = {}
    for key, _o, vtype, vi, vr, vt in cells:
        by.setdefault(key, []).append((vtype, {"int": vi, "real": vr, "text": vt}.get(vtype)))
    assert by["vid"] == [("int", 10011), ("int", 10021)]
    assert by["form"] == [("text", "lin_inv"), ("text", "lin_inv")]
    assert by["p1"] == [("real", 526.1), ("real", 555.5)]
    assert by["tot_max"] == [("int", 23), ("int", 26)]
    empty = next(a[0] for a in active if a[1] == 0)
    assert con.execute("SELECT key, value_int FROM cond_values WHERE table_name = "
                       "'mupix_timewalk' AND iov_row_id = ?", (empty,)).fetchall() == [("n_chips", 0)]


def test_show(conditions, tmp_path, capsys):
    _add(conditions, _fit(tmp_path), "--run-start", 459, "--last-run", 459, "--split",
         "--comment", "c", "--write")
    capsys.readouterr()
    assert main(["--conditions", str(conditions), "show", "--run", "459"]) == 0
    out = capsys.readouterr().out
    assert "run 459 reads row 4 [459, 460)" in out and "2 chip(s) with constants" in out
    assert "10021" in out and "lin_inv" in out
    assert main(["--conditions", str(conditions), "show", "--run", "5"]) == 0
    assert "the correction is a copy" in capsys.readouterr().out


def test_non_ascii_is_written_as_is(conditions, tmp_path):
    assert _add(conditions, _fit(tmp_path), "--run-start", 459, "--last-run", 459, "--split",
                "--comment", "ToT in 256 ns, 5 µs bursts", "--write") == 0
    text = (conditions / tw.CONTAINER).read_text(encoding="utf-8")
    assert "5 µs bursts" in text and "\\u00b5" not in text
    assert tw.dump(json.loads(text)) == text


def test_container_resolution(conditions, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NL_CONDITIONS_DIR", str(conditions))
    assert main(["check"]) == 0
    assert str(conditions / tw.CONTAINER) in capsys.readouterr().out
    monkeypatch.setenv("NL_CONDITIONS_DIR", str(tmp_path / "nowhere"))
    assert main(["check"]) == 1
    assert "no conditions container" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# validate() and check

def _check(conditions):
    return main(["--conditions", str(conditions), "check"])


GOOD = {"n_chips": 1, "vid": [10011], "form": ["lin_inv"], "p0": [-71.0], "p1": [526.0],
        "p2": [-1.3], "tot_min": [2], "tot_max": [23]}


def _rows(**changes):
    d = {**GOOD, **changes}
    return [{"key": k, "value": v} for k, v in d.items()]


@pytest.mark.parametrize("breakage, why", [
    ("overlap", "overlap"),
    ("gap", "leave a gap"),
    ("start", "runs [0, 10) have no interval"),
    ("closed", "not open-ended"),
    ("unknown_vid", "detector id 2001 is no chip"),
    ("two_raws", "raw chip ids [0, 7] at run 200"),
    ("duplicate", "detector id 10011 is listed more than once"),
    ("count", "each must hold exactly n_chips"),
    ("n_chips", "n_chips is -1"),
    ("comments", "the comment array holds 2 element(s) for 1 chip(s)"),
    ("form", "form is 'cubic'; the known forms are inverse, power, exp, lin_inv"),
    ("nan", "are not all finite"),
    ("string_param", "p1 is '526', not a number"),
    ("float_vid", "vid is 10011.0, not an integer"),
    ("bool_tot", "tot_min is True, not an integer"),
    ("tot_order", "tot_min 9 and tot_max 8"),
    ("tot_high", "tot_min 2 and tot_max 32"),
    ("tot_negative", "tot_min -1 and tot_max 23"),
    ("inverse_pole", "pole p2 = 2.5 is not below tot_min 2"),
    ("power_zero", "power form needs tot_min >= 1"),
    ("exp_tau", "decay length p2 = -1.0 is not positive"),
    ("lin_inv_zero", "lin_inv form needs tot_min >= 1"),
    ("overflow", "W overflows"),
])
def test_validate_catches(conditions, breakage, why, capsys):
    doc = _doc(conditions)
    t = doc["mupix_timewalk"]
    iov, byv = t["iov"], t["values_by_iov"]
    payload = {
        "unknown_vid": _rows(vid=[2001]),
        "duplicate": _rows(n_chips=2, vid=[10011, 10011], form=["lin_inv"] * 2, p0=[1.0, 1.0],
                           p1=[1.0, 1.0], p2=[1.0, 1.0], tot_min=[2, 2], tot_max=[9, 9]),
        "count": _rows(n_chips=2),
        "n_chips": _rows(n_chips=-1),
        "comments": _rows(comment=["a", "b"]),
        "form": _rows(form=["cubic"]),
        "nan": _rows(p1=[float("nan")]),
        "string_param": _rows(p1=["526"]),
        "float_vid": _rows(vid=[10011.0]),
        "bool_tot": _rows(tot_min=[True]),
        "tot_order": _rows(tot_min=[9], tot_max=[8]),
        "tot_high": _rows(tot_max=[32]),
        "tot_negative": _rows(tot_min=[-1]),
        "inverse_pole": _rows(form=["inverse"], p2=[2.5]),
        "power_zero": _rows(form=["power"], p2=[0.5], tot_min=[0]),
        "exp_tau": _rows(form=["exp"], p2=[-1.0]),
        "lin_inv_zero": _rows(tot_min=[0]),
        "overflow": _rows(form=["exp"], p0=[1.7e308], p1=[1.7e308], p2=[5.0]),
    }
    if breakage == "overlap":
        iov.append({"row_id": 2, "tag": TAG, "run_start": 5, "run_end": 6, "is_active": True})
        byv["2"] = _rows()
    elif breakage == "gap":
        iov[0]["run_end"] = 10
        iov.append({"row_id": 2, "tag": TAG, "run_start": 11, "run_end": None, "is_active": True})
        byv["2"] = _rows()
    elif breakage == "start":
        iov[0]["run_start"] = 10
    elif breakage == "closed":
        iov[0]["run_end"] = 10
    elif breakage == "two_raws":
        byv["1"] = _rows()
        cmap = doc["mupix_chip_map"]["values_by_iov"]["2"]
        next(r for r in cmap if r["channel_id"] == 7)["vid"] = 10011
    else:
        byv["1"] = payload[breakage]
    problems = tw.validate(doc)
    assert any(why in p for p in problems), problems
    (conditions / tw.CONTAINER).write_text(json.dumps(doc, indent=2) + "\n")
    assert _check(conditions) == 1
    assert why in capsys.readouterr().out


def test_check_passes_the_fixture_and_a_good_payload(conditions, capsys):
    assert _check(conditions) == 0
    doc = _doc(conditions)
    doc["mupix_timewalk"]["values_by_iov"]["1"] = _rows(comment=["x"])
    assert tw.validate(doc) == []
    for form, p2, lo in (("inverse", -2.8, 2), ("power", 0.55, 1), ("exp", 7.3, 0)):
        doc["mupix_timewalk"]["values_by_iov"]["1"] = _rows(form=[form], p2=[p2], tot_min=[lo])
        assert tw.validate(doc) == [], form
    assert "every run resolves to exactly one interval" in capsys.readouterr().out


def test_payload_rules():
    assert tw.payload([]) == [{"key": "n_chips", "value": 0}]
    chips = [tw.Chip(10021, "exp", 1, 2, 3, 0, 31), tw.Chip(10011, "lin_inv", 1, 2, 3, 1, 20)]
    rows = tw.payload(chips)
    assert {r["key"]: r["value"] for r in rows}["vid"] == [10011, 10021], "ascending VID"
    assert {r["key"]: r["value"] for r in rows}["p0"] == [1.0, 1.0], "parameters as floats"
    assert "comment" not in {r["key"] for r in rows}, "no comment array without comments"
    assert tw.timewalk_chips(rows) == sorted(chips, key=lambda c: c.vid)
    with pytest.raises(TimewalkError):
        tw.timewalk_chips([{"key": "vid", "value": [1]}])


@pytest.mark.skipif(not SHIPPED.is_file(), reason="offline tree not checked out beside this repo")
def test_real_container_invariants(tmp_path):
    """The container the nearline reads, whatever constants it holds: every run resolves to
    exactly one active interval of the default tag, every payload passes the correction's
    rules against the chip map of its runs, the tool round-trips the file byte for byte, and
    a fit still goes into a copy of it (--split --replace)."""
    text = SHIPPED.read_text(encoding="utf-8")
    doc = json.loads(text)
    table = doc["mupix_timewalk"]
    assert (table["schema"], table["version"], table["kind"]) == ("mupix_timewalk", 1, "parameter_set")
    assert tw.validate(doc) == []
    default = tw.select_tag(table, None, "mupix_timewalk")
    for r in [r for r in table["iov"] if r["tag"] == default and r.get("is_active", True)]:
        last = r["run_start"] + 100000 if r["run_end"] is None else r["run_end"] - 1
        for run in (r["run_start"], last):
            row, payload = tw.resolve(table, "mupix_timewalk", run)
            assert row["row_id"] == r["row_id"]
            tw.timewalk_chips(payload)
    assert tw.dump(doc) == text, "the tool rewrites the file with json.dumps(indent=2); it must round-trip"

    d = tmp_path / "c"
    d.mkdir()
    (d / tw.CONTAINER).write_text(text, encoding="utf-8")
    assert _add(d, _fit(tmp_path), "--run-start", 459, "--last-run", 459, "--split", "--replace",
                "--comment", "c", "--write") == 0
    after = json.loads((d / tw.CONTAINER).read_text(encoding="utf-8"))
    assert tw.validate(after) == []
    _row, payload = tw.resolve(after["mupix_timewalk"], "mupix_timewalk", 459)
    assert [c.vid for c in tw.timewalk_chips(payload)] == [10011, 10021]


# ---------------------------------------------------------------------------
# fit, on synthetic nearline-layout files

TRUE = {10011: (-71.0, 526.0, -1.31), 10012: (-56.0, 592.0, -1.49), 10021: (-64.0, 555.0, -0.89)}


def _need_fit_stack():
    for mod in ("numpy", "scipy", "iminuit", "uproot"):
        pytest.importorskip(mod)


def _spectrum(np, scale=1.0, cliff=None):
    t = np.arange(32, dtype=float)
    n = scale * 5000.0 * (t / 9.0) * np.exp(1.0 - t / 9.0)
    if cliff is not None:
        n[cliff + 1] = 0.25 * n[cliff]
        n[cliff + 2:] = 0.02 * n[cliff]
    return n


def _synth(np, rng, p, spectrum, shift=None, sigma0=10.0, bkg=0.2):
    """[dt, tot] Poisson counts: per ToT column a Gaussian at the lin_inv W(ToT) (plus
    ``shift(t)``) and a flat background, spectrum[t] pairs in all."""
    from scipy.stats import norm
    edges = np.arange(-150, 452, 2.0)
    h = np.zeros((300, 32))
    for t in range(1, 32):
        mu = p[0] + p[1] / t + p[2] * t + (shift(t) if shift else 0.0)
        sigma = sigma0 + 40.0 / t
        n = spectrum[t]
        expect = (1 - bkg) * n * np.diff(norm.cdf(edges, mu, sigma)) + bkg * n / 300
        h[:, t] = rng.poisson(expect)
    return h, edges


def _write(tmp_path, chips, edges, spectra=None, name="run00001_00000_hists.root"):
    path = tmp_path / name
    tw.write_hists(path, chips, edges, spectra)
    return path


def _run_fit(path, out, *extra):
    return main(["fit", str(path), "--out", str(out), *map(str, extra)])


def test_fit_recovers_known_parameters(tmp_path, capsys):
    _need_fit_stack()
    import numpy as np
    rng = np.random.default_rng(459)
    spec = _spectrum(np)
    parts = [{v: _synth(np, rng, p, spec / 2)[0] for v, p in TRUE.items()} for _ in range(2)]
    edges = np.arange(-150, 452, 2.0)
    files = [_write(tmp_path, part, edges, name=f"run00001_0000{i}_hists.root")
             for i, part in enumerate(parts)]
    out = tmp_path / "fit.json"
    assert main(["fit", *map(str, files), "--out", str(out), "--plots", str(tmp_path / "png")]) == 0
    fit = json.loads(out.read_text())
    assert fit["form"] == "lin_inv" and len(fit["files"]) == 2
    assert fit["spectrum"] == "dt histogram ToT projection", "no monitor histogram in these files"
    for vid, true in TRUE.items():
        c = fit["chips"][str(vid)]
        assert c["valid"] and c["fit_tots"][0] == 4 and c["fit_tot_max"] == 31
        assert (c["tot_min"], c["tot_max"]) == (2, 31)
        for got, err, want in zip(c["params"], c["errors"], true):
            assert abs(got - want) < 3 * err, (vid, c["params"], c["errors"], true)
        assert 0.3 < c["chi2_ndf"] < 2.5
        assert c["entries"] == int(sum(p[vid].sum() for p in parts)), "the files are summed"
        assert len(c["cov"]) == 3 and len(c["w_applied"]) == 32
        assert (tmp_path / "png" / f"twc_fit_{vid}.png").is_file()
    # and the fit JSON goes straight into the table
    d = tmp_path / "cond"
    d.mkdir()
    (d / tw.CONTAINER).write_text(json.dumps(_container(), indent=2) + "\n")
    assert _add(d, out, "--run-start", 1, "--last-run", 1, "--split", "--comment", "c",
                "--write") == 0
    assert [c.vid for c in _chips_at(d, 1)] == sorted(TRUE)


def test_fit_saturation_cliff(tmp_path):
    """The monitor's ToT spectrum ends in a cliff at 20: the fit stops 3 columns before
    it; the clamp tot_max is the last good column (or, with --clamp fit, 17)."""
    _need_fit_stack()
    import numpy as np
    rng = np.random.default_rng(1)
    p = TRUE[10011]
    spec = _spectrum(np, cliff=20)
    h, edges = _synth(np, rng, p, spec, shift=lambda t: -15.0 if t > 20 else 0.0)
    path = _write(tmp_path, {10011: h}, edges, spectra={10011: spec})
    out = tmp_path / "fit.json"
    assert _run_fit(path, out) == 0
    fit = json.loads(out.read_text())
    assert fit["spectrum"] == "monitor tot_vs_chip"
    c = fit["chips"]["10011"]
    assert c["fit_tot_max"] == 17 and c["fit_tot_max_reason"] == "ToT spectrum cliff at 20"
    assert c["tot_max"] >= 20, "last good column"
    for got, err, want in zip(c["params"], c["errors"], p):
        assert abs(got - want) < 3 * err
    assert _run_fit(path, out, "--clamp", "fit") == 0
    assert json.loads(out.read_text())["chips"]["10011"]["tot_max"] == 17
    # the same cliff seen through the dt histogram's own ToT projection
    assert _run_fit(path, out, "--spectrum", "pairs") == 0
    assert json.loads(out.read_text())["chips"]["10011"]["fit_tot_max"] == 17


def test_fit_step_rule(tmp_path):
    """A pile-up that pulls the peaks 25 ns below the curve from ToT 20 on ends the range."""
    _need_fit_stack()
    import numpy as np
    rng = np.random.default_rng(2)
    h, edges = _synth(np, rng, TRUE[10011], _spectrum(np), shift=lambda t: -25.0 if t >= 20 else 0.0)
    path = _write(tmp_path, {10011: h}, edges)
    out = tmp_path / "fit.json"
    assert _run_fit(path, out) == 0
    c = json.loads(out.read_text())["chips"]["10011"]
    assert c["fit_tot_max"] == 19 and c["fit_tot_max_reason"].startswith("step at 20")


def test_fit_refuses_sparse_chips(tmp_path, capsys):
    _need_fit_stack()
    import numpy as np
    rng = np.random.default_rng(3)
    edges = np.arange(-150, 452, 2.0)
    dense = _synth(np, rng, TRUE[10011], _spectrum(np))[0]
    sparse = _synth(np, rng, TRUE[10021], _spectrum(np, scale=0.01))[0]
    path = _write(tmp_path, {10011: dense, 10021: sparse}, edges)
    out = tmp_path / "fit.json"
    assert _run_fit(path, out) == 0
    fit = json.loads(out.read_text())
    assert "refused" not in fit["chips"]["10011"]
    assert "need 5" in fit["chips"]["10021"]["refused"]
    assert "10021" in capsys.readouterr().out
    assert _run_fit(path, out, "--min-columns", 40) == 1, "nothing fitted: exit 1"
    assert all("refused" in c for c in json.loads(out.read_text())["chips"].values())


def test_fit_input_errors(tmp_path, capsys):
    _need_fit_stack()
    import numpy as np
    assert _run_fit(tmp_path / "missing.root", tmp_path / "f.json") == 1
    assert "no such file" in capsys.readouterr().err
    path = _write(tmp_path, {10011: np.zeros((300, 32))}, np.arange(-150, 452, 2.0))
    assert main(["fit", str(path), "--out", str(tmp_path / "f.json"), "--folder", "Other"]) == 1
    assert "no twc_dt_vs_tot_raw_<vid> histograms" in capsys.readouterr().err
    assert main(["fit", str(path), "--out", str(tmp_path / "f.json"), "--spectrum", "monitor"]) == 1
    assert "no usable PIPSMMuPixMonitor/tot_vs_chip" in capsys.readouterr().err
    other = _write(tmp_path, {10011: np.zeros((150, 32))}, np.arange(-150, 452, 4.0), name="o.root")
    assert main(["fit", str(path), str(other), "--out", str(tmp_path / "f.json")]) == 1
    assert "cannot be summed" in capsys.readouterr().err
