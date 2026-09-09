#!/usr/bin/env python3
"""Load conditions JSON containers into a PostgreSQL conditions database.

Same schema and same JSON-to-cells mapping as json2sqlite.py (both call
cond_loader.py, so the two loaders cannot drift). SQL goes through psql: the
analysis container carries the postgres client but no python driver, and psql
with ON_ERROR_STOP is all a loader needs.

    python3 json2pg.py "host=testbeam-pgdb dbname=conditions user=postgres" a.json
    python3 json2pg.py --docker testbeam-pgdb a.json b.json ...

--docker runs psql inside the named container, which is how the host talks to
the sidecar started by start-conditions-db.sh without a local postgres client.

PostgreSQL is the campaign store, so the load is append-only: for every tag the
container touches, the constants the currently active intervals were serving
are pinned onto those intervals, the intervals are deactivated, and the
container's intervals are inserted with fresh row_ids. Nothing that a written
output file's ConditionsHeader points at is ever deleted or rewritten.
--replace does delete the table; it is for dev databases only.

A tag may be moved to being the table's default only with --set-default,
because that changes what every job without an explicit tag reads.

Passwords never go on the command line or into the output: put one in
PGPASSWORD or ~/.pgpass. Only the host/port/dbname tokens of a conninfo are
printed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cond_loader import LoaderError, make_executor, load


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        usage="%(prog)s (CONNINFO | --docker NAME) CONTAINER [CONTAINER ...] "
              "[--replace] [--set-default]")
    ap.add_argument("--docker", metavar="NAME",
                    help="run psql inside this docker container instead of "
                         "using a local psql and a conninfo")
    # One positional list rather than a mutually exclusive group: argparse
    # cannot decide on its own whether the first word is a conninfo or the
    # first container once --docker has taken the conninfo's place.
    ap.add_argument("words", nargs="+", metavar="ARG",
                    help='CONNINFO (omitted with --docker) followed by the '
                         'conditions JSON container files. A conninfo is a '
                         'libpq string such as "host=... dbname=..."; it must '
                         'not carry a password (use PGPASSWORD or ~/.pgpass)')
    ap.add_argument("--database", default="conditions",
                    help="database name for --docker (default: conditions)")
    ap.add_argument("--replace", action="store_true",
                    help="delete each named table first (dev databases only: "
                         "this discards the history already-written output files "
                         "point at)")
    ap.add_argument("--set-default", action="store_true",
                    help="allow the container to move the table's default tag")
    args = ap.parse_args()

    if args.docker:
        conninfo, containers = None, args.words
    else:
        conninfo, containers = args.words[0], args.words[1:]
        if not containers:
            ap.error("give a CONNINFO and at least one container file, "
                     "or use --docker NAME")

    if args.replace:
        print("warning: --replace deletes existing history; "
              "use it on dev databases only", file=sys.stderr)

    try:
        ex = make_executor(conninfo=conninfo, docker=args.docker,
                           database=args.database)
        loaded = load(ex, containers, replace=args.replace,
                      set_default=args.set_default)
    except LoaderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for path, name, ncells, first_row in loaded:
        print(f"{path}: loaded table '{name}' ({ncells} value cells, "
              f"iov row_ids from {first_row})")
    print(f"loaded into {ex.label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
