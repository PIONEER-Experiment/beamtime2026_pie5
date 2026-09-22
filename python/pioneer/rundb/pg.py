"""Connections to the run database that can only ever read.

The run database is the live record of data taking: the sequencer pops the
queue from `state.midas_run` and the nearline daemon claims rows in
`state.postproc_job` with FOR UPDATE SKIP LOCKED about once a second.  A viewer
that took a lock, held a transaction open or ran an unbounded query would stop
data taking, so every connection made here

* is read only at session level (`default_transaction_read_only`), which turns
  an accidental INSERT into an error instead of a write,
* has a statement timeout, so a query over an unindexed column gives up rather
  than grinding,
* has an idle-in-transaction timeout, so a forgotten transaction cannot pin a
  snapshot open and block VACUUM,
* names itself in `application_name`, so `pg_stat_activity` says who is asking.

This module is deliberately separate from `pioneer.rundb.config` and
`pioneer.rundb.interface`.  `config.connect` hardcodes the host, and
`interface.load_run_config` writes `status='ERROR'` when it meets a bad
configuration -- neither belongs anywhere near a page that polls.
"""

import os
from contextlib import contextmanager

import psycopg
from psycopg import sql

from pioneer.rundb import config

# What the viewer calls itself in pg_stat_activity.application_name.
APPLICATION_NAME = "rundb_view"

# Every statement gives up after this long unless the caller says otherwise.
DEFAULT_TIMEOUT_MS = 4000

# A session may sit inside an open transaction doing nothing for this long.
# Comfortably longer than a statement, short enough that a bug clears itself up.
IDLE_TIMEOUT_MS = 30_000

# The environment variable the command line and the RPC server fall back to.
DSN_ENV = "PIONEER_RUNDB_DSN"

# Connection-string keys that must never be printed.
_SECRET_KEYS = frozenset({"password", "sslpassword", "passfile"})

# The order keys are shown in, so two descriptions can be compared by eye.
_DESCRIBE_ORDER = ("host", "hostaddr", "port", "dbname", "user", "application_name")


class PgError(Exception):
    """A connection could not be made or could not be configured read only."""


def default_dsn() -> str:
    """The connection string to use when the caller gave none.

    `$PIONEER_RUNDB_DSN` wins if it is set; otherwise the host, port and
    database name from `pioneer.rundb.config` are used with the `readonly`
    role, which is the same place the rest of the package looks.
    """
    from_env = os.environ.get(DSN_ENV)
    if from_env:
        return from_env
    return (
        f"host={config.DB_HOST} port={config.DB_PORT} dbname={config.DB_NAME} "
        "user=readonly password=readonly"
    )


def connect_readonly(
    dsn: str,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    application_name: str = APPLICATION_NAME,
) -> psycopg.Connection:
    """Open a connection that cannot write and cannot run for long.

    `dsn` is a libpq connection string.  The returned connection is not in
    autocommit mode, so the work belongs inside `readonly_transaction`.

    The SET statements are issued while the connection is still in autocommit
    mode.  That is what makes them session settings: run inside a transaction
    they would be rolled back with it.
    """
    timeout = max(int(timeout_ms), 1)
    idle_timeout = max(timeout * 5, IDLE_TIMEOUT_MS)

    try:
        conn = psycopg.connect(dsn, autocommit=True, application_name=application_name)
    except psycopg.Error as exc:
        raise PgError(f"cannot connect to {describe_dsn(dsn)}: {exc}") from exc

    try:
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
            cur.execute(
                sql.SQL("SET statement_timeout = {}").format(sql.Literal(f"{timeout}ms"))
            )
            cur.execute(
                sql.SQL("SET idle_in_transaction_session_timeout = {}").format(
                    sql.Literal(f"{idle_timeout}ms")
                )
            )
    except psycopg.Error as exc:
        conn.close()
        raise PgError(
            f"cannot configure a read-only session on {describe_dsn(dsn)}: {exc}"
        ) from exc

    conn.autocommit = False
    return conn


@contextmanager
def readonly_transaction(conn: psycopg.Connection):
    """Run a block inside one short read-only transaction.

    SET TRANSACTION READ ONLY only works as the first statement of a
    transaction, which is why this is a context manager and not a note in a
    docstring.  It is belt and braces on top of the session default: should
    anybody ever hand this function a connection made elsewhere, the
    transaction is still read only.
    """
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        yield conn


def describe_dsn(dsn: str) -> str:
    """The connection string with the password removed, for logs and /status.

    Anything that will not parse is reported as `<unparseable dsn>` rather than
    echoed, because a malformed connection string may still hold the password.
    """
    try:
        params = psycopg.conninfo.conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:
        return "<unparseable dsn>"

    shown = {
        key: str(value)
        for key, value in params.items()
        if key not in _SECRET_KEYS and value is not None
    }
    ordered = [f"{key}={shown.pop(key)}" for key in _DESCRIBE_ORDER if key in shown]
    ordered += [f"{key}={value}" for key, value in sorted(shown.items())]
    if any(key in params for key in _SECRET_KEYS):
        ordered.append("password=***")
    return " ".join(ordered) if ordered else "<empty dsn>"
