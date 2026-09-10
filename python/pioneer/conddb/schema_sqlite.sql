-- Conditions database schema, SQLite dialect, version 2.
--
-- Applied to an EMPTY database only (plain CREATE TABLE, no IF NOT EXISTS):
-- cond_loader.py refuses to touch a database whose recorded version is not 2,
-- and an accidental re-apply must fail loudly rather than silently agree with
-- whatever is already there.
--
-- The relational shape mirrors the JSON container format one-to-one (see
-- PICondSqliteLayer.h). Every invariant the C++ layers assume is written down
-- here, because a schema that only documents its invariants in comments will
-- eventually be violated by a hand-written INSERT:
--   * one default tag per table            (uq_tags_default)
--   * half-open, non-empty, non-negative intervals   (CHECKs on cond_iov)
--   * no two active intervals of one tag overlap     (triggers below)
--   * exactly one typed column populated per cell    (CHECK on cond_values)
--   * one cell per (tag, interval, channel/key, column, ordinal) (uq_values_cell)
-- SQLite cannot express the PostgreSQL EXCLUDE constraint, so the overlap rule
-- is enforced by triggers; the C++ layers repeat the check anyway because JSON
-- containers have no database to enforce anything.
--
-- Keep this file byte-identical to the copy embedded in
-- reco_testbeam/tests/test_conditions_sqlite.cpp between the
-- "// --- schema_sqlite.sql begin ---" / "end" markers; run_fake_closure.py
-- compares them.

CREATE TABLE cond_schema (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE cond_tables (
    name    TEXT PRIMARY KEY,
    schema  TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN ('channel_values', 'parameter_set'))
);

CREATE TABLE cond_tags (
    table_name  TEXT NOT NULL REFERENCES cond_tables(name),
    tag         TEXT NOT NULL,
    is_default  INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (table_name, tag)
);

-- At most one default tag per table: the resolver picks the default when no
-- tag is requested and errors on ambiguity, so ambiguity must not be storable.
CREATE UNIQUE INDEX uq_tags_default ON cond_tags (table_name) WHERE is_default;

CREATE TABLE cond_iov (
    table_name  TEXT NOT NULL REFERENCES cond_tables(name),
    row_id      INTEGER NOT NULL,
    tag         TEXT NOT NULL,
    run_start   INTEGER NOT NULL
        CONSTRAINT cond_iov_run_start_check CHECK (run_start >= 0),
    run_end     INTEGER,            -- EXCLUSIVE; NULL = open-ended
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    inserted_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    created_by  TEXT NOT NULL DEFAULT '',
    comment     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (table_name, row_id),
    CHECK (run_end IS NULL OR run_end > run_start),
    FOREIGN KEY (table_name, tag) REFERENCES cond_tags(table_name, tag)
);

CREATE INDEX idx_iov_active ON cond_iov (table_name, tag, is_active);

-- The one-active-interval-per-run contract, enforced on the two paths that can
-- create an overlap: inserting a row, and reactivating, retagging or moving an
-- existing one. Deactivating a row can never create an overlap but is covered
-- anyway.
CREATE TRIGGER cond_iov_no_overlap_insert
BEFORE INSERT ON cond_iov
FOR EACH ROW WHEN NEW.is_active = 1
BEGIN
    SELECT RAISE(ABORT, 'cond_iov: active intervals of one tag overlap')
    WHERE EXISTS (
        SELECT 1 FROM cond_iov o
        WHERE o.table_name = NEW.table_name
          AND o.tag        = NEW.tag
          AND o.is_active  = 1
          AND o.row_id    <> NEW.row_id
          AND NEW.run_start < COALESCE(o.run_end, 2147483647)
          AND o.run_start   < COALESCE(NEW.run_end, 2147483647)
    );
END;

CREATE TRIGGER cond_iov_no_overlap_update
BEFORE UPDATE OF is_active, run_start, run_end, tag, table_name ON cond_iov
FOR EACH ROW WHEN NEW.is_active = 1
BEGIN
    SELECT RAISE(ABORT, 'cond_iov: active intervals of one tag overlap')
    WHERE EXISTS (
        SELECT 1 FROM cond_iov o
        WHERE o.table_name = NEW.table_name
          AND o.tag        = NEW.tag
          AND o.is_active  = 1
          AND o.row_id    <> NEW.row_id
          AND NEW.run_start < COALESCE(o.run_end, 2147483647)
          AND o.run_start   < COALESCE(NEW.run_end, 2147483647)
    );
END;

CREATE TABLE cond_values (
    table_name  TEXT NOT NULL REFERENCES cond_tables(name),
    tag         TEXT NOT NULL,
    iov_row_id  INTEGER,            -- NULL = tag-wide payload; else per-interval
    channel_id  INTEGER,            -- channel_values kind
    key         TEXT NOT NULL DEFAULT '',  -- parameter_set kind
    column_name TEXT NOT NULL DEFAULT '',
    ordinal     INTEGER NOT NULL DEFAULT 0,
    value_type  TEXT NOT NULL CHECK (value_type IN ('int', 'real', 'text', 'bool', 'null')),
    value_int   INTEGER,
    value_real  REAL,
    value_text  TEXT,
    FOREIGN KEY (table_name, tag) REFERENCES cond_tags(table_name, tag),
    FOREIGN KEY (table_name, iov_row_id) REFERENCES cond_iov(table_name, row_id),
    -- value_type names exactly one populated column: a cell read as an int
    -- must not silently be a NULL that the C++ layer turns into 0.
    CHECK (
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

-- One cell per addressable slot. COALESCE because SQLite (like PostgreSQL)
-- treats NULLs in a unique index as distinct, which would let a duplicate
-- tag-wide cell through.
CREATE UNIQUE INDEX uq_values_cell ON cond_values (
    table_name, tag, COALESCE(iov_row_id, -1), COALESCE(channel_id, -1),
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
