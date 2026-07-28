

import psycopg2
import psycopg2.sql
import psycopg2.extras

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
        with conn.cursor(cursor_factory = psycopg2.extras.RealDictCursor) as cursor:
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

        with conn.cursor(cursor_factory = psycopg2.extras.RealDictCursor) as cur:
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

                print(query)
                cur.execute(query, (config_id,))
                row = cur.fetchone()

                if row is not None:
                    row.pop("id", None)
                    configuration[table] = row

        # done reading DB, close connection
        conn.close()

        configuration["job_id"] = job_id

        return configuration

    def load_config(self, table_name : str, cfg_id : int) -> dict | None:
        conn = connect(self.user, self.password)
        with conn.cursor(cursor_factory = psycopg2.extras.RealDictCursor) as cursor:
            table_ident = psycopg2.sql.Identifier(table_name)
            query = psycopg2.sql.SQL(
                    "SELECT * FROM config.{table} WHERE id = {value}"
                ).format(
                    table=table_ident,
                    value=psycopg2.sql.Placeholder(),
                )
            cursor.execute(
                query, (cfg_id,)
            )
            config = cursor.fetchone()
        return dict(config) if config is not None else None

    def load_config_sequence(self, table_name : str, seq_id : int):
        conn = connect(self.user, self.password)
        with conn.cursor(cursor_factory = psycopg2.extras.RealDictCursor) as cursor:
            table_ident = psycopg2.sql.Identifier(table_name)

            query = psycopg2.sql.SQL(
                    "SELECT * FROM config.{table} WHERE seq_id = {value}"
                ).format(
                    table=table_ident,
                    value=psycopg2.sql.Placeholder(),
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

    def end_of_midas_run(self, run_id : int, schedule_post_processing : bool = True) -> bool:
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            # Mark the run in the job-list as complete
            cursor.execute(
                """
                UPDATE state.midas_run
                SET
                    status = 'DONE'
                WHERE id = %s
                RETURNING midas_run_number
                """,
                (run_id,)
            )
            result = cursor.fetchone()
            if result is None:
                return False

            # Schedule the analysis jobs.
            if (schedule_post_processing):
                run_number = result[0]
                tasks = ['nearline', 'backup', 'remote', 'cleanup']
                task_ids = dict()
                for task in tasks:
                    cursor.execute(
                        """INSERT INTO state.postproc_job (midas_run_id, job_type, priority, status)
                           VALUES (%s, %s, %s, 'PENDING') RETURNING id"""
                           , (run_id, task, run_number)
                        )
                    task_ids[task] = cursor.fetchone()[0]

                # Lock cleanup until all other processes completed
                cleanup_id = task_ids['cleanup']
                for anID in task_ids.values():
                    if (anID == cleanup_id):
                        continue
                    cursor.execute("INSERT INTO state.postproc_depends VALUES (%s, %s)", (cleanup_id, anID))

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
                table_ident = psycopg2.sql.Identifier(table)
                cursor.execute(
                    "INSERT INTO config.configuration (config_type) VALUES (%s) RETURNING id",
                    (table, )
                )
                values['id'] = cursor.fetchone()[0]

                columns = list(values.keys())
                value_list = [values[col] for col in columns]


                columns_ident = psycopg2.sql.SQL(', ').join(
                    psycopg2.sql.Identifier(col) for col in columns
                )
                placeholders = psycopg2.sql.SQL(', ').join(
                    psycopg2.sql.Placeholder() for _ in columns
                )


                query = psycopg2.sql.SQL(
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

    def schedule_new_run(self, configs : list) -> int:
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
                    "INSERT INTO state.midas_run (priority, status) VALUES (%s, 'PENDING') RETURNING id",
                    (priority, )
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

    def register_sequence(self, run_ids : list, on_complete : str) -> bool:
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
        return True

    def find_sequences(self, status : str, limit : int = 1) -> list[dict]:
        conn = connect(user = self.user, password = self.password)
        with conn.cursor(cursor_factory = psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                "SELECT * FROM state.run_sequence WHERE status = %s LIMIT %s", (status, limit)
            )
            seqs = cursor.fetchall()
        return [dict(s) for s in seqs]

    def claim_sequences(self, limit : int = 1) -> list[dict]:
        conn = connect(user = self.user, password = self.password)
        with conn.cursor(cursor_factory = psycopg2.extras.RealDictCursor) as cursor:
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
        with conn.cursor(cursor_factory = psycopg2.extras.RealDictCursor) as cursor:
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
            table_ident = psycopg2.sql.Identifier(table)
            query = psycopg2.sql.SQL(
                    "UPDATE state.{table} SET status = {status} WHERE id = {value}"
                ).format(
                    table=table_ident,
                    status = psycopg2.sql.Placeholder(),
                    value=psycopg2.sql.Placeholder(),
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
        with conn.cursor(cursor_factory = psycopg2.extras.RealDictCursor) as cursor:
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
