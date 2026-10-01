#!/usr/bin/env python3
"""Shared machinery for loading conditions JSON containers into a database.

json2sqlite.py and json2pg.py used to carry a copy of the DDL and of the
JSON-to-cells mapping each. Two copies of a mapping drift, and the acceptance
criterion for the whole database path is that a table served from the .db,
from the server and from the source .json produce identical canonical dumps
and sha256 hashes -- which is only true while the mapping is one mapping. So
the mapping lives here, the DDL lives in schema_sqlite.sql / schema_pg.sql,
and the two front ends are argument parsing plus a printout.

Loading is APPEND-ONLY by default, because the database is the record of what
was served during data taking. The old PostgreSQL loader deleted the table and
re-inserted it, which threw away the history the ConditionsHeader points at.
Instead, loading a container:

  * pins the constants the tag's intervals were serving. A tag-wide payload
    (JSON ``values``) belongs to every interval of the tag that has no payload
    of its own, the inactive ones included, so before it is deleted it is
    copied onto each of them as a per-interval payload
    (``iov_row_id = row_id``). History then keeps its constants, and no
    interval ever carries both payload kinds;
  * deactivates the tag's intervals (``is_active`` is the only column of an
    existing cond_iov row a load ever changes);
  * inserts the container's intervals with fresh ``row_id``s above the highest
    one the table has ever used, and their values.

``--replace`` deletes the table first and then takes the same path from empty.
It is for dev databases; the front ends say so.

The executors speak SQL text, not a driver API: SQLite through the stdlib
sqlite3 module, PostgreSQL through psql (the analysis container has the
postgres client but no python driver, and ON_ERROR_STOP is all a loader
needs). Nothing here imports anything outside the standard library.

Passwords never appear in an argument list or in output: use PGPASSWORD,
~/.pgpass or PGSERVICE, and see describe_conninfo().
"""
from __future__ import annotations

import csv
import io
import json
import math
import shlex
import sqlite3
import subprocess
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
SCHEMA_VERSION = 2

#: Column list of every INSERT, spelled out so that a column reorder in the
#: DDL is a loud error rather than a silently shifted payload.
VALUE_COLUMNS = ("table_name", "tag", "iov_row_id", "channel_id", "key",
                 "column_name", "ordinal", "value_type", "value_int",
                 "value_real", "value_text")


class LoaderError(Exception):
    """A container, a database state or a value the loader refuses."""


class FingerprintMoved(LoaderError):
    """A table's intervals changed between reading it and loading into it."""


#: The key under which ``condtool export`` records where a table came from
#: (``{"fingerprint": [max_row_id, active], "source": ...}``). The loader reads
#: it to refuse a stale edit and stores nothing of it.
EXPORT_KEY = "_export"


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------

class SqliteExec:
    """Run SQL against a SQLite file through the stdlib driver."""

    dialect = "sqlite"

    def __init__(self, path):
        self.label = str(path)
        self.con = sqlite3.connect(path)
        self.con.isolation_level = None      # we write our own BEGIN/COMMIT
        self.con.execute("PRAGMA foreign_keys = ON")

    def script(self, sql: str) -> None:
        try:
            self.con.executescript(sql)
        except sqlite3.Error as exc:
            # executescript leaves the failed script's transaction open, and
            # the next executescript would COMMIT it: roll back here so that a
            # rejected load really has changed nothing.
            try:
                self.con.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            raise LoaderError(f"{self.label}: {exc}") from exc

    def query(self, sql: str) -> list[list[str]]:
        """Rows of strings, same shape as PsqlExec.query: NULL comes back ''.

        psql prints SQL NULL and the empty string identically, so neither
        executor promises to tell them apart; a query that has to know selects
        a companion ``col IS NULL`` column.
        """
        try:
            cur = self.con.execute(sql)
        except sqlite3.Error as exc:
            raise LoaderError(f"{self.label}: {exc}") from exc
        return [["" if c is None else str(c) for c in row] for row in cur.fetchall()]

    def close(self) -> None:
        self.con.close()


class PsqlExec:
    """Run SQL against PostgreSQL through the psql client.

    ``argv`` never carries a password; put it in PGPASSWORD or ~/.pgpass.
    """

    dialect = "pg"

    def __init__(self, argv: list[str], label: str):
        self.argv = list(argv)
        self.label = label

    def _run(self, sql: str, extra: list[str]) -> str:
        proc = subprocess.run(self.argv + extra, input=sql, text=True,
                              capture_output=True)
        if proc.returncode != 0:
            raise LoaderError(f"{self.label}: psql failed:\n{proc.stderr.strip()}")
        return proc.stdout

    def script(self, sql: str) -> None:
        self._run(sql, ["-q"])

    def query(self, sql: str) -> list[list[str]]:
        """Rows of strings, parsed as CSV; NULL comes back ''.

        The unaligned format this used to ask for gave a comment or a
        description containing a tab, a newline or the two characters \\N back
        as column boundaries, row boundaries or a NULL. CSV quotes all three,
        and csv.reader unquotes them. -t drops the header row. psql prints SQL
        NULL and the empty string identically in CSV, so a query that has to
        tell them apart selects a companion ``col IS NULL`` column.

        ``extra_float_digits = 3`` first: a server (or role) set to 0 prints a
        double with 15 significant digits, and an export would then round
        every real it reads without anything noticing. -q keeps the SET's
        command tag out of the output.
        """
        out = self._run("SET extra_float_digits = 3;\n" + sql, ["--csv", "-t", "-q"])
        return [row for row in csv.reader(io.StringIO(out))]

    def close(self) -> None:
        pass


def make_executor(sqlite=None, conninfo=None, docker=None, database="conditions"):
    """Exactly one of sqlite / conninfo / docker selects the backend."""
    chosen = [n for n, v in (("sqlite", sqlite), ("conninfo", conninfo),
                             ("docker", docker)) if v]
    if len(chosen) != 1:
        raise LoaderError("give exactly one of --sqlite, --conninfo, --docker; "
                          f"got {chosen or 'none'}")
    if sqlite:
        return SqliteExec(sqlite)
    if docker:
        # -e PGPASSWORD (no value: docker takes it from this environment) so
        # that the password reaches psql on a server that does not trust the
        # local socket. It stays out of the argument list either way.
        return PsqlExec(["docker", "exec", "-i", "-e", "PGPASSWORD", docker, "psql",
                         "-U", "postgres", "-d", database,
                         "-v", "ON_ERROR_STOP=1"],
                        f"docker:{docker}/{database}")
    return PsqlExec(["psql", conninfo, "-v", "ON_ERROR_STOP=1"],
                    describe_conninfo(conninfo))


def describe_conninfo(conninfo: str) -> str:
    """A conninfo rendered for logs: the tokens that identify the server only.

    A libpq conninfo routinely carries ``password=``; printing it into a log
    file or a provenance record leaks the credential to everyone who can read
    the output. Only service/host/hostaddr/port/dbname say which server was
    used, and that is what an operator reading a log needs. A service name is
    kept because ``service=pioneer-conditions-admin`` alone is the normal
    spelling on the DAQ host; its expansion is in ~/.pg_service.conf.
    """
    if "://" in conninfo:
        return "<postgresql URI>"
    keep, seen = [], {}
    try:
        tokens = shlex.split(conninfo)
    except ValueError:
        return "<unparseable conninfo>"
    for token in tokens:
        if "=" not in token:
            return "<unparseable conninfo>"
        key, _, value = token.partition("=")
        seen[key.strip()] = value
    for key in ("service", "host", "hostaddr", "port", "dbname"):
        if key in seen:
            keep.append(f"{key}={seen[key]}")
    return " ".join(keep) or "<conninfo without service, host or dbname>"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def read_schema(dialect: str) -> str:
    return (TOOLS / f"schema_{dialect}.sql").read_text()


def schema_version(ex) -> int:
    """0 = empty database, 1 = pre-cond_schema layout, else the recorded version."""
    if ex.dialect == "sqlite":
        names = {r[0] for r in ex.query(
            "SELECT name FROM sqlite_master WHERE type = 'table';")}
    else:
        names = {r[0] for r in ex.query(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = ANY (current_schemas(false));")}
    if "cond_tables" not in names:
        return 0
    if "cond_schema" not in names:
        return 1
    # COALESCE rather than a NULL test: an executor row is text, and an empty
    # cond_schema (no MAX) is the v1 layout with the table already created.
    rows = ex.query("SELECT COALESCE(MAX(version), 0) FROM cond_schema;")
    if not rows or not rows[0][0] or rows[0][0] == "0":
        return 1
    return int(rows[0][0])


def ensure_schema(ex) -> int:
    """Apply the schema to an empty database; refuse anything but version 2."""
    version = schema_version(ex)
    if version == 0:
        ex.script(read_schema(ex.dialect))
        return SCHEMA_VERSION
    if version == SCHEMA_VERSION:
        return version
    migration = TOOLS / f"migrate_v1_to_v{SCHEMA_VERSION}_{ex.dialect}.sql"
    raise LoaderError(
        f"{ex.label}: conditions schema version {version}, this loader writes "
        f"version {SCHEMA_VERSION}. Migrate first: see {migration}. Loading into "
        f"a v{version} database would store data the v{SCHEMA_VERSION} "
        f"invariants forbid.")


# ---------------------------------------------------------------------------
# JSON container -> cells
# ---------------------------------------------------------------------------

def typed(value):
    """(value_type, value_int, value_real, value_text) for one scalar cell.

    Anything the five typed columns cannot hold exactly is refused rather than
    stringified: a silently str()ed list would read back as text and change the
    table's sha256 relative to the JSON layer.
    """
    if value is None:
        return "null", None, None, None
    if isinstance(value, bool):
        return "bool", int(value), None, None
    if isinstance(value, int):
        if abs(value) >= 2 ** 63:
            raise LoaderError(f"integer {value} does not fit a 64-bit column")
        return "int", value, None, None
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LoaderError(f"non-finite value {value!r} has no SQL spelling")
        return "real", None, value, None
    if isinstance(value, str):
        return "text", None, None, value
    raise LoaderError(f"value of type {type(value).__name__} is not a scalar cell: "
                      f"{value!r}")


def lit(value, dialect: str = "pg") -> str:
    """A SQL literal for one python value."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        if dialect == "pg":
            return "TRUE" if value else "FALSE"
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value == 0.0 and math.copysign(1.0, value) < 0 and dialect == "pg":
            # "-0.0" is unary minus on the numeric 0.0, which has no sign: the
            # column would get +0. The float8 input function keeps it. (SQLite
            # stores -0.0 as 0 whatever the spelling.)
            return "'-0'::float8"
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def payloads(table: dict):
    """Yield (tag, iov_row_id, rows): tag-wide payloads first, then per-interval ones."""
    for tag, rows in table.get("values", {}).items():
        yield tag, None, rows
    by_row = {row.get("row_id", i): row["tag"]
              for i, row in enumerate(table["iov"], start=1)}
    available = ", ".join(str(r) for r in sorted(by_row)) or "none"
    for key, rows in table.get("values_by_iov", {}).items():
        try:
            row_id = int(key)
        except (TypeError, ValueError):
            raise LoaderError(f"values_by_iov key {key!r} is not a row_id "
                              f"(the container's row_ids are {available})") from None
        if row_id not in by_row:
            raise LoaderError(f"values_by_iov key {key!r} names no interval of "
                              f"this container (its row_ids are {available})")
        yield by_row[row_id], row_id, rows


def cells(table: dict):
    """Yield (tag, iov_row_id, channel_id, key, column_name, ordinal, value).

    This is the whole JSON-to-relational mapping: a channel_values row becomes
    one cell per (channel, column, list element), a parameter_set row one cell
    per (key, list element) counted from the row's ``ordinal``.
    """
    kind = table.get("kind", "channel_values")
    for tag, iov_row_id, rows in payloads(table):
        for row in rows:
            if kind == "channel_values":
                channel = row["channel_id"]
                for column, value in row.items():
                    if column == "channel_id":
                        continue
                    elements = value if isinstance(value, list) else [value]
                    for ordinal, item in enumerate(elements):
                        yield tag, iov_row_id, channel, "", column, ordinal, item
            else:
                value = row.get("value")
                elements = value if isinstance(value, list) else [value]
                base = row.get("ordinal", 0)
                if isinstance(base, bool) or not isinstance(base, int) or base < 0:
                    raise LoaderError(f"key '{row['key']}': ordinal {base!r} is not "
                                      f"a non-negative integer")
                for k, item in enumerate(elements):
                    yield tag, iov_row_id, None, row["key"], "value", base + k, item


# ---------------------------------------------------------------------------
# Database state
# ---------------------------------------------------------------------------

def read_existing(ex, table: str) -> dict:
    """What the database already holds for one table.

    ``max_row_id`` is read here rather than written as an inline ``MAX()`` in
    the INSERT: inside the load transaction a MAX() would move as rows are
    added, and reading it once means the primary key aborts the whole load if
    another writer took the range in the meantime.
    """
    name = lit(table, ex.dialect)
    true = lit(True, ex.dialect)
    out: dict = {"table": None, "tags": {}, "max_row_id": 0, "active": {},
                 "unpinned": {}}

    rows = ex.query(f"SELECT schema, version, kind FROM cond_tables "
                    f"WHERE name = {name};")
    if rows:
        out["table"] = {"schema": rows[0][0], "version": int(rows[0][1]),
                        "kind": rows[0][2]}

    for tag, is_default, description in ex.query(
            f"SELECT tag, is_default, description FROM cond_tags "
            f"WHERE table_name = {name};"):
        out["tags"][tag] = {"is_default": is_default in ("1", "t", "true", "True"),
                            "description": description or ""}

    rows = ex.query(f"SELECT COALESCE(MAX(row_id), 0) FROM cond_iov "
                    f"WHERE table_name = {name};")
    if rows and rows[0][0]:
        out["max_row_id"] = int(rows[0][0])

    for tag, row_id, own in ex.query(
            f"SELECT i.tag, i.row_id, (SELECT COUNT(*) FROM cond_values v "
            f"WHERE v.table_name = i.table_name AND v.iov_row_id = i.row_id) "
            f"FROM cond_iov i "
            f"WHERE i.table_name = {name} AND i.is_active = {true} "
            f"ORDER BY i.row_id;"):
        out["active"].setdefault(tag, []).append(
            {"row_id": int(row_id), "own_payload": int(own) > 0})

    # Every interval, active or not, that reads the tag-wide payload because it
    # has none of its own. An interval retired by an earlier load was pinned
    # then, but a container may carry inactive intervals itself (a mapping kept
    # for the record, say), and those read the tag-wide payload until a reload
    # deletes it.
    for tag, row_id in ex.query(
            f"SELECT i.tag, i.row_id FROM cond_iov i "
            f"WHERE i.table_name = {name} AND NOT EXISTS (SELECT 1 FROM cond_values v "
            f"WHERE v.table_name = i.table_name AND v.iov_row_id = i.row_id) "
            f"ORDER BY i.row_id;"):
        out["unpinned"].setdefault(tag, []).append(int(row_id))
    return out


# ---------------------------------------------------------------------------
# Statement generation
# ---------------------------------------------------------------------------

def _pin_sql(name: str, tag: str, row_id: int, dialect: str) -> str:
    """Copy the tag-wide payload onto one interval as its own payload.

    Safe as a self-referencing INSERT ... SELECT in both dialects because the
    inserted rows have a non-NULL iov_row_id and the SELECT only reads rows
    with iov_row_id IS NULL, so no row this statement writes can be read back
    by it whatever order the engine chooses.
    """
    columns = ", ".join(VALUE_COLUMNS)
    selected = ", ".join(str(row_id) if c == "iov_row_id" else c
                         for c in VALUE_COLUMNS)
    return (f"INSERT INTO cond_values ({columns})\n"
            f"    SELECT {selected} FROM cond_values\n"
            f"     WHERE table_name = {lit(name, dialect)} "
            f"AND tag = {lit(tag, dialect)} AND iov_row_id IS NULL;")


def statements(name: str, table: dict, dialect: str, existing: dict,
               replace: bool = False, set_default: bool = False) -> list[str]:
    """The SQL that loads one container table into the state ``existing``."""
    true, false = lit(True, dialect), lit(False, dialect)
    tname = lit(name, dialect)
    kind = table.get("kind", "channel_values")
    version = int(table.get("version", 0))
    schema = table["schema"]
    out: list[str] = []

    if replace:
        # Children first: SQLite has no ON DELETE CASCADE here, and an explicit
        # order is readable in the log either way.
        out += [f"DELETE FROM cond_values WHERE table_name = {tname};",
                f"DELETE FROM cond_iov    WHERE table_name = {tname};",
                f"DELETE FROM cond_tags   WHERE table_name = {tname};",
                f"DELETE FROM cond_tables WHERE name = {tname};"]
        existing = {"table": None, "tags": {}, "max_row_id": 0, "active": {},
                    "unpinned": {}}

    # 1. the table's identity ------------------------------------------------
    if existing["table"] is None:
        out.append(f"INSERT INTO cond_tables (name, schema, version, kind) "
                   f"VALUES ({tname}, {lit(schema, dialect)}, {version}, "
                   f"{lit(kind, dialect)});")
    else:
        for field, have, want in (("schema", existing["table"]["schema"], schema),
                                  ("version", existing["table"]["version"], version),
                                  ("kind", existing["table"]["kind"], kind)):
            if have != want:
                raise LoaderError(
                    f"table '{name}': the database says {field} = {have!r}, the "
                    f"container says {want!r}. A table's identity is what the "
                    f"cache key and the sha256 are computed over, so it may not "
                    f"change under existing history; load under a new table name, "
                    f"or use --replace on a dev database.")

    # 2. tags ----------------------------------------------------------------
    default_tag = next((t for t, info in existing["tags"].items()
                        if info["is_default"]), None)
    specs = table.get("tags", [])
    wants = [s["tag"] for s in specs if s.get("is_default")]
    if len(wants) > 1:
        raise LoaderError(f"table '{name}': the container marks {len(wants)} tags "
                          f"as default ({', '.join(wants)}); at most one is allowed")

    for spec in specs:
        tag, want_default = spec["tag"], bool(spec.get("is_default", False))
        description = spec.get("description", "")
        if want_default and default_tag not in (None, tag):
            if not set_default:
                raise LoaderError(
                    f"table '{name}': tag '{tag}' is marked default but '{default_tag}' "
                    f"is the current default in the database. Moving the default "
                    f"changes what every job without an explicit tag reads, so it "
                    f"needs --set-default.")
            out.append(f"UPDATE cond_tags SET is_default = {false} "
                       f"WHERE table_name = {tname} AND tag = {lit(default_tag, dialect)};")
            default_tag = None
        if tag in existing["tags"]:
            out.append(f"UPDATE cond_tags SET description = {lit(description, dialect)} "
                       f"WHERE table_name = {tname} AND tag = {lit(tag, dialect)};")
            if want_default and not existing["tags"][tag]["is_default"]:
                out.append(f"UPDATE cond_tags SET is_default = {true} "
                           f"WHERE table_name = {tname} AND tag = {lit(tag, dialect)};")
        else:
            out.append(f"INSERT INTO cond_tags (table_name, tag, is_default, description) "
                       f"VALUES ({tname}, {lit(tag, dialect)}, "
                       f"{true if want_default else false}, "
                       f"{lit(description, dialect)});")
            existing["tags"][tag] = {"is_default": want_default,
                                     "description": description}
        if want_default:
            default_tag = tag

    # Every tag an interval names must exist, or the FK aborts the load with a
    # message that does not say which tag was missing.
    container_tags: list[str] = []
    for row in table["iov"]:
        if row["tag"] not in container_tags:
            container_tags.append(row["tag"])
    for tag in container_tags:
        if tag not in existing["tags"]:
            raise LoaderError(f"table '{name}': interval tag '{tag}' is neither in "
                              f"the container's 'tags' list nor in the database")

    # 3. the payload shapes the readers accept --------------------------------
    # A tag is all-tag-wide or all-per-interval. Loading anything else produces
    # a database the loader is happy with and every reader refuses, so it is
    # refused here, where the message can still name the container's tag.
    per_iov_tags: list[str] = []
    for tag, iov_row_id, _rows in payloads(table):
        if iov_row_id is not None and tag not in per_iov_tags:
            per_iov_tags.append(tag)
    for tag in table.get("values", {}):
        if tag not in container_tags:
            # The tag-wide cells would be inserted beside the ones the tag's
            # live intervals are serving, which no interval of this load ever
            # retires: the load would silently change a live payload.
            raise LoaderError(f"table '{name}': values for tag '{tag}' but no "
                              f"interval of '{tag}' in the container")
        if tag in per_iov_tags:
            raise LoaderError(f"table '{name}': tag '{tag}' has both a tag-wide "
                              f"payload ('values') and a per-interval one "
                              f"('values_by_iov'); a tag is one or the other, and "
                              f"a reader throws at Fetch on an interval that has "
                              f"both")

    # 4. pin, then retire, the intervals this load supersedes -----------------
    # The tag-wide payload is about to be deleted, so every interval that reads
    # it -- active or already inactive -- gets its own copy first. Pinning only
    # the active ones would leave an inactive interval reading whatever
    # tag-wide payload the container brings: its history would change.
    for tag in container_tags:
        for row_id in existing["unpinned"].get(tag, []):
            out.append(_pin_sql(name, tag, row_id, dialect))
        out.append(f"DELETE FROM cond_values WHERE table_name = {tname} "
                   f"AND tag = {lit(tag, dialect)} AND iov_row_id IS NULL;")
        out.append(f"UPDATE cond_iov SET is_active = {false} WHERE table_name = {tname} "
                   f"AND tag = {lit(tag, dialect)} AND is_active = {true};")

    # 5. the container's intervals, above every row_id the table ever used ----
    next_id = existing["max_row_id"]
    remap: dict[str, int] = {}
    for i, row in enumerate(table["iov"], start=1):
        next_id += 1
        remap[str(row.get("row_id", i))] = next_id
        columns = ["table_name", "row_id", "tag", "run_start", "run_end", "is_active"]
        values = [tname, str(next_id), lit(row["tag"], dialect),
                  str(int(row.get("run_start", 0))), lit(row.get("run_end"), dialect),
                  true if row.get("is_active", True) else false]
        # inserted_at / created_by / comment are listed only when the container
        # says something, so that the database defaults (now, current_user, '')
        # record who actually loaded the row.
        for key in ("inserted_at", "created_by", "comment"):
            if key in row:
                columns.append(key)
                values.append(lit(row[key], dialect))
        out.append(f"INSERT INTO cond_iov ({', '.join(columns)}) "
                   f"VALUES ({', '.join(values)});")

    # 6. the payload ----------------------------------------------------------
    columns = ", ".join(VALUE_COLUMNS)
    for tag, iov_row_id, channel, key, column, ordinal, value in cells(table):
        try:
            vtype, vint, vreal, vtext = typed(value)
        except LoaderError as exc:
            raise LoaderError(f"table '{name}', tag '{tag}', "
                              f"{'channel ' + str(channel) if key == '' else 'key ' + key}"
                              f" column '{column}' ordinal {ordinal}: {exc}") from exc
        rid = "NULL" if iov_row_id is None else str(remap[str(iov_row_id)])
        out.append(
            f"INSERT INTO cond_values ({columns}) VALUES ("
            f"{tname}, {lit(tag, dialect)}, {rid}, {lit(channel, dialect)}, "
            f"{lit(key, dialect)}, {lit(column, dialect)}, {ordinal}, "
            f"{lit(vtype, dialect)}, {lit(vint, dialect)}, {lit(vreal, dialect)}, "
            f"{lit(vtext, dialect)});")
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def load(ex, containers, replace: bool = False, set_default: bool = False,
         expect: dict | None = None, force: bool = False):
    """Load container files into ``ex``; returns [(path, table, cells, first_row_id)].

    One transaction for everything: a container that fails half way through
    leaves the database exactly as it was, so a rejected load never has to be
    unwound by hand. ``expect`` and ``force``: see load_tables().
    """
    return load_tables(ex, [(str(path), json.loads(Path(path).read_text()))
                            for path in containers],
                       replace=replace, set_default=set_default, expect=expect,
                       force=force)


def fingerprint_sql(name: str, dialect: str) -> tuple[str, str]:
    """(highest row_id ever used, active intervals) of one table, as two SQL
    scalar subqueries. Every load and every ``condtool close`` takes a new
    row_id and every ``condtool deactivate`` changes the active count, so an
    unchanged pair means nobody changed the table's intervals."""
    tname = lit(name, dialect)
    return (f"(SELECT COALESCE(MAX(row_id), 0) FROM cond_iov WHERE table_name = {tname})",
            f"(SELECT COUNT(*) FROM cond_iov WHERE table_name = {tname} "
            f"AND is_active = {lit(True, dialect)})")


def fingerprint(ex, name: str) -> tuple[int, int]:
    """fingerprint_sql() read now."""
    top, active = fingerprint_sql(name, ex.dialect)
    rows = ex.query(f"SELECT {top}, {active};")
    return int(rows[0][0] or 0), int(rows[0][1] or 0)


def _guard_sql(name: str, want: tuple[int, int], dialect: str) -> list[str]:
    """Abort the load transaction if the table no longer has fingerprint ``want``.

    Runs after the table lock, so it sees every load that committed before
    this one could start, and none can commit until this one is done.
    """
    top, active = fingerprint_sql(name, dialect)
    moved = f"{top} <> {int(want[0])} OR {active} <> {int(want[1])}"
    message = (f"conditions table {name} changed since it was read "
               f"(expected highest row_id {int(want[0])}, {int(want[1])} active)")
    if dialect == "pg":
        return [f"DO $cond_guard$ BEGIN IF {moved} THEN RAISE EXCEPTION "
                f"{lit(message, dialect)}; END IF; END $cond_guard$;"]
    # SQLite has no DO block; RAISE() exists only inside a trigger.
    return ["CREATE TEMP TABLE IF NOT EXISTS cond_load_guard (tbl TEXT);",
            "CREATE TEMP TRIGGER IF NOT EXISTS cond_load_guard_abort BEFORE INSERT "
            "ON cond_load_guard BEGIN SELECT RAISE(ABORT, 'conditions table changed "
            "since it was read'); END;",
            f"INSERT INTO cond_load_guard SELECT {lit(name, dialect)} WHERE {moved};"]


def load_tables(ex, containers, replace: bool = False, set_default: bool = False,
                expect: dict | None = None, force: bool = False):
    """load() for containers already in memory: ``containers`` is a list of
    (label, {table name: table}). The label only names the container in
    messages and in the returned list. Same single transaction.

    The statements are computed from the table's state as read before the
    transaction. So the transaction first takes an exclusive lock on the
    interval table (PostgreSQL: SHARE ROW EXCLUSIVE, which concurrent loads,
    closes and deactivations all wait for; SQLite: BEGIN IMMEDIATE) and then
    checks that each table still has the fingerprint it was read with. A
    concurrent writer makes the load fail with FingerprintMoved, never lose
    the other writer's intervals.

    ``expect`` maps a table name to the fingerprint the caller read it with
    (a writer that exported, edited and now loads the table). A table
    carrying an ``_export`` record (condtool export) expects the fingerprint
    recorded there. ``force`` drops both expectations; the lock and the check
    against the state just read stay.
    """
    ensure_schema(ex)
    seen: dict[str, str] = {}
    sql = ["BEGIN;" if ex.dialect == "pg" else "BEGIN IMMEDIATE;"]
    if ex.dialect == "pg":
        sql.append("LOCK TABLE cond_iov IN SHARE ROW EXCLUSIVE MODE;")
    body: list[str] = []
    loaded = []
    for path, tables in containers:
        for name, table in tables.items():
            if name in seen:
                raise LoaderError(f"table '{name}' is defined in both {seen[name]} "
                                  f"and {path}; merge them (merge_conditions.py) "
                                  f"or load them separately")
            seen[name] = path
            existing = read_existing(ex, name)
            now = (existing["max_row_id"], sum(len(v) for v in existing["active"].values()))
            want = None
            if not force:
                record = table.get(EXPORT_KEY) or {}
                if expect and name in expect:
                    want = tuple(int(x) for x in expect[name])
                elif record.get("fingerprint") is not None:
                    want = tuple(int(x) for x in record["fingerprint"])
            if want is not None and want != now:
                raise FingerprintMoved(
                    f"conditions table {name} changed since it was read: highest row_id "
                    f"{want[0]} and {want[1]} active interval(s) then, {now[0]} and {now[1]} "
                    f"now in {ex.label}. Nothing was written.")
            sql += _guard_sql(name, now, ex.dialect)
            body += statements(name, table, ex.dialect, existing, replace, set_default)
            first_row = 1 if replace else existing["max_row_id"] + 1
            loaded.append((str(path), name, sum(1 for _ in cells(table)), first_row))
    sql += body
    sql.append("COMMIT;")
    try:
        ex.script("\n".join(sql))
    except LoaderError as exc:
        text = str(exc)
        if "changed since it was read" in text:
            raise FingerprintMoved(
                f"{text}\n\nAnother writer changed the table between this load reading "
                f"it and locking it. Nothing was written; rerun.") from exc
        if "cond_iov" in text and any(w in text.lower() for w in
                                      ("unique", "primary key", "duplicate key")):
            raise LoaderError(
                f"{text}\n\nThe row_id range this load reserved was taken by "
                f"another writer between reading it and inserting. Nothing was "
                f"written; rerun the load.") from exc
        raise
    return loaded


__all__ = ["LoaderError", "SqliteExec", "PsqlExec", "make_executor",
           "describe_conninfo", "read_schema", "schema_version", "ensure_schema",
           "typed", "lit", "payloads", "cells", "read_existing", "statements",
           "load", "load_tables", "fingerprint", "fingerprint_sql", "FingerprintMoved",
           "EXPORT_KEY", "SCHEMA_VERSION", "VALUE_COLUMNS"]
