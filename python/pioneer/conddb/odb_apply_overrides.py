#!/usr/bin/env python3
"""Rebuild the corrected ODB tree the conditions service used from the raw dump.

The ODB conditions layer applies ``odb_overrides`` rows to a private copy of
the begin-of-run ODB dump before mapping it onto tables; the ``ODBHeader``
written to the output file stays the raw dump. This module implements the same
rules in Python so that a reader can reproduce the corrected tree from two
things that are both in the output file: the raw ODB JSON and the canonical
payload of the ``odb_overrides`` table stamped into the ConditionsHeader.

Rules (one node at a time, after grouping rows by node):

* a row's key is an absolute ODB path; segments match exactly, else by a
  unique case-insensitive match; ``X/key`` entries never match;
* **replace**: the value is written in the dump's own spelling for the node's
  MIDAS type id (int for BYTE/SBYTE/SHORT/INT/INT64, ``"0x…"`` of the existing
  width for WORD/DWORD/BITS/QWORD, number for FLOAT/DOUBLE, bool, string); a
  value of the wrong kind is an error, nothing is coerced;
* **single element**: a row with an ``ordinal`` replaces that element of an
  array node (``num_values`` required, ``ordinal < num_values``);
* **whole array**: a list value must match ``num_values`` unless a companion
  ``<path>/key/num_values`` row resizes the node;
* **delete**: a ``null`` value removes the node and its ``/key``;
* **add**: a path whose last segment does not exist is created; a companion
  ``<path>/key/type`` row gives it MIDAS metadata, without one it has none;
* only ``<path>/key/type`` and ``<path>/key/num_values`` may be edited under
  ``/key``; ``/Runinfo/Run number`` can never be changed.

Two input shapes are accepted for the rows: the JSON conditions-file shape
(``{"key": path, "value": v[, "ordinal": n]}``) and the canonical-dump lines
of the ConditionsHeader (``<key>\\tvalue[<ordinal>]\\t<canonical scalar>``).

    python3 odb_apply_overrides.py odb.json overrides.json --run 4711 [--tag T]
    python3 odb_apply_overrides.py odb.json --canonical payload.txt

Prints the corrected tree as JSON. Exit code 1 on any rule violation.
"""

import argparse
import json
import sys

INT_TIDS = {1: (0, 255), 2: (-128, 127), 5: (-32768, 32767),
            7: (-2**31, 2**31 - 1), 17: (-2**63, 2**63 - 1)}
HEX_TIDS = {4: (0, 0xffff, 4), 6: (0, 0xffffffff, 8), 11: (0, 0xffffffff, 8),
            18: (0, 2**63 - 1, 16)}
FLOAT_TIDS = {9, 10}
BOOL_TID = 8
STRING_TIDS = {3, 12}

TID_NAMES = {1: "BYTE", 2: "SBYTE", 3: "CHAR", 4: "WORD", 5: "SHORT", 6: "DWORD", 7: "INT",
             8: "BOOL", 9: "FLOAT", 10: "DOUBLE", 11: "BITS", 12: "STRING", 17: "INT64",
             18: "QWORD"}


__all__ = ["OverrideError", "apply_overrides", "rows_from_conditions",
           "rows_from_canonical", "resolve", "exists", "split_path",
           "TID_NAMES", "main"]


class OverrideError(Exception):
    """A row that the rules reject; the message names path, type and value."""


def tid_name(tid):
    return TID_NAMES.get(tid, f"tid {tid}")


# ---- path walk ----------------------------------------------------------------

def split_path(path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise OverrideError(f"path {path!r} does not start with '/'")
    if path == "/":
        return []
    segs = path[1:].split("/")
    if any(s == "" for s in segs):
        raise OverrideError(f"path {path!r} has an empty segment")
    return segs


def match_segment(node, seg, parent_path):
    """The spelling of ``seg`` in ``node``: exact, else unique case-insensitive."""
    if seg in node and not seg.endswith("/key"):
        return seg
    hits = [k for k in node if not k.endswith("/key") and k.lower() == seg.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        avail = sorted(k for k in node if not k.endswith("/key"))
        raise OverrideError(f"no key {seg!r} under {parent_path!r} (available: {', '.join(avail) or '<none>'})")
    raise OverrideError(f"key {seg!r} under {parent_path!r} is ambiguous case-insensitively ({', '.join(sorted(hits))})")


def walk(root, segs):
    """(parent dict, spelling, canonical path) of the node at ``segs``."""
    cur, parent, name, path = root, None, None, ""
    for seg in segs:
        if not isinstance(cur, dict):
            raise OverrideError(f"{path or '/'!r} is a value, not a directory, so it has no key {seg!r}")
        found = match_segment(cur, seg, path or "/")
        parent, name = cur, found
        cur = cur[found]
        path += "/" + found
    return parent, name, (path or "/")


def resolve(root, path):
    """The value at ``path`` (canonical matching). Raises OverrideError when absent."""
    parent, name, _ = walk(root, split_path(path))
    return root if parent is None else parent[name]


def exists(root, path):
    try:
        resolve(root, path)
        return True
    except OverrideError:
        return False


def meta_of(parent, name):
    m = parent.get(name + "/key") if parent is not None else None
    return m if isinstance(m, dict) else None


# ---- value conversion --------------------------------------------------------

def scalar_kind(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "double"
    return "string"


def to_dump_spelling(tid, value, existing, path):
    """Convert an override scalar into the JSON the dump uses for ``tid``."""
    kind = scalar_kind(value)
    name = f"{tid_name(tid)} ({tid})"
    if tid in INT_TIDS or tid in HEX_TIDS:
        if kind != "int":
            raise OverrideError(f"{path}: MIDAS type {name} cannot take a {kind} value; nothing is coerced")
        lo, hi = (INT_TIDS.get(tid) or HEX_TIDS[tid][:2])
        if not lo <= value <= hi:
            raise OverrideError(f"{path}: {value} does not fit MIDAS type {name}")
        if tid in INT_TIDS or (isinstance(existing, int) and not isinstance(existing, bool)):
            return int(value)
        width = HEX_TIDS[tid][2]
        if isinstance(existing, str) and existing[:2].lower() == "0x":
            width = len(existing) - 2
        return f"0x{value:0{width}x}"
    if tid in FLOAT_TIDS:
        if kind not in ("int", "double"):
            raise OverrideError(f"{path}: MIDAS type {name} cannot take a {kind} value; nothing is coerced")
        return float(value)
    if tid == BOOL_TID:
        if kind != "bool":
            raise OverrideError(f"{path}: MIDAS type {name} cannot take a {kind} value; nothing is coerced")
        return bool(value)
    if tid in STRING_TIDS:
        if kind != "string":
            raise OverrideError(f"{path}: MIDAS type {name} cannot take a {kind} value; nothing is coerced")
        return str(value)
    raise OverrideError(f"{path}: unsupported MIDAS type id {tid}")


# ---- rows -> edits -----------------------------------------------------------

def rows_from_conditions(table, run, tag=None):
    """The override rows of a JSON conditions table for ``run``.

    Selects the tag (explicit, else the default) and the single active
    interval covering ``run``; returns its rows, or [] when none covers it.
    """
    tags = table.get("tags", [])
    if tag is None:
        defaults = [t["tag"] for t in tags if t.get("is_default")]
        if len(defaults) != 1:
            raise OverrideError("no unique default tag in the override table")
        tag = defaults[0]
    covering = [i for i in table["iov"]
                if i["tag"] == tag and i.get("is_active", True)
                and i.get("run_start", 0) <= run < (i.get("run_end") or float("inf"))]
    if not covering:
        return []
    if len(covering) > 1:
        raise OverrideError(f"run {run} is covered by {len(covering)} intervals of tag {tag!r}")
    iov = covering[0]
    by_iov = table.get("values_by_iov", {})
    key = str(iov.get("row_id", 0))
    if key in by_iov:
        return by_iov[key]
    return table.get("values", {}).get(tag, [])


def rows_from_canonical(text):
    """Override rows from the canonical dump stamped in the ConditionsHeader.

    Lines are ``<key>\\t<column>[<ordinal>]\\t<value>`` after a ``name=value``
    header block; the value is a canonical scalar (``null``, ``true``,
    ``false``, an integer, a shortest-round-trip double, or a quoted string).
    Several ordinals of one key are gathered back into a list.
    """
    cells = {}
    order = []
    for line in text.splitlines():
        if "\t" not in line:
            continue
        key, column, value = line.split("\t", 2)
        col, ordinal = column[:-1].split("[")
        if col != "value":
            continue
        if key not in cells:
            order.append(key)
            cells[key] = {}
        cells[key][int(ordinal)] = json.loads(value)
    rows = []
    for key in order:
        ords = cells[key]
        if len(ords) == 1:
            (ordinal, value), = ords.items()
            row = {"key": key, "value": value}
            if ordinal != 0:
                row["ordinal"] = ordinal
            rows.append(row)
        else:
            if sorted(ords) != list(range(len(ords))):
                raise OverrideError(f"{key}: array cells must carry ordinals 0..{len(ords) - 1}")
            rows.append({"key": key, "value": [ords[i] for i in range(len(ords))]})
    return rows


def apply_overrides(tree, rows):
    """Apply ``rows`` to ``tree`` in place. Returns the touched canonical paths."""
    groups = {}
    order = []
    for r, row in enumerate(rows, start=1):
        where = f"odb_overrides row {r} ({row.get('key')!r})"
        try:
            segs = split_path(row.get("key"))
        except OverrideError as e:
            raise OverrideError(f"{where}: {e}") from None
        if not segs:
            raise OverrideError(f"{where}: the root cannot be overridden")
        edit = {"row": r}
        if segs[-1] == "key":
            raise OverrideError(f"{where}: only <path>/key/type and <path>/key/num_values may be edited under /key")
        if len(segs) >= 2 and segs[-2] == "key":
            field = segs[-1]
            if field not in ("type", "num_values"):
                raise OverrideError(f"{where}: only <path>/key/type and <path>/key/num_values may be edited under /key")
            segs = segs[:-2]
            if not segs:
                raise OverrideError(f"{where}: the root has no /key")
            value = row.get("value")
            if scalar_kind(value) != "int":
                raise OverrideError(f"{where}: /key/{field} takes exactly one integer value")
            edit.update(kind="meta_" + field, value=value)
        else:
            value = row.get("value")
            if value is None:
                edit.update(kind="delete")
            elif isinstance(value, list):
                if any(v is None for v in value):
                    raise OverrideError(f"{where}: an array element cannot be null")
                edit.update(kind="array", values=list(value))
            else:
                edit.update(kind="one", value=value, ordinal=int(row.get("ordinal", 0)))
        # Canonical grouping key.
        try:
            _, _, canonical = walk(tree, segs)
        except OverrideError:
            try:
                _, _, parent_path = walk(tree, segs[:-1])
                canonical = ("" if parent_path == "/" else parent_path) + "/" + segs[-1]
            except OverrideError:
                canonical = "/" + "/".join(segs)
        if canonical.lower() == "/runinfo/run number":
            raise OverrideError(f"{where}: /Runinfo/Run number may not be overridden")
        if canonical not in groups:
            order.append(canonical)
            groups[canonical] = []
        groups[canonical].append(edit)

    touched = []
    for canonical in order:
        edits = groups[canonical]
        where = f"odb_overrides {canonical!r}"
        by_kind = {}
        for e in edits:
            k = "value" if e["kind"] in ("one", "array") else e["kind"]
            if k in by_kind:
                raise OverrideError(f"{where}: {k} given twice for one node")
            by_kind[k] = e
        delete = by_kind.get("delete")
        if delete and len(by_kind) > 1:
            raise OverrideError(f"{where}: a delete cannot be combined with other edits of the same node")
        segs = split_path(canonical)
        try:
            parent, name, path = walk(tree, segs)
            existing = True
        except OverrideError as absent:
            existing = False
            absent_why = str(absent)

        if delete:
            if not existing:
                raise OverrideError(f"{where}: nothing to delete ({absent_why})")
            del parent[name]
            parent.pop(name + "/key", None)
            touched.append(path)
            continue

        meta_type = by_kind.get("meta_type")
        meta_num = by_kind.get("meta_num_values")
        value_edit = by_kind.get("value")

        if not existing:
            try:
                gp, gname, parent_path = walk(tree, segs[:-1])
            except OverrideError as e:
                raise OverrideError(f"{where}: cannot add, {e}") from None
            parent = tree if gp is None else gp[gname]
            if not isinstance(parent, dict):
                raise OverrideError(f"{where}: cannot add under {parent_path!r}, which is a value, not a directory")
            leaf = segs[-1]
            if leaf.endswith("/key"):
                raise OverrideError(f"{where}: a node name may not end in /key")
            if not value_edit:
                raise OverrideError(f"{where}: the node does not exist; a value row is needed to add it")
            new_path = ("" if parent_path == "/" else parent_path) + "/" + leaf
            is_array = value_edit["kind"] == "array" or (
                meta_num is not None and value_edit["kind"] == "one" and value_edit["ordinal"] == 0)
            values = value_edit["values"] if value_edit["kind"] == "array" else [value_edit["value"]]
            if value_edit["kind"] == "one" and value_edit["ordinal"] != 0:
                raise OverrideError(f"{where}: ordinal {value_edit['ordinal']} given for a node that does not exist")
            if meta_type:
                tid = meta_type["value"]
                conv = [to_dump_spelling(tid, v, None, new_path) for v in values]
                meta = {"type": tid, "access_mode": 7, "last_written": 0}
                if is_array:
                    if meta_num is not None and meta_num["value"] != len(conv):
                        raise OverrideError(f"{where}: /key/num_values says {meta_num['value']} but {len(conv)} value(s) were given")
                    meta["num_values"] = len(conv)
                    parent[leaf] = conv
                else:
                    parent[leaf] = conv[0]
                parent[leaf + "/key"] = meta
            else:
                if meta_num is not None:
                    raise OverrideError(f"{where}: /key/num_values without /key/type")
                parent[leaf] = values if is_array else values[0]
            touched.append(new_path)
            continue

        node = parent[name]
        if isinstance(node, dict):
            raise OverrideError(f"{where}: is a directory; only values can be overridden")
        meta = meta_of(parent, name)
        has_meta = meta is not None
        tid = meta_type["value"] if meta_type else (meta.get("type", 0) if has_meta else 0)
        if meta_type and not has_meta:
            raise OverrideError(f"{where}: /key/type given but the node has no /key sibling")
        num = meta.get("num_values") if has_meta else None
        if meta_num is not None:
            if not has_meta:
                raise OverrideError(f"{where}: /key/num_values given but the node has no /key sibling")
            if meta_num["value"] < 1:
                raise OverrideError(f"{where}: /key/num_values must be at least 1")
            num = meta_num["value"]

        if value_edit:
            if value_edit["kind"] == "array":
                vals = value_edit["values"]
                if has_meta:
                    if num is None:
                        raise OverrideError(f"{where}: an array was given but the node is a scalar (no num_values)")
                    if num != len(vals):
                        raise OverrideError(f"{where}: array of {len(vals)} given but num_values is {num} "
                                            "(add a <path>/key/num_values row to resize)")
                elif not isinstance(node, list):
                    raise OverrideError(f"{where}: an array was given but the node is a scalar")
                out = []
                for k, v in enumerate(vals):
                    ex = node[k] if isinstance(node, list) and k < len(node) else None
                    out.append(to_dump_spelling(tid, v, ex, path) if has_meta else v)
                parent[name] = out
            else:
                node_is_array = (num is not None) if has_meta else isinstance(node, list)
                ordinal = value_edit["ordinal"]
                if node_is_array:
                    n = num if has_meta else len(node)
                    if not isinstance(node, list):
                        raise OverrideError(f"{where}: /key/num_values given but the node holds a scalar")
                    if not 0 <= ordinal < n:
                        raise OverrideError(f"{where}: ordinal {ordinal} out of range (num_values {n})")
                    if len(node) != n:
                        raise OverrideError(f"{where}: num_values {n} but the node holds {len(node)} values; "
                                            "resize with a whole-array row")
                    node[ordinal] = (to_dump_spelling(tid, value_edit["value"], node[ordinal], path)
                                     if has_meta else value_edit["value"])
                else:
                    if ordinal != 0:
                        raise OverrideError(f"{where}: ordinal {ordinal} given but the node is not an array (no num_values)")
                    parent[name] = (to_dump_spelling(tid, value_edit["value"], node, path)
                                    if has_meta else value_edit["value"])
        elif meta_num is not None and isinstance(node, list) and len(node) != num:
            raise OverrideError(f"{where}: /key/num_values {num} does not match the node's {len(node)} values "
                                "and no whole-array row resizes it")

        if has_meta:
            if meta_type:
                meta["type"] = tid
            if meta_num is not None:
                meta["num_values"] = num
        touched.append(path)
    return touched


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("odb", help="raw ODB dump (JSON)")
    ap.add_argument("overrides", nargs="?", help="conditions container holding the override table")
    ap.add_argument("--table", default="odb_overrides", help="override table name in the container")
    ap.add_argument("--run", type=int, help="run to select the interval for (with a container)")
    ap.add_argument("--tag", help="override tag (default: the table's default tag)")
    ap.add_argument("--canonical", help="canonical payload text of the override table instead of a container")
    ap.add_argument("--indent", type=int, default=2)
    args = ap.parse_args(argv)

    with open(args.odb) as f:
        tree = json.load(f)
    try:
        if args.canonical:
            with open(args.canonical) as f:
                rows = rows_from_canonical(f.read())
        elif args.overrides:
            if args.run is None:
                ap.error("--run is required with a conditions container")
            with open(args.overrides) as f:
                container = json.load(f)
            rows = rows_from_conditions(container[args.table], args.run, args.tag)
        else:
            ap.error("give a conditions container or --canonical")
        touched = apply_overrides(tree, rows)
    except OverrideError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for p in touched:
        print(f"# applied: {p}", file=sys.stderr)
    print(json.dumps(tree, indent=args.indent, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
