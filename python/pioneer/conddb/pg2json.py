#!/usr/bin/env python3
"""Export the conditions database into the five bt2026 JSON containers.

The inverse of json2pg.py. The database is where constants are written first
(mupix_mask.py / mupix_timewalk.py --db, json2pg.py); these files are its
snapshot: what git keeps, and what a job reads with --conditions json:DIR when
the database is down.

    python3 pg2json.py service=pioneer-conditions --out-dir DIR
    python3 pg2json.py service=pioneer-conditions --out-dir DIR --check
    python3 pg2json.py service=pioneer-conditions \\
        --out-dir $PIONEERSYS/reco_testbeam/conditions     # then git diff

Every table goes to a fixed file, the layout reco_testbeam/conditions has
(CONTAINERS below). A table a file names that the database lacks is an error
and nothing is written. A table of the database that no file names is not
exported (``condtool.py export`` writes any single table): a warning by
default, an error with ``--strict`` (nothing written, exit code 3; the
snapshot job runs with it), and a failure of ``--check`` either way, since
such an export cannot stand in for the database.

Only ACTIVE intervals are exported, renumbered 1..N (see cond_export.py).
Order, the scalar-or-array spelling of one-element arrays and a table's
free-text ``description`` are not in the database; they are taken from the
same file in ``--order-from DIR`` (default: the file already in --out-dir)
wherever it has the same tag, interval, channel or key. So exporting into the
git checkout changes only what the database changed. Files are written as
json.dumps(indent=2, ensure_ascii=False) plus a newline, the format the
writers keep.

--check loads the written files into an in-memory SQLite database through
cond_loader and compares, table by table, the tags and every active
interval's metadata and served cell set with the source database. Exit code
1 if anything differs.

Passwords never go on the command line: use ~/.pgpass or PGPASSWORD. Only
the service/host/port/dbname of the conninfo is printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    from . import cond_loader
    from .cond_export import (active_state, compare_states, executor_for, export_table,
                              list_tables)
    from .cond_loader import LoaderError
except ImportError:          # run as a script: python3 pg2json.py ...
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cond_loader
    from cond_export import (active_state, compare_states, executor_for, export_table,
                             list_tables)
    from cond_loader import LoaderError

#: file -> tables, in the order the file lists them (reco_testbeam/conditions).
CONTAINERS: dict[str, tuple[str, ...]] = {
    "bt2026_psm_channel_map.json": ("psm_channel_map",),
    "bt2026_psm_geometry.json": ("psm_geometry",),
    "bt2026_psm_readout_map.json": ("mupix_chip_map", "mutrig_channel_map",
                                    "sma_coarse_shift", "mupix_pixel_mask",
                                    "mupix_timewalk", "sma_time_alignment",
                                    "sma_rf"),
    "bt2026_wavedream_calibration.json": ("wd_rf", "wd_time_alignment",
                                          "wd_energy_calibration", "wd_channel_map"),
    "bt2026_wavedream_timebase.json": ("wd_timebase",),
}

#: Parallel-array keys that stay JSON arrays even when they hold one element,
#: so that a first export with no --order-from still reads back in the
#: writers (mupix_mask.MASK_ARRAYS, mupix_timewalk.ARRAYS + comment; a test
#: keeps the two in step).
LIST_KEYS: dict[str, tuple[str, ...]] = {
    "mupix_pixel_mask": ("vid", "col", "row", "reason"),
    "mupix_timewalk": ("vid", "form", "p0", "p1", "p2", "tot_min", "tot_max", "comment"),
}

#: Exit code of --strict when the database has a table no file names.
EXIT_UNMAPPED = 3


def dump(doc: dict) -> str:
    """The container as the repository keeps it (mupix_mask.dump)."""
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _hint(order_dir: Path | None, fname: str) -> dict:
    if order_dir is None or not (order_dir / fname).is_file():
        return {}
    try:
        return json.loads((order_dir / fname).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoaderError(f"--order-from {order_dir / fname}: {exc}") from None


def export_all(ex, order_dir: Path | None) -> tuple[dict[str, str], list[str]]:
    """({file: text}, [tables in the database no file names])."""
    have = set(list_tables(ex))
    missing = [t for tables in CONTAINERS.values() for t in tables if t not in have]
    if missing:
        raise LoaderError(f"{ex.label}: no table {', '.join(missing)}; the containers "
                          f"would be incomplete, nothing written")
    texts: dict[str, str] = {}
    for fname, tables in CONTAINERS.items():
        hint = _hint(order_dir, fname)
        texts[fname] = dump({t: export_table(ex, t, order_from=hint.get(t),
                                             list_keys=LIST_KEYS.get(t, ()))
                             for t in tables})
    named = {t for tables in CONTAINERS.values() for t in tables}
    return texts, sorted(have - named)


def _write(path: Path, text: str) -> bool:
    """Write atomically; False when the file already holds exactly ``text``."""
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def _unmapped_message(t: str) -> str:
    return (f"table '{t}' is in the database but in no bt2026 container, so the export "
            f"does not hold it (add it to pg2json.CONTAINERS; condtool.py export {t} "
            f"--out FILE writes it alone)")


def check(ex, paths: list[Path]) -> list[str]:
    """Load ``paths`` into in-memory SQLite and compare with ``ex``, table by table.
    A table of the database that no file names is a difference too: the
    export cannot stand in for the database without it."""
    mem = cond_loader.make_executor(sqlite=":memory:")
    try:
        cond_loader.load(mem, paths)
        named = {t for tables in CONTAINERS.values() for t in tables}
        problems: list[str] = [_unmapped_message(t) for t in list_tables(ex)
                               if t not in named]
        for tables in CONTAINERS.values():
            for t in tables:
                problems += compare_states(t, active_state(ex, t), active_state(mem, t))
        return problems
    finally:
        mem.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("conninfo", metavar="CONNINFO",
                    help='libpq conninfo, e.g. "service=pioneer-conditions" (no password; '
                         'use ~/.pgpass), or sqlite:PATH')
    ap.add_argument("--out-dir", required=True, type=Path, help="directory to write into")
    ap.add_argument("--order-from", type=Path, metavar="DIR",
                    help="containers to take order and spelling from (default: --out-dir)")
    ap.add_argument("--check", action="store_true",
                    help="load the written files into in-memory SQLite and compare with "
                         "the database (a table no file names fails it)")
    ap.add_argument("--strict", action="store_true",
                    help=f"a table of the database that no file names is an error: nothing "
                         f"is written and the exit code is {EXIT_UNMAPPED} (for snapshots)")
    args = ap.parse_args(argv)

    out_dir = args.out_dir
    if not out_dir.is_dir():
        print(f"error: --out-dir {out_dir} is not a directory", file=sys.stderr)
        return 1
    order_dir = args.order_from if args.order_from is not None else out_dir
    try:
        ex = executor_for(args.conninfo)
        texts, unnamed = export_all(ex, order_dir)
        if unnamed and args.strict:
            for t in unnamed:
                print(f"error: {_unmapped_message(t)}", file=sys.stderr)
            print(f"error: --strict: {len(unnamed)} table(s) would be missing from the "
                  f"export; nothing written", file=sys.stderr)
            return EXIT_UNMAPPED
        for fname, text in texts.items():
            changed = _write(out_dir / fname, text)
            print(f"{out_dir / fname}: {'written' if changed else 'unchanged'}")
        for t in unnamed:
            print(f"warning: {_unmapped_message(t)}", file=sys.stderr)
        print(f"exported from {ex.label}")
        if args.check:
            problems = check(ex, [out_dir / f for f in texts])
            if problems:
                print(f"check FAILED, {len(problems)} difference(s):", file=sys.stderr)
                for p in problems:
                    print(f"  {p}", file=sys.stderr)
                return 1
            n = sum(len(t) for t in CONTAINERS.values())
            print(f"check: the {len(texts)} files load into SQLite and serve the same "
                  f"tags and active intervals as the database, cell for cell ({n} tables)")
    except LoaderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
