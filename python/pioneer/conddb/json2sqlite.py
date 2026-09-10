#!/usr/bin/env python3
"""Load conditions JSON containers into a SQLite conditions database.

The relational schema is schema_sqlite.sql and the JSON-to-cells mapping is
cond_loader.py; this tool is argument parsing and a printout. The acceptance
criterion for the whole database path is that a table served from the .db and
from the source .json produce identical canonical dumps and sha256 hashes in
the ConditionsHeader.

    python3 json2sqlite.py out/conditions.db a.json b.json ...

Loading is append-only: the intervals a tag already had are pinned (a tag-wide
payload is copied onto them) and deactivated, and the container's intervals go
in with fresh row_ids. --replace empties the table first; that discards the
history the ConditionsHeader of already-produced files points at, so it is for
dev databases and throwaway fixtures only.

Tag-wide payloads (JSON ``values``) are stored with iov_row_id NULL;
per-interval payloads (JSON ``values_by_iov``, keyed by row_id) with
iov_row_id = the interval's row_id in the database.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cond_loader import LoaderError, make_executor, load


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("database", help="SQLite database to create/extend")
    ap.add_argument("containers", nargs="+", help="conditions JSON container files")
    ap.add_argument("--replace", action="store_true",
                    help="delete each named table first (dev databases only: "
                         "this discards the history already-written output files "
                         "point at)")
    args = ap.parse_args()

    if args.replace:
        print("warning: --replace deletes existing history; "
              "use it on dev databases only", file=sys.stderr)

    ex = make_executor(sqlite=args.database)
    try:
        loaded = load(ex, args.containers, replace=args.replace)
    except LoaderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        ex.close()

    for path, name, ncells, first_row in loaded:
        print(f"{path}: loaded table '{name}' ({ncells} value cells, "
              f"iov row_ids from {first_row})")
    print(f"wrote {args.database}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
