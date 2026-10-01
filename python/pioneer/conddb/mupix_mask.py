#!/usr/bin/env python3
"""Fill the MuPix pixel mask (conditions table mupix_pixel_mask).

The decoder PITMidasMusip drops every pixel word on a masked pixel and counts
it (histograms/musip/mupix_masked_hits). The mask it uses is the run's
interval of the conditions table ``mupix_pixel_mask`` in
``bt2026_psm_readout_map.json``, which ships with an empty mask on
[0, open). This tool adds an interval holding the pixels of a noisy-pixel
study, or of a plain list, to that container:

    python -m pioneer.conddb.mupix_mask add STUDY.json \\
        --run-start 459 --last-run 459 --split \\
        --comment "hot pixels of run 459, ThHigh/ThLow 0x7a/0x79"
    python -m pioneer.conddb.mupix_mask show --run 459
    python -m pioneer.conddb.mupix_mask check

``add`` is a dry run by default: it prints what it would change (the
intervals before and after, the pixels, and a unified diff of the table) and
writes nothing. ``--write`` applies it. ``check`` runs the decoder's rules
over every run (see validate()).

Where the table lives
---------------------
``--db [CONNINFO]`` works on the conditions database, which is where the
constants are written first during the 2026 beamtime (the pinky PG18
cluster); the default CONNINFO is ``service=pioneer-conditions-admin``, from
~/.pg_service.conf with the password in ~/.pgpass:

    python -m pioneer.conddb.mupix_mask --db add STUDY.json \\
        --run-start 459 --last-run 459 --split --comment "..."          # dry run
    python -m pioneer.conddb.mupix_mask --db add STUDY.json ... --write
    python -m pioneer.conddb.mupix_mask --db show --run 459

It reads the table's active intervals (and the chip map) with
cond_export.export_table(), applies exactly the edit the JSON mode applies,
and with ``--write`` loads the WHOLE resulting table through cond_loader in
one transaction. A load replaces every active interval of each tag it names,
so it has to carry the complete table, never the new interval alone; the
intervals the edit deactivated are left out, since the load retires the
database's own copies of them. The write is refused if the table changed
between the read and the load. Afterwards the JSON snapshot is refreshed
(snapshot_after_write()); the git containers are exports (pg2json.py).

Without ``--db`` the tool edits a container file (``--conditions``, default
the git-tracked reco_testbeam/conditions): the development path, and the way
to try constants in a scratch copy. A change there must be committed to
reco_testbeam, and the nearline job only reads it in json mode.

Input
-----
* the study JSON of psm-analysis ``mupix-timewalk/noisy_pixels.py``:
  ``noisy_pixels.json`` (``recommended.pixel_mask.pixels``, a list of
  [chip, col, row]) or a per-run ``runNNNNN/noisy_pixels_runNNNNN.json``
  (``hot.pixels``, records with chip/col/row). Their ``chip`` is the RAW chip
  id of the pixel word, converted here to the detector id through
  ``mupix_chip_map`` at the study's run (``--map-run`` overrides it);
* a JSON list of [chip, col, row(, reason)] or of objects with ``vid`` or
  ``chip`` plus ``col``, ``row`` and optionally ``reason``;
* a CSV with a header naming ``vid`` or ``chip``, ``col``, ``row`` and
  optionally ``reason``, or without a header (then ``--chip-ids`` says which).

``vid`` is a detector id, ``chip`` a raw chip id; a raw id needs a run to be
converted at (``--map-run``, or the study's own run).

The table
---------
A parameter set of parallel arrays: ``n_pixels``, then ``vid``, ``col`` and
``row`` of n_pixels elements each and an optional ``reason`` per pixel. An
empty mask is ``n_pixels`` 0 and no arrays. The chip is the DETECTOR id,
because the raw id is the global ASIC id the FEB Mapping assigns and that
Mapping changed mid-beamtime, while a hot pixel belongs to its sensor.

Refused
-------
* a column outside 0-255 or a row outside 0-249 (the 256 x 250 sensor): rows
  250-255 exist only in corrupted words, which the decoder drops as out of
  range before it consults the mask, so they could never match;
* a pixel listed twice, a raw chip id the chip map does not have at the run
  it is converted at, or a detector id that is no chip of the chip map;
* a new interval overlapping an active interval of the same tag. ``--split``
  carves it out instead: each overlapped interval is deactivated (kept, for
  reproducibility) and its parts outside the new range are added back with
  their own payload ("Split from row N [a, b): <its original comment>"),
  which is how the first mask replaces part of the shipped empty [0, open)
  interval;
* ``--split`` over an interval that already masks pixels, unless it says what
  becomes of them: ``--union`` keeps them (the new range gets the old pixels
  plus the new ones, split at the overlapped intervals' edges where their
  masks differ), ``--replace-mask`` drops them in the overlap and prints how
  many per overlapped row;
* any change after which the table would fail the decoder (validate()).

``--table-out`` writes a container holding only this table as it was
written (in --db mode: as it was loaded), for another database:

    python -m pioneer.conddb.mupix_mask add ... --write --table-out /tmp/mask.json
    python3 json2pg.py "host=... dbname=..." /tmp/mask.json
"""
from __future__ import annotations

import argparse
import csv
import difflib
import getpass
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pioneer.conddb import cond_export, cond_loader
from pioneer.conddb.cond_loader import FingerprintMoved, LoaderError

TABLE = "mupix_pixel_mask"
CHIP_MAP_TABLE = "mupix_chip_map"
CONTAINER = "bt2026_psm_readout_map.json"
SCHEMA = "mupix_pixel_mask"
SCHEMA_VERSION = 1

# The MuPix sensor: 256 columns x 250 rows. The pixel word's row field is 8
# bits wide, so rows 250-255 can be decoded, but only from corrupted words,
# and PITMidasMusip drops those as out of range before the mask is consulted.
SENSOR_COLS = 256
SENSOR_ROWS = 250

#: --db without a value: the writer's libpq service (~/.pg_service.conf).
DB_DEFAULT = "service=pioneer-conditions-admin"
#: the module the writers run after a database write, when it exists
SNAPSHOT_MODULE = "pioneer.conddb.snapshot"
#: the mask's parallel arrays: arrays even when they hold one pixel
MASK_ARRAYS = ("vid", "col", "row", "reason")


class MaskError(Exception):
    """Anything that stops a mask from being written, with the reason."""


@dataclass(frozen=True)
class Pixel:
    vid: int
    col: int
    row: int
    reason: str = ""
    raw: int | None = None   # the raw chip id it came in as, for display


# ---------------------------------------------------------------------------
# The container and the tag/interval rules (PICondIov.h)
# ---------------------------------------------------------------------------

def default_conditions() -> Path:
    """Where the nearline job looks: NL_CONDITIONS_DIR, else the source tree."""
    if os.environ.get("NL_CONDITIONS_DIR"):
        return Path(os.environ["NL_CONDITIONS_DIR"])
    if os.environ.get("PIONEERSYS"):
        return Path(os.environ["PIONEERSYS"]) / "reco_testbeam" / "conditions"
    here = Path(__file__).resolve()
    # beamtime2026_pie5/python/pioneer/conddb -> the workspace root
    return here.parents[4] / "main" / "reco_testbeam" / "conditions"


def container_path(arg: str | None) -> Path:
    path = Path(arg) if arg else default_conditions()
    if path.is_dir():
        path = path / CONTAINER
    if not path.is_file():
        raise MaskError(f"no conditions container at {path}")
    return path


@dataclass
class Source:
    """Where the tables were read from, and where a write goes back to: a
    container file (``path``) or a database (``spec``, --db)."""
    label: str
    doc: dict
    path: Path | None = None
    spec: str | None = None
    fingerprint: tuple | None = None

    @property
    def what(self) -> str:
        return "database" if self.spec else "container"


def open_source(args, table: str, list_keys=()) -> Source:
    """The container of --conditions, or with --db the database's ``table``
    and ``mupix_chip_map`` exported into container form (active intervals)."""
    if getattr(args, "db", None):
        try:
            ex = cond_export.executor_for(args.db)
            try:
                # The fingerprint first: a change after it is caught by the
                # load; one between an export and a later fingerprint would not.
                fingerprint = cond_export.table_fingerprint(ex, table)
                doc = {CHIP_MAP_TABLE: cond_export.export_table(ex, CHIP_MAP_TABLE),
                       table: cond_export.export_table(ex, table, list_keys=list_keys)}
            finally:
                ex.close()
        except LoaderError as exc:
            raise MaskError(str(exc)) from None
        return Source(ex.label, doc, spec=args.db, fingerprint=fingerprint)
    path = container_path(args.conditions)
    return Source(str(path), json.loads(path.read_text(encoding="utf-8")), path=path)


def db_table(table: dict) -> dict:
    """The table as --db loads it: its active intervals only. The rows an edit
    deactivated are the database's current rows, which the load retires (and
    pins) itself; loading them again would add a second, inactive copy."""
    new = json.loads(json.dumps(table))
    new["iov"] = [r for r in new["iov"] if r.get("is_active", True)]
    keep = {str(r["row_id"]) for r in new["iov"]}
    if "values_by_iov" in new:
        new["values_by_iov"] = {k: v for k, v in new["values_by_iov"].items() if k in keep}
    return new


def write_db(src: Source, table: str, new_table: dict) -> None:
    """Load the complete ``new_table`` into the database of ``src``, in one transaction.

    Refused when the table's intervals changed since open_source() read them
    (another writer, a condtool close/deactivate): the load would retire
    intervals this edit never saw. The loader checks the fingerprint again
    inside the load transaction, under its lock on the interval table, so a
    writer that slips in between cannot be overwritten either.
    """
    try:
        ex = cond_export.executor_for(src.spec)
        try:
            cond_loader.load_tables(ex, [(f"{table} (edited)", {table: new_table})],
                                    expect={table: src.fingerprint})
        finally:
            ex.close()
    except FingerprintMoved as exc:
        raise MaskError(f"{exc}\nRerun the command: it reads the table again.") from None
    except LoaderError as exc:
        raise MaskError(f"the load was refused, nothing written: {exc}") from None


def _snapshot_available() -> bool:
    try:
        return importlib.util.find_spec(SNAPSHOT_MODULE) is not None
    except ImportError:
        return False


def snapshot_after_write(spec: str) -> None:
    """Refresh the JSON snapshot after a database write. Never fatal: the
    constants are in the database by now, and a missing snapshot only means
    the fallback is older than it could be."""
    if spec.startswith("sqlite:"):
        print("snapshot: none for a SQLite database")
        return
    if not _snapshot_available():
        print(f"reminder: no {SNAPSHOT_MODULE} in this checkout, so no snapshot was "
              f"taken. Export one by hand: python3 pg2json.py '{spec}' --out-dir DIR --check")
        return
    root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.run([sys.executable, "-m", SNAPSHOT_MODULE, "--conninfo", spec],
                              env=env)
        code = proc.returncode
    except OSError as exc:
        code = str(exc)
    if code:
        print(f"WARNING: the snapshot failed ({code}); the constants are in the database. "
              f"Run python -m {SNAPSHOT_MODULE} by hand.")


def db_argv(argv: list[str]) -> list[str]:
    """``--db`` followed by something that is not a database spec (a
    subcommand, an option, a file) means ``--db DB_DEFAULT``. argparse's
    nargs='?' alone would swallow the subcommand name."""
    out: list[str] = []
    for i, word in enumerate(argv):
        out.append(word)
        if word == "--db" and (i + 1 == len(argv) or not cond_export.is_db_spec(argv[i + 1])):
            out.append(DB_DEFAULT)
    return out


def add_db_argument(ap) -> None:
    """--conditions / --db, shared by mupix_mask and mupix_timewalk."""
    where = ap.add_mutually_exclusive_group()
    where.add_argument("--conditions", metavar="PATH",
                       help=f"conditions directory or container (default: $NL_CONDITIONS_DIR, "
                            f"else $PIONEERSYS/reco_testbeam/conditions; a directory means its "
                            f"{CONTAINER})")
    where.add_argument("--db", metavar="CONNINFO",
                       help=f"work on the conditions database instead: a libpq conninfo "
                            f"(default with no value: {DB_DEFAULT}; the password comes from "
                            f"~/.pgpass), or sqlite:PATH")


def show_diff(src: Source, table: str, before_doc: dict, after_doc: dict, args) -> str:
    """The unified diff add prints; returns the text a --write stores (JSON mode:
    the whole container; --db mode: the table as it will be loaded)."""
    if src.spec:
        before = dump({table: before_doc[table]})
        after = dump({table: db_table(after_doc[table])})
        names = (f"{src.label} {table} (now)", f"{src.label} {table} (after the load)")
    else:
        before, after = dump(before_doc), dump(after_doc)
        names = (str(src.path), str(src.path))
    if not args.no_diff:
        print()
        if src.spec:
            print("(row ids are the export's, 1..N; the load gives every interval a fresh "
                  "database row id)")
        sys.stdout.writelines(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=names[0], tofile=names[1], n=2))
    return after


def commit_write(src: Source, table: str, new_table: dict, after: str, table_out) -> None:
    """--write: the file, or the database plus its snapshot; then --table-out."""
    if src.spec:
        loaded = db_table(new_table)
        write_db(src, table, loaded)
        print(f"written: {table} loaded into {src.label}")
        snapshot_after_write(src.spec)
    else:
        loaded = new_table
        src.path.write_text(after, encoding="utf-8")
        print(f"written: {src.path}")
        print()
        print("NOTE: this changed a JSON container, NOT the conditions database. The "
              "nearline job reads the DATABASE, so this edit does not reach production "
              "jobs. Use --db to write the constants where the job reads them; a JSON "
              "container is read only by a job run with --conditions json:DIR.")
    if table_out:
        out = Path(table_out)
        out.write_text(dump({table: loaded}), encoding="utf-8")
        print(f"written: {out} (this table alone, for json2pg.py / json2sqlite.py)")


def _end(run_end) -> float:
    return float("inf") if run_end is None else run_end


def _range(run_start, run_end) -> str:
    return f"[{run_start}, {'open' if run_end is None else run_end})"


def select_tag(table: dict, requested: str | None, name: str) -> str:
    """The requested tag, or the table's unique default tag."""
    tags = table.get("tags", [])
    names = ", ".join(t["tag"] for t in tags) or "none"
    if requested:
        if not any(t["tag"] == requested for t in tags):
            raise MaskError(f"{name}: no tag '{requested}' (available: {names})")
        return requested
    defaults = [t["tag"] for t in tags if t.get("is_default")]
    if len(defaults) != 1:
        raise MaskError(f"{name}: {len(defaults)} default tags ({names}); name one with --tag")
    return defaults[0]


def resolve(table: dict, name: str, run: int, tag: str | None = None) -> tuple[dict, list]:
    """(interval, payload rows) a run reads: the tag's one active covering interval."""
    tag = select_tag(table, tag, name)
    covering = [r for r in table["iov"]
                if r["tag"] == tag and r.get("is_active", True)
                and r.get("run_start", 0) <= run < _end(r.get("run_end"))]
    if len(covering) != 1:
        raise MaskError(f"{name}: run {run} is covered by {len(covering)} active interval(s) "
                        f"of tag '{tag}'")
    row = covering[0]
    key = str(row["row_id"])
    by_iov = table.get("values_by_iov", {})
    rows = by_iov[key] if key in by_iov else table.get("values", {}).get(tag, [])
    return row, rows


def chip_map_at(doc: dict, run: int) -> dict[int, int]:
    """raw chip id -> detector id at a run, from mupix_chip_map."""
    if CHIP_MAP_TABLE not in doc:
        raise MaskError(f"the container has no {CHIP_MAP_TABLE} to convert raw chip ids with")
    _row, rows = resolve(doc[CHIP_MAP_TABLE], CHIP_MAP_TABLE, run)
    return {int(r["channel_id"]): int(r["vid"]) for r in rows}


def known_vids(doc: dict) -> set[int]:
    """Every detector id of an active interval of the chip map's default tag."""
    table = doc.get(CHIP_MAP_TABLE)
    if table is None:
        raise MaskError(f"the container has no {CHIP_MAP_TABLE} to check detector ids against")
    tag = select_tag(table, None, CHIP_MAP_TABLE)
    vids: set[int] = set()
    for row in table["iov"]:
        if row["tag"] != tag or not row.get("is_active", True):
            continue
        key = str(row["row_id"])
        by_iov = table.get("values_by_iov", {})
        rows = by_iov[key] if key in by_iov else table.get("values", {}).get(tag, [])
        vids |= {int(r["vid"]) for r in rows}
    return vids


def mask_pixels(rows: list) -> list[Pixel]:
    """The pixels of a mupix_pixel_mask payload (the C++ CheckPayload rules)."""
    params = {r["key"]: r.get("value") for r in rows}
    n = params.get("n_pixels")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise MaskError(f"n_pixels is {n!r}")
    arrays = [params.get(k, []) for k in ("vid", "col", "row")]
    reasons = params.get("reason", [])
    if any(len(a) != n for a in arrays) or len(reasons) not in (0, n):
        raise MaskError(f"n_pixels {n} does not match the array lengths "
                        f"{[len(a) for a in arrays]} (reason {len(reasons)})")
    return [Pixel(v, c, r, reasons[i] if reasons else "")
            for i, (v, c, r) in enumerate(zip(*arrays))]


def _active(table: dict, tag: str) -> list[dict]:
    return sorted((r for r in table["iov"] if r["tag"] == tag and r.get("is_active", True)),
                  key=lambda r: r.get("run_start", 0))


def _payload_of(table: dict, row: dict) -> list | None:
    by_iov = table.get("values_by_iov", {})
    key = str(row["row_id"])
    return by_iov[key] if key in by_iov else table.get("values", {}).get(row["tag"])


def chip_map_starts(doc: dict) -> list[int]:
    """The first run of every active interval of the chip map's default tag."""
    cmap_table = doc.get(CHIP_MAP_TABLE)
    if cmap_table is None:
        raise MaskError(f"the container has no {CHIP_MAP_TABLE} to check detector ids against")
    return sorted({r.get("run_start", 0)
                   for r in _active(cmap_table, select_tag(cmap_table, None, CHIP_MAP_TABLE))})


def chip_map_runs(row: dict, cmap_starts: list[int]) -> list[int]:
    """One run per chip-map interval an interval overlaps: its own first run,
    then every chip-map interval start inside it."""
    lo, hi = row.get("run_start", 0), _end(row.get("run_end"))
    return [lo] + [s for s in cmap_starts if lo < s < hi]


def vid_problems(doc: dict, vids, where: str, runs: list[int]) -> list[str]:
    """Each detector id must be a chip of mupix_chip_map that exactly one raw
    chip id maps to, at each of ``runs``."""
    problems: list[str] = []
    for run in runs:
        try:
            cmap = chip_map_at(doc, run)
        except MaskError as exc:
            problems.append(f"{where}: at run {run}: {exc}")
            continue
        raws: dict[int, list[int]] = {}
        for raw, vid in cmap.items():
            raws.setdefault(vid, []).append(raw)
        for vid in sorted(set(vids)):
            if vid not in raws:
                problems.append(f"{where}: detector id {vid} is no chip of "
                                f"{CHIP_MAP_TABLE} at run {run}")
            elif len(raws[vid]) > 1:
                problems.append(f"{where}: {CHIP_MAP_TABLE} gives detector id {vid} to "
                                f"raw chip ids {sorted(raws[vid])} at run {run}")
    return problems


def validate_table(doc: dict, name: str, check_row) -> list[str]:
    """The interval rules of table ``name`` over every run, plus ``check_row``
    on every active payload; [] if everything holds.

    The default tag's active intervals tile [0, open): no gap and no overlap,
    so every run resolves to exactly one. Any other tag's active intervals do
    not overlap. ``check_row(doc, row, payload_rows, where, runs)`` returns
    the problems of one active interval's payload; ``runs`` holds one run per
    chip-map interval it overlaps (chip_map_runs()).
    """
    table = doc.get(name)
    if table is None:
        return [f"the container has no table {name}"]
    problems: list[str] = []
    try:
        default = select_tag(table, None, name)
        cmap_starts = chip_map_starts(doc)
    except MaskError as exc:
        return [str(exc)]

    for tag in [t["tag"] for t in table.get("tags", [])]:
        spans = _active(table, tag)
        if tag == default:
            if not spans:
                problems.append(f"default tag '{tag}' has no active interval")
            elif spans[0].get("run_start", 0) != 0:
                problems.append(f"default tag '{tag}': runs [0, {spans[0].get('run_start', 0)}) "
                                f"have no interval")
            if spans and spans[-1].get("run_end") is not None:
                problems.append(f"default tag '{tag}': runs from {spans[-1]['run_end']} on have "
                                f"no interval (the last one is not open-ended)")
        for a, b in zip(spans, spans[1:]):
            where = (f"tag '{tag}' rows {a['row_id']} {_range(a.get('run_start', 0), a.get('run_end'))}"
                     f" and {b['row_id']} {_range(b.get('run_start', 0), b.get('run_end'))}")
            if _end(a.get("run_end")) > b.get("run_start", 0):
                problems.append(f"{where} overlap")
            elif tag == default and a.get("run_end") < b.get("run_start", 0):
                problems.append(f"{where} leave a gap")
        for r in spans:
            where = f"tag '{tag}' row {r['row_id']} {_range(r.get('run_start', 0), r.get('run_end'))}"
            rows = _payload_of(table, r)
            if rows is None:
                problems.append(f"{where}: no payload")
                continue
            problems += check_row(doc, r, rows, where, chip_map_runs(r, cmap_starts))
    return problems


def _check_mask_row(doc: dict, row: dict, rows: list, where: str, runs: list[int]) -> list[str]:
    """PIMuPixMask::ResolveMask on one payload, at each of ``runs``."""
    try:
        pixels = mask_pixels(rows)
    except MaskError as exc:
        return [f"{where}: {exc}"]
    problems: list[str] = []
    seen: set[tuple[int, int, int]] = set()
    for p in pixels:
        key = (p.vid, p.col, p.row)
        if key in seen:
            problems.append(f"{where}: pixel {key} listed twice")
        seen.add(key)
        if not (0 <= p.col < SENSOR_COLS and 0 <= p.row < SENSOR_ROWS):
            problems.append(f"{where}: pixel {key} is off the {SENSOR_COLS} x "
                            f"{SENSOR_ROWS} sensor")
    return problems + vid_problems(doc, [p.vid for p in pixels], where, runs)


def validate(doc: dict) -> list[str]:
    """Every rule the decoder applies to the table, over every run; [] if it holds.

    The default tag's active intervals tile [0, open): no gap and no overlap,
    so every run resolves to exactly one. Any other tag's active intervals do
    not overlap. Every active payload passes the C++ rules
    (PIMuPixMask::ResolveMask): n_pixels matches the arrays, every pixel on the
    256 x 250 sensor and listed once, and every detector id a chip of
    mupix_chip_map that exactly one raw chip id maps to, at every run the
    interval covers (checked at each chip-map interval it overlaps).
    """
    return validate_table(doc, TABLE, _check_mask_row)


def payload(pixels: list[Pixel]) -> list[dict]:
    """A mupix_pixel_mask payload. No arrays for an empty mask: an empty array
    has no cells in the database, and the two backends would hash differently."""
    rows: list[dict] = [{"key": "n_pixels", "value": len(pixels)}]
    if pixels:
        rows += [{"key": "vid", "value": [p.vid for p in pixels]},
                 {"key": "col", "value": [p.col for p in pixels]},
                 {"key": "row", "value": [p.row for p in pixels]}]
        if any(p.reason for p in pixels):
            rows.append({"key": "reason", "value": [p.reason for p in pixels]})
    return rows


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    chip: int
    col: int
    row: int
    reason: str = ""


def _int(value, what: str) -> int:
    if isinstance(value, bool):
        raise MaskError(f"{what} is {value!r}, not an integer")
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise MaskError(f"{what} is {value!r}, not an integer") from None
    if isinstance(value, float) and value != out:
        raise MaskError(f"{what} is {value!r}, not an integer")
    return out


def _study_reason(record: dict, run: int) -> str:
    parts = [f"hot in run {run}"]
    if "hits_no_burst" in record:
        parts.append(f"{record['hits_no_burst']} hits outside bursts")
    if "expected_no_burst" in record:
        parts.append(f"{record['expected_no_burst']} expected")
    return ", ".join(parts)


def read_input(path: Path, chip_ids: str | None) -> tuple[list[Entry], str, int | None, str]:
    """(entries, chip id kind 'raw'|'vid', run of the study or None, description)."""
    text = path.read_text()
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        doc = None

    if isinstance(doc, dict) and "recommended" in doc:
        rec = doc["recommended"]
        run = _int(rec.get("run", doc.get("primary_run")), "recommended.run")
        hot = {(p["chip"], p["col"], p["row"]): p
               for p in doc.get("runs", {}).get(str(run), {}).get("hot", {}).get("pixels", [])}
        entries = []
        for i, triple in enumerate(rec["pixel_mask"]["pixels"]):
            if len(triple) != 3:
                raise MaskError(f"{path}: recommended.pixel_mask.pixels[{i}] is {triple!r}, "
                                f"not [chip, col, row]")
            chip, col, row = (_int(v, f"pixel {i}") for v in triple)
            record = hot.get((chip, col, row))
            entries.append(Entry(chip, col, row, _study_reason(record, run) if record else
                                 f"recommended mask of run {run}"))
        return entries, "raw", run, f"noisy-pixel study {path.name}, recommended mask of run {run}"

    if isinstance(doc, dict) and "hot" in doc and "run" in doc:
        run = _int(doc["run"], "run")
        entries = [Entry(_int(p["chip"], "chip"), _int(p["col"], "col"), _int(p["row"], "row"),
                         _study_reason(p, run)) for p in doc["hot"]["pixels"]]
        return entries, "raw", run, f"noisy-pixel study {path.name}, hot pixels of run {run}"

    if isinstance(doc, list):
        entries, kinds = [], set()
        for i, item in enumerate(doc):
            if isinstance(item, dict):
                if ("vid" in item) == ("chip" in item):
                    raise MaskError(f"{path}: entry {i} must name exactly one of 'vid' "
                                    f"(detector id) and 'chip' (raw chip id)")
                kinds.add("vid" if "vid" in item else "raw")
                chip = item["vid"] if "vid" in item else item["chip"]
                entries.append(Entry(_int(chip, f"entry {i} chip"), _int(item["col"], f"entry {i} col"),
                                     _int(item["row"], f"entry {i} row"), str(item.get("reason", ""))))
            elif isinstance(item, list) and len(item) in (3, 4):
                if chip_ids is None:
                    raise MaskError(f"{path}: a list of [chip, col, row] needs --chip-ids "
                                    f"raw|vid to say what the first number is")
                kinds.add(chip_ids)
                entries.append(Entry(_int(item[0], f"entry {i} chip"), _int(item[1], f"entry {i} col"),
                                     _int(item[2], f"entry {i} row"),
                                     str(item[3]) if len(item) == 4 else ""))
            else:
                raise MaskError(f"{path}: entry {i} is {item!r}, neither [chip, col, row] "
                                f"nor an object")
        if len(kinds) > 1:
            raise MaskError(f"{path}: mixes detector ids and raw chip ids")
        kind = kinds.pop() if kinds else (chip_ids or "vid")
        if chip_ids and kind != chip_ids:
            raise MaskError(f"{path}: the entries name '{kind}' ids but --chip-ids says {chip_ids}")
        return entries, kind, None, f"pixel list {path.name}"

    if doc is not None:
        raise MaskError(f"{path}: JSON that is neither a noisy-pixel study nor a list of pixels")

    # CSV
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    rows = list(csv.reader(io.StringIO("\n".join(lines))))
    if not rows:
        return [], chip_ids or "vid", None, f"pixel list {path.name}"
    header = [h.strip().lower() for h in rows[0]]
    if header and not header[0].lstrip("-").isdigit():
        if ("vid" in header) == ("chip" in header) or "col" not in header or "row" not in header:
            raise MaskError(f"{path}: the header must name 'vid' or 'chip', and 'col' and 'row'")
        kind = "vid" if "vid" in header else "raw"
        if chip_ids and kind != chip_ids:
            raise MaskError(f"{path}: the header names '{kind}' ids but --chip-ids says {chip_ids}")
        ic = header.index("vid" if kind == "vid" else "chip")
        icol, irow = header.index("col"), header.index("row")
        ireason = header.index("reason") if "reason" in header else None
        body = rows[1:]
    else:
        if chip_ids is None:
            raise MaskError(f"{path}: a CSV without a header needs --chip-ids raw|vid")
        kind, ic, icol, irow, ireason, body = chip_ids, 0, 1, 2, 3, rows
    entries = []
    for n, r in enumerate(body, start=1):
        cells = [c.strip() for c in r]
        entries.append(Entry(_int(cells[ic], f"line {n} chip"), _int(cells[icol], f"line {n} col"),
                             _int(cells[irow], f"line {n} row"),
                             cells[ireason] if ireason is not None and ireason < len(cells) else ""))
    return entries, kind, None, f"pixel list {path.name}"


def to_pixels(entries: list[Entry], kind: str, doc: dict, map_run: int | None) -> list[Pixel]:
    """Detector-id pixels, checked: on the sensor, known chips, no duplicates."""
    problems: list[str] = []
    pixels: list[Pixel] = []
    if kind == "raw":
        if map_run is None:
            raise MaskError("raw chip ids need a run to convert them at: --map-run N")
        cmap = chip_map_at(doc, map_run)
        for e in entries:
            if e.chip not in cmap:
                problems.append(f"raw chip {e.chip} has no entry in {CHIP_MAP_TABLE} at run "
                                f"{map_run} (raw ids there: {sorted(cmap)})")
                continue
            pixels.append(Pixel(cmap[e.chip], e.col, e.row, e.reason, e.chip))
    else:
        pixels = [Pixel(e.chip, e.col, e.row, e.reason) for e in entries]

    vids = known_vids(doc)
    seen: dict[tuple[int, int, int], int] = {}
    for i, p in enumerate(pixels):
        where = f"pixel {i} (vid {p.vid}, col {p.col}, row {p.row})"
        if p.vid not in vids:
            problems.append(f"{where}: detector id {p.vid} is no chip of {CHIP_MAP_TABLE} "
                            f"({sorted(vids)})")
        if not 0 <= p.col < SENSOR_COLS:
            problems.append(f"{where}: column outside 0-{SENSOR_COLS - 1}")
        if not 0 <= p.row < SENSOR_ROWS:
            problems.append(f"{where}: row outside 0-{SENSOR_ROWS - 1}; rows 250-255 come only "
                            f"from corrupted words, which the decoder drops as out of range "
                            f"before it consults the mask")
        key = (p.vid, p.col, p.row)
        if key in seen:
            problems.append(f"{where}: listed twice (also pixel {seen[key]})")
        seen.setdefault(key, i)
    if problems:
        raise MaskError(f"{len(problems)} problem(s) with the pixels:\n  " + "\n  ".join(problems))
    return sorted(pixels, key=lambda p: (p.vid, p.col, p.row))


# ---------------------------------------------------------------------------
# The change
# ---------------------------------------------------------------------------

_SPLIT_PREFIX = re.compile(r"^Split from row \d+ \[\d+, (?:\d+|open)\): ")


def original_comment(comment: str) -> str:
    """A row's comment without the "Split from row N [a, b): " a split put in
    front of it, so a remnant of a remnant says where it came from once."""
    return _SPLIT_PREFIX.sub("", comment or "", count=1)


def _key(p: Pixel) -> tuple[int, int, int]:
    return (p.vid, p.col, p.row)


def _union(new: list[Pixel], old: list[Pixel]) -> list[Pixel]:
    """new + the old pixels it does not list; a pixel in both keeps the new
    record, and the old reason when the new one has none."""
    merged = {_key(p): p for p in old}
    for p in new:
        prev = merged.get(_key(p))
        merged[_key(p)] = p if p.reason or prev is None else Pixel(p.vid, p.col, p.row, prev.reason, p.raw)
    return sorted(merged.values(), key=_key)


def plan_add(table: dict, pixels: list[Pixel], run_start: int, run_end: int | None, tag: str,
             comment: str, created_by: str, split: bool,
             tag_description: str | None, overlap: str | None = None) -> tuple[dict, list[str]]:
    """The table with the new interval added, and a list of what changed.

    ``overlap`` says what happens to the pixels of an overlapped interval
    that holds a mask (n_pixels > 0) inside the new range: None refuses,
    "replace" drops them (the new mask alone holds there) and "union" keeps
    them, splitting the new range at the overlapped intervals' edges where
    their masks differ. An overlapped EMPTY mask needs neither.
    """
    if run_start < 0:
        raise MaskError(f"run_start {run_start} is negative")
    if run_end is not None and run_end <= run_start:
        raise MaskError(f"the interval {_range(run_start, run_end)} is empty; run_end is "
                        f"EXCLUSIVE (to cover run N alone: --run-start N --last-run N)")
    if overlap not in (None, "replace", "union"):
        raise MaskError(f"overlap is {overlap!r}: None, 'replace' or 'union'")
    if overlap and not split:
        raise MaskError("--replace-mask and --union say what --split does with an overlapped "
                        "mask; they need --split")
    new = json.loads(json.dumps(table))
    notes: list[str] = []
    if table.get("schema") != SCHEMA or table.get("version") != SCHEMA_VERSION \
            or table.get("kind") != "parameter_set":
        raise MaskError(f"{TABLE} is not schema {SCHEMA} v{SCHEMA_VERSION}, parameter_set")

    if not any(t["tag"] == tag for t in new.get("tags", [])):
        if not tag_description:
            raise MaskError(f"{TABLE} has no tag '{tag}'; give --tag-description to create it "
                            f"(not as the default tag)")
        new.setdefault("tags", []).append(
            {"tag": tag, "is_default": False, "description": tag_description})
        notes.append(f"new tag '{tag}' (not default): {tag_description}")
    if tag in new.get("values", {}):
        raise MaskError(f"tag '{tag}' has a tag-wide payload ('values'); this tool writes "
                        f"per-interval payloads ('values_by_iov') and a tag holds one kind only")

    by_iov = new.setdefault("values_by_iov", {})
    next_id = max((int(r["row_id"]) for r in new["iov"]), default=0) + 1
    overlapping = sorted((r for r in new["iov"]
                          if r["tag"] == tag and r.get("is_active", True)
                          and r.get("run_start", 0) < _end(run_end)
                          and run_start < _end(r.get("run_end"))),
                         key=lambda r: r.get("run_start", 0))
    if overlapping and not split:
        raise MaskError(
            f"{_range(run_start, run_end)} overlaps active interval(s) of tag '{tag}': "
            + ", ".join(f"row {r['row_id']} {_range(r.get('run_start', 0), r.get('run_end'))}"
                        for r in overlapping)
            + ". Close or split them first: --split deactivates them and adds back their "
              "parts outside the new range with their own payload.")

    # The old masks, before anything is touched
    old_pixels: dict[int, list[Pixel]] = {}
    for old in overlapping:
        old_payload = by_iov.get(str(old["row_id"]))
        if old_payload is None:
            raise MaskError(f"row {old['row_id']} has no per-interval payload to carry over")
        try:
            old_pixels[old["row_id"]] = mask_pixels(old_payload)
        except MaskError as exc:
            raise MaskError(f"row {old['row_id']}: {exc}") from None
    masked = [r for r in overlapping if old_pixels[r["row_id"]]]
    if masked and overlap is None:
        raise MaskError(
            f"{_range(run_start, run_end)} overlaps interval(s) that already mask pixels: "
            + ", ".join(f"row {r['row_id']} {_range(r.get('run_start', 0), r.get('run_end'))} "
                        f"({len(old_pixels[r['row_id']])} pixel(s))" for r in masked)
            + ". --split alone would unmask them in the overlap. Say what to do: "
              "--union keeps them (old + new pixels in the overlap), --replace-mask drops "
              "them (the new pixels alone).")

    new_keys = {_key(p) for p in pixels}

    def replace_note(old: dict) -> list[str]:
        if overlap != "replace" or not old_pixels[old["row_id"]]:
            return []
        ov = _range(max(run_start, old.get("run_start", 0)), _min_end(run_end, old.get("run_end")))
        n_old = len(old_pixels[old["row_id"]])
        dropped = sum(1 for p in old_pixels[old["row_id"]] if _key(p) not in new_keys)
        return [f"row {old['row_id']}: --replace-mask drops {dropped} of its {n_old} "
                f"pixel(s) over {ov} (the new mask does not list them)"]

    added, next_id = carve_out(
        overlapping, by_iov, run_start, run_end, tag, created_by, next_id, notes, "mupix_mask",
        lambda old: f"{len(old_pixels[old['row_id']])} pixel(s)", replace_note)

    # The new range: one row, or with --union one per stretch whose merged
    # mask differs from its neighbour's.
    segments: list[tuple[int, int | None, list[Pixel], list[int]]] = []
    if overlap == "union":
        cursor: int | None = run_start
        for old in overlapping:
            lo = max(run_start, old.get("run_start", 0))
            hi = _min_end(run_end, old.get("run_end"))
            if cursor is not None and cursor < lo:
                segments.append((cursor, lo, pixels, []))
            segments.append((lo, hi, _union(pixels, old_pixels[old["row_id"]]),
                             [old["row_id"]] if old_pixels[old["row_id"]] else []))
            cursor = hi
        if cursor is not None and cursor < _end(run_end):
            segments.append((cursor, run_end, pixels, []))
        merged: list[tuple[int, int | None, list[Pixel], list[int]]] = []
        for seg in segments:
            if merged and merged[-1][1] == seg[0] and payload(merged[-1][2]) == payload(seg[2]):
                prev = merged[-1]
                merged[-1] = (prev[0], seg[1], prev[2], prev[3] + seg[3])
            else:
                merged.append(seg)
        segments = merged
    else:
        segments = [(run_start, run_end, pixels, [])]

    for lo, hi, seg_pixels, from_rows in segments:
        text = comment
        if from_rows:
            kept = len(seg_pixels) - len(pixels)
            text += (f" Union with the mask of row(s) {', '.join(map(str, from_rows))}: "
                     f"{kept} of their pixel(s) added.")
        row = {"row_id": next_id, "tag": tag, "run_start": lo, "run_end": hi,
               "is_active": True, "created_by": created_by, "comment": text}
        added.append(row)
        by_iov[str(next_id)] = payload(seg_pixels)
        notes.append(f"row {next_id} {_range(lo, hi)} added: {len(seg_pixels)} pixel(s)"
                     + (f" ({len(pixels)} new, union with row(s) "
                        f"{', '.join(map(str, from_rows))})" if from_rows else ""))
        next_id += 1
    new["iov"] += sorted(added, key=lambda r: r["row_id"])
    return new, notes


def carve_out(overlapping: list[dict], by_iov: dict, run_start: int, run_end: int | None,
              tag: str, created_by: str, next_id: int, notes: list[str], tool: str,
              size, extra_notes=None) -> tuple[list[dict], int]:
    """--split: deactivate each overlapped row and add back its parts outside
    [run_start, run_end) with a copy of its payload.

    The rows are edited in place (``is_active`` False, the comment gets
    "deactivated by <tool>: split around [a, b)"), the remnants' payloads go
    into ``by_iov`` and what happened into ``notes``. A remnant's comment is
    "Split from row N [a, b): <the original comment>", the original once
    however often it is split (original_comment()). ``size(old)`` describes
    an old payload ("3 pixel(s)"); ``extra_notes(old)``, if given, adds notes
    right after the deactivation. Returns the new rows and the next free
    row_id.
    """
    added: list[dict] = []
    for old in overlapping:
        o_lo, o_hi = old.get("run_start", 0), old.get("run_end")
        was = original_comment(old.get("comment", ""))
        old["is_active"] = False
        old["comment"] = (old.get("comment", "") + " | " if old.get("comment") else "") \
            + f"deactivated by {tool}: split around {_range(run_start, run_end)}"
        notes.append(f"row {old['row_id']} {_range(o_lo, o_hi)} deactivated")
        if extra_notes is not None:
            notes.extend(extra_notes(old))
        remnants = []
        if o_lo < run_start:
            remnants.append((o_lo, run_start))
        if run_end is not None and run_end < _end(o_hi):
            remnants.append((run_end, o_hi))
        for lo, hi in remnants:
            row = {"row_id": next_id, "tag": tag, "run_start": lo, "run_end": hi,
                   "is_active": True, "created_by": created_by,
                   "comment": f"Split from row {old['row_id']} {_range(o_lo, o_hi)}: {was}"}
            added.append(row)
            by_iov[str(next_id)] = json.loads(json.dumps(by_iov[str(old["row_id"])]))
            notes.append(f"row {next_id} {_range(lo, hi)} added, payload of row {old['row_id']} "
                         f"({size(old)})")
            next_id += 1
    return added, next_id


def _min_end(a: int | None, b: int | None) -> int | None:
    """The earlier of two exclusive ends, None being open."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _npix(rows: list, key: str = "n_pixels") -> int:
    return next((r["value"] for r in rows if r.get("key") == key), 0)


def describe(table: dict, count_key: str = "n_pixels", count_label: str = "pixels") -> str:
    """One line per interval: row, tag, runs, active, the payload's
    ``count_key`` (under the heading ``count_label``) and the comment."""
    lines = [f"{'row':>4} {'tag':<20} {'runs':<14} {'active':<6} {count_label:>6}  comment"]
    by_iov = table.get("values_by_iov", {})
    for r in table["iov"]:
        rows = by_iov.get(str(r["row_id"]), table.get("values", {}).get(r["tag"], []))
        comment = r.get("comment", "")
        lines.append(f"{r['row_id']:>4} {r['tag']:<20} {_range(r.get('run_start', 0), r.get('run_end')):<14} "
                     f"{'yes' if r.get('is_active', True) else 'no':<6} {_npix(rows, count_key):>6}  "
                     f"{comment[:70] + ('...' if len(comment) > 70 else '')}")
    return "\n".join(lines)


def pixel_table(pixels: list[Pixel]) -> str:
    lines = [f"{'vid':>6} {'raw':>4} {'col':>4} {'row':>4}  reason"]
    for p in pixels:
        lines.append(f"{p.vid:>6} {'-' if p.raw is None else p.raw:>4} {p.col:>4} {p.row:>4}  {p.reason}")
    per_chip: dict[int, int] = {}
    for p in pixels:
        per_chip[p.vid] = per_chip.get(p.vid, 0) + 1
    lines.append("per chip: " + (", ".join(f"{v}: {n}" for v, n in sorted(per_chip.items())) or "none"))
    return "\n".join(lines)


def dump(doc: dict) -> str:
    """The container as the repository keeps it: json.dumps(indent=2) + newline."""
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_add(args) -> int:
    src = open_source(args, TABLE, MASK_ARRAYS)
    doc = src.doc
    if TABLE not in doc:
        raise MaskError(f"{src.label} has no table {TABLE}")
    entries, kind, study_run, source = read_input(Path(args.input), args.chip_ids)
    map_run = args.map_run if args.map_run is not None else study_run
    pixels = to_pixels(entries, kind, doc, map_run)

    if args.last_run is not None:
        run_end = args.last_run + 1
    else:
        run_end = args.run_end
    tag = args.tag or select_tag(doc[TABLE], None, TABLE)
    comment = args.comment
    comment += f" Source: {source}" + (f", raw chip ids converted at run {map_run}"
                                        if kind == "raw" else "") + f"; {len(pixels)} pixel(s)."
    overlap = "union" if args.union else "replace" if args.replace_mask else None
    new_table, notes = plan_add(doc[TABLE], pixels, args.run_start, run_end, tag, comment,
                                args.created_by, args.split, args.tag_description, overlap)

    new_doc = dict(doc)
    new_doc[TABLE] = new_table
    problems = validate(new_doc)
    if problems:
        raise MaskError("the table after the change would fail the decoder:\n  "
                        + "\n  ".join(problems))
    print(f"{src.what:<10} {src.label}")
    print(f"table      {TABLE}, tag '{tag}'")
    print(f"input      {source} ({kind} chip ids{f', converted at run {map_run}' if kind == 'raw' else ''})")
    print()
    print("changes:")
    for n in notes:
        print(f"  {n}")
    print()
    print("intervals after:")
    print(describe(new_table))
    print()
    print(f"pixels of the new interval ({len(pixels)}):")
    print(pixel_table(pixels))
    after = show_diff(src, TABLE, doc, new_doc, args)
    print()
    if not args.write:
        print("dry run: nothing written. Add --write to apply.")
        return 0
    commit_write(src, TABLE, new_table, after, args.table_out)
    return 0


def cmd_show(args) -> int:
    src = open_source(args, TABLE, MASK_ARRAYS)
    if TABLE not in src.doc:
        raise MaskError(f"{src.label} has no table {TABLE}")
    table = src.doc[TABLE]
    print(f"{src.what:<10} {src.label}")
    print(describe(table))
    if args.run is None:
        return 0
    row, rows = resolve(table, TABLE, args.run, args.tag)
    pixels = mask_pixels(rows)
    print()
    print(f"run {args.run} reads row {row['row_id']} {_range(row.get('run_start', 0), row.get('run_end'))}"
          f" of tag '{row['tag']}': {len(pixels)} masked pixel(s)")
    if pixels:
        print(pixel_table(pixels))
    return 0


def cmd_check(args) -> int:
    src = open_source(args, TABLE, MASK_ARRAYS)
    problems = validate(src.doc)
    print(f"{src.what:<10} {src.label}")
    if problems:
        print(f"{len(problems)} problem(s); the decoder would fail on them:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"{TABLE}: every run resolves to exactly one interval of the default tag and every "
          f"active mask passes the decoder's rules")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pioneer.conddb.mupix_mask",
                                 description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="See the module docstring (pydoc pioneer.conddb.mupix_mask).")
    add_db_argument(ap)
    subs = ap.add_subparsers(dest="command", required=True)

    p = subs.add_parser("add", help="add an interval with the pixels of a study or a list")
    p.add_argument("input", help="noisy_pixels.json, noisy_pixels_runNNNNN.json, or a JSON/CSV list")
    p.add_argument("--run-start", type=int, required=True, help="first run of the interval")
    end = p.add_mutually_exclusive_group()
    end.add_argument("--last-run", type=int, help="last run of the interval, INCLUSIVE")
    end.add_argument("--run-end", type=int, help="end of the interval, EXCLUSIVE")
    p.add_argument("--tag", help="tag of the interval (default: the table's default tag)")
    p.add_argument("--tag-description",
                   help="create --tag with this description if it does not exist (never as default)")
    p.add_argument("--comment", required=True, help="why these pixels, for these runs")
    p.add_argument("--created-by", default=getpass.getuser())
    p.add_argument("--chip-ids", choices=("raw", "vid"),
                   help="what the chip number of a header-less list is")
    p.add_argument("--map-run", type=int,
                   help="run whose mupix_chip_map converts raw chip ids (default: the study's run)")
    p.add_argument("--split", action="store_true",
                   help="carve the interval out of overlapping active intervals of the tag")
    ov = p.add_mutually_exclusive_group()
    ov.add_argument("--union", action="store_true",
                    help="with --split: keep the pixels an overlapped interval already masks "
                         "(the new range gets old + new, split where the old masks differ)")
    ov.add_argument("--replace-mask", action="store_true",
                    help="with --split: drop the pixels an overlapped interval already masks "
                         "in the overlap (the new pixels alone hold there)")
    p.add_argument("--write", action="store_true", help="apply; without it nothing is written")
    p.add_argument("--table-out", metavar="PATH",
                   help="with --write, also write a container of this table alone for the DB loaders")
    p.add_argument("--no-diff", action="store_true", help="leave out the unified diff")

    p = subs.add_parser("show", help="the intervals, and with --run the pixels a run masks")
    p.add_argument("--run", type=int)
    p.add_argument("--tag")

    subs.add_parser("check", help="the decoder's rules over every run: no gap or overlap in "
                                  "the default tag, every mask valid against the chip map")

    args = ap.parse_args(db_argv(sys.argv[1:] if argv is None else list(argv)))
    try:
        return {"add": cmd_add, "show": cmd_show, "check": cmd_check}[args.command](args)
    except MaskError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
