

import psycopg
import psycopg.sql
import psycopg.rows

from pioneer.rundb.config import connect


class interface:
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

    def register_run(self, status : str) -> int:
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO state.midas_run (status) VALUES (%s) RETURNING id",
                (status, )
            )
            run_id = cursor.fetchone()[0]
        conn.commit()
        conn.close()
        return run_id

    def start_of_midas_run(self, run_id : int, run_number : int):
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            # Mark the run in the job-list as complete
            cursor.execute(
                """
                UPDATE state.midas_run
                SET
                    status = 'RUNNING',
                    midas_run_number = %s
                WHERE id = %s
                """,
                (run_number, run_id)
            )
        conn.commit()
        conn.close()

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

    def get_run_times(self, midas_run_numbers : int | list[int]) -> dict:
        """Start and stop time of MIDAS runs `midas_run_numbers`.

        Returns ``{run number: {"bor": datetime | None, "eor": datetime | None}}``
        with an entry for every number asked about.  The times are the
        ``log_time`` of the begin-of-run and end-of-run rows the slow-control
        logger writes to ``logs.slow_control`` (earliest BOR, latest EOR, as
        the run database page shows them); a run without such a row gets None.
        Read-only.
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

    def schedule_postproc_job(self, run_id : int, task : str):
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO state.postproc_job (midas_run_id, job_type, status)
                VALUES (%s, %s, 'PENDING') ON CONFLICT DO NOTHING RETURNING id
                """, (run_id, task)
            )
            result = cursor.fetchone()
            if result is not None:
                job_id = result[0]
            else:
                job_id = -1
        conn.commit()
        conn.close()
        return job_id

    def schedule_postproc_job_on_file(self, file_id : int, task : str):
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO
                    state.postproc_job (midas_run_id, file_id, job_type, status)
                SELECT fl.run_id, fl.id, %s, 'PENDING'
                FROM state.file_list AS fl
                WHERE fl.id = %s
                ON CONFLICT DO NOTHING
                RETURNING id
                """, (task, file_id)
            )
            result = cursor.fetchone()
            if result is not None:
                job_id = result[0]
            else:
                job_id = -1
        conn.commit()
        conn.close()
        return job_id

    def end_of_midas_run(self, run_id : int, schedule_post_processing : bool = True) -> bool:
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            # Mark the run in the job-list as complete
            cursor.execute(
                """
                UPDATE state.midas_run
                SET
                    status = 'DONE'
                WHERE id = %s AND status IS DISTINCT FROM 'DONE'
                RETURNING midas_run_number
                """,
                (run_id,)
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

        if (schedule_post_processing):
            # Schedule the backup jobs.
            self.schedule_postproc_job(run_id, 'backup')
            self.schedule_postproc_job(run_id, 'remote')

            # Create the cleanup job. This one does feature dependencies
            # Hence a more complex fill than the typical schedule_postproc_job
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    WITH new_job AS (
                        INSERT INTO state.postproc_job (midas_run_id, job_type, status)
                        VALUES (%s, 'cleanup', 'PENDING') ON CONFLICT DO NOTHING
                        RETURNING id, midas_run_id
                    )
                    INSERT INTO state.postproc_depends (pp_job_id, depends_on)
                    SELECT
                        new_job.id,
                        ppj.id
                    FROM new_job
                    JOIN state.postproc_job AS ppj
                    ON ppj.midas_run_id = new_job.midas_run_id
                    WHERE ppj.id <> new_job.id;
                    """,
                    (run_id,),
                )

        conn.commit()
        conn.close()
        return True

    def add_new_configuration(self, table : str, values : dict) -> int | None:
        """
        Insert a new configuration to the database.

        Arguments:
        - table: table name it shall be inserted to.
        - values: dict of config values to be inserted,

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
                    "INSERT INTO config.configuration (config_type) VALUES (%s) RETURNING id",
                    (table, )
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

    def schedule_new_run(self, num_ev :int,  configs : list) -> int:
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
                    INSERT INTO state.midas_run (priority, status, requested_events)
                    VALUES (%s, 'PENDING', %s)
                    RETURNING id
                    """,
                    (priority, num_ev)
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
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT midas_run_id FROM state.runs_in_sequence WHERE seq_id = %s", (id, )
            )
            run_ids = cursor.fetchall()
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
        conn = connect(user = self.user, password = self.password)
        retVal = True
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE state.postproc_job SET status = %s WHERE id = %s", (new_status, job_id)
            )
        conn.commit()
        conn.close()
        return retVal

    def find_pending_postproc_jobs(self, job_type : str, max_jobs : int = 1) -> list:
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
        conn = connect(user = self.user, password = self.password)
        conn.autocommit = True
        with conn.cursor(row_factory = psycopg.rows.dict_row) as cursor:
            cursor.execute(
                    """
                    WITH claimed AS (
                        SELECT ppj.id, ppj.midas_run_id, mr.midas_run_number
                        FROM state.postproc_job AS ppj
                        JOIN state.midas_run AS mr ON ppj.midas_run_id = mr.id
                        WHERE ppj.status = 'PENDING'
                        AND ppj.job_type = %s
                        ORDER BY ppj.priority ASC
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE state.postproc_job s
                    SET status = 'CLAIMED'
                    FROM claimed
                    WHERE s.id = claimed.id
                    RETURNING
                        claimed.id as job_id,
                        claimed.midas_run_id as run_id,
                        claimed.midas_run_number as midas_run_number
                    """, (job_type, max_jobs,))
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
