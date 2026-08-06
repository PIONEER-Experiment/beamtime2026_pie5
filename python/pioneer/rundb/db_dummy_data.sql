

-- =========================================================
-- Create some fake entries to the database for testing
-- =========================================================


INSERT INTO config.configuration (config_type) VALUES 
    ('dummy'),
    ('dummy')
    ;

INSERT INTO config.dummy (id, p1, p2) VALUES
    (1, '15', '18'),
    (2, '12', '12')
    ;

-- Let's schedule two runs

INSERT INTO state.midas_run (priority, status, midas_run_number)
VALUES
    (1, 'PENDING', NULL),
    (2, 'PENDING', NULL);

-- and configure them
INSERT INTO state.midas_run_config(run_id, config_id, priority)
VALUES
    (1, 1, 1),
    (2, 2, 1)
    ;