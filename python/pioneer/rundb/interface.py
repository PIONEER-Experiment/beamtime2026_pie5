

import psycopg
import psycopg.sql
import psycopg.rows

from pioneer.rundb.config import connect


class interface:
    # The run-level stages schedule_run_post_processing queues, in the order
    # the stop transition queues them.
    RUN_STAGES = ("transfer", "farline")

    def __init__(self, user = "readonly", password = "readonly"):
        self.user = user
        self.password = password

    def find_next_run_config(self) -> dict | None:
        conn = connect(self.user, self.password)

        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id FROM state.midas_run WHERE status='PENDING' ORDER BY priority ASC LIMIT 1
                """
            )
            next_job = cursor.fetchone()
        conn.close()

        if (next_job is None):
            return None

        return self.load_run_config(next_job[0])

    def load_run_config(self, job_id) -> dict | None:
        conn = connect(self.user, self.password)
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            # Find job configuration
            cursor.execute(
                """
                SELECT
                    cfg.id as config_id,
                    cfg.config_type,
                    cfg.do_not_use
                FROM
                    state.midas_run AS mr
                       JOIN
                    state.midas_run_config AS mrc ON mr.id = mrc.run_id
                       JOIN
                    config.configuration AS cfg ON cfg.id = mrc.config_id
                WHERE
                    mr.id = %s
                ORDER BY mrc.priority
                """, (job_id, )
            )

            configs = cursor.fetchall()

        if (len(configs) == 0):
            print("empty configuration list")
            return None;
        print(configs)

        configuration = dict()

        with conn.cursor(row_factory = psycopg.rows.dict_row) as cur:
            for cfg in configs:
                try:
                    table = cfg["config_type"]
                    if (table in configuration.keys()):
                        raise ValueError(f"Found double reference to device {table}")
                    config_id = cfg["config_id"]

                    if cfg["do_not_use"]:
                        raise ValueError(
                            f"Job {job_id} tries to access configuration {config_id} marked as DO_NOT_USE"
                        )
                except ValueError:
                    # Something is wrong with this run. Let's put it in error state right now
                    cur.execute("UPDATE state.midas_run SET status='ERROR' WHERE id = %s", (job_id, ))
                    conn.commit()

                    # Raise the error again for the calling instance to deal with it.
                    raise

                query = f"""
                    SELECT *
                    FROM config.{table}
                    WHERE id = %s
                """

                cur.execute(query, (config_id,))
                row = cur.fetchone()

                if row is not None:
                    row.pop("id", None)
                    row.pop("seq_id", None)
                    configuration[table] = row
        with conn.cursor() as cur:
            cur.execute("SELECT requested_events FROM state.midas_run WHERE id = %s", (job_id, ))
            num_ev = cur.fetchone()[0]

        # done reading DB, close connection
        conn.close()

        configuration["job_id"] = job_id
        configuration["num_ev"] = num_ev

        return configuration

    def load_config(self, table_name : str, cfg_id : int) -> dict | None:
        conn = connect(self.user, self.password)
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            table_ident = psycopg.sql.Identifier(table_name)
            query = psycopg.sql.SQL(
                    "SELECT * FROM config.{table} WHERE id = {value}"
                ).format(
                    table=table_ident,
                    value=psycopg.sql.Placeholder(),
                )
            cursor.execute(
                query, (cfg_id,)
            )
            config = cursor.fetchone()
        return dict(config) if config is not None else None

    def load_config_sequence(self, table_name : str, seq_id : int):
        conn = connect(self.user, self.password)
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            table_ident = psycopg.sql.Identifier(table_name)

            query = psycopg.sql.SQL(
                    "SELECT * FROM config.{table} WHERE seq_id = {value}"
                ).format(
                    table=table_ident,
                    value=psycopg.sql.Placeholder(),
                )
            cursor.execute(
                query, (seq_id,)
            )
            configs = cursor.fetchall()
        return configs

    def register_run(self, status : str, author : str, note : str, quality : str) -> int:
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                WITH new_run AS (
                    INSERT INTO state.midas_run (status, quality)
                    VALUES (%s, %s) RETURNING id
                )
                INSERT INTO logs.run_annotations (run_id, author, note)
                SELECT new_run.id, %s, %s
                FROM new_run
                RETURNING run_id;
                """,
                (status, quality, author, note)
            )
            run_id = cursor.fetchone()[0]
        conn.commit()
        conn.close()
        return run_id

    def create_finished_run(self, run_number : int, start_time, stop_time, recorded_events : int | None,
                            quality : str | None, author : str, note : str) -> int:
        """
        Register MIDAS run `run_number` as a run that is over (status DONE),
        for a run the nearline daemon never saw start or stop. One row with
        its number, times and events plus its annotation, written in one
        transaction, so a failure leaves no half-made row. The times are
        passed on as given, like the daemon passes /Runinfo/Start time and
        /Runinfo/Stop time: MIDAS strings are read by the server in its time
        zone.

        Returns:
        The run database id of the new row.
        """
        conn = connect(self.user, self.password)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    WITH new_run AS (
                        INSERT INTO state.midas_run
                            (status, midas_run_number, start_time, stop_time, recorded_events, quality)
                        VALUES ('DONE', %s, %s, %s, %s, %s) RETURNING id
                    )
                    INSERT INTO logs.run_annotations (run_id, author, note)
                    SELECT new_run.id, %s, %s
                    FROM new_run
                    RETURNING run_id;
                    """,
                    (run_number, start_time, stop_time, recorded_events, quality, author, note)
                )
                run_id = cursor.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        return run_id

    def get_midas_run(self, run_id : int) -> dict | None:
        """Row `run_id` of state.midas_run as a dict (id, status,
        midas_run_number, start_time, stop_time, recorded_events, quality),
        or None."""
        conn = connect(self.user, self.password)
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            cursor.execute(
                """
                SELECT id, status, midas_run_number, start_time, stop_time, recorded_events, quality
                FROM state.midas_run WHERE id = %s
                """, (run_id, )
            )
            result = cursor.fetchone()
        conn.close()
        return dict(result) if result is not None else None

    def start_of_midas_run(self, run_id : int, run_number : int, start_time : str):
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            # Mark the run in the job-list as complete
            cursor.execute(
                """
                UPDATE state.midas_run
                SET
                    status = 'RUNNING',
                    midas_run_number = %s,
                    start_time = %s
                WHERE id = %s AND status IN ('PENDING', 'CLAIMED')
                RETURNING id
                """,
                (run_number, start_time, run_id)
            )
            id = cursor.fetchone()
            if id is None:
                cursor.execute(
                    "SELECT status FROM state.midas_run WHERE id = %s" , (run_id, )
                )
                result = cursor.fetchone()
                conn.rollback()
                if result is None:
                    raise RuntimeError(f"Can't start run with id {run_id}. No such run exists.")
                else:
                    raise RuntimeError(f"Can't start run with id {run_id}. Expected status to be 'PENDING' or 'CLAIMED', got {result[0]} instead")

        conn.commit()
        conn.close()
        return id[0]

    def get_midas_run_number(self, run_id : int) -> int | None:
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT midas_run_number FROM state.midas_run WHERE id = %s", (run_id, )
            )
            result = cursor.fetchone()
        conn.close()
        return result[0] if result is not None else None

    def get_run_id(self, midas_run_number : int) -> int | None:
        """Run database id of MIDAS run `midas_run_number` (the newest, should
        a number have been used twice), or None."""
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM state.midas_run WHERE midas_run_number = %s ORDER BY id DESC LIMIT 1",
                (midas_run_number, )
            )
            result = cursor.fetchone()
        conn.close()
        return result[0] if result is not None else None

    def get_run_times(self, midas_run_numbers : int | list[int], timeout_s : float = 5.0) -> dict:
        """Start and stop time of MIDAS runs `midas_run_numbers`.

        Returns ``{run number: {"bor": datetime | None, "eor": datetime | None}}``
        with an entry for every number asked about.  The times are the
        ``log_time`` of the begin-of-run and end-of-run rows the slow-control
        logger writes to ``logs.slow_control`` (earliest BOR, latest EOR, as
        the run database page shows them); a run without such a row gets None.
        Read-only.

        Without the index on (midas_run_number, reason) that db_viewer.sql
        creates this scans the whole log, which only grows: the query is
        cancelled after `timeout_s` seconds (statement_timeout, raised as
        psycopg.errors.QueryCanceled).
        """
        if isinstance(midas_run_numbers, int):
            midas_run_numbers = [midas_run_numbers]
        numbers = sorted({int(n) for n in midas_run_numbers})
        out = {n: {"bor": None, "eor": None} for n in numbers}
        if not numbers:
            return out
        conn = connect(self.user, self.password)
        try:
            with conn.cursor() as cursor:
                # an int, not user input: SET takes no bind parameters
                cursor.execute("SET LOCAL statement_timeout = '%dms'"
                               % max(1, int(float(timeout_s) * 1000)))
                cursor.execute(
                    """
                    SELECT midas_run_number,
                           min(log_time) FILTER (WHERE reason = 'BOR'),
                           max(log_time) FILTER (WHERE reason = 'EOR')
                    FROM logs.slow_control
                    WHERE reason IN ('BOR', 'EOR')
                      AND midas_run_number = ANY(%s)
                    GROUP BY midas_run_number
                    """, (numbers, )
                )
                for number, started, stopped in cursor.fetchall():
                    out[number] = {"bor": started, "eor": stopped}
        finally:
            conn.close()
        return out

    def schedule_postproc_job(self, run_id : int, task : str, client : str, dependencies : str | list[int] | None = None):
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            if not dependencies:
                # No dependencies, plain insert
                cursor.execute(
                    """
                    INSERT INTO state.postproc_job (midas_run_id, client, job_type, status)
                    VALUES (%s, %s, %s, 'PENDING')
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """, (run_id, client, task)
                )
            elif dependencies == "all":
                # This shall depend on all already scheduled jobs for the same midas run
                cursor.execute(
                    """
                    WITH new_job AS (
                        INSERT INTO state.postproc_job (midas_run_id, client, job_type, status)
                        VALUES (%s, %s, %s, 'PENDING') ON CONFLICT DO NOTHING
                        RETURNING id, midas_run_id
                    )
                    INSERT INTO state.postproc_depends (pp_job_id, depends_on)
                    SELECT
                        new_job.id,
                        ppj.id
                    FROM new_job
                    JOIN state.postproc_job AS ppj
                    ON ppj.midas_run_id = new_job.midas_run_id
                    WHERE ppj.id <> new_job.id
                    RETURNING pp_job_id
                    """,
                    (run_id, client, task)
                )
            elif isinstance(dependencies, (list, tuple)) and all(isinstance(x, int) for x in dependencies):
                # WE got a list of dependencies
                cursor.execute(
                    """
                    WITH new_job AS (
                        INSERT INTO state.postproc_job (midas_run_id, client, job_type, status)
                        VALUES (%s, %s, %s, 'PENDING')
                        ON CONFLICT DO NOTHING
                        RETURNING id
                    )
                    INSERT INTO state.postproc_depends (pp_job_id, depends_on)
                    SELECT
                        new_job.id,
                        ppj.id
                    FROM new_job
                    JOIN state.postproc_job AS ppj
                        ON ppj.id = ANY(%s)
                    RETURNING pp_job_id
                    """, (run_id, client, task, dependencies)
                )
            else:
                # An option I have not yet thought of.
                raise RuntimeError("Either you mistyped the dependency or I forgot to implement it.")
            result = cursor.fetchone()
            if result is None:
                conn.rollback()
                return None

            job_id = result[0]
        conn.commit()
        conn.close()
        return job_id

    def schedule_postproc_job_on_file(self, file_id : int, task : str, client : str, dependencies : str | list[int] | None = None):
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            if not dependencies:
                cursor.execute(
                    """
                    INSERT INTO state.postproc_job
                        (midas_run_id, file_id, client, job_type, status)
                    SELECT fl.run_id, fl.id, %s, %s, 'PENDING'
                    FROM state.file_list AS fl
                    WHERE fl.id = %s
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """, (client, task, file_id)
                )
            elif isinstance(dependencies, (list, tuple)) and all(isinstance(x, int) for x in dependencies):
                cursor.execute(
                    """
                    WITH new_job AS (
                        INSERT INTO state.postproc_job
                            (midas_run_id, file_id, client, job_type, status)
                        SELECT fl.run_id, fl.id, %s, %s, 'PENDING'
                        FROM state.file_list AS fl
                        WHERE fl.id = %s
                        ON CONFLICT DO NOTHING
                        RETURNING id
                    )
                    INSERT INTO state.postproc_depends (pp_job_id, depends_on)
                    SELECT new_job.id, ppj.id
                    FROM new_job
                    JOIN state.postproc_job AS ppj ON ppj.id = ANY(%s)
                    RETURNING pp_job_id
                    """, (client, task, file_id, list(dependencies))
                )
            else:
                raise RuntimeError("Either you mistyped the dependency or I forgot to implement it.")
            result = cursor.fetchone()
            if result is not None:
                job_id = result[0]
            else:
                job_id = -1
        conn.commit()
        conn.close()
        return job_id

    def end_of_midas_run(self, run_id : int, recorded_events : int, stop_time : str,
                         schedule_post_processing : bool = True) -> bool:
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            # Mark the run in the job-list as complete
            cursor.execute(
                """
                UPDATE state.midas_run
                SET
                    status = 'DONE',
                    stop_time = %s,
                    recorded_events = %s
                WHERE id = %s AND status IS DISTINCT FROM 'DONE'
                RETURNING midas_run_number
                """,
                (stop_time, recorded_events, run_id)
            )
            result = cursor.fetchone()
            if result is None:
                # No update was done. This is either because
                # a) no such run exists (bad)
                # b) it was already updated (fine)
                cursor.execute(
                    """
                    SELECT 1 FROM state.midas_run WHERE id = %s
                    """, (run_id, )
                )
                result2 = cursor.fetchone()

                # roll back and close the connection for good measure
                conn.rollback()
                conn.close()
                if result2 is None:
                    # run id does not exist, return False
                    return False
                else:
                    # run id exists, but was already in 'DONE' state.
                    return True
        conn.commit()
        conn.close()

        if (schedule_post_processing):
            return self.schedule_run_post_processing(run_id)

        return True

    def schedule_run_post_processing(self, run_id : int, stages = RUN_STAGES,
                                     existing_ok : bool = False, outcome : list | None = None) -> bool:
        """
        Queue the run-level post-processing of run `run_id`: the jobs the stop
        transition queues once the run's last file is closed.

        `stages` picks from RUN_STAGES:
        - 'transfer' (client 'nearline', pinky): raw backup, remote copy and
          cleanup. The cleanup depends on every job of the run queued before
          it, so the per-file nearline jobs have to be queued first.
        - 'farline' (client 'farline', piana): one full job per raw file and
          the SSD->HDD backup, all waiting for the remote copy.

        With `existing_ok` False (the stop transition) a run-level job that is
        already there ends the scheduling with False, as it always did. With
        `existing_ok` True (pioneer.nearline.requeue) a job that is already
        there is fine and its id is looked up: the farline jobs need the id
        of a remote copy queued earlier. Then False means a job could neither
        be queued nor found, or 'farline' was asked for and the run has no
        remote copy job to wait for.

        `outcome`, if given, gets one dict per job: client, job_type, file_id,
        job_id and created (False: it was already there).

        Returns:
        True when everything asked for is queued.
        """
        ok = True

        def note(client, task, file_id, job_id, created):
            if outcome is not None:
                outcome.append({"client" : client, "job_type" : task, "file_id" : file_id,
                                "job_id" : job_id, "created" : created})

        def run_job(task, client, dependencies = None):
            job_id = self.schedule_postproc_job(run_id, task, client, dependencies)
            if job_id is not None:
                note(client, task, None, job_id, True)
                return job_id
            if not existing_ok:
                return None
            existing = self.find_postproc_job(run_id, client, task)
            if existing is None:
                return None
            note(client, task, None, existing['id'], False)
            return existing['id']

        remote_job_id = None
        if 'transfer' in stages:
            # Schedule the backup jobs.
            if run_job('backup', 'nearline') is None:
                return False
            remote_job_id = run_job('remote', 'nearline')
            if remote_job_id is None:
                return False
            if run_job('cleanup', 'nearline', 'all') is None:
                return False

        if 'farline' in stages:
            if remote_job_id is None:
                existing = self.find_postproc_job(run_id, 'nearline', 'remote')
                if existing is None:
                    return False
                remote_job_id = existing['id']

            # List all registered files
            all_files = self.find_files(run_ids = [run_id], extensions = ['mid.lz4'])
            for f in all_files:
                job_id = self.schedule_postproc_job_on_file(f['id'], 'farline', 'farline', [remote_job_id])
                if job_id != -1:
                    note('farline', 'farline', f['id'], job_id, True)
                elif existing_ok:
                    existing = self.find_postproc_job(run_id, 'farline', 'farline', f['id'])
                    if existing is None:
                        ok = False
                    else:
                        note('farline', 'farline', f['id'], existing['id'], False)

            # farline backup SSD->HDD. The stop transition never failed over it.
            if run_job('backup', 'farline', [remote_job_id]) is None and existing_ok:
                ok = False

        return ok

    def find_postproc_job(self, run_id : int, client : str, job_type : str, file_id : int | None = None) -> dict | None:
        """The job of run `run_id` with this client, type and file (None: a
        run-level job) as {"id", "status"}, or None. There is at most one,
        by the unique index on those four columns."""
        conn = connect(self.user, self.password)
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            cursor.execute(
                """
                SELECT id, status FROM state.postproc_job
                WHERE midas_run_id = %s AND client = %s AND job_type = %s
                AND file_id IS NOT DISTINCT FROM %s
                """, (run_id, client, job_type, file_id)
            )
            result = cursor.fetchone()
        conn.close()
        return dict(result) if result is not None else None

    def find_postproc_jobs(self, run_id : int) -> list[dict]:
        """Every post-processing job of run `run_id`, in id order, as
        {"id", "file_id", "client", "job_type", "status"}."""
        conn = connect(self.user, self.password)
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            cursor.execute(
                """
                SELECT id, file_id, client, job_type, status FROM state.postproc_job
                WHERE midas_run_id = %s ORDER BY id
                """, (run_id, )
            )
            result = cursor.fetchall()
        conn.close()
        return [dict(r) for r in result]

    def find_dependents(self, job_ids : list[int]) -> list[dict]:
        """Every job that waits for one of `job_ids`, directly or through
        other jobs (state.postproc_depends), as {"id", "file_id", "client",
        "job_type", "status"}, in id order. These are the jobs the database
        recomputes when one of `job_ids` changes status."""
        if not job_ids:
            return []
        conn = connect(self.user, self.password)
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            cursor.execute(
                """
                WITH RECURSIVE dependent(id) AS (
                    SELECT pp_job_id FROM state.postproc_depends WHERE depends_on = ANY(%s)
                    UNION
                    SELECT d.pp_job_id FROM state.postproc_depends d JOIN dependent ON d.depends_on = dependent.id
                )
                SELECT j.id, j.file_id, j.client, j.job_type, j.status
                FROM state.postproc_job j JOIN dependent ON dependent.id = j.id
                ORDER BY j.id
                """, (list(job_ids), )
            )
            result = cursor.fetchall()
        conn.close()
        return [dict(r) for r in result]

    def reset_jobs(self, run_id : int, job_types : list[str], clients : list[str],
                   file_ids : list[int] | None = None) -> list[int]:
        """
        Put the DONE and FAILED jobs of run `run_id` with one of `job_types`
        and one of `clients` back to PENDING, so the daemons run them again.
        `file_ids`, if given, limits it to the jobs on those files. A job
        that is CLAIMED or RUNNING belongs to a daemon and is never touched,
        nor is one a person put on hold or cancelled.

        Each reset job then has its state recomputed from its dependencies,
        as the database does when a dependency changes: a cleanup reset
        together with the jobs it waits for goes to DEPENDING instead of
        running at once, and one that waits for a failed job to BLOCKED.

        Returns:
        The ids of the jobs reset.
        """
        query = """
            UPDATE state.postproc_job SET status = 'PENDING'
            WHERE midas_run_id = %s AND job_type = ANY(%s) AND client = ANY(%s)
            AND status IN ('DONE', 'FAILED')
            """
        params = [run_id, list(job_types), list(clients)]
        if file_ids is not None:
            query += " AND file_id = ANY(%s)"
            params.append(list(file_ids))
        query += " RETURNING id"

        conn = connect(self.user, self.password)
        try:
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                job_ids = sorted(r[0] for r in cursor.fetchall())
                for job_id in job_ids:
                    cursor.execute("SELECT state.recompute_job_state(%s)", (job_id, ))
            conn.commit()
        finally:
            conn.close()
        return job_ids

    def add_new_configuration(self, table : str, values : dict, comment : str = "Mystery Configuration") -> int | None:
        """
        Insert a new configuration to the database.

        Arguments:
        - table: table name it shall be inserted to.
        - values: dict of config values to be inserted,
        - comment: Some description of the configuration

        Returns:
        Configuration ID created.
        """

        if not values:
            return None

        conn = connect(self.user, self.password)

        try:
            with conn.cursor() as cursor:
                # Create the parent table entry first
                table_ident = psycopg.sql.Identifier(table)
                cursor.execute(
                    "INSERT INTO config.configuration (config_type, comment) VALUES (%s, %s) RETURNING id",
                    (table, comment)
                )
                values['id'] = cursor.fetchone()[0]

                columns = list(values.keys())
                value_list = [values[col] for col in columns]


                columns_ident = psycopg.sql.SQL(', ').join(
                    psycopg.sql.Identifier(col) for col in columns
                )
                placeholders = psycopg.sql.SQL(', ').join(
                    psycopg.sql.Placeholder() for _ in columns
                )


                query = psycopg.sql.SQL(
                    "INSERT INTO config.{table} ({columns}) VALUES ({values}) RETURNING id"
                ).format(
                    table=table_ident,
                    columns=columns_ident,
                    values=placeholders,
                )
                cursor.execute(query, value_list)
                inserted_id = cursor.fetchone()[0]

            conn.commit()
        finally:
            conn.close()
        return inserted_id

    def annotate_run_id(self, run_id : int, author : str, note : str):
        conn = connect(self.user, self.password)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO logs.run_annotations (run_id, author, note) VALUES (%s, %s, %s)",
                    (run_id, author, note)
                )
            conn.commit()
        finally:
            conn.close()

    def annotate_run_number(self, run_number : int, author : str, note : str):
        conn = connect(self.user, self.password)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO logs.run_annotations (run_id, author, note)
                    SELECT mr.id, %s, %s FROM state.midas_run AS mr
                    WHERE mr.midas_run_number = %s LIMIT 1
                    """,
                    (author, note, run_number)
                )
            conn.commit()
        finally:
            conn.close()

    def schedule_new_run(self, num_ev :int,  configs : list, author : str, note : str,
                         quality : str | None = None) -> int:
        """
        Schedule a new run in the midas_run table

        Parameters:
         - configs: list of configurations in config.configuration to be used.

        `priority` shall be increased by 1 w.r.t. largest value of any `PENDING`
        job. `status` shall be `PENDING`

        Returns:
        Job ID of the newly created job.
        """

        if not configs:
            raise ValueError("No configuration for run provided")

        conn = connect(self.user, self.password)
        try:
            with conn.cursor() as cursor:

                cursor.execute(
                    "SELECT MAX(priority) FROM state.midas_run WHERE status = 'PENDING'"
                )
                max_priority = cursor.fetchone()[0]
                priority = 1 if max_priority is None else max_priority + 1

                # We enter right into status 'PENDING' despite not having all sub-configurations
                # registered. This is fine as it becomes visible only after committing down below.
                cursor.execute(
                    """
                    WITH new_run AS (
                        INSERT INTO state.midas_run (priority, status, requested_events, quality)
                        VALUES (%s, 'PENDING', %s, %s)
                        RETURNING id
                    )
                    INSERT INTO logs.run_annotations (run_id, author, note)
                    SELECT new_run.id, %s, %s
                    FROM new_run
                    RETURNING run_id;
                    """,
                    (priority, num_ev, quality, author, note)
                )
                run_id = cursor.fetchone()[0]

                for icon, config in enumerate(configs):
                    cursor.execute(
                        "INSERT INTO state.midas_run_config (run_id, config_id, priority) VALUES (%s, %s, %s)",
                        (run_id, config, icon)
                    )

            conn.commit()
        finally:
            conn.close()

        return run_id

    def validate_run_number(self, run_id : int, run_number : int):
        conn = connect(user = self.user, password= self.password)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM state.midas_run WHERE id = %(id)s AND midas_run_number = %(nr)s
                """, { "id" : run_id, "nr" : run_number})
            result = cursor.fetchone()[0]
        conn.close()
        return bool(result)


    def register_sequence(self, run_ids : list, on_complete : str) -> int:
        """Create a sequence around `run_ids`; returns the new sequence id."""
        conn = connect(user = self.user, password = self.password)

        with conn.cursor() as cursor:
            # Step 1: Add sequence
            cursor.execute(
                "INSERT INTO state.run_sequence (status, on_complete) VALUES ('PENDING', %s) RETURNING id", (on_complete, )
            )
            seq_id = cursor.fetchone()[0]
            for run_id in run_ids:
                cursor.execute(
                    "INSERT INTO state.runs_in_sequence (seq_id, midas_run_id) VALUES (%s, %s)", (seq_id, run_id)
                )
        conn.commit()
        conn.close()
        return seq_id

    def get_sequence_progress(self, seq_id : int) -> dict | None:
        """
        Status of a sequence, its runs and their nearline jobs, for progress
        reports. None when the sequence does not exist. Otherwise
        {"id", "status", "on_complete", "runs": [{"run_db_id", "run_number",
        "status", "requested_events", "nearline_total", "nearline_done",
        "nearline_failed"}]}, runs in id order.
        """
        conn = connect(user = self.user, password = self.password)
        try:
            with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
                cursor.execute(
                    "SELECT id, status, on_complete FROM state.run_sequence WHERE id = %s", (seq_id, )
                )
                seq = cursor.fetchone()
                if seq is None:
                    return None
                cursor.execute(
                    """
                    SELECT
                        mr.id AS run_db_id,
                        mr.midas_run_number AS run_number,
                        mr.status,
                        mr.requested_events,
                        COUNT(ppj.id) AS nearline_total,
                        COUNT(ppj.id) FILTER (WHERE utils.is_success(ppj.status)) AS nearline_done,
                        COUNT(ppj.id) FILTER (WHERE utils.is_failure(ppj.status)) AS nearline_failed
                    FROM state.runs_in_sequence AS ris
                    JOIN state.midas_run AS mr ON ris.midas_run_id = mr.id
                    LEFT JOIN state.postproc_job AS ppj
                        ON ppj.midas_run_id = mr.id AND ppj.job_type = 'nearline'
                    WHERE ris.seq_id = %s
                    GROUP BY mr.id
                    ORDER BY mr.id
                    """, (seq_id, )
                )
                runs = [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()
        result = dict(seq)
        result["runs"] = runs
        return result

    def find_sequences(self, status : str, limit : int = 1) -> list[dict]:
        conn = connect(user = self.user, password = self.password)
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            cursor.execute(
                "SELECT * FROM state.run_sequence WHERE status = %s LIMIT %s", (status, limit)
            )
            seqs = cursor.fetchall()
        return [dict(s) for s in seqs]

    def claim_sequences(self, limit : int = 1) -> list[dict]:
        conn = connect(user = self.user, password = self.password)
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            cursor.execute(
                    """
                    WITH claimed AS (
                        SELECT id
                        FROM state.run_sequence
                        WHERE status = 'RUNSDONE'
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE state.run_sequence s
                    SET status = 'CLAIMED'
                    FROM claimed
                    WHERE s.id = claimed.id
                    RETURNING
                        s.*
                    """, (limit,))
            seqs = cursor.fetchall()
        conn.commit()
        conn.close()
        return [dict(s) for s in seqs]

    def get_sequence_entry(self, id : int) -> dict | None:
        conn = connect(user = self.user, password = self.password)
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            cursor.execute(
                "SELECT * FROM state.run_sequence WHERE id = %s", (id, )
            )
            result = cursor.fetchone()
        return dict(result) if result is not None else result

    def get_all_runs_in_sequence(self, id : int) -> list[int]:
        conn = connect(user = self.user, password = self.password)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT midas_run_id FROM state.runs_in_sequence WHERE seq_id = %s", (id, )
                )
                run_ids = cursor.fetchall()
        finally:
            conn.close()
        return [r[0] for r in run_ids]


    def update_status(self, table : str, id : int, new_status : str) -> bool:
        conn = connect(user = self.user, password = self.password)
        retVal = True
        with conn.cursor() as cursor:
            table_ident = psycopg.sql.Identifier(table)
            query = psycopg.sql.SQL(
                    "UPDATE state.{table} SET status = {status} WHERE id = {value}"
                ).format(
                    table=table_ident,
                    status = psycopg.sql.Placeholder(),
                    value=psycopg.sql.Placeholder(),
                )
            cursor.execute(
                query, (new_status, id)
            )
        conn.commit()
        conn.close()
        return retVal


    def update_postproc_status(self, job_id : int, new_status : str) -> bool:
        return self.update_status("postproc_job", job_id, new_status)

    def find_pending_postproc_jobs(self, job_type : str | list[str], client : str, max_jobs : int = 1) -> list:
        """
        Find jobs in the `state.postproc_job` table

        Parameters:
        - `job_type` : Select jobs of this type only.
        - `max_jobs`: The maximum number of jobs to be returned.

        Returns:
        A list of job configurations that has no more than `max_job` entries.

        Each selected database entry shall change status from `PENDING` to `CLAIMED`.
        Database entries shall be picked based on `priority` where highest
        priorities are denoted by smallest values.
        """
        if isinstance(job_type, str):
            job_type = [job_type]
        conn = connect(user = self.user, password = self.password)
        conn.autocommit = True
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            cursor.execute(
                    """
                    WITH claimed AS (
                        SELECT
                            ppj.id,
                            ppj.midas_run_id,
                            ppj.file_id,
                            ppj.job_type,
                            mr.midas_run_number,
                            fl.producer
                        FROM state.postproc_job AS ppj
                        JOIN state.midas_run AS mr
                            ON ppj.midas_run_id = mr.id
                        LEFT JOIN state.file_list AS fl
                            ON fl.id = ppj.file_id
                        WHERE ppj.status = 'PENDING'
                        AND ppj.job_type = ANY(%s)
                        AND ppj.client = %s
                        ORDER BY ppj.priority ASC
                        LIMIT %s
                        FOR UPDATE OF ppj SKIP LOCKED
                    )
                    UPDATE state.postproc_job AS s
                    SET status = 'CLAIMED'
                    FROM claimed
                    WHERE s.id = claimed.id
                    RETURNING
                        claimed.id AS job_id,
                        claimed.job_type as job_type,
                        claimed.midas_run_id AS run_id,
                        claimed.file_id AS file_id,
                        claimed.midas_run_number AS midas_run_number,
                        claimed.producer AS producer
                    """, (job_type, client, max_jobs,))
            results = cursor.fetchall()
        conn.close()

        return results

    def open_file(self, writer : str, run_id : int, file_name : str):
        # Note: partition will split on the first dot, most pathlib utils on the last.
        # We want to split on the first dot to separate
        # run00042.mid.lz4 -> (run00042, mid.lz4)
        file_base, _, file_ext = file_name.partition('.')

        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO state.file_list (run_id, filebase, fileext, producer, status)
                VALUES (%s, %s, %s, %s, 'RUNNING') RETURNING id
                """, (run_id, file_base, file_ext, writer)
            )
            result = cursor.fetchone()
        conn.commit()
        conn.close()
        return result[0] if result else None

    def register_logger_file(self, run_id : int, filebase : str, fileext : str, channel : int | str = 0) -> int:
        """Register a raw file MIDAS logger channel `channel` has finished
        writing (status DONE), for a file the daemon never saw opened.
        open_file followed by the close of the stop transition, in one row.

        Returns:
        The file's id in state.file_list.
        """
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO state.file_list (run_id, filebase, fileext, producer, status)
                VALUES (%s, %s, %s, %s, 'DONE') RETURNING id
                """, (run_id, filebase, fileext, f"logger_{channel}")
            )
            result = cursor.fetchone()
        conn.commit()
        conn.close()
        return result[0]

    def close_files_in_channel(self, logger_channel : int):
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE state.file_list
                SET status = 'DONE'
                WHERE utils.is_running(status)
                AND producer = %s
                RETURNING id
                """, (f"logger_{logger_channel}", )
            )
            file_ids = cursor.fetchall()
        conn.commit()
        conn.close()
        return [i[0] for i in file_ids]

    def update_file_status(self, file_id : int, status : str):
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            cursor.execute("UPDATE state.file_list SET status = %s WHERE id = %s", (status, file_id))
        conn.commit()
        conn.close()

    def find_files(self, run_ids : int | list[int], extensions : str | list[str]):
        if isinstance(extensions, str):
            extensions = [extensions]
        if isinstance(run_ids, int):
            run_ids = [run_ids]
        conn = connect(self.user, self.password)
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            cursor.execute(
                """
                SELECT * FROM state.file_list
                WHERE run_id = ANY(%s) AND fileext = ANY(%s)
                ORDER BY filebase
                """, (run_ids, extensions)
            )
            result = cursor.fetchall()
        conn.close()
        return result

    def find_job_file(self, job_id : int):
        conn = connect(self.user, self.password)
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            cursor.execute(
                """
                SELECT fl.* FROM state.file_list AS fl
                JOIN state.postproc_job AS ppj
                ON ppj.file_id = fl.id
                WHERE ppj.id = %s
                """, (job_id,)
            )
            result = cursor.fetchone()
        conn.close()
        return result

    def log_sc_values(self, midas_run_number : int, reason : str, log_values : list[dict]) -> None:
        conn = connect(user = self.user, password = self.password)
        with conn.cursor() as cursor:
            for entry in log_values:
                cursor.execute(
                    """INSERT INTO logs.slow_control (midas_run_number, reason, upd_time, equipment, channel, label, reading) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (midas_run_number, reason, entry.get('upd_time'), entry.get('equipment'), entry.get('channel'), entry.get('label'), entry.get('reading'))
                )
        conn.commit()
        conn.close()
