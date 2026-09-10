#!/usr/bin/env python3
"""Merge conditions JSON containers into one, combining same-name tables.

The single-table writers (make_rf_conditions.py, derive_wd_alignment.py, ...)
each emit one container; the committed payload is their union. Distinct table
names are simply collected. When the SAME table name appears in several inputs
(e.g. wd_rf with a fitted default tag in one file and a fiat psi-nominal tag
in another), the tables are merged: tags, intervals and per-tag payloads are
concatenated, which is the sanctioned way to carry competing constants for
the same runs.

    python3 merge_conditions.py out.json a.json b.json ...

Merge rules (violations are errors):
  - same-name tables must agree on schema, version and kind;
  - a tag name may appear in only one input per table;
  - at most one tag per merged table may be the default;
  - iov row_ids are renumbered 1..N per table (they must be unique per table,
    mirroring the DB primary key (table_name, row_id)); values_by_iov keys,
    which name row_ids, follow the renumbering.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def renumber(table: dict, offset: int) -> dict:
    """Give the iov rows row_id offset+1.., remapping values_by_iov keys alongside."""
    new = {}
    for i, row in enumerate(table["iov"], start=1):
        new[str(row.get("row_id", i))] = str(offset + i)
        row["row_id"] = offset + i
    if "values_by_iov" in table:
        table["values_by_iov"] = {new[k]: v for k, v in table["values_by_iov"].items()}
    return table


def merge_tables(name: str, parts: list[tuple[str, dict]]) -> dict:
    """Merge the same-named table from several (source, table) inputs."""
    src0, merged = parts[0]
    merged = renumber(json.loads(json.dumps(merged)), 0)  # deep copy
    for src, part in parts[1:]:
        for field in ("schema", "version", "kind"):
            if part[field] != merged[field]:
                raise SystemExit(
                    f"{name}: {field} differs between {src0} ({merged[field]!r}) "
                    f"and {src} ({part[field]!r})")
        have = {t["tag"] for t in merged["tags"]}
        for t in part["tags"]:
            if t["tag"] in have:
                raise SystemExit(f"{name}: tag {t['tag']!r} appears in both "
                                 f"{src0} and {src}")
        merged["tags"] += part["tags"]
        merged["iov"] += renumber(part, len(merged["iov"]))["iov"]
        for block in ("values", "values_by_iov"):
            if block in part:
                merged.setdefault(block, {}).update(part[block])

    defaults = [t["tag"] for t in merged["tags"] if t.get("is_default")]
    if len(defaults) > 1:
        raise SystemExit(f"{name}: more than one default tag: {defaults}")
    return merged


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("output", help="merged container JSON to write")
    ap.add_argument("inputs", nargs="+", help="container JSONs to merge")
    args = ap.parse_args()

    collected: dict[str, list[tuple[str, dict]]] = {}
    for src in args.inputs:
        container = json.loads(Path(src).read_text())
        for name, table in container.items():
            collected.setdefault(name, []).append((src, table))

    merged = {name: merge_tables(name, parts)
              for name, parts in collected.items()}
    Path(args.output).write_text(json.dumps(merged, indent=2) + "\n")
    for name, parts in collected.items():
        tags = ", ".join(t["tag"] for t in merged[name]["tags"])
        print(f"{name}: {len(parts)} input(s), tags [{tags}]")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
