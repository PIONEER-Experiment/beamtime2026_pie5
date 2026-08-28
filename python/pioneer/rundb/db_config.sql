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
    isSuccess BOOLEAN DEFAULT false, -- flag to signal that this shall be treated as successful completion
    isFailure BOOLEAN DEFAULT false, -- flag to signal that the job execution will never succeed.
    isPending BOOLEAN DEFAULT false, -- flag to signal that this job is awaiting execution but has not been touched yet.
    isRunning BOOLEAN DEFAULT false, -- flag to signal that this job is currently executed.
    isUser    BOOLEAN DEFAULT false  -- flag to signal that the status has been assigned due to user intervention
);


-- pending states
INSERT INTO utils.status (name, description, isPending) VALUES
    ('HOLDING'  , 'Put on hold by user interaction',                          true),
    ('PENDING'  , 'Waiting for resources to become available',                true),
    ('DEPENDING', 'Depends on a job that did neither succeed nor fail',       true)
ON CONFLICT (name) DO NOTHING;

INSERT INTO utils.status (name, description, isRunning) VALUES
    ('CLAIMED'  , 'Locked and should change to RUNNING immediately',          true),
    ('RUNNING'  , 'Execution started and termination was not registered yet', true),
    ('RUNSDONE' , 'Jobs in Sequence completed, ready for postprocessing',     true),
    ('PPROC'    , 'Job Sequence in post-processing',                          true)
ON CONFLICT (name) DO NOTHING;

INSERT INTO utils.status (name, description, isSuccess) VALUES
    ('DONE'     , 'Job completed successfully (return code 0)',               true)
ON CONFLICT (name) DO NOTHING;

INSERT INTO utils.status (name, description, isFailure) VALUES
    ('FAILED'   , 'Job terminated with any return code other than 0',         true),
    ('BLOCKED'  , 'Job has a dependency that will never be met',              true),
    ('ERROR'    , 'An error occured, user investigation required',            true),
    ('CANCELLED', 'Cancelled by user interaction',                            true)
ON CONFLICT (name) DO NOTHING;

UPDATE utils.status SET isUser = true WHERE name IN ('HOLDING', 'CANCELLED');

CREATE OR REPLACE FUNCTION utils.is_success(s TEXT)
RETURNS BOOLEAN AS $$
    SELECT isSuccess FROM utils.status WHERE name = s;
$$ LANGUAGE SQL STABLE;

CREATE OR REPLACE FUNCTION utils.is_failure(s TEXT)
RETURNS BOOLEAN AS $$
    SELECT isFailure FROM utils.status WHERE name = s;
$$ LANGUAGE SQL STABLE;

CREATE OR REPLACE FUNCTION utils.is_user(s TEXT)
RETURNS BOOLEAN AS $$
    SELECT isUser FROM utils.status WHERE name = s;
$$ LANGUAGE SQL STABLE;

CREATE OR REPLACE FUNCTION utils.is_pending(s TEXT)
RETURNS BOOLEAN AS $$
    SELECT isPending FROM utils.status WHERE name = s;
$$ LANGUAGE SQL STABLE;

CREATE OR REPLACE FUNCTION utils.is_running(s TEXT)
RETURNS BOOLEAN AS $$
    SELECT isRunning FROM utils.status WHERE name = s;
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

-- Detector position table
CREATE TABLE IF NOT EXISTS config.target_position (
    id INT PRIMARY KEY REFERENCES config.configuration(id), -- Reference to the main configuration table
    seq_id INT DEFAULT 0,                                   -- helper to encode position sequences that are often executed together, e.g. 5 point measurement
    xpos FLOAT,                                             -- x position where the target should be placed, in mm
    ypos FLOAT                                              -- y position where the target should be placed, in mm
);

WITH positions(rn, seq_id, xpos, ypos) AS (
    SELECT *
    FROM (
        VALUES
            (1, 1,  0.0,   0.0),    -- single point measurement, centre only
            (2, 2,  0.0,   0.0),    -- 5 point measurement, centre
            (3, 2, 17.0,  17.0),    -- 5 point measurement, top right
            (4, 2,-17.0,  17.0),    -- 5 point measurement, top left
            (5, 2, 17.0, -17.0),    -- 5 point measurement, bottom right
            (6, 2,-17.0, -17.0)     -- 5 point measurement, bottom left
    ) v(rn, seq_id, xpos, ypos)
),
configs AS (
    INSERT INTO config.configuration (config_type)
    SELECT 'target_position'
    FROM positions
    ORDER BY rn
    RETURNING id
),
config_ids AS (
    SELECT id, row_number() OVER (ORDER BY id) AS rn
    FROM configs
)
INSERT INTO config.target_position (id, seq_id, xpos, ypos)
SELECT c.id, p.seq_id, p.xpos, p.ypos
FROM config_ids c
JOIN positions p USING (rn);

-- -------------------------
-- STATE SCHEMA
-- -------------------------

CREATE TABLE IF NOT EXISTS state.run_sequence (
    id SERIAL PRIMARY KEY,
    status TEXT REFERENCES utils.status(name),
    on_complete TEXT
);

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

-- This table is used because people anticipate the concept of
-- subruns, where the trivial mapping run_id -> run{run_id : 5d}.mid.lz4
-- stops working. Hence a table that maps run_id -> files associated
CREATE TABLE IF NOT EXISTS state.file_list (
    id SERIAL PRIMARY KEY,                      -- internal primary key
    run_id INT REFERENCES state.midas_run(id),  -- id of the attributed midas run in this DB
    filebase TEXT NOT NULL,                     -- the base name of the file (e.g. run00042)
    fileext  TEXT NOT NULL,                     -- the extionsion of the file (e.g. mid.lz4)
    producer TEXT DEFAULT NULL,                 -- producer of the file (e.g. logger_0 for midas logger channel 0)
    status TEXT REFERENCES utils.status(name)   -- status the file is currently in
);

CREATE TABLE IF NOT EXISTS state.runs_in_sequence (
    id SERIAL PRIMARY KEY,
    seq_id INT REFERENCES state.run_sequence(id),
    midas_run_id INT REFERENCES state.midas_run(id)
);

-- list of postprocessing jobs.
CREATE TABLE IF NOT EXISTS state.postproc_job (
    id SERIAL PRIMARY KEY,
    midas_run_id INT REFERENCES state.midas_run(id),
    file_id INT REFERENCES state.file_list(id),
    job_type TEXT,
    priority INT,
    status TEXT REFERENCES utils.status(name)
);

CREATE OR REPLACE FUNCTION state.set_postproc_job_priority()
RETURNS trigger AS $$
BEGIN
    IF NEW.priority IS NULL THEN
        SELECT COALESCE(MAX(priority), 0) + 1
        INTO NEW.priority
        FROM state.postproc_job j
        WHERE utils.is_pending(j.status);
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_postproc_job_priority
BEFORE INSERT ON state.postproc_job
FOR EACH ROW
EXECUTE FUNCTION state.set_postproc_job_priority();

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
    IF NOT EXISTS ( -- no dependency exists
            SELECT 1
            FROM state.postproc_depends
            WHERE pp_job_id = p_job_id
        )
        OR NOT EXISTS ( -- no dependcy that is not in success state, aka all dependencies succeeded.
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
          AND NOT utils.is_user(status)          -- protect user-assigned states
          AND status IS DISTINCT FROM 'PENDING'; -- do not reassign the same state and fire extra triggers
    ELSE
        UPDATE state.postproc_job
        SET status = 'DEPENDING'
        WHERE id = p_job_id
          AND NOT utils.is_user(status)            -- protect user-assigned states
          AND status IS DISTINCT FROM 'DEPENDING'; -- do not reassign the same state and fire extra triggers
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION state.recompute_sequence_state(aSeqID INT)
RETURNS VOID AS $$
BEGIN

    -- check if any run associated with this sequence failed in any capacity.
    IF EXISTS ( -- check status of the midas run itself, aka the sequencer part.
            SELECT 1
            FROM state.runs_in_sequence AS ris
            JOIN state.midas_run AS mr ON ris.midas_run_id = mr.id
            WHERE ris.seq_id = aSeqID AND utils.is_failure(mr.status)
        )
        OR EXISTS ( -- check the status of the nearline postprocessing of the midas run
            SELECT 1
            FROM state.runs_in_sequence AS ris
            JOIN state.postproc_job AS ppj ON ris.midas_run_id = ppj.midas_run_id
            WHERE ris.seq_id = aSeqID AND ppj.job_type = 'nearline' AND utils.is_failure(ppj.status)
        )
    THEN
        UPDATE state.run_sequence SET status = 'FAILED' WHERE id = aSeqID;
        RETURN;
    END IF;

    -- check if ALL midas runs are in a pending state.
    -- This does not require checking post-processing as those
    -- by their very nature only spawn after the midas run completed.
    IF NOT EXISTS (
            SELECT 1
            FROM state.runs_in_sequence AS ris
            JOIN state.midas_run AS mr ON ris.midas_run_id = mr.id
            WHERE ris.seq_id = aSeqID AND NOT utils.is_pending(mr.status)
        )
    THEN
        UPDATE state.run_sequence SET status = 'PENDING' WHERE id = aSeqID;
        RETURN;
    END IF;

    -- check if all runs are in a success state
    -- check if any run associated with this sequence failed in any capacity.
    IF NOT EXISTS ( -- check status of the midas run itself, aka the sequencer part.
            SELECT 1
            FROM state.runs_in_sequence AS ris
            JOIN state.midas_run AS mr ON ris.midas_run_id = mr.id
            WHERE ris.seq_id = aSeqID AND NOT utils.is_success(mr.status)
        )
        AND NOT EXISTS ( -- check the status of the nearline postprocessing of the midas run
            SELECT 1
            FROM state.runs_in_sequence AS ris
            JOIN state.postproc_job AS ppj ON ris.midas_run_id = ppj.midas_run_id
            WHERE ris.seq_id = aSeqID AND ppj.job_type = 'nearline' AND NOT utils.is_success(ppj.status)
        )
    THEN
        -- just update to `RUNSDONE` if we are currently in status `RUNNING`. We have no intention
        -- of overwriting any other state futher down the line, e.g. `PPROC` or `DONE`

        UPDATE state.run_sequence
        SET status = CASE
                        WHEN on_complete is NULL THEN 'DONE' -- All jobs in the sequence are done and there is nothing to do on completion. We are all done
                        ELSE 'RUNSDONE'                      -- All jobs in the sequence are done and there is some other job to be scheduled on completion of the sequence.
                     END
        WHERE
            id = aSeqID
            AND (status = 'RUNNING'             -- default path that should be taken by the statemachine
                                 OR utils.is_pending(status)  -- This one would be very weird where all runs suddenly go from pending to success.
                                                              -- May happen if a user manually progresses the state machine
                                 OR utils.is_failure(status)  -- This sequence had a failed run and now all are in success state.
                                                              -- This path is taken if a shifter fixed a failed run.
            );
        RETURN;
    ELSE
        UPDATE state.run_sequence SET status = 'RUNNING' WHERE id = aSeqID;
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

CREATE OR REPLACE FUNCTION state.trg_pp_status_change()
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

    -- recompute sequence state
    FOR r IN
        SELECT seq_id
        FROM state.runs_in_sequence
        WHERE midas_run_id = NEW.midas_run_id
    LOOP
        PERFORM state.recompute_sequence_state(r.seq_id);
    END LOOP;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER pp_status_change
AFTER UPDATE OF status ON state.postproc_job
FOR EACH ROW
EXECUTE FUNCTION state.trg_pp_status_change();

CREATE OR REPLACE FUNCTION state.trg_mr_status_change()
RETURNS trigger AS $$
DECLARE
    r RECORD;
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;

    -- recompute sequence state
    FOR r IN
        SELECT seq_id
        FROM state.runs_in_sequence
        WHERE midas_run_id = NEW.id
    LOOP
        PERFORM state.recompute_sequence_state(r.seq_id);
    END LOOP;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER mr_status_change
AFTER UPDATE OF status ON state.midas_run
FOR EACH ROW
EXECUTE FUNCTION state.trg_mr_status_change();

-- -------------------------
-- LOGS SCHEMA (mostly append-only)
-- -------------------------
CREATE TABLE IF NOT EXISTS logs.slow_control (
    id BIGSERIAL PRIMARY KEY,           -- internal primary key
    midas_run_number INT,               -- run number to which this log entry belongs
    reason TEXT NOT NULL,               -- what caused this log entry (see below)
    log_time TIMESTAMPTZ DEFAULT now(), -- time at which the log was created
    upd_time TIMESTAMPTZ NOT NULL,      -- time at which the ODB value was last updated
    equipment TEXT,                     -- the equipment this parameter is attributed to
    channel TEXT,                       -- slow control parameter monitored
    label TEXT,                         -- the name given the SC parameter in the frontend
    reading TEXT                        -- value of sc parameter monitored

    CHECK ( reason IN (
        'BOR',      -- Begin of run
        'EOR',      -- End of run
        'UPDATE',   -- The ODB value has changed significantly
        'PERIODIC', -- Time since last log entry exceeded timeout
        'ENABLE',   -- This channel got newly added.
        'DISABLE',  -- This channel got disabled.
        'VANISHED'  -- This channel vanished completely
        ))
);

CREATE TABLE IF NOT EXISTS logs.last_sc_update(
    channel TEXT PRIMARY KEY,            -- channel
    upd_time TIMESTAMPTZ NOT NULL,       -- last recorded update time
    log_time TIMESTAMPTZ DEFAULT now()   -- time at which this record was added
);

CREATE OR REPLACE FUNCTION logs.filter_slow_control_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    last_upd timestamptz;
BEGIN
    IF NEW.reason NOT IN ('BOR', 'EOR', 'ENABLE', 'DISABLE', 'VANISHED') THEN
        SELECT upd_time
          INTO last_upd
          FROM logs.last_sc_update
         WHERE channel = NEW.channel FOR UPDATE;

        IF FOUND AND last_upd = NEW.upd_time THEN
            RETURN NULL;
        END IF;
    END IF;

    INSERT INTO logs.last_sc_update(channel, upd_time)
    VALUES (NEW.channel, NEW.upd_time)
    ON CONFLICT (channel)
    DO UPDATE SET
        upd_time = EXCLUDED.upd_time,
        log_time = now()
    ;

    RETURN NEW;
END;
$$;

CREATE TRIGGER slow_control_filter
BEFORE INSERT ON logs.slow_control
FOR EACH ROW
EXECUTE FUNCTION logs.filter_slow_control_insert();


-- =========================================================
-- role specific permissions on selected tables
-- =========================================================

-- BOT: write access only where needed
GRANT INSERT ON config.configuration   TO bot;
GRANT INSERT ON state.midas_run        TO bot;
GRANT INSERT ON state.postproc_job     TO bot;
GRANT INSERT ON state.postproc_depends TO bot;
GRANT INSERT ON state.run_sequence     TO bot;
GRANT INSERT ON state.runs_in_sequence TO bot;

GRANT USAGE, SELECT ON SEQUENCE config.configuration_id_seq    TO bot;
GRANT USAGE, SELECT ON SEQUENCE state.midas_run_id_seq         TO bot;
GRANT USAGE, SELECT ON SEQUENCE state.midas_run_config_id_seq  TO bot;
GRANT USAGE, SELECT ON SEQUENCE state.postproc_job_id_seq      TO bot;
GRANT USAGE, SELECT ON SEQUENCE state.run_sequence_id_seq      TO bot;
GRANT USAGE, SELECT ON SEQUENCE state.runs_in_sequence_id_seq  TO bot;

GRANT UPDATE (status, midas_run_number) ON state.midas_run     TO bot;
GRANT UPDATE (status)                   ON state.postproc_job  TO bot;
GRANT UPDATE (status)                   ON state.run_sequence  TO bot;
-- The daemon closes file rows at end-of-run (close_files_in_channel) and
-- records job results on them (update_file_status); without this the EOR
-- callback dies on permission denied and the run never leaves RUNNING.
GRANT UPDATE (status)                   ON state.file_list     TO bot;

GRANT UPDATE (upd_time, log_time) ON logs.last_sc_update TO bot;

-- SHIFTER: broader control
-- a shifter may mark a configuration as faulty
GRANT UPDATE (do_not_use) ON config.configuration TO shifter;
