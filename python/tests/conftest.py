"""Fixtures for the run-database viewer tests.

Every test that needs a database needs a scratch one: these tests create runs,
files and jobs, and must never be pointed at the database the experiment uses.
Two things guard that.  `$PIONEER_RUNDB_TEST_DSN` has to be set, so nothing
runs against a default; and the database it names has to be called
`pioneer_rundb_test`, so a connection string copied from somewhere else is
refused rather than obeyed.

Without that variable every database test skips, which is what happens when
the test suite is run on a machine with no scratch Postgres.
"""

import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = TESTS_DIR.parent

# rundb_seed.py sits next to this file and is also imported by the standalone
# seeding script, so it is importable by name rather than as part of a package.
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

# The only database name these tests will touch.
TEST_DB_NAME = "pioneer_rundb_test"

SCHEMA_FILE = PACKAGE_DIR / "pioneer" / "rundb" / "db_config.sql"


@pytest.fixture(scope="session")
def scratch_dsn():
    """The scratch database, or a skip.

    Deliberately not defaulted: a test suite that invents a connection string
    is a test suite that can write to the wrong database.
    """
    dsn = os.environ.get("PIONEER_RUNDB_TEST_DSN")
    if not dsn:
        pytest.skip("set $PIONEER_RUNDB_TEST_DSN to run the database tests")

    import psycopg

    params = psycopg.conninfo.conninfo_to_dict(dsn)
    if params.get("dbname") != TEST_DB_NAME:
        # Not a skip: the variable was set on purpose, and pointing it at
        # another database is a mistake that has to be seen, not passed over.
        pytest.fail(
            f"$PIONEER_RUNDB_TEST_DSN names database {params.get('dbname')!r}; "
            f"these tests drop and rebuild their database and only run "
            f"against {TEST_DB_NAME!r}"
        )
    return dsn


@pytest.fixture(scope="session")
def fresh_db(scratch_dsn):
    """Drop, create and populate the scratch database once per session.

    `db_config.sql` is applied to an empty database on purpose: its INSERTs
    seed the status table and the standard target and degrader positions and
    are *not* idempotent, so applying it twice would double every one of those
    rows.  That is why the database is dropped first rather than cleaned.

    The file is plain SQL with no psql meta-commands, so psycopg can execute it
    in one go; without parameters psycopg uses the simple query protocol, which
    is what allows several statements in one string.
    """
    import psycopg

    params = psycopg.conninfo.conninfo_to_dict(scratch_dsn)
    admin_dsn = psycopg.conninfo.make_conninfo(scratch_dsn, dbname="postgres")

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')

    schema = SCHEMA_FILE.read_text()
    with psycopg.connect(scratch_dsn, autocommit=True) as conn:
        conn.execute(schema)

    assert params.get("dbname") == TEST_DB_NAME
    return scratch_dsn


@pytest.fixture(scope="session")
def seeded(fresh_db):
    """A run database with the shape the page has to cope with.

    See `rundb_seed.seed` for what is in it.
    """
    from rundb_seed import seed

    return seed(fresh_db)


@pytest.fixture(scope="session")
def view(fresh_db, seeded):
    """A read-only view of the seeded scratch database."""
    from pioneer.rundb.view import RunDbView

    the_view = RunDbView(dsn=fresh_db)
    yield the_view
    the_view.close()
