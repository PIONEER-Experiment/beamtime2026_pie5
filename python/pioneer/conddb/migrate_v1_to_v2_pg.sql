-- Migrate a PostgreSQL conditions database from schema v1 to v2.
--
-- v1 is what json2pg.py created before 2026-09-08: the same five columns per
-- table, but with none of the invariants enforced and no cond_schema row.
-- v2 adds the constraints (see schema_pg.sql for the annotated definitions);
-- the column layout is unchanged, so no data is rewritten.
--
--     docker exec -i testbeam-pgdb psql -U postgres -d conditions \
--         -v ON_ERROR_STOP=1 < migrate_v1_to_v2_pg.sql
--
-- The whole migration is one transaction: a v1 database that violates any of
-- the new invariants -- duplicate cells, overlapping active intervals, two
-- default tags, a value_type that does not match its populated column -- fails
-- the migration and stays v1. That is deliberate. Such a database was already
-- serving ambiguous constants; fix the data (or, for a dev database, drop it
-- and reload from the JSON containers) rather than weakening the schema.

BEGIN;

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE cond_schema (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_at BIGINT  NOT NULL DEFAULT (EXTRACT(EPOCH FROM now())::bigint)
);

-- cond_tags -----------------------------------------------------------------
UPDATE cond_tags SET description = '' WHERE description IS NULL;
ALTER TABLE cond_tags ALTER COLUMN description SET DEFAULT '';
ALTER TABLE cond_tags ALTER COLUMN description SET NOT NULL;
CREATE UNIQUE INDEX uq_tags_default ON cond_tags (table_name) WHERE is_default;

-- cond_iov ------------------------------------------------------------------
UPDATE cond_iov SET created_by = '' WHERE created_by IS NULL;
UPDATE cond_iov SET comment    = '' WHERE comment    IS NULL;
ALTER TABLE cond_iov ALTER COLUMN created_by SET DEFAULT current_user;
ALTER TABLE cond_iov ALTER COLUMN created_by SET NOT NULL;
ALTER TABLE cond_iov ALTER COLUMN comment    SET DEFAULT '';
ALTER TABLE cond_iov ALTER COLUMN comment    SET NOT NULL;
ALTER TABLE cond_iov ALTER COLUMN inserted_at
    SET DEFAULT (EXTRACT(EPOCH FROM now())::bigint);
ALTER TABLE cond_iov ADD CONSTRAINT cond_iov_run_start_check
    CHECK (run_start >= 0);
ALTER TABLE cond_iov ADD CONSTRAINT cond_iov_run_end_check
    CHECK (run_end IS NULL OR run_end > run_start);
ALTER TABLE cond_iov ADD CONSTRAINT cond_iov_tag_fkey
    FOREIGN KEY (table_name, tag)
    REFERENCES cond_tags(table_name, tag) ON DELETE CASCADE;
ALTER TABLE cond_iov ADD CONSTRAINT cond_iov_no_overlap EXCLUDE USING gist (
    table_name WITH =,
    tag WITH =,
    (int4range(run_start, COALESCE(run_end, 2147483647))) WITH &&
) WHERE (is_active);
CREATE INDEX idx_iov_active ON cond_iov (table_name, tag, is_active);

-- cond_values ---------------------------------------------------------------
ALTER TABLE cond_values ADD CONSTRAINT cond_values_tag_fkey
    FOREIGN KEY (table_name, tag)
    REFERENCES cond_tags(table_name, tag) ON DELETE CASCADE;
ALTER TABLE cond_values ADD CONSTRAINT cond_values_iov_fkey
    FOREIGN KEY (table_name, iov_row_id)
    REFERENCES cond_iov(table_name, row_id) ON DELETE CASCADE;
ALTER TABLE cond_values ADD CONSTRAINT cond_values_typed_column_check CHECK (
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
);
CREATE UNIQUE INDEX uq_values_cell ON cond_values (
    table_name, tag, (COALESCE(iov_row_id, -1)), (COALESCE(channel_id, -1)),
    key, column_name, ordinal
);
CREATE INDEX idx_values_iov ON cond_values (
    table_name, iov_row_id, channel_id, key, column_name, ordinal
);
CREATE INDEX idx_values_tag ON cond_values (
    table_name, tag, channel_id, key, column_name, ordinal
) WHERE iov_row_id IS NULL;
DROP INDEX IF EXISTS idx_values;

INSERT INTO cond_schema (version) VALUES (2);

COMMIT;
