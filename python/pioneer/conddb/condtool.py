#!/usr/bin/env python3
"""Inspect and edit the interval bookkeeping of a conditions database.

The loaders write containers; this is the other half of the operational job:
"what will run N read?", "which intervals are still open?", "close this
open-ended calibration at run M", "retire that row". It speaks to SQLite, to a
PostgreSQL server over a conninfo, and to the sidecar container over
docker exec, through the same executors the loaders use.

    python3 condtool.py --docker testbeam-pgdb tables
    python3 condtool.py --docker testbeam-pgdb tags wd_align
    python3 condtool.py --sqlite out/conditions.db iov wd_align --all
    python3 condtool.py --docker testbeam-pgdb resolve wd_align --run 193
    python3 condtool.py --docker testbeam-pgdb close wd_align --row-id 3 --run-end 300
    python3 condtool.py --docker testbeam-pgdb deactivate wd_align --row-id 3 \
        --comment "wrong cable map"

``resolve`` reproduces PICondIov.h exactly -- explicit tag or the unique
default tag, that tag's active rows only, half-open [run_start, run_end), and
zero or several survivors is an error, never a "most recent wins" guess -- so
that the answer here is the answer the service will give. It also says where
the payload comes from (the interval's own rows, or the tag-wide rows) and how
many cells it has, which is what tells an operator whether a load pinned what
they expected.

The two editing subcommands are append-only in the same sense as the loaders:
``close`` never edits the interval it closes, it deactivates it and inserts a
closed copy with a fresh row_id and a copy of its per-interval payload;
``deactivate`` changes is_active and appends to the comment. Overlap is left
to the database (the EXCLUDE constraint on PostgreSQL, the triggers on
SQLite), which is the only place that sees concurrent writers.

Passwords go in PGPASSWORD or ~/.pgpass, never in a conninfo on the command
line.
"""
from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cond_loader import LoaderError, lit, make_executor, schema_version

OPEN_END = 2147483647   # the sentinel the C++ layers use for an open interval


def _bool(cell) -> bool:
    return cell in ("1", "t", "true", "True")


def _stamp(cell) -> str:
    """An epoch-seconds column as a readable UTC time."""
    try:
        seconds = int(cell)
    except (TypeError, ValueError):
        return "?"
    if seconds <= 0:
        return "-"
    return datetime.datetime.fromtimestamp(
        seconds, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _range(run_start, run_end) -> str:
    return f"[{run_start}, {run_end if run_end is not None else 'open'})"


def _scalar(ex, sql, default=0):
    rows = ex.query(sql)
    if not rows or rows[0][0] == "":
        return default
    return rows[0][0]


def _require_table(ex, table: str) -> None:
    if not ex.query(f"SELECT name FROM cond_tables WHERE name = {lit(table, ex.dialect)};"):
        raise LoaderError(f"no table '{table}' in {ex.label}")


def read_tags(ex, table: str) -> list[dict]:
    return [{"tag": r[0], "is_default": _bool(r[1]), "description": r[2] or ""}
            for r in ex.query(
                f"SELECT tag, is_default, description FROM cond_tags "
                f"WHERE table_name = {lit(table, ex.dialect)} ORDER BY tag;")]


def read_iov(ex, table: str, active_only: bool) -> list[dict]:
    name = lit(table, ex.dialect)
    where = "" if not active_only else f" AND is_active = {lit(True, ex.dialect)}"
    rows = []
    # (run_end IS NULL) as its own column because an executor hands back text
    # and an open-ended interval must not read as run_end 0: psql prints SQL
    # NULL and the empty string identically.
    for r in ex.query(f"SELECT row_id, tag, run_start, (run_end IS NULL), "
                      f"COALESCE(run_end, 0), is_active, "
                      f"inserted_at, created_by, comment FROM cond_iov "
                      f"WHERE table_name = {name}{where} ORDER BY row_id;"):
        rows.append({"row_id": int(r[0]), "tag": r[1], "run_start": int(r[2]),
                     "run_end": None if _bool(r[3]) else int(r[4]),
                     "is_active": _bool(r[5]), "inserted_at": r[6],
                     "created_by": r[7] or "", "comment": r[8] or ""})
    return rows


def payload_of(ex, table: str, row: dict) -> tuple[str, int]:
    """(origin, cells) for one interval: its own payload, or the tag-wide one."""
    name = lit(table, ex.dialect)
    own = int(_scalar(ex, f"SELECT COUNT(*) FROM cond_values WHERE table_name = {name} "
                          f"AND iov_row_id = {row['row_id']};"))
    if own:
        return "per-interval", own
    wide = int(_scalar(ex, f"SELECT COUNT(*) FROM cond_values WHERE table_name = {name} "
                           f"AND tag = {lit(row['tag'], ex.dialect)} "
                           f"AND iov_row_id IS NULL;"))
    return "tag-wide", wide


def select_tag(tags: list[dict], requested: str | None) -> str:
    """PICondIov.h SelectTag: the requested tag, or the unique default."""
    names = ", ".join(t["tag"] for t in tags) or "none"
    if requested:
        if not any(t["tag"] == requested for t in tags):
            raise LoaderError(f"no tag '{requested}' (available: {names})")
        return requested
    defaults = [t["tag"] for t in tags if t["is_default"]]
    if len(defaults) > 1:
        raise LoaderError(f"more than one tag is marked default ({', '.join(defaults)})")
    if not defaults:
        raise LoaderError(f"no tag requested and none is marked default "
                          f"(available: {names})")
    return defaults[0]


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_tables(ex, args) -> int:
    rows = ex.query("SELECT name, schema, version, kind FROM cond_tables ORDER BY name;")
    if not rows:
        print(f"{ex.label}: no tables")
        return 0
    print(f"{ex.label} (schema version {schema_version(ex)})")
    print(f"{'table':<24} {'schema':<20} {'ver':>3} {'kind':<15} {'tags':>4} "
          f"{'iov':>4} {'active':>6} {'cells':>8}")
    for name, schema, version, kind in rows:
        n = lit(name, ex.dialect)
        true = lit(True, ex.dialect)
        print(f"{name:<24} {schema:<20} {int(version):>3} {kind:<15} "
              f"{_scalar(ex, f'SELECT COUNT(*) FROM cond_tags WHERE table_name = {n};'):>4} "
              f"{_scalar(ex, f'SELECT COUNT(*) FROM cond_iov WHERE table_name = {n};'):>4} "
              f"{_scalar(ex, f'SELECT COUNT(*) FROM cond_iov WHERE table_name = {n} AND is_active = {true};'):>6} "
              f"{_scalar(ex, f'SELECT COUNT(*) FROM cond_values WHERE table_name = {n};'):>8}")
    return 0


def cmd_tags(ex, args) -> int:
    _require_table(ex, args.table)
    tags = read_tags(ex, args.table)
    if not tags:
        print(f"{args.table}: no tags")
        return 0
    print(f"{'tag':<20} {'default':<8} {'active':>6} {'total':>6}  description")
    for t in tags:
        n, g = lit(args.table, ex.dialect), lit(t["tag"], ex.dialect)
        true = lit(True, ex.dialect)
        print(f"{t['tag']:<20} {'yes' if t['is_default'] else '':<8} "
              f"{_scalar(ex, f'SELECT COUNT(*) FROM cond_iov WHERE table_name = {n} AND tag = {g} AND is_active = {true};'):>6} "
              f"{_scalar(ex, f'SELECT COUNT(*) FROM cond_iov WHERE table_name = {n} AND tag = {g};'):>6}  "
              f"{t['description']}")
    return 0


def cmd_iov(ex, args) -> int:
    _require_table(ex, args.table)
    rows = read_iov(ex, args.table, active_only=not args.all)
    if not rows:
        print(f"{args.table}: no {'' if args.all else 'active '}intervals")
        return 0
    print(f"{'row':>5} {'tag':<16} {'runs':<18} {'act':<4} {'payload':<13} "
          f"{'cells':>7} {'loaded':<21} {'by':<12} comment")
    for row in rows:
        origin, cells = payload_of(ex, args.table, row)
        print(f"{row['row_id']:>5} {row['tag']:<16} {_range(row['run_start'], row['run_end']):<18} "
              f"{'yes' if row['is_active'] else 'no':<4} {origin:<13} {cells:>7} "
              f"{_stamp(row['inserted_at']):<21} {row['created_by'][:12]:<12} {row['comment']}")
    return 0


def cmd_resolve(ex, args) -> int:
    _require_table(ex, args.table)
    tags = read_tags(ex, args.table)
    tag = select_tag(tags, args.tag)
    covering = [r for r in read_iov(ex, args.table, active_only=True)
                if r["tag"] == tag
                and r["run_start"] <= args.run < (r["run_end"] if r["run_end"] is not None
                                                  else OPEN_END)]
    if not covering:
        raise LoaderError(f"no active interval of tag '{tag}' covers run {args.run}")
    if len(covering) > 1:
        raise LoaderError(f"run {args.run} is covered by {len(covering)} active intervals "
                          f"of tag '{tag}' (rows "
                          f"{' '.join(str(r['row_id']) for r in covering)})")
    row = covering[0]
    origin, cells = payload_of(ex, args.table, row)
    print(f"table    {args.table}")
    print(f"tag      {tag}" + ("" if args.tag else "  (default)"))
    print(f"run      {args.run}")
    print(f"row_id   {row['row_id']}")
    print(f"runs     {_range(row['run_start'], row['run_end'])}")
    print(f"payload  {origin}, {cells} cells")
    print(f"loaded   {_stamp(row['inserted_at'])} by {row['created_by'] or '-'}")
    print(f"comment  {row['comment'] or '-'}")
    return 0


def cmd_close(ex, args) -> int:
    """Give an open-ended (or too-long) interval an end, without editing it."""
    _require_table(ex, args.table)
    name, true, false = lit(args.table, ex.dialect), lit(True, ex.dialect), lit(False, ex.dialect)
    rows = [r for r in read_iov(ex, args.table, active_only=False)
            if r["row_id"] == args.row_id]
    if not rows:
        raise LoaderError(f"{args.table}: no interval with row_id {args.row_id}")
    row = rows[0]
    if not row["is_active"]:
        raise LoaderError(f"{args.table}: row {args.row_id} is already inactive")
    if args.run_end <= row["run_start"]:
        raise LoaderError(f"--run-end {args.run_end} is not after the interval's "
                          f"run_start {row['run_start']}")
    if row["run_end"] is not None and args.run_end >= row["run_end"]:
        raise LoaderError(f"--run-end {args.run_end} does not shorten "
                          f"{_range(row['run_start'], row['run_end'])}")

    new_id = int(_scalar(ex, f"SELECT COALESCE(MAX(row_id), 0) FROM cond_iov "
                             f"WHERE table_name = {name};")) + 1
    comment = f"closed from row {args.row_id}"
    if args.comment:
        comment += f": {args.comment}"
    # Deactivate first: the closed copy overlaps the original until it is gone.
    # The payload copy is safe as a self-reference (it reads iov_row_id = R and
    # writes iov_row_id = new_id, so it can never read back what it wrote).
    sql = [
        "BEGIN;",
        f"UPDATE cond_iov SET is_active = {false} WHERE table_name = {name} "
        f"AND row_id = {args.row_id};",
        f"INSERT INTO cond_iov (table_name, row_id, tag, run_start, run_end, is_active, comment) "
        f"VALUES ({name}, {new_id}, {lit(row['tag'], ex.dialect)}, {row['run_start']}, "
        f"{args.run_end}, {true}, {lit(comment, ex.dialect)});",
        f"INSERT INTO cond_values (table_name, tag, iov_row_id, channel_id, key, "
        f"column_name, ordinal, value_type, value_int, value_real, value_text)\n"
        f"    SELECT table_name, tag, {new_id}, channel_id, key, column_name, ordinal, "
        f"value_type, value_int, value_real, value_text FROM cond_values\n"
        f"     WHERE table_name = {name} AND iov_row_id = {args.row_id};",
        "COMMIT;",
    ]
    ex.script("\n".join(sql))
    print(f"{args.table}: row {args.row_id} "
          f"{_range(row['run_start'], row['run_end'])} deactivated; "
          f"row {new_id} {_range(row['run_start'], args.run_end)} is active")
    return 0


def cmd_deactivate(ex, args) -> int:
    _require_table(ex, args.table)
    name = lit(args.table, ex.dialect)
    rows = [r for r in read_iov(ex, args.table, active_only=False)
            if r["row_id"] == args.row_id]
    if not rows:
        raise LoaderError(f"{args.table}: no interval with row_id {args.row_id}")
    row = rows[0]
    if not row["is_active"]:
        raise LoaderError(f"{args.table}: row {args.row_id} is already inactive")
    comment = (row["comment"] + " | " if row["comment"] else "") \
        + f"deactivated: {args.comment}"
    ex.script(
        "BEGIN;\n"
        f"UPDATE cond_iov SET is_active = {lit(False, ex.dialect)}, "
        f"comment = {lit(comment, ex.dialect)} "
        f"WHERE table_name = {name} AND row_id = {args.row_id};\n"
        "COMMIT;")
    print(f"{args.table}: row {args.row_id} "
          f"{_range(row['run_start'], row['run_end'])} deactivated")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    backend = ap.add_mutually_exclusive_group(required=True)
    backend.add_argument("--sqlite", metavar="FILE", help="SQLite conditions database")
    backend.add_argument("--conninfo", metavar="STR",
                         help='libpq connection string (no password: use PGPASSWORD)')
    backend.add_argument("--docker", metavar="NAME",
                         help="run psql inside this docker container")
    ap.add_argument("--database", default="conditions",
                    help="database name for --docker (default: conditions)")

    subs = ap.add_subparsers(dest="command", required=True)
    subs.add_parser("tables", help="every table, with tag/interval/cell counts")

    p = subs.add_parser("tags", help="the tags of one table")
    p.add_argument("table")

    p = subs.add_parser("iov", help="the intervals of one table")
    p.add_argument("table")
    p.add_argument("--all", action="store_true",
                   help="include retired intervals (default: active only)")

    p = subs.add_parser("resolve", help="what a run will read, by PICondIov.h rules")
    p.add_argument("table")
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--tag", help="explicit tag (default: the table's default tag)")

    p = subs.add_parser("close", help="end an interval at a run, append-only")
    p.add_argument("table")
    p.add_argument("--row-id", type=int, required=True)
    p.add_argument("--run-end", type=int, required=True,
                   help="EXCLUSIVE end run for the closed copy")
    p.add_argument("--comment", default="", help="why it was closed")

    p = subs.add_parser("deactivate", help="retire an interval")
    p.add_argument("table")
    p.add_argument("--row-id", type=int, required=True)
    p.add_argument("--comment", required=True, help="why it was retired")

    args = ap.parse_args()
    handlers = {"tables": cmd_tables, "tags": cmd_tags, "iov": cmd_iov,
                "resolve": cmd_resolve, "close": cmd_close,
                "deactivate": cmd_deactivate}
    try:
        ex = make_executor(sqlite=args.sqlite, conninfo=args.conninfo,
                           docker=args.docker, database=args.database)
        version = schema_version(ex)
        if version != 2:
            raise LoaderError(f"{ex.label}: conditions schema version {version}, "
                              f"this tool reads version 2")
        return handlers[args.command](ex, args)
    except LoaderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
