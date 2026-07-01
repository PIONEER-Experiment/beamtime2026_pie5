

import psycopg2
import psycopg2.sql
import psycopg2.extras

from pioneer.rundb.config import connect


class interface:
    def __init__(self, user = "readonly", password = "readonly"):
        self.user = user
        self.password = password

    def find_next_config(self) -> dict | None:
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

        return self.load_config(next_job[0])

    def load_config(self, job_id) -> dict | None:
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
                table = cfg["config_type"]
                if (table in configuration.keys()):
                    raise ValueError(f"Found double reference to device {table}")
                config_id = cfg["config_id"]

                if cfg["do_not_use"]:
                    raise ValueError(
                        f"Job {job_id} tries to access configuration {config_id} marked as DO_NOT_USE"
                    )

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

    def end_of_midas_run(self, job_id : int, run_number : int, schedule_post_processing : bool = True) -> bool:
        conn = connect(self.user, self.password)
        with conn.cursor() as cursor:
            # Mark the run in the job-list as complete
            cursor.execute(f"UPDATE state.midas_run SET status = 'DONE', midas_run_number = {run_number} WHERE id = {job_id}")

            # Schedule the analysis jobs.
            if (schedule_post_processing):
                tasks = ['nearline', 'backup', 'remote', 'cleanup']
                task_ids = dict()
                for task in tasks:
                    cursor.execute(
                        """INSERT INTO state.postproc_job (midas_run_id, job_type, priority, status)
                           VALUES (%s, %s, %s, 'PENDING') RETURNING id"""
                           , (job_id, task, run_number)
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

                cursor.execute(
                    "INSERT INTO state.midas_run (priority, status) VALUES (%s, 'CONFIG') RETURNING id",
                    (priority, )
                )
                run_id = cursor.fetchone()[0]

                for icon, config in enumerate(configs):
                    cursor.execute(
                        "INSERT INTO state.midas_run_config (run_id, config_id, priority) VALUES (%s, %s, %s)",
                        (run_id, config, icon)
                    )

                cursor.execute(
                    "UPDATE state.midas_run SET status = 'PENDING' WHERE id = %s",
                    (run_id, )
                )

            conn.commit()
        finally:
            conn.close()

        return run_id

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