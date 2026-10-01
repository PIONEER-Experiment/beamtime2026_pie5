#!/usr/bin/env python3
"""Export a table of a conditions database back into the JSON container format.

The database is where constants are written first; a JSON container is a
snapshot of it, for git and as the fallback the nearline job reads when the
database is down. export_table() is the inverse of cond_loader's JSON-to-cells
mapping, restricted to what a load of the export must reproduce:

  * ACTIVE intervals only. The retired ones are the database's history; a
    container that carried them would re-insert them as new inactive rows on
    every reload. Row ids are renumbered 1..N in database order (the order
    they were loaded in), exactly as merge_conditions.renumber() does, and
    ``values_by_iov`` keys follow;
  * a tag keeps the payload kind it has in the database: tag-wide cells
    (iov_row_id NULL) come back as ``values[tag]``, per-interval cells as
    ``values_by_iov[row_id]``. A tag with no active interval keeps its entry
    in ``tags`` but not its tag-wide payload, which only its retired
    intervals read;
  * every tag of the table, with its default flag and description; the
    table's schema, version and kind; the interval's run range (run_end NULL
    is open-ended), created_by and comment. ``inserted_at`` is left out, so
    that a reload records when it happened, as a load of any container does;
  * cells as typed: int, real, text, bool, null. Ordinals 0..n-1 become a JSON
    array. A parameter_set key whose ordinals do not start at 0 or have gaps
    becomes one row per cell with an explicit ``ordinal`` and a scalar value,
    the one spelling both the C++ JSON layer and the loader read the same way
    (the C++ layer counts an array from 0 whatever ``ordinal`` says).

What the database does not record, and so an export cannot know by itself:

  * the ORDER of tags, channels, columns and parameter keys. Without a hint
    they come out in a fixed order: tags by their first active interval (the
    others by name), channels ascending, columns and keys by name;
  * whether a single cell at ordinal 0 was written as a scalar or as a
    one-element array. It is an array when the same key (or column) is an
    array anywhere else in the table, or is named in ``list_keys``;
  * a table-level free-text ``description``: cond_tables has no column for it.

``order_from`` (the same table as an earlier container holds it, typically
the git copy or the previous export) settles all three: its order is reused
wherever the same tag, interval (matched by tag and run range), channel,
column or key exists, its scalar-or-array spelling is reused, and its
``description`` and top-level key order are carried over. Nothing else is
taken from it: every constant, interval and tag comes from the database.

The standard library only; PostgreSQL through psql, like cond_loader.

    python3 condtool.py --conninfo service=pioneer-conditions export wd_rf --out wd_rf.json
    python3 pg2json.py service=pioneer-conditions --out-dir DIR --check
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from . import cond_loader
    from .cond_loader import LoaderError, lit, schema_version
    from .merge_conditions import renumber
except ImportError:          # run as a script from this directory (condtool.py, pg2json.py)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cond_loader
    from cond_loader import LoaderError, lit, schema_version
    from merge_conditions import renumber

try:
    from . import pgservice
except ImportError:
    try:
        import pgservice
    except ImportError:
        pgservice = None

#: The top-level key order of a container table without a hint.
TOP_LEVEL = ("schema", "version", "kind", "tags", "iov", "values", "values_by_iov")


def _bool(cell) -> bool:
    return cell in ("1", "t", "true", "True")


# ---------------------------------------------------------------------------
# Where to read from
# ---------------------------------------------------------------------------

def is_db_spec(text: str) -> bool:
    """True for a database spec: a libpq keyword conninfo, a URI or sqlite:PATH."""
    return ("=" in text or text.startswith(("postgres://", "postgresql://", "sqlite:")))


def executor_for(spec: str):
    """An executor for a database spec.

    ``sqlite:PATH`` opens a SQLite file (dev and tests). Anything else is a
    libpq conninfo handed to psql as it is, so ``service=NAME`` and ~/.pgpass
    work the way they do for psql itself. The label printed in messages is
    the service's host/port/dbname when pgservice can expand it, never a
    password.
    """
    if spec.startswith("sqlite:"):
        return cond_loader.make_executor(sqlite=spec[len("sqlite:"):])
    ex = cond_loader.make_executor(conninfo=spec)
    if pgservice is not None and "service=" in spec:
        try:
            ex.label = (f"{cond_loader.describe_conninfo(spec)} "
                        f"({pgservice.describe(pgservice.resolve_conninfo(spec))})")
        except ValueError:
            # Not in the files pgservice reads; psql may still find it (its
            # compiled-in PGSYSCONFDIR), and will say so clearly if not.
            pass
    return ex


def _executor(source):
    """(executor, owned): a conninfo/spec string is opened here and closed by the caller."""
    if isinstance(source, str):
        return executor_for(source), True
    return source, False


# ---------------------------------------------------------------------------
# Reading one table
# ---------------------------------------------------------------------------

def _value(vtype: str, vint: str, vreal: str, vtext: str):
    """A cond_values cell back as the python value the loader was given."""
    if vtype == "int":
        return int(vint)
    if vtype == "real":
        return float(vreal)
    if vtype == "text":
        return vtext
    if vtype == "bool":
        return _bool(vint)
    if vtype == "null":
        return None
    raise LoaderError(f"unknown value_type {vtype!r}")


def read_table(ex, table: str) -> dict:
    """The raw database state of one table: identity, tags, intervals, cells.

    ``cells`` holds (tag, iov_row_id or None, channel_id or None, key,
    column_name, ordinal, value) for every cell of the table, retired
    intervals included.
    """
    if schema_version(ex) != cond_loader.SCHEMA_VERSION:
        raise LoaderError(f"{ex.label}: conditions schema version {schema_version(ex)}, "
                          f"this exporter reads version {cond_loader.SCHEMA_VERSION}")
    name = lit(table, ex.dialect)
    rows = ex.query(f"SELECT schema, version, kind FROM cond_tables WHERE name = {name};")
    if not rows:
        raise LoaderError(f"no table '{table}' in {ex.label}")
    out = {"schema": rows[0][0], "version": int(rows[0][1]), "kind": rows[0][2]}

    out["tags"] = [{"tag": t, "is_default": _bool(d), "description": desc or ""}
                   for t, d, desc in ex.query(
                       f"SELECT tag, is_default, description FROM cond_tags "
                       f"WHERE table_name = {name} ORDER BY tag;")]
    # (run_end IS NULL) as its own column: an executor hands back text, and
    # psql prints SQL NULL and the empty string identically.
    out["iov"] = [{"row_id": int(r[0]), "tag": r[1], "run_start": int(r[2]),
                   "run_end": None if _bool(r[3]) else int(r[4]),
                   "is_active": _bool(r[5]), "created_by": r[6] or "",
                   "comment": r[7] or ""}
                  for r in ex.query(
                      f"SELECT row_id, tag, run_start, (run_end IS NULL), "
                      f"COALESCE(run_end, 0), is_active, created_by, comment "
                      f"FROM cond_iov WHERE table_name = {name} ORDER BY row_id;")]
    out["cells"] = [(r[0], None if _bool(r[1]) else int(r[2]),
                     None if _bool(r[3]) else int(r[4]), r[5], r[6], int(r[7]),
                     _value(r[8], r[9], r[10], r[11]))
                    for r in ex.query(
                        f"SELECT tag, (iov_row_id IS NULL), COALESCE(iov_row_id, 0), "
                        f"(channel_id IS NULL), COALESCE(channel_id, 0), key, "
                        f"column_name, ordinal, value_type, value_int, value_real, "
                        f"value_text FROM cond_values WHERE table_name = {name} "
                        f"ORDER BY tag, iov_row_id, channel_id, key, column_name, ordinal;")]
    return out


def table_fingerprint(ex, table: str) -> tuple[int, int]:
    """(highest row_id ever used, active intervals) of one table.

    Every load and every ``condtool close`` takes a new row_id, and every
    ``condtool deactivate`` changes the active count. A writer passes the
    fingerprint it exported with to cond_loader.load_tables(expect=...), which
    checks it again inside the load transaction, under the table lock.
    """
    return cond_loader.fingerprint(ex, table)


def list_tables(ex) -> list[str]:
    return [r[0] for r in ex.query("SELECT name FROM cond_tables ORDER BY name;")]


# ---------------------------------------------------------------------------
# Order and shape hints
# ---------------------------------------------------------------------------

def merge_orders(sequences) -> list:
    """One order consistent with every sequence, when there is one.

    Rows of one payload need not carry the same columns (a rotation only on
    the chips that have one, say), so the order of a name is not where it
    first appears but where every row puts it relative to its neighbours:
    each row contributes "a before b" for each consecutive pair, and the
    result is a topological order of those constraints, ties broken by first
    appearance. Filtering it to the names of any one row gives back that row's
    order exactly, which is what makes an export hinted by an earlier export a
    fixed point. Rows that contradict each other (a cycle) cannot all be
    honoured; their names follow in order of first appearance.
    """
    first: dict = {}
    after: dict = {}
    before_count: dict = {}
    for seq in sequences:
        for name in seq:
            if name not in first:
                first[name] = len(first)
                after[name] = set()
                before_count[name] = 0
        for a, b in zip(seq, seq[1:]):
            if a != b and b not in after[a]:
                after[a].add(b)
                before_count[b] += 1
    out: list = []
    ready = sorted((n for n in first if before_count[n] == 0), key=first.get)
    while ready:
        name = ready.pop(0)
        out.append(name)
        for b in after[name]:
            before_count[b] -= 1
            if before_count[b] == 0:
                ready.append(b)
        ready.sort(key=first.get)
    placed = set(out)
    out += sorted((n for n in first if n not in placed), key=first.get)
    return out


class _Hint:
    """What an earlier copy of the table says about order and spelling."""

    def __init__(self, table: dict | None, kind: str):
        self.table = table or {}
        self.kind = kind
        self.tags = [t["tag"] for t in self.table.get("tags", [])]
        self.values_tags = list(self.table.get("values", {}))
        self.top_level = list(self.table)
        # every payload of the hint, and the names spelled as arrays anywhere
        self.payloads: list[list] = list(self.table.get("values", {}).values()) + \
            list(self.table.get("values_by_iov", {}).values())
        self.arrays: set[str] = set()
        # channel_values: one name sequence per row (its columns), one channel
        # sequence per payload; parameter_set: one key sequence per payload.
        name_seqs: list[list] = []
        channel_seqs: list[list] = []
        #: channel_id -> its columns in order, from the last row naming it
        self.row_columns: dict = {}
        for rows in self.payloads:
            keys: list = []
            channels: list = []
            for row in rows:
                named = self._named(row)
                for name, value in named:
                    if isinstance(value, list):
                        self.arrays.add(name)
                if kind == "channel_values":
                    name_seqs.append([n for n, _ in named])
                    if "channel_id" in row:
                        channels.append(row["channel_id"])
                        self.row_columns[row["channel_id"]] = [n for n, _ in named]
                else:
                    keys.append(row.get("key"))
            if kind != "channel_values":
                name_seqs.append(keys)
            channel_seqs.append(channels)
        self.names: list = merge_orders(name_seqs)
        self.channels: list = merge_orders(channel_seqs)

    def _named(self, row: dict):
        """(name, value) pairs of one payload row: columns, or the key's value."""
        if self.kind == "channel_values":
            return [(k, v) for k, v in row.items() if k != "channel_id"]
        return [(row.get("key"), row.get("value"))]

    def payload_for(self, tag: str, iov: dict | None) -> list | None:
        """The hint's payload for a tag-wide block, or for the interval with
        the same tag and run range (active in the hint)."""
        if iov is None:
            return self.table.get("values", {}).get(tag)
        by_iov = self.table.get("values_by_iov", {})
        for row in self.table.get("iov", []):
            if (row.get("tag") == tag and row.get("is_active", True)
                    and row.get("run_start", 0) == iov["run_start"]
                    and row.get("run_end") == iov["run_end"]):
                key = str(row.get("row_id"))
                if key in by_iov:
                    return by_iov[key]
        return None

    def order(self, local: list, universe: list, name, own: list = ()):
        """A sort key: the matching row's own order, then the matching
        payload's, then the table's, then natural."""
        if name in own:
            return (0, own.index(name), "")
        if name in local:
            return (1, local.index(name), "")
        if name in universe:
            return (2, universe.index(name), "")
        return (3, 0, name) if isinstance(name, str) else (3, name, "")


# ---------------------------------------------------------------------------
# Cells -> container rows
# ---------------------------------------------------------------------------

def _payload_rows(kind: str, cells: list, arrays: set, hint: _Hint, local: list | None,
                  where: str) -> list[dict]:
    """The container rows of one payload from its cells (channel, key, column, ordinal, value)."""
    local = local or []
    local_hint = _Hint({"values": {"_": local}}, kind) if local else None
    local_names = local_hint.names if local_hint else []
    local_arrays = local_hint.arrays if local_hint else set()

    def is_array(name, n_cells):
        if n_cells != 1:
            return True
        if local_hint is not None and name in local_names:
            return name in local_arrays     # the matching payload's own spelling wins
        return name in arrays

    grouped: dict = {}
    for channel, key, column, ordinal, value in cells:
        ident = channel if kind == "channel_values" else key
        grouped.setdefault(ident, {}).setdefault(column, []).append((ordinal, value))

    rows: list[dict] = []
    if kind == "channel_values":
        local_channels = local_hint.channels if local_hint else []
        for channel in sorted(grouped, key=lambda c: hint.order(local_channels, hint.channels, c)):
            if channel is None:
                raise LoaderError(f"{where}: a channel_values cell without a channel_id")
            row: dict = {"channel_id": channel}
            columns = grouped[channel]
            own = local_hint.row_columns.get(channel, []) if local_hint else []
            for column in sorted(columns, key=lambda c: hint.order(local_names, hint.names,
                                                                   c, own)):
                items = sorted(columns[column])
                ordinals = [o for o, _ in items]
                if ordinals != list(range(len(items))):
                    raise LoaderError(f"{where}: channel {channel} column '{column}' has "
                                      f"ordinals {ordinals}; a container can only spell "
                                      f"0..n-1 for a channel column")
                values = [v for _, v in items]
                row[column] = values if is_array(column, len(values)) else values[0]
            rows.append(row)
        return rows

    for key in sorted(grouped, key=lambda k: hint.order(local_names, hint.names, k)):
        by_column = grouped[key]
        if set(by_column) != {"value"}:
            raise LoaderError(f"{where}: key '{key}' has cells in column(s) "
                              f"{sorted(by_column)}; a parameter_set holds 'value' only")
        items = sorted(by_column["value"])
        ordinals = [o for o, _ in items]
        values = [v for _, v in items]
        if ordinals == list(range(len(items))):
            rows.append({"key": key,
                         "value": values if is_array(key, len(values)) else values[0]})
            continue
        # Not 0..n-1: one scalar row per cell, each with its ordinal, which the
        # C++ JSON layer and the loader both read as that one cell.
        rows += [{"key": key, "ordinal": o, "value": v} for o, v in items]
    return rows


def export_table(source, table: str, order_from: dict | None = None,
                 list_keys=()) -> dict:
    """The container form of ``table`` as the database serves it now.

    ``source`` is an executor or a database spec (see executor_for).
    ``order_from`` is an earlier copy of the same table, used for order,
    array-vs-scalar spelling and the table description only (module
    docstring). ``list_keys`` names keys/columns that are always arrays, for
    readers that need a one-element array to stay one.
    """
    ex, owned = _executor(source)
    try:
        raw = read_table(ex, table)
    finally:
        if owned:
            ex.close()
    return container_table(raw, table, order_from, list_keys)


def container_table(raw: dict, table: str, order_from: dict | None = None,
                    list_keys=()) -> dict:
    """export_table() on a read_table() result (split out for tests and --check)."""
    kind = raw["kind"]
    hint = _Hint(order_from, kind)
    where = f"table '{table}'"

    active = [r for r in raw["iov"] if r["is_active"]]
    # Without a hint: tags by their first ACTIVE interval, then the others by
    # name, so that the order does not depend on history the export leaves out.
    first_row: dict[str, int] = {}
    for r in active:
        first_row.setdefault(r["tag"], r["row_id"])
    tags = sorted(raw["tags"], key=lambda t: (
        (0, hint.tags.index(t["tag"]), 0, "") if t["tag"] in hint.tags else
        (1, 0, first_row.get(t["tag"], 1 << 62), t["tag"])))

    active_ids = {r["row_id"] for r in active}
    active_tags = {r["tag"] for r in active}
    wide: dict[str, list] = {}
    own: dict[int, list] = {}
    for tag, iov_row_id, channel, key, column, ordinal, value in raw["cells"]:
        cell = (channel, key, column, ordinal, value)
        if iov_row_id is None:
            if tag in active_tags:
                wide.setdefault(tag, []).append(cell)
        elif iov_row_id in active_ids:
            own.setdefault(iov_row_id, []).append(cell)

    # One-cell arrays: named explicitly, spelled as arrays by the hint, or an
    # array anywhere in the table (every cell of the table, history included).
    arrays = set(list_keys) | hint.arrays
    counts: dict = {}
    for tag, iov_row_id, channel, key, column, ordinal, value in raw["cells"]:
        name = column if kind == "channel_values" else key
        ident = (tag, iov_row_id, channel, key, name)
        counts[ident] = counts.get(ident, 0) + 1
        if ordinal > 0:
            arrays.add(name)
    arrays |= {ident[4] for ident, n in counts.items() if n > 1}

    for r in active:
        if r["row_id"] in own and r["tag"] in wide:
            raise LoaderError(f"{where}: active row {r['row_id']} of tag '{r['tag']}' has "
                              f"its own payload and the tag has a tag-wide one; a reader "
                              f"throws on that interval, so it is not exported")

    out: dict = {
        "schema": raw["schema"], "version": raw["version"], "kind": kind,
        "tags": [{"tag": t["tag"], "is_default": t["is_default"],
                  "description": t["description"]} for t in tags],
        "iov": [{"row_id": r["row_id"], "tag": r["tag"], "run_start": r["run_start"],
                 "run_end": r["run_end"], "is_active": True, "created_by": r["created_by"],
                 "comment": r["comment"]} for r in active],
    }
    values = {}
    for tag in sorted(wide, key=lambda t: (
            (0, hint.values_tags.index(t)) if t in hint.values_tags else
            (1, [x["tag"] for x in tags].index(t)))):
        values[tag] = _payload_rows(kind, wide[tag], arrays, hint,
                                    hint.payload_for(tag, None), f"{where} tag '{tag}'")
    by_iov = {}
    for r in active:
        if r["row_id"] in own:
            by_iov[str(r["row_id"])] = _payload_rows(
                kind, own[r["row_id"]], arrays, hint, hint.payload_for(r["tag"], r),
                f"{where} row {r['row_id']}")
    if values or "values" in hint.top_level or not by_iov:
        out["values"] = values
    if by_iov or "values_by_iov" in hint.top_level:
        out["values_by_iov"] = by_iov
    if "description" in hint.table:
        out["description"] = hint.table["description"]
    # Database row ids -> 1..N in load order, values_by_iov keys alongside:
    # the renumbering merge_conditions applies to every table it writes.
    renumber(out, 0)

    # Top-level key order: the hint's, then the fixed one.
    order = [k for k in hint.top_level if k in out] + \
        [k for k in TOP_LEVEL if k in out and k not in hint.top_level]
    order += [k for k in out if k not in order]
    return {k: out[k] for k in order}


# ---------------------------------------------------------------------------
# Comparison, for pg2json --check and the tests
# ---------------------------------------------------------------------------

def _canon(value):
    """A cell value in a form that compares by type as well as value (True != 1)."""
    return (type(value).__name__, value)


def active_state(source, table: str) -> dict:
    """What a job can read from one table, independent of row ids and order:
    identity, tags, and per active interval its metadata and the cell set it
    is served (its own cells, else its tag's tag-wide ones)."""
    ex, owned = _executor(source)
    try:
        raw = read_table(ex, table)
    finally:
        if owned:
            ex.close()
    wide: dict[str, list] = {}
    own: dict[int, list] = {}
    for tag, iov_row_id, channel, key, column, ordinal, value in raw["cells"]:
        cell = (channel if channel is not None else -1, key, column, ordinal, _canon(value))
        if iov_row_id is None:
            wide.setdefault(tag, []).append(cell)
        else:
            own.setdefault(iov_row_id, []).append(cell)
    intervals = []
    for r in raw["iov"]:
        if not r["is_active"]:
            continue
        served = own.get(r["row_id"]) or wide.get(r["tag"], [])
        intervals.append((r["tag"], r["run_start"], -1 if r["run_end"] is None else r["run_end"],
                          r["created_by"], r["comment"], tuple(sorted(served, key=repr))))
    return {"identity": (raw["schema"], raw["version"], raw["kind"]),
            "tags": sorted((t["tag"], t["is_default"], t["description"]) for t in raw["tags"]),
            "intervals": sorted(intervals, key=repr)}


def compare_states(table: str, a: dict, b: dict, a_name: str = "database",
                   b_name: str = "export") -> list[str]:
    """Human-readable differences between two active_state() results; [] if equal."""
    problems: list[str] = []
    if a["identity"] != b["identity"]:
        problems.append(f"{table}: schema/version/kind {a['identity']} in the {a_name}, "
                        f"{b['identity']} in the {b_name}")
    if a["tags"] != b["tags"]:
        problems.append(f"{table}: tags differ: {a_name} {a['tags']}, {b_name} {b['tags']}")
    ia = {iv[:3]: iv for iv in a["intervals"]}
    ib = {iv[:3]: iv for iv in b["intervals"]}
    for k in sorted(set(ia) | set(ib), key=repr):
        rng = f"tag '{k[0]}' [{k[1]}, {'open' if k[2] == -1 else k[2]})"
        if k not in ib:
            problems.append(f"{table}: {rng} is active in the {a_name} only")
        elif k not in ia:
            problems.append(f"{table}: {rng} is active in the {b_name} only")
        elif ia[k][3:5] != ib[k][3:5]:
            problems.append(f"{table}: {rng}: created_by/comment differ")
        elif ia[k][5] != ib[k][5]:
            only_a = set(ia[k][5]) - set(ib[k][5])
            only_b = set(ib[k][5]) - set(ia[k][5])
            problems.append(f"{table}: {rng}: {len(ia[k][5])} cells in the {a_name}, "
                            f"{len(ib[k][5])} in the {b_name}; {len(only_a)} only in the "
                            f"{a_name}, {len(only_b)} only in the {b_name}"
                            + (f", e.g. {sorted(only_a or only_b, key=repr)[0]}"
                               if only_a or only_b else ""))
    return problems


__all__ = ["export_table", "container_table", "read_table", "table_fingerprint",
           "list_tables", "active_state", "compare_states", "executor_for", "is_db_spec",
           "TOP_LEVEL"]
