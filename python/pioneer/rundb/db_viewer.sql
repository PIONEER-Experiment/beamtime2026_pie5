-- =========================================================================
-- A read-only login for the PSM nearline website, and the indexes it needs.
--
--   psql "host=... dbname=pioneer user=<owner>" \
--        -v ON_ERROR_STOP=1 -v viewer_password='...' -f rundb_viewer.sql
--
-- The site works without this script: any role that can SELECT is enough.
-- What the script adds is safety and speed.
--
--   Safety   `nearline_viewer` can only read, and says so at the role level:
--            `default_transaction_read_only` makes an accidental INSERT an
--            error rather than a write, and the statement and idle timeouts
--            mean a stuck query cannot sit on the DAQ's database.  The site
--            sets the same three things per session, so this is belt and
--            braces -- the point is that they hold even if somebody connects
--            with psql.
--
--   Speed    `logs.slow_control` is the one large table here and it ships with
--            no secondary index at all, so the site's "give me the BOR/EOR
--            rows of the last 300 runs" query is a sequential scan of the
--            whole log.  Five indexes fix that -- see section 3 for which of
--            them the website actually needs and which are for humans.
--
-- The script is idempotent: run it again to rotate the password.
--
-- Run it as the OWNER of the tables.  `ALTER DEFAULT PRIVILEGES` without
-- `FOR ROLE` only covers objects the current role creates later, so running
-- this as a superuser who is not the owner would leave future tables
-- unreadable to the viewer.
--
-- A copy of this file lives in the run database's own repository at
-- beamtime2026_pie5/python/pioneer/rundb/db_viewer.sql; the two must match.
-- =========================================================================

\set ON_ERROR_STOP on

-- -------------------------------------------------------------------------
-- 1. the role
-- -------------------------------------------------------------------------
-- CREATE ROLE has no IF NOT EXISTS, and psql does NOT substitute :'variables'
-- inside a dollar-quoted DO block, so the statement is built with format() in
-- a SELECT and run with \gexec.  format('%L') quotes the password properly,
-- so one containing a quote or a backslash is handled rather than turned into
-- a syntax error.  The SELECT returns no row when the role is already there,
-- and \gexec then executes nothing.

SELECT format('CREATE ROLE nearline_viewer LOGIN PASSWORD %L', :'viewer_password')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nearline_viewer')
\gexec

-- Unconditional, so re-running the script rotates the password.
SELECT format('ALTER ROLE nearline_viewer LOGIN PASSWORD %L', :'viewer_password')
\gexec

ALTER ROLE nearline_viewer NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- Session defaults for every connection this role makes.  Even a human with
-- psql and this password cannot write or hold a transaction open.
ALTER ROLE nearline_viewer SET default_transaction_read_only = on;
ALTER ROLE nearline_viewer SET statement_timeout = '5s';
ALTER ROLE nearline_viewer SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE nearline_viewer SET lock_timeout = '1s';

-- -------------------------------------------------------------------------
-- 2. read access to the four schemas
-- -------------------------------------------------------------------------

GRANT USAGE ON SCHEMA config, state, logs, utils TO nearline_viewer;

GRANT SELECT ON ALL TABLES    IN SCHEMA config, state, logs, utils TO nearline_viewer;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA config, state, logs, utils TO nearline_viewer;

-- Tables added later (a new configurable device gets its own config.<table>)
-- must be readable too, or the site starts silently skipping configurations.
ALTER DEFAULT PRIVILEGES IN SCHEMA config, state, logs, utils
    GRANT SELECT ON TABLES TO nearline_viewer;
ALTER DEFAULT PRIVILEGES IN SCHEMA config, state, logs, utils
    GRANT SELECT ON SEQUENCES TO nearline_viewer;

-- The site reads column names out of information_schema to build its queries;
-- that view is world-readable, so nothing extra is needed for it.

-- -------------------------------------------------------------------------
-- 3. indexes on logs.slow_control
-- -------------------------------------------------------------------------
-- The table is append-only and grows with every slow-control update of every
-- channel.  The site asks it exactly one question -- "the BOR/EOR rows of runs
-- at or after N" -- which without an index reads the whole table.  The third
-- index is for the run page's "what was the beam doing during this run?".
--
-- CREATE INDEX (not CONCURRENTLY) takes a lock that blocks writers to this one
-- table for as long as the build takes; run it between runs, or switch to
-- CONCURRENTLY (which cannot be inside a transaction block) if the log is
-- already large.

CREATE INDEX IF NOT EXISTS idx_slow_control_run
    ON logs.slow_control (midas_run_number);

CREATE INDEX IF NOT EXISTS idx_slow_control_run_reason
    ON logs.slow_control (midas_run_number, reason);

CREATE INDEX IF NOT EXISTS idx_slow_control_time
    ON logs.slow_control (log_time);

-- The last two are for ad-hoc psql use ("what did this channel do yesterday?")
-- and for any other reader of this table.  The website's trends mirror does
-- NOT need them: it walks the primary key (`WHERE id > <cursor> ORDER BY id
-- LIMIT n`) and finds where to start by binary search on that same key, so it
-- reads one index range per tick no matter how large the log has grown.  They
-- are here because a log that is worth mirroring is also worth querying by
-- hand, and because `ORDER BY upd_time` over forty million rows is otherwise a
-- sort of the whole table.

CREATE INDEX IF NOT EXISTS idx_slow_control_channel_time
    ON logs.slow_control (equipment, channel, upd_time);

CREATE INDEX IF NOT EXISTS idx_slow_control_upd_time
    ON logs.slow_control (upd_time);

-- -------------------------------------------------------------------------
-- 4. what to put in the site's .env
-- -------------------------------------------------------------------------

\echo ''
\echo 'nearline_viewer is ready.  In the website checkout, .env should say:'
\echo '  PSM_RUNDB_HOST=<this host>'
\echo '  PSM_RUNDB_PORT=<this port>'
\echo '  PSM_RUNDB_USER=nearline_viewer'
\echo '  PSM_RUNDB_PASSWORD=<the password given to -v viewer_password>'
\echo ''
