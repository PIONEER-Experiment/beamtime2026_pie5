-- =========================================================
-- 1. ROLES
-- =========================================================

-- DB roles 
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'readonly') THEN
        CREATE ROLE readonly;
    END IF;
    ALTER ROLE readonly WITH
        LOGIN
        NOSUPERUSER
        NOCREATEDB
        NOCREATEROLE
        NOREPLICATION
        NOBYPASSRLS
        NOINHERIT;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bot') THEN
        CREATE ROLE bot;
    END IF;
    ALTER ROLE bot WITH
        LOGIN PASSWORD 'bot'
        NOSUPERUSER
        NOCREATEDB
        NOCREATEROLE
        NOREPLICATION
        NOBYPASSRLS;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'shifter') THEN
        CREATE ROLE shifter;
    END IF;
    ALTER ROLE shifter WITH
        LOGIN PASSWORD '12345'
        NOSUPERUSER
        NOCREATEDB
        NOCREATEROLE
        NOREPLICATION
        NOBYPASSRLS;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'admin') THEN
        CREATE ROLE admin;
    END IF;
    ALTER ROLE admin WITH
        LOGIN PASSWORD 'BewareIamAdmin'
        NOSUPERUSER
        NOCREATEDB
        NOCREATEROLE
        NOREPLICATION
        NOBYPASSRLS;
END$$;

-- =========================================================
-- 2. SCHEMAS
-- =========================================================

CREATE SCHEMA IF NOT EXISTS config;
CREATE SCHEMA IF NOT EXISTS state;
CREATE SCHEMA IF NOT EXISTS logs;
CREATE SCHEMA IF NOT EXISTS utils;


-- Lock down public schema (important in fresh systems)
REVOKE ALL ON SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA config TO readonly, bot, shifter, admin;
GRANT USAGE ON SCHEMA state TO readonly, bot, shifter, admin;
GRANT USAGE ON SCHEMA logs TO readonly, bot, shifter, admin;
GRANT USAGE ON SCHEMA utils TO readonly, bot, shifter, admin;

-- explicit schema write control
REVOKE ALL ON ALL TABLES IN SCHEMA config FROM readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA state FROM readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA logs FROM readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA utils FROM readonly;

GRANT SELECT ON ALL TABLES IN SCHEMA config TO readonly, bot, shifter, admin;
GRANT SELECT ON ALL TABLES IN SCHEMA state TO readonly, bot, shifter, admin;
GRANT SELECT ON ALL TABLES IN SCHEMA logs TO readonly, bot, shifter, admin;
GRANT SELECT ON ALL TABLES IN SCHEMA utils TO readonly, bot, shifter, admin;

GRANT SELECT ON ALL SEQUENCES IN SCHEMA config TO readonly, bot, shifter, admin;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA state TO readonly, bot, shifter, admin;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA logs TO readonly, bot, shifter, admin;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA utils TO readonly, bot, shifter, admin;

-- =========================================================
-- 3. DEFAULT PRIVILEGES FOR FUTURE TABLES
-- =========================================================

-- Default permissions on SCHEMA config:
--
-- All shall be able to read TABLES
-- bots shifters and admin shall be able to insert into tables
-- this also requires SELECT/USAGE permission on corresponding sequences.
-- Updating configurations is considered bad practice
-- and shall be reserved for admins only.

ALTER DEFAULT PRIVILEGES IN SCHEMA config
GRANT SELECT ON TABLES TO readonly, bot, shifter, admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA config
GRANT INSERT ON TABLES TO bot, shifter, admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA config
GRANT USAGE, SELECT on SEQUENCES TO bot, shifter, admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA config
GRANT UPDATE ON TABLES TO admin;


-- Default permissions on SCHEMA state:
--
-- All shall be able to read TABLES
-- bots shifters and admin shall be able to insert into tables
-- this also requires SELECT/USAGE permission on corresponding sequences.
-- shifters and administrators shall have the right to update
-- the run schedule and bump priorities.

ALTER DEFAULT PRIVILEGES IN SCHEMA state
GRANT SELECT ON TABLES TO readonly, bot, shifter, admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA state
GRANT INSERT ON TABLES TO bot, shifter, admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA state
GRANT USAGE, SELECT on SEQUENCES TO bot, shifter, admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA state
GRANT UPDATE ON TABLES TO shifter, admin;

-- Default permissions on SCHEMA logs:
--
-- These are generally append only.
-- bots, shifters and admins require USAGE and SELECT
-- on the sequences
ALTER DEFAULT PRIVILEGES IN SCHEMA logs
GRANT SELECT ON TABLES TO readonly, bot, shifter, admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA logs
GRANT INSERT ON TABLES TO bot, shifter, admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA logs
GRANT USAGE, SELECT on SEQUENCES TO bot, shifter, admin;

-- Default permissions on SCHEMA utils
--
-- To be determined. Basically bots shall stay out of here
-- only maintainers and admins shall modify those tables.
ALTER DEFAULT PRIVILEGES IN SCHEMA utils
GRANT SELECT ON TABLES TO readonly, bot, shifter, admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA utils
GRANT ALL PRIVILEGES ON TABLES TO admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA utils
GRANT ALL PRIVILEGES ON SEQUENCES TO admin;

-- =========================================================
-- 4. CORE TABLES
-- =========================================================


-- -------------------------
-- UTILS SCHEMA
--
-- These are tables that store metadata used by other tables.
-- -------------------------

-- This table defines all states jobs can be in.
CREATE TABLE IF NOT EXISTS utils.status (
    name TEXT PRIMARY KEY,           -- Name of the status stored in other tables
    description TEXT,                -- a verbose description of the status
    isSuccess BOOLEAN DEFAULT NULL,  -- flag to signal that this shall be treated as successful completion 
    isFailure BOOLEAN DEFAULT NULL   -- flag to signal that the job execution will never succeed.
);

-- Define possible states here and now such that they are available for other tables to use.
INSERT INTO utils.status (name, description, isSuccess, isFailure) VALUES
    ('HOLDING'  , 'Put on hold by user interaction',                          false, false),
    ('PENDING'  , 'Waiting for resources to become available',                false, false),
    ('DEPENDING', 'Depends on a job that did neither succeed nor fail',       false, false),
    ('CLAIMED'  , 'Locked and should change to RUNNING immediately',          false, false),
    ('RUNNING'  , 'Execution started and termination was not registered yet', false, false),
    ('DONE'     , 'Job completed successfully (return code 0)',               true , false),
    ('FAILED'   , 'Job terminated with any return code other than 0',         false, true ),
    ('BLOCKED'  , 'Job has a dependency that will never be met',              false, true ),
    ('CANCELLED', 'Cancelled by user interaction',                            false, true )
;

CREATE OR REPLACE FUNCTION utils.is_success(s TEXT)
RETURNS BOOLEAN AS $$
    SELECT isSuccess FROM utils.status WHERE name = s;
$$ LANGUAGE SQL STABLE;

CREATE OR REPLACE FUNCTION utils.is_failure(s TEXT)
RETURNS BOOLEAN AS $$
    SELECT isFailure FROM utils.status WHERE name = s;
$$ LANGUAGE SQL STABLE;

-- Right now, all transitions are allowed, so a job could go from any state to any other state.
-- Let's for now assume that shifters have the brain to not mess up fatally by switching a random
-- job from BLOCKED to RUNNING without making the actual state change.

-- -------------------------
-- CONFIG SCHEMA
-- 
-- Note: Each configurable device shall gets its own table
-- Any specific table shall use a primary key that doubles as
-- foreign key in config.configuration.
-- -------------------------

-- Parent table that contains all configurations
CREATE TABLE IF NOT EXISTS config.configuration (
    id SERIAL PRIMARY KEY,               -- primary identifier of the configuration
    config_type TEXT,                    -- specify the configuration type. Shall reference another table in SCHEMA config
    do_not_use BOOLEAN DEFAULT false     -- set this to true if specific configuration causes problems, e.g. tripping hardware
);

-- dummy configuration table for testing
CREATE TABLE IF NOT EXISTS config.dummy (
    id INT PRIMARY KEY REFERENCES config.configuration(id),
    p1 TEXT,
    p2 TEXT
);

-- -------------------------
-- STATE SCHEMA
-- -------------------------

-- state.midas_run is a table for scheduling runs for
-- the pythonic sequencer. Priority defines the order
-- in which jobs are considered, where lowest numbers
-- go first.

CREATE TABLE IF NOT EXISTS state.midas_run (
    id SERIAL PRIMARY KEY,                      -- This is the database key established when creating the entry
    priority INT,                               -- This is a priority value
    status TEXT REFERENCES utils.status(name),  -- Indicates the status of the job
    midas_run_number INT                        -- run number assigned when actually run in midas
);

-- midas_run_config: child table of midas_run
-- this links a specifc run to a list of configurations
-- to be loaded from config.configuration and tables
-- referenced therein.

CREATE TABLE IF NOT EXISTS state.midas_run_config (
    id SERIAL PRIMARY KEY,
    run_id INT REFERENCES state.midas_run(id),
    config_id INT REFERENCES config.configuration(id),
    priority INT
);

-- list of postprocessing jobs.
CREATE TABLE IF NOT EXISTS state.postproc_job (
    id SERIAL PRIMARY KEY,
    midas_run_id INT REFERENCES state.midas_run(id),
    job_type TEXT,
    priority INT,
    status TEXT REFERENCES utils.status(name)
);

CREATE INDEX idx_postproc_job_status
ON state.postproc_job(status);

-- a dependency table. Make sure some jobs reach DONE status
-- before starting some other job. You want the cleanup to
-- start only after the raw data has been processed, backed up
-- and sent to remote location.
CREATE TABLE IF NOT EXISTS state.postproc_depends (
    pp_job_id  INT REFERENCES state.postproc_job(id),
    depends_on INT REFERENCES state.postproc_job(id),
    PRIMARY KEY (pp_job_id, depends_on)
);

CREATE INDEX idx_depends_on
ON state.postproc_depends(depends_on);
CREATE INDEX idx_pp_job_id
ON state.postproc_depends(pp_job_id);
CREATE INDEX idx_dep_graph_traversal
ON state.postproc_depends(depends_on, pp_job_id);

-- Above job table has to be acyclic, let's enforce it
CREATE OR REPLACE FUNCTION state.prevent_postproc_dependency_cycles()
RETURNS trigger AS $$
DECLARE
    cycle_found boolean;
BEGIN
    WITH RECURSIVE reach(id) AS (
        SELECT NEW.depends_on
        UNION
        SELECT d.depends_on
        FROM state.postproc_depends d
        JOIN reach r ON d.pp_job_id = r.id
    )
    SELECT EXISTS (
        SELECT 1 FROM reach WHERE id = NEW.pp_job_id
    )
    INTO cycle_found;

    IF cycle_found THEN
        RAISE EXCEPTION
            'Cycle detected: % depends (directly or indirectly) on %',
            NEW.depends_on, NEW.pp_job_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER no_cycles_postproc_depends
BEFORE INSERT OR UPDATE ON state.postproc_depends
FOR EACH ROW
EXECUTE FUNCTION state.prevent_postproc_dependency_cycles();


-- Logic to recompute the state of a job based on its dependencies
CREATE OR REPLACE FUNCTION state.recompute_job_state(p_job_id INT)
RETURNS void AS $$
BEGIN
    -- blocked if any failed dependency exists
    IF EXISTS (
        SELECT 1
        FROM state.postproc_depends d
        JOIN state.postproc_job j ON j.id = d.depends_on
        WHERE d.pp_job_id = p_job_id
          AND utils.is_failure(j.status)
    ) THEN
        UPDATE state.postproc_job
        SET status = 'BLOCKED'
        WHERE id = p_job_id;
        RETURN;
    END IF;

    -- pending if all dependencies are success
    IF EXISTS (SELECT 1 FROM state.postproc_depends WHERE pp_job_id = p_job_id)
       AND NOT EXISTS (
           SELECT 1
           FROM state.postproc_depends d
           JOIN state.postproc_job j ON j.id = d.depends_on
           WHERE d.pp_job_id = p_job_id
             AND NOT utils.is_success(j.status)
       )
    THEN
        UPDATE state.postproc_job
        SET status = 'PENDING'
        WHERE id = p_job_id
          AND status NOT IN ('HOLDING', 'CANCELLED');
    ELSE
        UPDATE state.postproc_job
        SET status = 'DEPENDING'
        WHERE id = p_job_id
          AND status NOT IN ('HOLDING', 'CANCELLED');
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION state.trg_dep_change()
RETURNS trigger AS $$
BEGIN
    PERFORM state.recompute_job_state(
        COALESCE(NEW.pp_job_id, OLD.pp_job_id)
    );
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER dep_change
AFTER INSERT OR DELETE ON state.postproc_depends
FOR EACH ROW
EXECUTE FUNCTION state.trg_dep_change();

CREATE OR REPLACE FUNCTION state.trg_status_change()
RETURNS trigger AS $$
DECLARE
    r RECORD;
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;

    -- only propagate outward (no recursion explosion)
    FOR r IN
        SELECT pp_job_id
        FROM state.postproc_depends
        WHERE depends_on = NEW.id
    LOOP
        PERFORM state.recompute_job_state(r.pp_job_id);
    END LOOP;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER status_change
AFTER UPDATE OF status ON state.postproc_job
FOR EACH ROW
EXECUTE FUNCTION state.trg_status_change();



-- -------------------------
-- LOGS SCHEMA (mostly append-only)
-- -------------------------
CREATE TABLE IF NOT EXISTS logs.slow_control (
    id BIGSERIAL PRIMARY KEY,          -- internal primary key
    midas_run_number INT,              -- run number to which this log entry belongs
    reason TEXT NOT NULL,              -- what caused this log entry (see below)
    log_time TIMESTAMP DEFAULT now(),  -- time at which the log was created
    upd_time TIMESTAMP NOT NULL,       -- time at which the ODB value was last updated
    odb_key TEXT,                      -- slow control parameter monitored
    odb_val TEXT                       -- value of sc parameter monitored

    CHECK ( reason IN (
        'BOR',      -- Begin of run
        'EOR',      -- End of run
        'UPDATE',   -- The ODB value has changed significantly
        'PERIODIC'  -- Time since last log entry exceeded timeout
        ))
);


-- =========================================================
-- role specific permissions on selected tables
-- =========================================================

-- BOT: write access only where needed
GRANT INSERT ON config.configuration   TO bot;
GRANT INSERT ON state.midas_run        TO bot;
GRANT INSERT ON state.postproc_job     TO bot;
GRANT INSERT ON state.postproc_depends TO bot;

GRANT USAGE, SELECT ON SEQUENCE config.configuration_id_seq    TO bot;
GRANT USAGE, SELECT ON SEQUENCE state.midas_run_id_seq         TO bot;
GRANT USAGE, SELECT ON SEQUENCE state.midas_run_config_id_seq  TO bot;
GRANT USAGE, SELECT ON SEQUENCE state.postproc_job_id_seq      TO bot;

GRANT UPDATE (status, midas_run_number) ON state.midas_run     TO bot;
GRANT UPDATE (status)                   ON state.postproc_job  TO bot;

-- SHIFTER: broader control
-- a shifter may mark a configuration as faulty
GRANT UPDATE (do_not_use) ON config.configuration TO shifter;
