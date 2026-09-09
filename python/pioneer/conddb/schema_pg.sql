-- Conditions database schema, PostgreSQL dialect, version 2.
--
-- Applied to an EMPTY database (or an empty schema) only: plain CREATE TABLE,
-- no IF NOT EXISTS, so an accidental re-apply fails loudly instead of quietly
-- agreeing with whatever is already there. cond_loader.py applies this file
-- when the recorded schema version is 0 and refuses to write otherwise.
--
-- Same relational shape as schema_sqlite.sql; the differences are dialect
-- only, except that PostgreSQL can express the non-overlap rule directly as an
-- EXCLUDE constraint, so the campaign store enforces it in the server rather
-- than in a trigger. The C++ layers repeat the check for every backend because
-- JSON containers have no server at all.
--
-- Every constraint is named, with the names migrate_v1_to_v2_pg.sql gives it,
-- so that a database created here and a database migrated there are the same
-- database down to the error messages an operator sees.
--
-- Keep this file byte-identical to the copy embedded in
-- reco_testbeam/tests/test_conditions_pg.cpp between the
-- "// --- schema_pg.sql begin ---" / "end" markers; run_fake_closure.py
-- compares them.

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE cond_schema (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_at BIGINT  NOT NULL DEFAULT (EXTRACT(EPOCH FROM now())::bigint)
);

CREATE TABLE cond_tables (
    name    TEXT PRIMARY KEY,
    schema  TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN ('channel_values', 'parameter_set'))
);

CREATE TABLE cond_tags (
    table_name  TEXT NOT NULL REFERENCES cond_tables(name) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    is_default  BOOLEAN NOT NULL DEFAULT FALSE,
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (table_name, tag)
);

-- At most one default tag per table: the resolver picks the default when no
-- tag is requested and errors on ambiguity, so ambiguity must not be storable.
CREATE UNIQUE INDEX uq_tags_default ON cond_tags (table_name) WHERE is_default;

CREATE TABLE cond_iov (
    table_name  TEXT NOT NULL REFERENCES cond_tables(name) ON DELETE CASCADE,
    row_id      INTEGER NOT NULL,
    tag         TEXT NOT NULL,
    run_start   INTEGER NOT NULL
        CONSTRAINT cond_iov_run_start_check CHECK (run_start >= 0),
    run_end     INTEGER,            -- EXCLUSIVE; NULL = open-ended
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    inserted_at BIGINT  NOT NULL DEFAULT (EXTRACT(EPOCH FROM now())::bigint),
    created_by  TEXT    NOT NULL DEFAULT current_user,
    comment     TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (table_name, row_id),
    CONSTRAINT cond_iov_run_end_check
        CHECK (run_end IS NULL OR run_end > run_start),
    CONSTRAINT cond_iov_tag_fkey FOREIGN KEY (table_name, tag)
        REFERENCES cond_tags(table_name, tag) ON DELETE CASCADE,
    -- The one-active-interval-per-run contract, enforced by the server. NULL
    -- run_end is the open end, spelled as the INT_MAX sentinel the C++ layers use.
    CONSTRAINT cond_iov_no_overlap EXCLUDE USING gist (
        table_name WITH =,
        tag WITH =,
        (int4range(run_start, COALESCE(run_end, 2147483647))) WITH &&
    ) WHERE (is_active)
);

CREATE INDEX idx_iov_active ON cond_iov (table_name, tag, is_active);

CREATE TABLE cond_values (
    table_name  TEXT NOT NULL REFERENCES cond_tables(name) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    iov_row_id  INTEGER,            -- NULL = tag-wide payload; else per-interval
    channel_id  INTEGER,            -- channel_values kind
    key         TEXT NOT NULL DEFAULT '',  -- parameter_set kind
    column_name TEXT NOT NULL DEFAULT '',
    ordinal     INTEGER NOT NULL DEFAULT 0,
    value_type  TEXT NOT NULL CHECK (value_type IN ('int', 'real', 'text', 'bool', 'null')),
    value_int   BIGINT,
    value_real  DOUBLE PRECISION,
    value_text  TEXT,
    CONSTRAINT cond_values_tag_fkey FOREIGN KEY (table_name, tag)
        REFERENCES cond_tags(table_name, tag) ON DELETE CASCADE,
    CONSTRAINT cond_values_iov_fkey FOREIGN KEY (table_name, iov_row_id)
        REFERENCES cond_iov(table_name, row_id) ON DELETE CASCADE,
    -- value_type names exactly one populated column: a cell read as an int
    -- must not silently be a NULL that the C++ layer turns into 0.
    CONSTRAINT cond_values_typed_column_check CHECK (
        (value_type = 'int'  AND value_int  IS NOT NULL
                             AND value_real IS NULL AND value_text IS NULL)
     OR (value_type = 'real' AND value_real IS NOT NULL
                             AND value_int  IS NULL AND value_text IS NULL)
     OR (value_type = 'text' AND value_text IS NOT NULL
                             AND value_int  IS NULL AND value_real IS NULL)
     OR (value_type = 'bool' AND value_int  IN (0, 1)
                             AND value_real IS NULL AND value_text IS NULL)
     OR (value_type = 'null' AND value_int  IS NULL
                             AND value_real IS NULL AND value_text IS NULL)
    )
);

-- One cell per addressable slot. COALESCE because NULLs in a unique index are
-- distinct, which would let a duplicate tag-wide cell through.
CREATE UNIQUE INDEX uq_values_cell ON cond_values (
    table_name, tag, (COALESCE(iov_row_id, -1)), (COALESCE(channel_id, -1)),
    key, column_name, ordinal
);

-- The two read paths: per-interval payload by iov row, tag-wide payload by tag.
CREATE INDEX idx_values_iov ON cond_values (
    table_name, iov_row_id, channel_id, key, column_name, ordinal
);
CREATE INDEX idx_values_tag ON cond_values (
    table_name, tag, channel_id, key, column_name, ordinal
) WHERE iov_row_id IS NULL;

INSERT INTO cond_schema (version) VALUES (2);
