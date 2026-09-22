"""The connection really cannot write, and really does give up.

These are the guarantees the rest of the viewer is built on: if a session here
could write or could run forever, a page refreshing every few seconds would be
able to disturb data taking.
"""

import psycopg
import pytest

from pioneer.rundb import pg


def test_session_is_read_only(fresh_db):
    """An INSERT is refused by the session, not by table permissions."""
    conn = pg.connect_readonly(fresh_db)
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with pg.readonly_transaction(conn):
                conn.execute(
                    "INSERT INTO config.configuration (config_type) VALUES ('nope')"
                )
    finally:
        conn.close()


def test_session_settings(fresh_db):
    """Statement timeout, idle timeout and application name are all set."""
    conn = pg.connect_readonly(fresh_db, timeout_ms=1234)
    try:
        with pg.readonly_transaction(conn):
            row = conn.execute(
                "SELECT current_setting('statement_timeout') AS statement, "
                "current_setting('idle_in_transaction_session_timeout') AS idle, "
                "current_setting('default_transaction_read_only') AS read_only, "
                "current_setting('application_name') AS name"
            ).fetchone()
    finally:
        conn.close()

    statement, idle, read_only, name = row
    assert statement == "1234ms"
    assert idle not in ("0", "")
    assert read_only == "on"
    assert name == pg.APPLICATION_NAME


def test_reading_still_works(fresh_db):
    """The read-only session can do the one thing it is for."""
    conn = pg.connect_readonly(fresh_db)
    try:
        with pg.readonly_transaction(conn):
            names = conn.execute("SELECT name FROM utils.status ORDER BY name").fetchall()
    finally:
        conn.close()
    assert ("PENDING",) in names
    assert ("DONE",) in names


def test_connect_failure_names_no_password():
    """A connection that cannot be made says where, but not with what."""
    dsn = "host=127.0.0.1 port=1 dbname=pioneer_rundb_test user=nobody password=hunter2"
    with pytest.raises(pg.PgError) as caught:
        pg.connect_readonly(dsn, timeout_ms=500)
    assert "hunter2" not in str(caught.value)


def test_describe_dsn_hides_the_password():
    described = pg.describe_dsn(
        "host=example port=5432 dbname=pioneer user=readonly password=hunter2"
    )
    assert "hunter2" not in described
    assert "password=***" in described
    assert described.startswith("host=example port=5432 dbname=pioneer user=readonly")


def test_describe_dsn_survives_rubbish():
    assert pg.describe_dsn("this is not a connection string") == "<unparseable dsn>"


def test_default_dsn_follows_the_environment(monkeypatch):
    monkeypatch.setenv(pg.DSN_ENV, "host=elsewhere dbname=pioneer")
    assert pg.default_dsn() == "host=elsewhere dbname=pioneer"
    monkeypatch.delenv(pg.DSN_ENV)
    assert "dbname=" in pg.default_dsn()
