#!/usr/bin/env python3
"""Fit and fill the MuPix timewalk constants (conditions table mupix_timewalk).

PIPSMMuPixTimewalkCorrection copies /Event/muquad to /Event/muquad_twc with
every pixel time moved by the walk of its chip, t -> t - W(ToT). The
constants are the run's interval of the conditions table ``mupix_timewalk``
in ``bt2026_psm_readout_map.json``, which ships empty on [0, open), so the
correction is a copy until this tool adds an interval:

    # 1. fit: the layer's own raw histograms of some processed files
    python -m pioneer.conddb.mupix_timewalk fit out/run00459_000*_hists.root \\
        --out twc_fit_run00459.json --plots twc_plots/
    # 2. add: a dry run first, then --write
    python -m pioneer.conddb.mupix_timewalk add twc_fit_run00459.json \\
        --run-start 459 --last-run 459 --split \\
        --comment "timewalk of run 459, ThHigh/ThLow 0x7a/0x79"
    python -m pioneer.conddb.mupix_timewalk show --run 459
    python -m pioneer.conddb.mupix_timewalk check

fit
---
Reads ``histograms/<folder>/twc_dt_vs_tot_raw_<vid>`` (folder
PIPSMMuPixTimewalkCorrection by default: x the pixel ToT, 32 bins over
[-0.5, 31.5); y dt = t(pixel) - t(S1) in ns, 300 bins over [-150, 450))
from every file and sums them. The layer books them only when its
CounterInput is set (the nearline job does). They are filled from the RAW
times, so a fit is valid whatever constants the job applied. Per chip
(detector id, VID), as psm-analysis ``mupix-timewalk/walk_fit.py`` does:

1. **Column fits.** Every ToT column is fitted with a Gaussian plus a flat
   background over the whole dt range (iminuit ``ExtendedBinnedNLL``; start
   values from the column's moments refined by ``scipy.optimize.curve_fit``).
   A column with fewer than MIN_PROMPT entries in the prompt window
   [-100 ns, the top of the dt range) is not fitted. A column is good when
   the fit is valid, the peak is not truncated (mu + 2 sigma inside the
   range) and its error is at most MAX_MU_ERR.
2. **Fit range.** The contiguous good columns from ``--fit-tot-min`` on,
   stopped CLIFF_MARGIN columns before a cliff in the chip's ToT spectrum
   (the MuPix ToT saturates and every larger charge piles up in the last
   columns, whose peaks then sit below the curve) and before a column whose
   peak step exceeds the previous step by more than max(STEP_SLACK_NS,
   3 sigma). A chip with fewer than ``--min-columns`` columns in the range
   is refused (listed in the JSON with the reason; ``add`` leaves it out, so
   it passes through uncorrected). The ToT spectrum is the MuPix monitor's
   ``tot_vs_chip`` when every file has it (``--spectrum auto``), else the
   ToT projection of the chip's own dt histogram (``--spectrum pairs``).
3. **Walk fit.** The walk form (``--form``, default lin_inv) is fitted to the
   column peaks, weighted by their errors, with iminuit ``LeastSquares``;
   start values from ``scipy.optimize.curve_fit``. chi2/ndf is well above 1
   (peak errors of 0.2-1 ns against an approximate curve): a comparison
   measure, not a probability.
4. **Clamps.** ``tot_max`` is the last good column (``--clamp last_good``) or
   the end of the fit range (``--clamp fit``); ``tot_min`` is the lowest ToT
   from ``--tot-min`` up at which W is finite and at most W_SANE_SPAN above
   W(fit_tot_max), the curve's value at the last fitted column (for the
   inverse form also 0.5 above its pole). The limit is relative because W
   carries the chip's constant offset from S1, which differs from chip to
   chip. A tot_min raised above ``--tot-min`` is printed as a warning.

It writes the fit JSON (per chip: the form, parameters, errors, covariance,
chi2/ndf, fit range, tot_min, tot_max, the column peaks, entries) and, with
``--plots DIR``, one PNG per chip (peaks, curve and residuals).

The walk forms (ToT in counts of 256 ns, W in ns, x = clamp(ToT, tot_min,
tot_max), corrected time t_raw - W(x)):

    inverse   W = p0 + p1 / (x - p2)       needs p2 < tot_min
    power     W = p0 + p1 * x^(-p2)        needs tot_min >= 1
    exp       W = p0 + p1 * exp(-x / p2)   needs p2 > 0
    lin_inv   W = p0 + p1 / x + p2 * x     needs tot_min >= 1

add
---
Builds the payload from a fit JSON (this tool's, or psm-analysis
``walk_fit.py``'s with ``--form``) and adds an interval, like
``mupix_mask add``: a dry run by default that prints the intervals before and
after, the chips and a unified diff of the file; ``--write`` applies it.
``--empty`` instead of a fit JSON adds an interval without constants
(n_chips 0), for runs that have no valid constants.
A new interval overlapping an active one of the same tag is refused;
``--split`` carves it out (each overlapped interval is deactivated and its
parts outside the new range come back with their own payload and the comment
"Split from row N [a, b): <its original comment>"). An overlapped interval
that already holds constants also needs ``--replace``: its constants are
dropped in the overlap (a union makes no sense, a chip has one curve).
Nothing is written that the C++ layer would reject (validate()).

``--write`` edits the git-tracked container in reco_testbeam/conditions (the
default location); commit it there. ``--table-out`` writes a container of
this table alone for the database loaders:

    python -m pioneer.conddb.mupix_timewalk add ... --write --table-out /tmp/twc.json
    python3 json2pg.py --docker testbeam-pgdb /tmp/twc.json

The container is ``--conditions PATH`` (a directory means its
bt2026_psm_readout_map.json), else $NL_CONDITIONS_DIR, else
$PIONEERSYS/reco_testbeam/conditions.

Environment
-----------
add, show and check need the standard library only. fit needs numpy, scipy,
iminuit and uproot (or PyROOT when uproot is missing), and matplotlib for
--plots; they are imported only when fit runs.
"""
from __future__ import annotations

import argparse
import difflib
import getpass
import json
import math
import re
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from pioneer.conddb.mupix_mask import (
    CONTAINER,
    MaskError,
    _end,
    _min_end,
    _range,
    carve_out,
    container_path,
    describe,
    dump,
    resolve,
    select_tag,
    validate_table,
    vid_problems,
)

TABLE = "mupix_timewalk"
SCHEMA = "mupix_timewalk"
SCHEMA_VERSION = 1
TOOL = "pioneer.conddb.mupix_timewalk"

#: the ToT a hit can carry: 0..31 (PIMuPixTimewalk::kTotMax)
TOT_MAX = 31
N_TOT = TOT_MAX + 1

# fit defaults, as psm-analysis mupix-timewalk/walk_fit.py and make_plots.py
FIT_TOT_MIN = 4
TOT_MIN = 2
#: prompt-window entries a column needs to be fitted
MIN_PROMPT = 40
#: the prompt window starts here [ns] and runs to the top of the dt range
PROMPT_LO = -100.0
#: a good column has a peak error at most this [ns]
MAX_MU_ERR = 5.0
#: a chip needs at least this many columns in its fit range
MIN_COLUMNS = 5
#: saturation cliff of the ToT spectrum (tot_cliff) and the columns kept clear of it
CLIFF_1, CLIFF_2, CLIFF_MARGIN = 0.4, 0.1, 3
#: a peak step this much [ns] smaller than the previous one ends the fit range
STEP_SLACK_NS = 6.0
#: W(tot_min) - W(fit_tot_max) above this [ns] is not sane: tot_min is raised
#: until it is. Relative to the curve, because W includes the chip's offset
#: from S1 (p0 differs by tens of ns between chips). 330 ns sits above the
#: measured W(2) - W(fit_tot_max) of every chip (208-298 ns on the runs
#: fitted so far), so tot_min stays 2 there, and far below W(1) - W(fit_tot_max)
#: (430-610 ns), where the lin_inv curve blows up.
W_SANE_SPAN = 330.0

DEFAULT_FOLDER = "PIPSMMuPixTimewalkCorrection"
HIST_PREFIX = "twc_dt_vs_tot_raw_"
SPECTRUM_HIST = "PIPSMMuPixMonitor/tot_vs_chip"


class TimewalkError(MaskError):
    """Anything that stops a fit or a table change, with the reason. A
    MaskError, because the container helpers shared with mupix_mask raise that."""


# ---------------------------------------------------------------------------
# The walk forms (PIMuPixTimewalk.hh)
# ---------------------------------------------------------------------------

#: table name -> the curve's text, the name psm-analysis walk_fit.py uses for it
FORMS = {
    "inverse": {"text": "p0 + p1 / (ToT - p2)", "walk_fit": "inv"},
    "power": {"text": "p0 + p1 * ToT^(-p2)", "walk_fit": "pow"},
    "exp": {"text": "p0 + p1 * exp(-ToT / p2)", "walk_fit": "exp"},
    "lin_inv": {"text": "p0 + p1 / ToT + p2 * ToT", "walk_fit": "lin_inv"},
}
WALK_FIT_NAMES = {v["walk_fit"]: k for k, v in FORMS.items()}
PARAM_NAMES = ("p0", "p1", "p2")


def w_unclamped(form: str, p0: float, p1: float, p2: float, tot: float) -> float:
    """W at ``tot`` without a clamp (ns); inf or nan where the form has no
    value, as EvaluateUnclamped() in C++ gives."""
    try:
        if form == "inverse":
            return p0 + p1 / (tot - p2)
        if form == "power":
            return p0 + p1 * math.pow(tot, -p2)
        if form == "exp":
            return p0 + p1 * math.exp(-tot / p2)
        if form == "lin_inv":
            return p0 + p1 / tot + p2 * tot
    except ZeroDivisionError:
        return math.inf
    except (OverflowError, ValueError):
        return math.nan
    return math.nan


def w_clamped(form: str, p0: float, p1: float, p2: float, tot: float,
              tot_min: float, tot_max: float) -> float:
    """W at ``tot`` held inside [tot_min, tot_max] (Evaluate() in C++)."""
    t = tot_min if math.isnan(tot) else min(max(tot, tot_min), tot_max)
    return w_unclamped(form, p0, p1, p2, t)


def _finite(v) -> bool:
    try:
        return math.isfinite(v)
    except (TypeError, OverflowError):
        return False


def check_curve(form: str, p0, p1, p2, tot_min, tot_max) -> str:
    """Why W is not finite everywhere on [tot_min, tot_max]; '' when it is
    (CheckCurve() in C++, plus lin_inv, which needs tot_min >= 1)."""
    if not (_finite(p0) and _finite(p1) and _finite(p2)):
        return f"the parameters ({p0}, {p1}, {p2}) are not all finite"
    if not tot_min <= tot_max:
        return f"tot_min {tot_min} is above tot_max {tot_max}"
    if form == "inverse" and not p2 < tot_min:
        return (f"the inverse form's pole p2 = {p2} is not below tot_min {tot_min}: "
                f"W = p0 + p1 / (ToT - p2) must be finite on [tot_min, tot_max]")
    if form == "power" and not tot_min >= 1:
        return f"the power form needs tot_min >= 1 (ToT^(-p2) at ToT 0), tot_min is {tot_min}"
    if form == "exp" and not p2 > 0:
        return f"the exp form's decay length p2 = {p2} is not positive"
    if form == "lin_inv" and not tot_min >= 1:
        return f"the lin_inv form needs tot_min >= 1 (p1 / ToT at ToT 0), tot_min is {tot_min}"
    if not (_finite(w_unclamped(form, p0, p1, p2, tot_min))
            and _finite(w_unclamped(form, p0, p1, p2, tot_max))):
        return f"W overflows on [{tot_min}, {tot_max}]"
    for t in range(math.ceil(tot_min), int(tot_max) + 1):
        if not _finite(w_unclamped(form, p0, p1, p2, t)):
            return f"W is not finite at ToT {t}"
    return ""


# ---------------------------------------------------------------------------
# The payload (PIMuPixTimewalk::CheckPayload)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Chip:
    vid: int
    form: str
    p0: float
    p1: float
    p2: float
    tot_min: int
    tot_max: int
    comment: str = ""

    def w(self, tot: float) -> float:
        return w_clamped(self.form, self.p0, self.p1, self.p2, tot, self.tot_min, self.tot_max)


ARRAYS = ("vid", "form", "p0", "p1", "p2", "tot_min", "tot_max")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def timewalk_chips(rows: list) -> list[Chip]:
    """The chips of a mupix_timewalk payload, every C++ payload rule checked
    (CheckPayload(): array lengths, types, a known form, finite parameters,
    0 <= tot_min <= tot_max <= 31, a curve finite on [tot_min, tot_max]).
    Raises TimewalkError with every problem found."""
    params = {r["key"]: r.get("value") for r in rows}
    n = params.get("n_chips")
    if not _is_int(n) or n < 0:
        raise TimewalkError(f"n_chips is {n!r}; it must be an integer 0 or more")
    arrays = {k: params.get(k, []) for k in ARRAYS}
    comments = params.get("comment", [])
    if any(not isinstance(a, list) for a in arrays.values()) or not isinstance(comments, list):
        raise TimewalkError("the arrays must be JSON lists")
    if any(len(a) != n for a in arrays.values()):
        raise TimewalkError(
            f"n_chips is {n} but the arrays hold "
            + ", ".join(f"{len(arrays[k])} {k}" for k in ARRAYS)
            + " element(s); each must hold exactly n_chips")
    if len(comments) not in (0, n):
        raise TimewalkError(f"the comment array holds {len(comments)} element(s) for {n} chip(s); "
                            f"leave it out or give one per chip")
    problems: list[str] = []
    chips: list[Chip] = []
    for i in range(n):
        vid, form = arrays["vid"][i], arrays["form"][i]
        where = f"chip {i} (vid {vid})"
        bad = [f"{k} is {arrays[k][i]!r}, not an integer" for k in ("vid", "tot_min", "tot_max")
               if not _is_int(arrays[k][i])]
        bad += [f"{k} is {arrays[k][i]!r}, not a number" for k in PARAM_NAMES
                if not _is_number(arrays[k][i])]
        if comments and not isinstance(comments[i], str):
            bad.append(f"comment is {comments[i]!r}, not a string")
        if not isinstance(form, str) or form not in FORMS:
            bad.append(f"form is {form!r}; the known forms are {', '.join(FORMS)}")
        if bad:
            problems += [f"{where}: {b}" for b in bad]
            continue
        c = Chip(vid, form, *(arrays[k][i] for k in PARAM_NAMES), arrays["tot_min"][i],
                 arrays["tot_max"][i], comments[i] if comments else "")
        if c.tot_min < 0 or c.tot_max > TOT_MAX or c.tot_min > c.tot_max:
            problems.append(f"{where}: tot_min {c.tot_min} and tot_max {c.tot_max}; they must "
                            f"satisfy 0 <= tot_min <= tot_max <= {TOT_MAX}")
            continue
        why = check_curve(c.form, c.p0, c.p1, c.p2, c.tot_min, c.tot_max)
        if why:
            problems.append(f"{where}, form {c.form}: {why}")
            continue
        chips.append(c)
    if problems:
        raise TimewalkError("; ".join(problems))
    return chips


def payload(chips: list[Chip]) -> list[dict]:
    """A mupix_timewalk payload, chips in ascending VID. No arrays for an
    empty table: an empty array has no cells in the database, and the two
    backends would hash differently."""
    chips = sorted(chips, key=lambda c: c.vid)
    rows: list[dict] = [{"key": "n_chips", "value": len(chips)}]
    if chips:
        rows += [{"key": "vid", "value": [c.vid for c in chips]},
                 {"key": "form", "value": [c.form for c in chips]},
                 {"key": "p0", "value": [float(c.p0) for c in chips]},
                 {"key": "p1", "value": [float(c.p1) for c in chips]},
                 {"key": "p2", "value": [float(c.p2) for c in chips]},
                 {"key": "tot_min", "value": [c.tot_min for c in chips]},
                 {"key": "tot_max", "value": [c.tot_max for c in chips]}]
        if any(c.comment for c in chips):
            rows.append({"key": "comment", "value": [c.comment for c in chips]})
    return rows


def _check_timewalk_row(doc: dict, row: dict, rows: list, where: str, runs: list[int]) -> list[str]:
    """ResolveTimewalk() on one payload, at each of ``runs``."""
    try:
        chips = timewalk_chips(rows)
    except TimewalkError as exc:
        return [f"{where}: {exc}"]
    problems: list[str] = []
    seen: set[int] = set()
    for c in chips:
        if c.vid in seen:
            problems.append(f"{where}: detector id {c.vid} is listed more than once")
        seen.add(c.vid)
    return problems + vid_problems(doc, [c.vid for c in chips], where, runs)


def validate(doc: dict) -> list[str]:
    """Every rule the correction applies to the table, over every run; [] if it holds.

    The default tag's active intervals tile [0, open); any other tag's do not
    overlap. Every active payload passes the C++ rules (ResolveTimewalk()):
    the payload rules of timewalk_chips(), no VID twice, and every VID a chip
    of mupix_chip_map that exactly one raw chip id maps to, at every run the
    interval covers.
    """
    return validate_table(doc, TABLE, _check_timewalk_row)


# ---------------------------------------------------------------------------
# The change
# ---------------------------------------------------------------------------

def plan_add(table: dict, chips: list[Chip], run_start: int, run_end: int | None, tag: str,
             comment: str, created_by: str, split: bool, tag_description: str | None,
             replace: bool = False) -> tuple[dict, list[str]]:
    """The table with the new interval added, and a list of what changed.

    An overlapped interval that holds constants (n_chips > 0) inside the new
    range is refused unless ``replace``, which drops them there. An
    overlapped EMPTY interval needs only ``split``.
    """
    if run_start < 0:
        raise TimewalkError(f"run_start {run_start} is negative")
    if run_end is not None and run_end <= run_start:
        raise TimewalkError(f"the interval {_range(run_start, run_end)} is empty; run_end is "
                            f"EXCLUSIVE (to cover run N alone: --run-start N --last-run N)")
    if replace and not split:
        raise TimewalkError("--replace says what --split does with overlapped constants; "
                            "it needs --split")
    if table.get("schema") != SCHEMA or table.get("version") != SCHEMA_VERSION \
            or table.get("kind") != "parameter_set":
        raise TimewalkError(f"{TABLE} is not schema {SCHEMA} v{SCHEMA_VERSION}, parameter_set")
    new = json.loads(json.dumps(table))
    notes: list[str] = []

    if not any(t["tag"] == tag for t in new.get("tags", [])):
        if not tag_description:
            raise TimewalkError(f"{TABLE} has no tag '{tag}'; give --tag-description to create "
                                f"it (not as the default tag)")
        new.setdefault("tags", []).append(
            {"tag": tag, "is_default": False, "description": tag_description})
        notes.append(f"new tag '{tag}' (not default): {tag_description}")
    if tag in new.get("values", {}):
        raise TimewalkError(f"tag '{tag}' has a tag-wide payload ('values'); this tool writes "
                            f"per-interval payloads ('values_by_iov') and a tag holds one kind only")

    by_iov = new.setdefault("values_by_iov", {})
    next_id = max((int(r["row_id"]) for r in new["iov"]), default=0) + 1
    overlapping = sorted((r for r in new["iov"]
                          if r["tag"] == tag and r.get("is_active", True)
                          and r.get("run_start", 0) < _end(run_end)
                          and run_start < _end(r.get("run_end"))),
                         key=lambda r: r.get("run_start", 0))
    if overlapping and not split:
        raise TimewalkError(
            f"{_range(run_start, run_end)} overlaps active interval(s) of tag '{tag}': "
            + ", ".join(f"row {r['row_id']} {_range(r.get('run_start', 0), r.get('run_end'))}"
                        for r in overlapping)
            + ". Close or split them first: --split deactivates them and adds back their "
              "parts outside the new range with their own payload.")

    old_chips: dict[int, list[Chip]] = {}
    for old in overlapping:
        old_payload = by_iov.get(str(old["row_id"]))
        if old_payload is None:
            raise TimewalkError(f"row {old['row_id']} has no per-interval payload to carry over")
        try:
            old_chips[old["row_id"]] = timewalk_chips(old_payload)
        except TimewalkError as exc:
            raise TimewalkError(f"row {old['row_id']}: {exc}") from None
    held = [r for r in overlapping if old_chips[r["row_id"]]]
    if held and not replace:
        raise TimewalkError(
            f"{_range(run_start, run_end)} overlaps interval(s) that already hold timewalk "
            f"constants: "
            + ", ".join(f"row {r['row_id']} {_range(r.get('run_start', 0), r.get('run_end'))} "
                        f"({len(old_chips[r['row_id']])} chip(s))" for r in held)
            + ". --replace drops them in the overlap (the new constants alone hold there; a "
              "union makes no sense, a chip has one curve).")

    new_vids = {c.vid for c in chips}

    def replace_note(old: dict) -> list[str]:
        was = old_chips[old["row_id"]]
        if not was:
            return []
        ov = _range(max(run_start, old.get("run_start", 0)), _min_end(run_end, old.get("run_end")))
        lost = sorted(c.vid for c in was if c.vid not in new_vids)
        return [f"row {old['row_id']}: --replace drops the constants of its {len(was)} chip(s) "
                f"over {ov}" + (f"; chip(s) {', '.join(map(str, lost))} are then uncorrected "
                                f"there (the new constants do not list them)" if lost else "")]

    added, next_id = carve_out(
        overlapping, by_iov, run_start, run_end, tag, created_by, next_id, notes, "mupix_timewalk",
        lambda old: f"{len(old_chips[old['row_id']])} chip(s)", replace_note)
    added.append({"row_id": next_id, "tag": tag, "run_start": run_start, "run_end": run_end,
                  "is_active": True, "created_by": created_by, "comment": comment})
    by_iov[str(next_id)] = payload(chips)
    notes.append(f"row {next_id} {_range(run_start, run_end)} added: {len(chips)} chip(s)")
    new["iov"] += sorted(added, key=lambda r: r["row_id"])
    return new, notes


def chips_from_fit(fit: dict, form: str | None = None,
                   skip: set[int] | frozenset = frozenset(),
                   warnings_out: list[str] | None = None) -> tuple[list[Chip], list[str], str]:
    """(chips, notes on the chips left out, description) from a fit JSON.

    This tool's fit JSON carries one form. A psm-analysis walk_fit.py JSON
    carries several per chip; ``form`` picks one (default lin_inv; either
    naming works). Refused chips, chips whose fit is not valid and ``skip``
    are left out, with a note each. A chip that is kept although its fit has
    a parameter at a limit (the fit's ``at_limit``), or whose tot_min the fit
    raised, gets a line in ``warnings_out`` when it is given.
    """
    if not isinstance(fit.get("chips"), dict):
        raise TimewalkError("the fit JSON has no 'chips' object")
    chips: list[Chip] = []
    notes: list[str] = []
    walk_fit = any("forms" in c for c in fit["chips"].values())
    if walk_fit:
        name = form or "lin_inv"
        table_form = WALK_FIT_NAMES.get(name, name)
        if table_form not in FORMS:
            raise TimewalkError(f"--form {name}: the known forms are {', '.join(FORMS)}")
        key = FORMS[table_form]["walk_fit"]
    elif form and fit.get("form") != WALK_FIT_NAMES.get(form, form):
        raise TimewalkError(f"--form {form}, but the fit JSON holds form {fit.get('form')!r}")
    for vid_s, c in sorted(fit["chips"].items(), key=lambda kv: int(kv[0])):
        vid = int(vid_s)
        if vid in skip:
            notes.append(f"vid {vid}: left out (--skip-vid)")
            continue
        if "refused" in c:
            notes.append(f"vid {vid}: refused by the fit ({c['refused']}); it stays uncorrected")
            continue
        if walk_fit:
            if key not in c.get("forms", {}):
                notes.append(f"vid {vid}: no {key} fit in the JSON; it stays uncorrected")
                continue
            f, fform = c["forms"][key], table_form
            ftmin, ftmax = f.get("fit_tot_min", c.get("fit_tot_min")), f.get("fit_tot_max")
        else:
            f, fform = c, fit.get("form")
            ftmin, ftmax = c.get("fit_tot_min"), c.get("fit_tot_max")
        if fform not in FORMS:
            raise TimewalkError(f"vid {vid}: form {fform!r} is none of {', '.join(FORMS)}")
        if not f.get("valid", True):
            notes.append(f"vid {vid}: the walk fit is not valid (iminuit); it stays uncorrected")
            continue
        p = [float(v) for v in f["params"]]
        if warnings_out is not None:
            if f.get("at_limit"):
                warnings_out.append(f"vid {vid}: parameter(s) {', '.join(f['at_limit'])} at a "
                                    f"limit of the fit; check its curve before writing it")
            if f.get("tot_min_raised", c.get("tot_min_raised")):
                warnings_out.append(f"vid {vid}: tot_min raised "
                                    f"{f.get('tot_min_raised', c.get('tot_min_raised'))}")
        chi2 = f.get("chi2_ndf")
        text = f"fit ToT {ftmin}-{ftmax}" + (f", chi2/ndf {chi2:.2f}" if chi2 is not None else "")
        chips.append(Chip(vid, fform, *p, int(f["tot_min"]), int(f["tot_max"]), text))
    if walk_fit:
        desc = f"walk_fit.py JSON, form {table_form} ({key})"
    else:
        desc = f"{TOOL} fit JSON, form {fit.get('form')}"
    return chips, notes, desc


def chip_table(chips: list[Chip]) -> str:
    lines = [f"{'vid':>6} {'form':<8} {'p0':>10} {'p1':>10} {'p2':>9} {'tot_min':>7} {'tot_max':>7} "
             f"{'W(0)':>7} {'W(4)':>7} {'W(10)':>7} {'W(20)':>7} {'W(31)':>7}  comment"]
    for c in sorted(chips, key=lambda c: c.vid):
        ws = " ".join(f"{c.w(t):7.1f}" for t in (0, 4, 10, 20, 31))
        lines.append(f"{c.vid:>6} {c.form:<8} {c.p0:10.3f} {c.p1:10.3f} {c.p2:9.4f} "
                     f"{c.tot_min:>7} {c.tot_max:>7} {ws}  {c.comment}")
    lines.append("W in ns, clamped to [tot_min, tot_max]; the corrected time is t_raw - W(ToT)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reading the histograms
# ---------------------------------------------------------------------------

@dataclass
class Hists:
    """The summed layer histograms of some files."""
    dt_edges: object                       # numpy array, 301 edges by default
    chips: dict = field(default_factory=dict)     # vid -> [dt, tot] counts
    spectra: dict | None = None            # vid -> ToT spectrum (monitor), or None
    files: list = field(default_factory=list)


_VID_RE = re.compile(r"^" + re.escape(HIST_PREFIX) + r"(\d+)$")
_INDEX_RE = re.compile(r"(\d+)=\S+ (\d+)")


def _np():
    try:
        import numpy as np
    except ImportError:
        raise TimewalkError("fit needs numpy, scipy and iminuit (the psm conda env "
                            "beamtune-psm has them)") from None
    return np


def _chip_index_vids(title: str) -> dict[int, int]:
    """chip index -> VID from the monitor's axis title "chip index (0=L1 10011, ...)"."""
    return {int(i): int(v) for i, v in _INDEX_RE.findall(title or "")}


class _UprootFile:
    def __init__(self, path):
        import uproot
        self.f = uproot.open(path)

    def dir_keys(self, folder):
        for base in (f"histograms/{folder}", folder):
            try:
                d = self.f[base]
            except KeyError:
                continue
            if hasattr(d, "keys"):
                return base, d.keys(cycle=False, recursive=False)
        return None, []

    def th2(self, path):
        """(values[x, y], x edges, y edges, y axis title), or None."""
        try:
            h = self.f[path]
        except KeyError:
            return None
        return (h.values(flow=False), h.axis(0).edges(), h.axis(1).edges(),
                h.axis(1).member("fTitle"))


class _RootFile:
    def __init__(self, path):
        import ROOT
        self.ROOT = ROOT
        self.f = ROOT.TFile.Open(str(path))
        if not self.f or self.f.IsZombie():
            raise TimewalkError(f"cannot open {path}")

    def dir_keys(self, folder):
        for base in (f"histograms/{folder}", folder):
            d = self.f.Get(base)
            if d and d.InheritsFrom("TDirectory"):
                return base, [k.GetName() for k in d.GetListOfKeys()]
        return None, []

    def th2(self, path):
        np = _np()
        h = self.f.Get(path)
        if not h or not h.InheritsFrom("TH2"):
            return None
        nx, ny = h.GetNbinsX(), h.GetNbinsY()
        v = np.array([[h.GetBinContent(ix, iy) for iy in range(1, ny + 1)]
                      for ix in range(1, nx + 1)], dtype=float)
        ex = np.array([h.GetXaxis().GetBinLowEdge(i) for i in range(1, nx + 2)])
        ey = np.array([h.GetYaxis().GetBinLowEdge(i) for i in range(1, ny + 2)])
        return v, ex, ey, h.GetYaxis().GetTitle()


def _open(path):
    try:
        import uproot  # noqa: F401
        return _UprootFile(path)
    except ImportError:
        pass
    try:
        import ROOT  # noqa: F401
    except ImportError:
        raise TimewalkError("fit reads ROOT files with uproot (pip install uproot; the conda "
                            "env beamtune-psm has it) or PyROOT, and neither can be imported") \
            from None
    return _RootFile(path)


def read_hists(paths, folder: str = DEFAULT_FOLDER, spectrum: str = "auto") -> Hists:
    """Sum ``<folder>/twc_dt_vs_tot_raw_<vid>`` over ``paths``; with ``spectrum``
    'auto' or 'monitor' also the monitor's per-chip ToT spectrum."""
    np = _np()
    out = Hists(dt_edges=None, files=[str(p) for p in paths])
    spectra: dict[int, object] = {}
    have_spectrum = True
    tot_edges = np.arange(N_TOT + 1) - 0.5
    for path in paths:
        f = _open(path)
        base, keys = f.dir_keys(folder)
        vids = sorted(int(m.group(1)) for m in map(_VID_RE.match, keys) if m)
        if not vids:
            raise TimewalkError(
                f"{path}: no {HIST_PREFIX}<vid> histograms in "
                f"{'histograms/' + folder if base is None else base}; the layer books them only "
                f"when its CounterInput is set (the nearline job sets it)")
        for vid in vids:
            v, ex, ey, _t = f.th2(f"{base}/{HIST_PREFIX}{vid}")
            if len(ex) != N_TOT + 1 or not np.allclose(ex, tot_edges):
                raise TimewalkError(f"{path}: {HIST_PREFIX}{vid} has a ToT axis of {len(ex) - 1} "
                                    f"bins over [{ex[0]}, {ex[-1]}); expected 32 over [-0.5, 31.5)")
            if out.dt_edges is None:
                out.dt_edges = np.asarray(ey, dtype=float)
            elif len(ey) != len(out.dt_edges) or not np.allclose(ey, out.dt_edges):
                raise TimewalkError(f"{path}: {HIST_PREFIX}{vid} has another dt axis than the "
                                    f"first histogram; the files cannot be summed")
            h = np.asarray(v, dtype=float).T                  # [dt, tot]
            out.chips[vid] = out.chips[vid] + h if vid in out.chips else h
        if spectrum == "pairs" or not have_spectrum:
            continue
        s = f.th2(f"histograms/{SPECTRUM_HIST}") or f.th2(SPECTRUM_HIST)
        idx = _chip_index_vids(s[3]) if s is not None else {}
        if s is None or not set(vids) <= set(idx.values()):
            if spectrum == "monitor":
                raise TimewalkError(f"{path}: no usable {SPECTRUM_HIST} (with the chip index in "
                                    f"its y-axis title) for the ToT spectrum; use --spectrum pairs")
            have_spectrum = False
            continue
        v = np.asarray(s[0], dtype=float)                     # [tot, chip index]
        for i, vid in idx.items():
            col = np.zeros(N_TOT)
            n = min(N_TOT, v.shape[0])
            col[:n] = v[:n, i]
            spectra[vid] = spectra[vid] + col if vid in spectra else col
    if have_spectrum and spectrum != "pairs":
        out.spectra = spectra
    return out


def write_hists(path, chips: dict, dt_edges, spectra: dict | None = None,
                folder: str = DEFAULT_FOLDER) -> None:
    """A file in the layout fit reads (uproot only): ``histograms/<folder>/
    twc_dt_vs_tot_raw_<vid>`` from ``chips`` (vid -> [dt, tot] counts) and,
    with ``spectra`` (vid -> 32 ToT counts), the monitor's tot_vs_chip. For
    tests and cross-checks."""
    import uproot
    from uproot.writing.identify import to_TAxis, to_TH2x
    np = _np()
    dt_edges = np.asarray(dt_edges, dtype=float)

    def th2(name, title, counts_xy, xedges, yedges, xtitle, ytitle):
        nx, ny = counts_xy.shape
        full = np.zeros((ny + 2, nx + 2))
        full[1:-1, 1:-1] = counts_xy.T
        n = float(counts_xy.sum())
        xa = to_TAxis("xaxis", xtitle, nx, float(xedges[0]), float(xedges[-1]))
        ya = to_TAxis("yaxis", ytitle, ny, float(yedges[0]), float(yedges[-1]))
        return to_TH2x(name, title, full.ravel().astype(">f4"), n, n, n, 0, 0, 0, 0, 0,
                       np.zeros(0, ">f8"), xa, ya)

    tot_edges = np.arange(N_TOT + 1) - 0.5
    with uproot.recreate(path) as f:
        for vid, h in sorted(chips.items()):
            f[f"histograms/{folder}/{HIST_PREFIX}{vid}"] = th2(
                f"{HIST_PREFIX}{vid}", f"{vid} pixel vs S1, raw times",
                np.asarray(h, dtype=float).T, tot_edges, dt_edges,
                f"{vid} pixel ToT [256 ns]", f"t({vid} pixel) - t(S1) (ns)")
        if spectra:
            vids = sorted(spectra)
            label = ", ".join(f"{i}=L{(v // 10) % 10} {v}" for i, v in enumerate(vids))
            counts = np.array([np.asarray(spectra[v], dtype=float) for v in vids]).T
            f[f"histograms/{SPECTRUM_HIST}"] = th2(
                "tot_vs_chip", "time over threshold per chip", counts, tot_edges,
                np.arange(len(vids) + 1) - 0.5, "ToT [256 ns]", f"chip index ({label})")


# ---------------------------------------------------------------------------
# The fit (a port of psm-analysis mupix-timewalk walk_fit.py / make_plots.py)
# ---------------------------------------------------------------------------

def _form_f(form):
    np = _np()
    if form == "inverse":
        return lambda x, p0, p1, p2: p0 + p1 / (x - p2)
    if form == "power":
        return lambda x, p0, p1, p2: p0 + p1 * np.power(x, -p2)
    if form == "exp":
        return lambda x, p0, p1, p2: p0 + p1 * np.exp(-x / p2)
    if form == "lin_inv":
        return lambda x, p0, p1, p2: p0 + p1 / x + p2 * x
    raise TimewalkError(f"form {form!r}: the known forms are {', '.join(FORMS)}")


def evaluate(form, params, tot, tot_min, tot_max):
    """W [ns] at ``tot`` (scalar or array), clamped to [tot_min, tot_max]."""
    np = _np()
    x = np.clip(np.asarray(tot, dtype=float), tot_min, tot_max)
    return _form_f(form)(x, *params)


def fit_column(h, edges, min_prompt: int = MIN_PROMPT, prompt_lo: float = PROMPT_LO) -> dict:
    """Gaussian + flat fit of one dt column (counts per bin of ``edges``)."""
    np = _np()
    from iminuit import Minuit
    from iminuit.cost import ExtendedBinnedNLL
    from scipy.optimize import curve_fit
    from scipy.stats import norm

    edges = np.asarray(edges, dtype=float)
    lo, hi = float(edges[0]), float(edges[-1])
    w = float(edges[1] - edges[0])

    def cdf(x, ns, mu, sigma, nb):
        return ns * norm.cdf(x, mu, sigma) + nb * (x - lo) / (hi - lo)

    def pdf_counts(x, ns, mu, sigma, nb):
        return ns * w * norm.pdf(x, mu, sigma) + nb * w / (hi - lo)

    h = np.asarray(h, dtype=float)
    x = 0.5 * (edges[1:] + edges[:-1])
    prompt = (x >= prompt_lo) & (x < hi)
    n_prompt = int(h[prompt].sum())
    res = {"n": int(h.sum()), "n_prompt": n_prompt}
    hp = h[prompt]
    if n_prompt:
        cs = np.cumsum(hp)
        res["median_prompt"] = float(x[prompt][np.searchsorted(cs, 0.5 * cs[-1])])
        res["mode"] = float(x[prompt][np.argmax(np.convolve(hp, np.ones(3), "same"))])
    if n_prompt < min_prompt:
        res["fitted"] = False
        return res
    # start values: moments in the prompt window, background from outside it
    mu0 = float((x[prompt] * hp).sum() / n_prompt)
    sd0 = float(np.sqrt(max(((x[prompt] - mu0) ** 2 * hp).sum() / n_prompt, 4.0)))
    out_w = (~prompt).sum()
    nb0 = float(h[~prompt].sum()) / out_w * h.size if out_w else 0.0
    ns0 = max(float(h.sum()) - nb0, 1.0)
    moments = [ns0, res["mode"], min(sd0, 60.0), max(nb0, 0.1)]
    p0 = moments
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            p0, _ = curve_fit(pdf_counts, x, h, p0=p0, sigma=np.sqrt(np.maximum(h, 1)),
                              bounds=([0, lo, 1.0, 0], [np.inf, hi, 200.0, np.inf]))
    except (RuntimeError, ValueError):
        pass

    def minimize(start):
        m = Minuit(ExtendedBinnedNLL(h, edges, cdf), ns=start[0], mu=start[1], sigma=start[2],
                   nb=max(start[3], 0.1))
        m.limits["ns"] = (0, None)
        m.limits["nb"] = (0, None)
        m.limits["mu"] = (lo, hi)
        m.limits["sigma"] = (1.0, 200.0)
        m.migrad()
        m.hesse()
        return m

    m = minimize(p0)
    if not m.valid:
        # Not in the prototype: curve_fit can walk off to the edge of the range
        # when the peak sits at the bottom of the prompt window; start again
        # from the moments. Only a column the prototype would call not good
        # (an invalid fit) can change.
        retry = minimize(moments)
        if retry.valid:
            m = retry
    res.update(fitted=True, valid=bool(m.valid),
               mu=float(m.values["mu"]), mu_err=float(m.errors["mu"]),
               sigma=float(m.values["sigma"]), sigma_err=float(m.errors["sigma"]),
               n_sig=float(m.values["ns"]), n_sig_err=float(m.errors["ns"]),
               n_bkg=float(m.values["nb"]), n_bkg_err=float(m.errors["nb"]))
    return res


def column_fits(h2, edges, min_prompt: int = MIN_PROMPT) -> list[dict]:
    """fit_column on every ToT column of ``h2[dt, tot]``, with ``good`` flags."""
    hi = float(edges[-1])
    cols = []
    for t in range(h2.shape[1]):
        r = fit_column(h2[:, t], edges, min_prompt)
        r["tot"] = t
        r["good"] = False
        if r.get("fitted"):
            r["truncated"] = bool(r["mu"] + 2 * r["sigma"] > hi)
            r["good"] = bool(r["valid"] and not r["truncated"] and 0 < r["mu_err"] <= MAX_MU_ERR)
        cols.append(r)
    return cols


def tot_cliff(spectrum, fit_tot_min: int = FIT_TOT_MIN):
    """First ToT >= fit_tot_min where the ToT spectrum ends in a cliff
    (n(t+1) < CLIFF_1 n(t) and n(t+2) < CLIFF_2 n(t)), or None."""
    np = _np()
    n = np.asarray(spectrum, dtype=float)
    for t in range(int(fit_tot_min), n.size - 2):
        if n[t] > 0 and n[t + 1] < CLIFF_1 * n[t] and n[t + 2] < CLIFF_2 * n[t]:
            return t
    return None


def fit_range(cols, fit_tot_min: int = FIT_TOT_MIN, spectrum=None,
              pileup_rules: bool = True) -> tuple[list[int], str]:
    """(ToTs of the fit, why it ends there): the contiguous good columns from
    ``fit_tot_min`` on, stopped CLIFF_MARGIN columns before a cliff of
    ``spectrum`` and before the first column whose peak step is smaller than
    the previous step by more than max(STEP_SLACK_NS, 3 sigma) (the walk
    curve is convex, so that marks a pile-up). ``pileup_rules`` False applies
    neither rule."""
    np = _np()
    out, why = [], "last good column"
    cliff = tot_cliff(spectrum, fit_tot_min) if spectrum is not None and pileup_rules else None
    for t in range(int(fit_tot_min), len(cols)):
        if not cols[t]["good"]:
            why = f"column {t} not good"
            break
        if cliff is not None and t > cliff - CLIFF_MARGIN:
            why = f"ToT spectrum cliff at {cliff}"
            break
        if pileup_rules and len(out) >= 2:
            a, b, c = cols[out[-2]], cols[out[-1]], cols[t]
            s_prev, s_now = b["mu"] - a["mu"], c["mu"] - b["mu"]
            err = np.sqrt(a["mu_err"] ** 2 + 2 * b["mu_err"] ** 2 + c["mu_err"] ** 2)
            if s_now < s_prev - max(STEP_SLACK_NS, 3 * err):
                why = f"step at {t} ({s_now:.1f} ns after {s_prev:.1f} ns)"
                break
        out.append(t)
    return out, why


def _limits(form, fit_tot_min):
    """(lower, upper) per parameter, for curve_fit and iminuit."""
    if form == "inverse":
        return [(-1000, 1000), (0, 1e5), (-50, fit_tot_min - 0.25)]
    if form == "power":
        return [(-1000, 1000), (0, 1e6), (0.05, 8.0)]
    if form == "exp":
        return [(-1000, 1000), (0, 1e5), (0.2, 100.0)]
    if form == "lin_inv":
        return [(-1000, 1000), (0, 1e5), (-20, 20)]
    raise TimewalkError(f"form {form!r}: the known forms are {', '.join(FORMS)}")


def _starts(form, x, y):
    """A few start points for curve_fit."""
    np = _np()
    a0 = float(y[-1]) - 5.0
    span = max(float(y[0] - a0), 1.0)
    if form == "inverse":
        return [[a0, span * (x[0] - c), c] for c in (0.0, 2.0, -3.0, -10.0)]
    if form == "power":
        return [[a0, span * x[0] ** p, p] for p in (1.0, 2.0, 0.5)]
    if form == "exp":
        return [[a0, span * np.exp(x[0] / t), t] for t in (2.0, 4.0, 8.0)]
    return [[a0, span * x[0], -1.0], [a0 + 30, span * x[0], -2.0]]      # lin_inv


def fit_form(form, x, y, yerr, fit_tot_min: int = FIT_TOT_MIN) -> dict:
    """iminuit LeastSquares fit of one form to the column peaks; scipy start values."""
    np = _np()
    from iminuit import Minuit
    from iminuit.cost import LeastSquares
    from scipy.optimize import curve_fit

    f = _form_f(form)
    lim = _limits(form, fit_tot_min)
    lo, hi = [l_[0] for l_ in lim], [l_[1] for l_ in lim]
    best = None
    for p0 in _starts(form, x, y):
        p0 = np.clip(p0, np.array(lo) + 1e-6, np.array(hi) - 1e-6)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                p, _ = curve_fit(f, x, y, p0=p0, sigma=yerr, absolute_sigma=True,
                                 bounds=(lo, hi), maxfev=20000)
        except (RuntimeError, ValueError):
            continue
        chi2 = float((((f(x, *p) - y) / yerr) ** 2).sum())
        if best is None or chi2 < best[1]:
            best = (p, chi2)
    start = best[0] if best is not None else np.clip(_starts(form, x, y)[0], lo, hi)
    m = Minuit(LeastSquares(x, y, yerr, f), *start, name=PARAM_NAMES)
    for n, l_ in zip(PARAM_NAMES, lim):
        m.limits[n] = l_
    m.migrad()
    if not m.valid:
        m.simplex()
        m.migrad()
    m.hesse()
    ndf = int(x.size - len(start))
    par = [float(v) for v in m.values]
    at_limit = [n for n, l_ in zip(PARAM_NAMES, lim)
                if min(abs(m.values[n] - l_[0]), abs(m.values[n] - l_[1]))
                < 1e-3 * (1 + abs(m.values[n]))]
    resid = y - f(x, *par)
    return {"form": form, "text": FORMS[form]["text"], "params": par,
            "errors": [float(v) for v in m.errors], "cov": np.asarray(m.covariance).tolist(),
            "chi2": float(m.fval), "ndf": ndf, "chi2_ndf": float(m.fval) / max(ndf, 1),
            "valid": bool(m.valid), "at_limit": at_limit, "start": [float(v) for v in start],
            "resid": resid.tolist(), "max_abs_resid": float(np.max(np.abs(resid)))}


def _w_raw(form, params, t):
    """Unclamped W at integer ToT ``t``, None where the form is not finite."""
    np = _np()
    if form == "inverse" and t <= params[2]:
        return None
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        w = float(_form_f(form)(np.float64(t), *params))
    return w if np.isfinite(w) else None


def choose_tot_min(form, params, fit_tot_max: int, tot_min: int = TOT_MIN,
                   fit_tot_min: int = FIT_TOT_MIN, w_span: float = W_SANE_SPAN) -> tuple[int, str]:
    """(tot_min, why it is above ``tot_min``, '' when it is not): the lowest ToT
    >= ``tot_min`` at which W is finite and W - W(``fit_tot_max``) <= ``w_span``;
    for the inverse form also at least 0.5 above its pole. At most
    ``fit_tot_min``."""
    ref = _w_raw(form, params, fit_tot_max)
    whys = []
    for t in range(int(tot_min), int(fit_tot_min) + 1):
        if form == "inverse" and t < params[2] + 0.5:
            whys.append(f"ToT {t} is within 0.5 of the inverse form's pole {params[2]:.2f}")
            continue
        w = _w_raw(form, params, t)
        if w is None or ref is None:
            whys.append(f"W({t}) is not finite")
            continue
        if w - ref <= w_span:
            return t, "; ".join(whys)
        whys.append(f"W({t}) - W({fit_tot_max}) = {w - ref:.1f} ns > {w_span:g} ns")
    return int(fit_tot_min), "; ".join(whys)


_PEAK_KEYS = ("tot", "n", "n_prompt", "fitted", "valid", "good", "mu", "mu_err", "sigma",
              "sigma_err", "n_sig", "n_bkg", "median_prompt", "truncated")


def fit_chip(h2, edges, form: str = "lin_inv", fit_tot_min: int = FIT_TOT_MIN,
             tot_min: int = TOT_MIN, spectrum=None, clamp: str = "last_good",
             min_columns: int = MIN_COLUMNS, min_prompt: int = MIN_PROMPT) -> dict:
    """Column fits and the walk fit of one chip's ``h2[dt, tot]``."""
    np = _np()
    h2 = np.asarray(h2, dtype=float)
    cols = column_fits(h2, edges, min_prompt)
    if spectrum is None:
        spectrum = h2.sum(axis=0)
    rng, why = fit_range(cols, fit_tot_min, spectrum)
    last_good = fit_range(cols, fit_tot_min, pileup_rules=False)[0]
    res = {"entries": int(h2.sum()), "fit_tot_min": int(fit_tot_min),
           "spectrum": [float(v) for v in spectrum],
           "peaks": [{k: c.get(k) for k in _PEAK_KEYS} for c in cols if c.get("fitted")]}
    if len(rng) < min_columns:
        res["refused"] = (f"{len(rng)} good columns from ToT {fit_tot_min} (need {min_columns}); "
                          f"stopped: {why}")
        return res
    x = np.array(rng, dtype=float)
    y = np.array([cols[t]["mu"] for t in rng])
    e = np.array([cols[t]["mu_err"] for t in rng])
    tmax = int(last_good[-1]) if clamp == "last_good" else int(rng[-1])
    r = fit_form(form, x, y, e, fit_tot_min)
    tm, raised = choose_tot_min(form, r["params"], int(rng[-1]), tot_min, fit_tot_min)
    res.update(r)
    if tm > tot_min:
        res["tot_min_raised"] = f"from {tot_min} to {tm}: {raised}"
    res.update(tot_min=tm, tot_max=tmax, fit_tot_max=int(rng[-1]), fit_tots=rng,
               fit_tot_max_reason=why, clamp=clamp,
               w_at={str(t): _w_raw(form, r["params"], t) for t in range(N_TOT)},
               w_applied=[float(v) for v in evaluate(form, r["params"], np.arange(N_TOT), tm, tmax)])
    return res


def fit_hists(H: Hists, form: str = "lin_inv", fit_tot_min: int = FIT_TOT_MIN,
              tot_min: int = TOT_MIN, clamp: str = "last_good", min_columns: int = MIN_COLUMNS,
              folder: str = DEFAULT_FOLDER, log=print) -> dict:
    """The fit JSON of summed histograms: one fit_chip() per VID."""
    if form not in FORMS:
        raise TimewalkError(f"--form {form}: the known forms are {', '.join(FORMS)}")
    edges = H.dt_edges
    out = {"tool": f"{TOOL} fit", "files": list(H.files), "folder": folder,
           "histogram": f"{HIST_PREFIX}<vid>",
           "dt_axis": {"lo": float(edges[0]), "hi": float(edges[-1]), "bins": len(edges) - 1},
           "form": form, "form_text": FORMS[form]["text"], "param_names": list(PARAM_NAMES),
           "fit_tot_min": int(fit_tot_min), "tot_min_default": int(tot_min), "clamp": clamp,
           "min_columns": int(min_columns), "min_prompt": MIN_PROMPT, "max_mu_err_ns": MAX_MU_ERR,
           "w_sane_span_ns": W_SANE_SPAN,
           "spectrum": "monitor tot_vs_chip" if H.spectra is not None else "dt histogram ToT projection",
           "convention": "t_corr = t_raw - W(clamp(ToT, tot_min, tot_max)); ToT in counts of "
                         "256 ns, W in ns; W = the fitted peak of dt(pixel - S1) at that ToT",
           "chips": {}}
    for vid in sorted(H.chips):
        spec = H.spectra.get(vid) if H.spectra is not None else None
        r = fit_chip(H.chips[vid], edges, form, fit_tot_min, tot_min, spec, clamp, min_columns)
        r["vid"] = vid
        out["chips"][str(vid)] = r
        if "refused" in r:
            log(f"  {vid}: {r['entries']:,} pairs; REFUSED: {r['refused']}")
        else:
            log(f"  {vid}: {r['entries']:,} pairs, fit ToT {r['fit_tots'][0]}-{r['fit_tot_max']} "
                f"({r['fit_tot_max_reason']}), clamp [{r['tot_min']}, {r['tot_max']}], "
                f"{form} p = ({', '.join(f'{p:.3f}' for p in r['params'])}), "
                f"chi2/ndf {r['chi2_ndf']:.2f}{'' if r['valid'] else ', NOT VALID'}")
            if "tot_min_raised" in r:
                log(f"  WARNING {vid}: tot_min raised {r['tot_min_raised']}")
            if r.get("at_limit"):
                log(f"  WARNING {vid}: parameter(s) {', '.join(r['at_limit'])} at a limit of "
                    f"the fit; check the curve before adding it")
    return out


def _clean(obj):
    """Non-finite floats -> None, numpy scalars -> Python, for strict JSON."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def plot_fit(fit: dict, out_dir: Path, log=print) -> list[Path]:
    """One PNG per chip: the column peaks, the curve (solid over the fit range,
    dotted where clamped or extrapolated) and the residuals."""
    np = _np()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    form = fit["form"]
    tt = np.linspace(0, TOT_MAX, 400)
    for vid, c in fit["chips"].items():
        fig, (ax, ar) = plt.subplots(2, 1, figsize=(7.5, 6.5), sharex=True,
                                     gridspec_kw={"height_ratios": [3, 1.2], "hspace": 0.06})
        pk = [p for p in c["peaks"] if p.get("mu") is not None]
        rng = set(c.get("fit_tots", []))
        for sel, style, lab in ((lambda p: p["tot"] in rng, dict(fmt="o", color="k"), "fit range"),
                                (lambda p: p["good"] and p["tot"] not in rng,
                                 dict(fmt="o", mfc="none", color="k"), "good, not fitted"),
                                (lambda p: not p["good"], dict(fmt="x", color="0.6"), "not good")):
            s = [p for p in pk if sel(p)]
            if s:
                ax.errorbar([p["tot"] for p in s], [p["mu"] for p in s],
                            [p["mu_err"] for p in s], ms=4, label=lab, **style)
        if "refused" in c:
            ax.text(0.5, 0.5, f"REFUSED\n{c['refused']}", transform=ax.transAxes, ha="center",
                    va="center", color="C3", fontsize=9)
            ax.set_title(f"MuPix chip {vid}: {c['entries']:,} pairs", fontsize=10)
        else:
            f = _form_f(form)
            inside = (tt >= c["fit_tot_min"]) & (tt <= c["fit_tot_max"])
            with np.errstate(divide="ignore", invalid="ignore"):
                ax.plot(tt, np.where(inside, f(tt, *c["params"]), np.nan), "-", color="C0",
                        label=f"{form} fit")
            wl = evaluate(form, c["params"], tt, c["tot_min"], c["tot_max"])
            ax.plot(tt, np.where(inside, np.nan, wl), ":", color="C0",
                    label=f"applied W, clamped to [{c['tot_min']}, {c['tot_max']}]")
            ax.set_title(f"MuPix chip {vid}: W = {FORMS[form]['text']}, p = ("
                         + ", ".join(f"{p:.2f}" for p in c["params"])
                         + f"), chi2/ndf {c['chi2_ndf']:.2f}", fontsize=9)
            ar.errorbar(c["fit_tots"], c["resid"],
                        [p["mu_err"] for p in pk if p["tot"] in rng], fmt="o", ms=3, color="k")
        ax.set_ylabel("dt(pixel - S1) peak [ns]")
        ax.axhline(0, color="0.85", lw=0.6)
        ax.legend(fontsize=8)
        ar.axhline(0, color="0.5", lw=0.6)
        ar.set_ylim(-6, 6)
        ar.set_xlim(-0.5, TOT_MAX + 0.5)
        ar.set_xlabel("pixel ToT [counts of 256 ns]")
        ar.set_ylabel("peak - W [ns]")
        path = out_dir / f"twc_fit_{vid}.png"
        fig.savefig(path, dpi=100, bbox_inches="tight")
        plt.close(fig)
        written.append(path)
        log(f"written: {path}")
    return written


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_fit(args) -> int:
    paths = [Path(p) for p in args.hists]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise TimewalkError(f"no such file(s): {', '.join(missing)}")
    H = read_hists(paths, args.folder, args.spectrum)
    print(f"{len(paths)} file(s), {len(H.chips)} chip(s), dt [{H.dt_edges[0]:g}, "
          f"{H.dt_edges[-1]:g}) in {len(H.dt_edges) - 1} bins; ToT spectrum from "
          f"{'the monitor tot_vs_chip' if H.spectra is not None else 'the dt histograms'}")
    fit = _clean(fit_hists(H, args.form, args.fit_tot_min, args.tot_min, args.clamp,
                           args.min_columns, args.folder))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fit, indent=1, allow_nan=False) + "\n")
    print(f"written: {out}")
    if args.plots:
        plot_fit(fit, Path(args.plots))
    good = [v for v, c in fit["chips"].items() if "refused" not in c]
    print(f"{len(good)} of {len(fit['chips'])} chip(s) fitted")
    return 0 if good else 1


def _load_doc(conditions):
    path = container_path(conditions)
    doc = json.loads(path.read_text(encoding="utf-8"))
    if TABLE not in doc:
        raise TimewalkError(f"{path} has no table {TABLE}")
    return path, doc


def cmd_add(args) -> int:
    if args.empty and (args.input or args.form or args.skip_vid):
        raise TimewalkError("--empty adds an interval without constants; it takes no fit JSON, "
                            "--form or --skip-vid")
    if not args.empty and not args.input:
        raise TimewalkError("give a fit JSON, or --empty for an interval without constants")
    path, doc = _load_doc(args.conditions)
    fit_warnings: list[str] = []
    if args.empty:
        chips, left_out, source = [], [], "none: --empty, no chip is corrected"
        input_text = source
        comment = f"{args.comment} No constants (--empty): every hit passes through uncorrected."
    else:
        fit_path = Path(args.input)
        try:
            fit = json.loads(fit_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise TimewalkError(f"{fit_path}: {exc}") from None
        chips, left_out, source = chips_from_fit(fit, args.form, set(args.skip_vid or ()),
                                                 fit_warnings)
        if not chips:
            raise TimewalkError(f"{fit_path}: no chip with usable constants"
                                + ("".join(f"\n  {n}" for n in left_out)))
        input_text = f"{fit_path} ({source})"
        comment = (f"{args.comment} Source: {fit_path.name} ({source}"
                   + (f", {len(fit['files'])} histogram file(s)" if fit.get("files") else "")
                   + f"); {len(chips)} chip(s).")
    run_end = args.last_run + 1 if args.last_run is not None else args.run_end
    tag = args.tag or select_tag(doc[TABLE], None, TABLE)
    new_table, notes = plan_add(doc[TABLE], chips, args.run_start, run_end, tag, comment,
                                args.created_by, args.split, args.tag_description, args.replace)
    new_doc = dict(doc)
    new_doc[TABLE] = new_table
    problems = validate(new_doc)
    if problems:
        raise TimewalkError("the table after the change would fail the correction:\n  "
                            + "\n  ".join(problems))
    before, after = dump(doc), dump(new_doc)
    print(f"container  {path}")
    print(f"table      {TABLE}, tag '{tag}'")
    print(f"input      {input_text}")
    if left_out:
        print("left out (these chips pass through uncorrected):")
        for n in left_out:
            print(f"  {n}")
    for w in fit_warnings:
        print(f"WARNING {w}")
    print()
    print("changes:")
    for n in notes:
        print(f"  {n}")
    print()
    print("intervals after:")
    print(describe(new_table, "n_chips", "chips"))
    print()
    print(f"constants of the new interval ({len(chips)} chip(s)):")
    print(chip_table(chips) if chips else "  none: the correction is a copy over this interval")
    if not args.no_diff:
        print()
        sys.stdout.writelines(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=str(path), tofile=str(path), n=2))
    print()
    if not args.write:
        print("dry run: nothing written. Add --write to apply.")
        return 0
    path.write_text(after, encoding="utf-8")
    print(f"written: {path}")
    if args.table_out:
        out = Path(args.table_out)
        out.write_text(dump({TABLE: new_table}), encoding="utf-8")
        print(f"written: {out} (this table alone, for json2pg.py / json2sqlite.py)")
    return 0


def cmd_show(args) -> int:
    path, doc = _load_doc(args.conditions)
    table = doc[TABLE]
    print(f"container  {path}")
    print(describe(table, "n_chips", "chips"))
    if args.run is None:
        return 0
    row, rows = resolve(table, TABLE, args.run, args.tag)
    chips = timewalk_chips(rows)
    print()
    print(f"run {args.run} reads row {row['row_id']} {_range(row.get('run_start', 0), row.get('run_end'))}"
          f" of tag '{row['tag']}': {len(chips)} chip(s) with constants"
          + ("" if chips else " (the correction is a copy)"))
    if chips:
        print(chip_table(chips))
    return 0


def cmd_check(args) -> int:
    path, doc = _load_doc(args.conditions)
    problems = validate(doc)
    print(f"container  {path}")
    if problems:
        print(f"{len(problems)} problem(s); the correction would fail on them:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"{TABLE}: every run resolves to exactly one interval of the default tag and every "
          f"active payload passes the correction's rules")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=f"python -m {TOOL}", description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=f"See the module docstring (pydoc {TOOL}).")
    ap.add_argument("--conditions", metavar="PATH",
                    help=f"conditions directory or container (default: $NL_CONDITIONS_DIR, else "
                         f"$PIONEERSYS/reco_testbeam/conditions; a directory means its {CONTAINER})")
    subs = ap.add_subparsers(dest="command", required=True)

    p = subs.add_parser("fit", help="fit the walk per chip from nearline _hists.root files")
    p.add_argument("hists", nargs="+", help="histogram files of the nearline job (*_hists.root)")
    p.add_argument("--out", required=True, help="the fit JSON to write")
    p.add_argument("--form", default="lin_inv", choices=list(FORMS), help="walk form (lin_inv)")
    p.add_argument("--fit-tot-min", type=int, default=FIT_TOT_MIN,
                   help=f"first ToT of the fit ({FIT_TOT_MIN}); below it crosstalk ghosts pull "
                        f"the column peaks")
    p.add_argument("--tot-min", type=int, default=TOT_MIN,
                   help=f"lowest clamp tot_min to try ({TOT_MIN}); raised (with a warning) "
                        f"until W is finite and at most {W_SANE_SPAN:g} ns above W at the last "
                        f"fitted column")
    p.add_argument("--clamp", choices=("last_good", "fit"), default="last_good",
                   help="tot_max: the last good column (default) or the end of the fit range")
    p.add_argument("--min-columns", type=int, default=MIN_COLUMNS,
                   help=f"refuse a chip with fewer columns in its fit range ({MIN_COLUMNS})")
    p.add_argument("--folder", default=DEFAULT_FOLDER,
                   help=f"histogram folder of the layer ({DEFAULT_FOLDER})")
    p.add_argument("--spectrum", choices=("auto", "monitor", "pairs"), default="auto",
                   help="ToT spectrum for the saturation cut: the monitor's tot_vs_chip, the "
                        "dt histogram's ToT projection, or the monitor's when every file has it")
    p.add_argument("--plots", metavar="DIR", help="write one PNG per chip into DIR")

    p = subs.add_parser("add", help="add an interval with the constants of a fit JSON, or an "
                                    "empty one (--empty)")
    p.add_argument("input", nargs="?",
                   help="fit JSON (of 'fit', or psm-analysis walk_fit.py with --form)")
    p.add_argument("--empty", action="store_true",
                   help="an interval without constants (n_chips 0), instead of a fit JSON: "
                        "the correction is a copy there (e.g. runs with no valid constants)")
    p.add_argument("--run-start", type=int, required=True, help="first run of the interval")
    end = p.add_mutually_exclusive_group()
    end.add_argument("--last-run", type=int, help="last run of the interval, INCLUSIVE")
    end.add_argument("--run-end", type=int, help="end of the interval, EXCLUSIVE")
    p.add_argument("--form", help="which form of a walk_fit.py JSON (default lin_inv)")
    p.add_argument("--skip-vid", type=int, action="append", metavar="VID",
                   help="leave this chip out (it then passes through uncorrected); repeatable")
    p.add_argument("--tag", help="tag of the interval (default: the table's default tag)")
    p.add_argument("--tag-description",
                   help="create --tag with this description if it does not exist (never as default)")
    p.add_argument("--comment", required=True, help="what these constants are, for these runs")
    p.add_argument("--created-by", default=getpass.getuser())
    p.add_argument("--split", action="store_true",
                   help="carve the interval out of overlapping active intervals of the tag")
    p.add_argument("--replace", action="store_true",
                   help="with --split: drop the constants an overlapped interval already holds "
                        "in the overlap")
    p.add_argument("--write", action="store_true", help="apply; without it nothing is written")
    p.add_argument("--table-out", metavar="PATH",
                   help="with --write, also write a container of this table alone for the DB loaders")
    p.add_argument("--no-diff", action="store_true", help="leave out the unified diff")

    p = subs.add_parser("show", help="the intervals, and with --run the constants a run reads")
    p.add_argument("--run", type=int)
    p.add_argument("--tag")

    subs.add_parser("check", help="the correction's rules over every run: no gap or overlap in "
                                  "the default tag, every payload valid against the chip map")

    args = ap.parse_args(argv)
    try:
        return {"fit": cmd_fit, "add": cmd_add, "show": cmd_show,
                "check": cmd_check}[args.command](args)
    except MaskError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
