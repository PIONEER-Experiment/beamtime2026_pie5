"""The read side of the run database: six views, plus a command line for them.

Everything a shifter is shown about the run database comes through this module
-- the custom page asks the RPC server, the RPC server asks these methods, and
`python -m pioneer.rundb.view` asks exactly the same methods from a shell.  So
whatever the page says can always be checked by hand, including when MIDAS is
down.

The views are:

    status      is the database reachable, what is in it, what the status names mean
    runlog      runs newest first, paged by database id
    queue       everything pending or running, in the order the sequencer takes it
    run         one run in full: configurations with values, files, jobs, sequence
    sequences   one line per scan with the state of its member runs
    config      one configuration row with its typed values

Two things are cached for the life of the process: the start and stop time of
runs that have finished (they cannot change, and the BOR/EOR lookup is a scan
over a table that only grows), and the list of tables and columns in schema
`config` (the whitelist that a `config_type` coming out of the data is checked
against before it is put into a query).

No method here writes, takes a lock, or reuses `pioneer.rundb.interface`: the
interface marks a run as ERROR when it meets a configuration it does not like,
which is not something a page refreshing every few seconds may do.
"""

import argparse
import json
import sys
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from pioneer.rundb import pg

# Bumped by hand whenever the JSON these views produce changes shape, so a page
# talking to an older client can say so.
VERSION = "1"

# How many runs' start/stop times are kept in memory.
TIME_CACHE_SIZE = 2000

# How long the times of a run that is not over yet may be reused.  Short,
# because the numbers are still moving; long enough that a queue poll every few
# seconds does not scan the slow-control table every time.
UNFINISHED_TIME_TTL_S = 30.0

# Shown instead of a configuration list for runs that predate the run database
# holding their settings.
NO_CONFIG_NOTE = "no configuration recorded for this run"


class ViewError(Exception):
    """A view could not be produced.

    `kind` is one of the kinds the JSON contract names -- `usage` for a bad
    argument, `db` for anything the database did or did not do -- and is what
    the caller turns into an error envelope.
    """

    def __init__(self, kind: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint


def jsonable(value):
    """Turn what psycopg hands back into something `json.dumps` accepts.

    Times keep their offset and lose anything below the second: the page shows
    them to a shifter, and a microsecond of a run start time is noise.
    """
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def _first_line(exc: Exception) -> str:
    """The first line of an exception's text, with no trailing punctuation.

    Postgres errors carry a DETAIL and a CONTEXT block that name table
    internals; the envelope is read by a shifter and gets the first line only.
    """
    text = str(exc).strip().splitlines()
    return text[0].strip() if text else exc.__class__.__name__


def _position(value) -> str:
    """One coordinate, or a question mark.

    Every position column in the configuration tables is nullable, and a row
    with a missing coordinate must not be able to take the whole runlog down.
    """
    return "?" if value is None else f"{value:g}"


def _summarise(row: dict) -> str:
    """The one-line summary of a configuration shown in the runlog and queue.

    Target and degrader positions are what a shifter steers by, so they are
    spelled out; everything else is named by type and id, and the full values
    are one click away in the run detail.
    """
    config_type = row.get("config_type") or "configuration"
    has_target = row.get("target_x") is not None or row.get("target_y") is not None
    if config_type == "target_position" and has_target:
        return (f"target x={_position(row['target_x'])} "
                f"y={_position(row['target_y'])} mm")
    if config_type == "degrader_position" and row.get("degrader_x") is not None:
        comment = row.get("degrader_comment")
        text = f"degrader x={_position(row['degrader_x'])} mm"
        return f"{text} ({comment})" if comment else text
    return f"{config_type} #{row['config_id']}"


class RunDbView:
    """Read-only access to the run database, one short transaction per call.

    The connection is opened on first use and kept, and dropped again whenever
    the database errors, so a database restart costs one failed command rather
    than a restart of the client.
    """

    def __init__(
        self,
        dsn: str | None = None,
        timeout_ms: int = pg.DEFAULT_TIMEOUT_MS,
        client_name: str = "RunDBView",
    ):
        self.dsn = dsn or pg.default_dsn()
        self.timeout_ms = timeout_ms
        self.client_name = client_name
        self.started_at = time.time()
        self.last_error: str | None = None
        self._conn: psycopg.Connection | None = None
        self._times: OrderedDict = OrderedDict()
        self._tables: set | None = None
        self._columns: dict = {}
        self._flag_cache: dict | None = None

    # ------------------------------------------------------------------ plumbing

    def close(self) -> None:
        """Give the database connection back.  Safe to call more than once."""
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:
                pass
            self._conn = None

    def _connection(self) -> psycopg.Connection:
        if self._conn is not None and self._conn.closed:
            self._conn = None
        if self._conn is None:
            try:
                self._conn = pg.connect_readonly(self.dsn, self.timeout_ms)
            except pg.PgError as exc:
                raise ViewError("db", _first_line(exc)) from exc
        return self._conn

    @contextmanager
    def _cursor(self):
        """One read-only transaction with a dict cursor.

        Any database error drops the connection: after a failed query the
        transaction is aborted anyway, and reconnecting is cheaper to reason
        about than rolling back and hoping.
        """
        conn = self._connection()
        try:
            with pg.readonly_transaction(conn):
                with conn.cursor(row_factory=dict_row) as cur:
                    yield cur
        except psycopg.Error as exc:
            self.close()
            raise ViewError("db", _first_line(exc)) from exc

    def _flags(self, cur) -> dict:
        """The `utils.status` table as a lookup, read once per process."""
        if self._flag_cache is None:
            cur.execute(
                "SELECT name, description, issuccess, isfailure, ispending, "
                "isrunning, isuser FROM utils.status ORDER BY name"
            )
            self._flag_cache = {row["name"]: dict(row) for row in cur.fetchall()}
        return self._flag_cache

    # --------------------------------------------------------------- run pieces

    def _run_times(self, cur, runs: list) -> dict:
        """Start and stop time per MIDAS run number, from the BOR/EOR log rows.

        `logs.slow_control` has millions of rows and only its primary key, so
        this asks for as few run numbers as it can.  A run that has reached a
        final state *and* has an end-of-run row can never change again and is
        kept for good; anything else -- a run still going, or one marked done
        before its end-of-run row was committed -- is kept for a few seconds
        only, so that a queue poll does not scan the table every time.
        """
        flags = self._flags(cur)
        numbers = {run["midas_run_number"] for run in runs if run["midas_run_number"]}
        now = time.monotonic()
        out = {}
        missing = []
        for number in numbers:
            entry = self._times.get(number)
            if entry is not None and (entry[2] is None or now < entry[2]):
                self._times.move_to_end(number)
                out[number] = (entry[0], entry[1])
            else:
                missing.append(number)

        if missing:
            cur.execute(
                """
                SELECT midas_run_number,
                       min(log_time) FILTER (WHERE reason = 'BOR') AS started,
                       max(log_time) FILTER (WHERE reason = 'EOR') AS stopped
                FROM logs.slow_control
                WHERE reason IN ('BOR', 'EOR')
                  AND midas_run_number = ANY(%(numbers)s)
                GROUP BY midas_run_number
                """,
                {"numbers": sorted(missing)},
            )
            found = {
                row["midas_run_number"]: (row["started"], row["stopped"])
                for row in cur.fetchall()
            }
            for number in missing:
                out[number] = found.get(number, (None, None))

        finished = set()
        for run in runs:
            flag = flags.get(run["status"]) or {}
            if flag.get("issuccess") or flag.get("isfailure"):
                finished.add(run["midas_run_number"])
        for number in missing:
            started, stopped = out[number]
            forever = number in finished and stopped is not None
            self._times[number] = (started, stopped,
                                   None if forever else now + UNFINISHED_TIME_TTL_S)
        while len(self._times) > TIME_CACHE_SIZE:
            self._times.popitem(last=False)
        return out

    def _config_summaries(self, cur, run_ids: list) -> dict:
        """Per run, the short form of every configuration attached to it.

        Only the target and degrader tables are joined: they are what the
        summary spells out, and joining every configuration table would make
        the runlog query grow with the number of devices.
        """
        if not run_ids:
            return {}
        cur.execute(
            """
            SELECT mrc.run_id,
                   cfg.id AS config_id,
                   cfg.config_type,
                   cfg.do_not_use,
                   COALESCE(tp.seq_id, dp.seq_id) AS seq_id,
                   tp.xpos AS target_x,
                   tp.ypos AS target_y,
                   dp.xpos AS degrader_x,
                   dp.comment AS degrader_comment
            FROM state.midas_run_config AS mrc
            JOIN config.configuration AS cfg ON cfg.id = mrc.config_id
            LEFT JOIN config.target_position AS tp
                   ON tp.id = cfg.id AND cfg.config_type = 'target_position'
            LEFT JOIN config.degrader_position AS dp
                   ON dp.id = cfg.id AND cfg.config_type = 'degrader_position'
            WHERE mrc.run_id = ANY(%(ids)s)
            ORDER BY mrc.run_id, mrc.priority, mrc.id
            """,
            {"ids": list(run_ids)},
        )
        out: dict = {}
        for row in cur.fetchall():
            out.setdefault(row["run_id"], []).append(
                {
                    "config_id": row["config_id"],
                    "config_type": row["config_type"],
                    "do_not_use": bool(row["do_not_use"]),
                    "seq_id": row["seq_id"],
                    "summary": _summarise(row),
                }
            )
        return out

    def _files(self, cur, run_ids: list) -> dict:
        if not run_ids:
            return {}
        cur.execute(
            "SELECT id, run_id, filebase, fileext, producer, status "
            "FROM state.file_list WHERE run_id = ANY(%(ids)s) "
            "ORDER BY run_id, filebase, fileext",
            {"ids": list(run_ids)},
        )
        out: dict = {}
        for row in cur.fetchall():
            out.setdefault(row["run_id"], []).append(
                {
                    "id": row["id"],
                    "filebase": row["filebase"],
                    "fileext": row["fileext"],
                    "producer": row["producer"],
                    "status": row["status"],
                }
            )
        return out

    def _job_rollup(self, cur, run_ids: list) -> dict:
        """Jobs counted per type and status -- the runlog shows no job ids."""
        if not run_ids:
            return {}
        cur.execute(
            "SELECT midas_run_id, job_type, status, count(*) AS count "
            "FROM state.postproc_job WHERE midas_run_id = ANY(%(ids)s) "
            "GROUP BY midas_run_id, job_type, status "
            "ORDER BY midas_run_id, job_type, status",
            {"ids": list(run_ids)},
        )
        out: dict = {}
        for row in cur.fetchall():
            out.setdefault(row["midas_run_id"], []).append(
                {
                    "job_type": row["job_type"],
                    "status": row["status"],
                    "count": int(row["count"]),
                }
            )
        return out

    def _sequence_of(self, cur, run_ids: list) -> dict:
        """The sequence a run belongs to.

        A run could in principle be listed in more than one sequence; the
        lowest sequence id wins, because the row has room for one.
        """
        if not run_ids:
            return {}
        cur.execute(
            "SELECT ris.midas_run_id, rs.id, rs.status, rs.on_complete "
            "FROM state.runs_in_sequence AS ris "
            "JOIN state.run_sequence AS rs ON rs.id = ris.seq_id "
            "WHERE ris.midas_run_id = ANY(%(ids)s) "
            "ORDER BY ris.midas_run_id, rs.id",
            {"ids": list(run_ids)},
        )
        out: dict = {}
        for row in cur.fetchall():
            out.setdefault(
                row["midas_run_id"],
                {
                    "id": row["id"],
                    "status": row["status"],
                    "on_complete": row["on_complete"],
                },
            )
        return out

    def _member_counts(self, cur, seq_ids: list) -> dict:
        """Per sequence, how many member runs are in each status.

        Counted under the status names the database uses, so a reader can write
        "9 runs: 4 DONE, 1 RUNNING, 4 PENDING" without anybody inventing a
        vocabulary along the way.
        """
        if not seq_ids:
            return {}
        cur.execute(
            """
            SELECT ris.seq_id, mr.status, count(*) AS count
            FROM state.runs_in_sequence AS ris
            JOIN state.midas_run AS mr ON mr.id = ris.midas_run_id
            WHERE ris.seq_id = ANY(%(ids)s)
            GROUP BY ris.seq_id, mr.status
            ORDER BY ris.seq_id, mr.status
            """,
            {"ids": list(seq_ids)},
        )
        out: dict = {}
        for row in cur.fetchall():
            out.setdefault(row["seq_id"], {})[row["status"]] = int(row["count"])
        return out

    def _decorate(self, cur, runs: list) -> list:
        """Turn plain `state.midas_run` rows into the row shape the page draws."""
        if not runs:
            return []
        ids = [run["id"] for run in runs]
        configs = self._config_summaries(cur, ids)
        files = self._files(cur, ids)
        jobs = self._job_rollup(cur, ids)
        sequences = self._sequence_of(cur, ids)
        times = self._run_times(cur, runs)

        out = []
        for run in runs:
            number = run["midas_run_number"]
            started, stopped = times.get(number, (None, None))
            duration = None
            if started is not None and stopped is not None and stopped >= started:
                duration = int(round((stopped - started).total_seconds()))
            attached = configs.get(run["id"], [])
            out.append(
                jsonable(
                    {
                        "id": run["id"],
                        "run_number": number,
                        "status": run["status"],
                        "priority": run["priority"],
                        "requested_events": run["requested_events"],
                        "started": started,
                        "stopped": stopped,
                        "duration_s": duration,
                        "times_known": started is not None,
                        "configs": attached,
                        "config_note": None if attached else NO_CONFIG_NOTE,
                        "files": files.get(run["id"], []),
                        "jobs": jobs.get(run["id"], []),
                        "sequence": sequences.get(run["id"]),
                    }
                )
            )
        return out

    # ----------------------------------------------------------- config values

    def _config_tables(self, cur) -> set:
        """The tables that exist in schema `config`, read once per process.

        This is the whitelist: `config_type` is a string stored in a data row,
        so nothing built from it reaches a query until it has been found here.
        """
        if self._tables is None:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'config'"
            )
            self._tables = {row["table_name"] for row in cur.fetchall()}
        return self._tables

    def _config_column_names(self, cur, config_type: str) -> list:
        """Column names of one `config.*` table, in declaration order.

        Named one by one, and never taken with a wildcard, so that every
        identifier in the query has come out of the catalogue, and so the JSON
        keeps its key order when a migration adds a column.  Several of these names contain colons and
        dashes, which is why they are quoted as identifiers.
        """
        if config_type not in self._columns:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'config' AND table_name = %s "
                "ORDER BY ordinal_position",
                (config_type,),
            )
            self._columns[config_type] = [row["column_name"] for row in cur.fetchall()]
        return self._columns[config_type]

    def _config_values(self, cur, wanted: list) -> dict:
        """Values of the given (config_type, config_id) pairs.

        A type with no table of its own -- a typo, or a device added to the
        database but not to this schema -- yields no entry, and the caller
        reports `values: null` for it rather than failing the whole view.
        """
        known = self._config_tables(cur)
        by_type: dict = {}
        for config_type, config_id in wanted:
            if config_type in known:
                by_type.setdefault(config_type, set()).add(config_id)

        out: dict = {}
        for config_type, ids in by_type.items():
            columns = self._config_column_names(cur, config_type)
            if not columns:
                continue
            query = sql.SQL("SELECT {cols} FROM {table} WHERE id = ANY(%s)").format(
                cols=sql.SQL(", ").join(sql.Identifier(name) for name in columns),
                table=sql.Identifier("config", config_type),
            )
            cur.execute(query, (sorted(ids),))
            for row in cur.fetchall():
                out[(config_type, int(row["id"]))] = jsonable(dict(row))
        return out

    # ----------------------------------------------------------------- the views

    def status(self, actions_allowed: bool = False, actions_built: bool = False) -> dict:
        """What the client is, whether the database answers, and what is in it.

        The only view that survives an unreachable database: the page needs
        something to say, and "not answering" is the something.
        """
        database = {
            "dsn": pg.describe_dsn(self.dsn),
            "reachable": False,
            "server_version": None,
            "read_only": None,
        }
        counts = None
        statuses: list = []

        try:
            with self._cursor() as cur:
                cur.execute(
                    "SELECT current_setting('server_version') AS server_version, "
                    "current_setting('default_transaction_read_only') AS read_only"
                )
                row = cur.fetchone()
                database["reachable"] = True
                database["server_version"] = str(row["server_version"]).split()[0]
                database["read_only"] = row["read_only"] == "on"

                cur.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM state.midas_run) AS runs_total,
                      (SELECT count(*) FROM state.midas_run AS mr
                         JOIN utils.status AS s ON s.name = mr.status
                        WHERE s.ispending) AS queue_pending,
                      (SELECT count(*) FROM state.midas_run AS mr
                         JOIN utils.status AS s ON s.name = mr.status
                        WHERE s.isrunning) AS queue_running,
                      (SELECT count(*) FROM state.run_sequence AS rs
                         JOIN utils.status AS s ON s.name = rs.status
                        WHERE s.ispending OR s.isrunning) AS sequences_open,
                      (SELECT count(*) FROM state.postproc_job AS j
                         JOIN utils.status AS s ON s.name = j.status
                        WHERE s.ispending) AS jobs_pending,
                      (SELECT count(*) FROM state.postproc_job AS j
                         JOIN utils.status AS s ON s.name = j.status
                        WHERE s.isfailure) AS jobs_failed
                    """
                )
                counts = {key: int(value) for key, value in cur.fetchone().items()}

                # The rows of utils.status exactly as they are: name,
                # description and the five flags.  What a status means is the
                # database's to say, not this client's.
                statuses = [flags for _, flags in sorted(self._flags(cur).items())]
            # The database answered, so whatever went wrong before is history
            # and the page should stop showing it.
            self.last_error = None
        except ViewError as exc:
            self.last_error = exc.message

        return {
            "client": {
                "name": self.client_name,
                "uptime_s": int(time.time() - self.started_at),
                "actions_allowed": bool(actions_allowed),
                "actions_built": bool(actions_built),
                "version": VERSION,
                "last_error": self.last_error,
            },
            "database": database,
            "counts": counts,
            "statuses": jsonable(statuses),
        }

    def runlog(self, limit: int = 50, before_id: int | None = None) -> dict:
        """Runs newest first, paged by database id.

        Paged by `id` and not by run number: a run that has not started yet has
        no run number, and the page has to be able to walk past those.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT id, priority, status, midas_run_number, requested_events
                FROM state.midas_run
                WHERE (%(before_id)s::int IS NULL OR id < %(before_id)s::int)
                ORDER BY id DESC
                LIMIT %(limit)s
                """,
                {"before_id": before_id, "limit": limit},
            )
            runs = self._decorate(cur, list(cur.fetchall()))
        next_before_id = runs[-1]["id"] if len(runs) == limit and runs else None
        return {"next_before_id": next_before_id, "runs": runs}

    def queue(self, limit: int = 50) -> dict:
        """Everything pending or running, in the order the sequencer takes it.

        Lowest priority number first, which is what `find_next_run_config` does.
        Running rows are put on top so a shifter sees what is happening now.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT mr.id, mr.priority, mr.status, mr.midas_run_number,
                       mr.requested_events
                FROM state.midas_run AS mr
                JOIN utils.status AS s ON s.name = mr.status
                WHERE s.ispending OR s.isrunning
                ORDER BY s.isrunning DESC, mr.priority ASC, mr.id ASC
                LIMIT %(limit)s
                """,
                {"limit": limit},
            )
            rows = self._decorate(cur, list(cur.fetchall()))

            # Counted over the whole queue, not only over the page shown.
            cur.execute(
                """
                SELECT mr.status, count(*) AS count
                FROM state.midas_run AS mr
                JOIN utils.status AS s ON s.name = mr.status
                WHERE s.ispending OR s.isrunning
                GROUP BY mr.status
                """
            )
            # Keyed by the status name the database uses, so the page shows
            # the same words as psql does.
            counts = {row["status"]: int(row["count"]) for row in cur.fetchall()}

        for position, row in enumerate(rows, start=1):
            row["position"] = position
        # The database id of the run the sequencer would take next: the first
        # row that is PENDING and not held by a person.  None when the queue
        # holds nothing that would start on its own.
        next_up = next((row["id"] for row in rows if row["status"] == "PENDING"), None)
        return {"runs": rows, "counts": counts, "next_up": next_up}

    def run(self, run_id: int) -> dict:
        """One run in full: configuration values, files, jobs, sequence members."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT id, priority, status, midas_run_number, requested_events "
                "FROM state.midas_run WHERE id = %s",
                (run_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ViewError("usage", f"no run with database id {run_id}")
            run = self._decorate(cur, [dict(row)])[0]

            summaries = run["configs"]
            values = self._config_values(
                cur, [(item["config_type"], item["config_id"]) for item in summaries]
            )
            configs = [
                dict(item, values=values.get((item["config_type"], item["config_id"])))
                for item in summaries
            ]

            cur.execute(
                "SELECT id, midas_run_id, file_id, job_type, priority, status "
                "FROM state.postproc_job WHERE midas_run_id = %s ORDER BY id",
                (run_id,),
            )
            jobs = [dict(job) for job in cur.fetchall()]

            sequence = run["sequence"]
            if sequence is not None:
                cur.execute(
                    "SELECT mr.id, mr.midas_run_number, mr.status "
                    "FROM state.runs_in_sequence AS ris "
                    "JOIN state.midas_run AS mr ON mr.id = ris.midas_run_id "
                    "WHERE ris.seq_id = %s ORDER BY mr.id",
                    (sequence["id"],),
                )
                members = [
                    {
                        "id": member["id"],
                        "run_number": member["midas_run_number"],
                        "status": member["status"],
                    }
                    for member in cur.fetchall()
                ]
                # Counted here rather than asked for again: the rows are in hand.
                counts: dict = {}
                for member in members:
                    counts[member["status"]] = counts.get(member["status"], 0) + 1
                sequence = dict(sequence, runs=members,
                                counts=dict(sorted(counts.items())))

        return jsonable(
            {
                "run": run,
                "configs": configs,
                "files": run["files"],
                "jobs": jobs,
                "sequence": sequence,
            }
        )

    def sequences(self, limit: int = 20) -> dict:
        """One line per scan, with its member runs counted by status.

        The sequence's own status lags its members until the database trigger
        that recomputes it fires, so the counts are shown beside the status
        rather than instead of it.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT rs.id,
                       rs.status,
                       rs.on_complete,
                       count(mr.id) AS n_runs,
                       min(mr.midas_run_number) AS first_run,
                       max(mr.midas_run_number) AS last_run
                FROM state.run_sequence AS rs
                LEFT JOIN state.runs_in_sequence AS ris ON ris.seq_id = rs.id
                LEFT JOIN state.midas_run AS mr ON mr.id = ris.midas_run_id
                GROUP BY rs.id, rs.status, rs.on_complete
                ORDER BY rs.id DESC
                LIMIT %(limit)s
                """,
                {"limit": limit},
            )
            rows = [dict(row) for row in cur.fetchall()]
            counts = self._member_counts(cur, [row["id"] for row in rows])
            rows = [dict(row, counts=counts.get(row["id"], {})) for row in rows]
        return {"sequences": jsonable(rows)}

    def config(self, config_id: int) -> dict:
        """One `config.configuration` row with the values it points at."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT id, config_type, do_not_use FROM config.configuration "
                "WHERE id = %s",
                (config_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ViewError("usage", f"no configuration with id {config_id}")
            values = self._config_values(cur, [(row["config_type"], row["id"])])
            known = row["config_type"] in self._config_tables(cur)

        return jsonable(
            {
                "config": {
                    "config_id": row["id"],
                    "config_type": row["config_type"],
                    "do_not_use": bool(row["do_not_use"]),
                    "known_type": known,
                    "values": values.get((row["config_type"], row["id"])),
                }
            }
        )


# ------------------------------------------------------------------ command line


def _table(rows: list, columns: list) -> str:
    """A fixed-width table of `rows`, one `(heading, key)` pair per column."""
    if not rows:
        return "(nothing to show)"
    headings = [heading for heading, _ in columns]
    body = [
        ["" if row.get(key) is None else str(row.get(key)) for _, key in columns]
        for row in rows
    ]
    widths = [
        max(len(headings[i]), max(len(line[i]) for line in body))
        for i in range(len(columns))
    ]
    out = ["  ".join(head.ljust(widths[i]) for i, head in enumerate(headings))]
    out.append("  ".join("-" * width for width in widths))
    for line in body:
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)))
    return "\n".join(out)


def _config_text(row: dict) -> str:
    return "; ".join(item["summary"] for item in row.get("configs") or []) or (
        row.get("config_note") or ""
    )


def _counts_text(counts: dict) -> str:
    """"4 DONE, 1 RUNNING" -- member runs of a sequence, by status."""
    return ", ".join(f"{count} {name}" for name, count in (counts or {}).items())


def _job_text(row: dict) -> str:
    return " ".join(
        f"{item['job_type']}:{item['status']}({item['count']})"
        for item in row.get("jobs") or []
    )


def _print_status(data: dict) -> None:
    client = data["client"]
    database = data["database"]
    print(f"client   {client['name']} up {client['uptime_s']} s, version {client['version']}, "
          f"actions {'allowed' if client['actions_allowed'] else 'disabled'}")
    if client["last_error"]:
        print(f"         last error: {client['last_error']}")
    print(f"database {database['dsn']}")
    if not database["reachable"]:
        print("         not answering")
        return
    print(f"         PostgreSQL {database['server_version']}, "
          f"read only: {database['read_only']}")
    counts = data["counts"] or {}
    print("counts   " + ", ".join(f"{key}={value}" for key, value in counts.items()))


def _print_data(cmd: str, data: dict) -> None:
    if cmd == "status":
        _print_status(data)
        return
    if cmd in ("runlog", "queue"):
        rows = [
            dict(
                row,
                configs_text=_config_text(row),
                jobs_text=_job_text(row),
                files_n=len(row.get("files") or []),
                seq=(row.get("sequence") or {}).get("id"),
            )
            for row in data["runs"]
        ]
        columns = [("id", "id"), ("run", "run_number"), ("status", "status"),
                   ("prio", "priority"), ("events", "requested_events"),
                   ("started", "started"), ("dur/s", "duration_s"),
                   ("files", "files_n"), ("nearline", "jobs_text"),
                   ("seq", "seq"), ("configuration", "configs_text")]
        if cmd == "queue":
            columns.insert(0, ("#", "position"))
        print(_table(rows, columns))
        if cmd == "queue":
            counts = data["counts"]
            print("\n" + ", ".join(f"{key}: {value}" for key, value in counts.items())
                  + f"; next up: {data['next_up'] if data['next_up'] else 'nothing'}")
        elif data["next_before_id"]:
            print(f"\nolder runs: --before-id {data['next_before_id']}")
        return
    if cmd == "sequences":
        rows = [dict(row, members=_counts_text(row.get("counts")))
                for row in data["sequences"]]
        print(_table(rows, [
            ("id", "id"), ("status", "status"), ("runs", "n_runs"),
            ("members", "members"), ("first run", "first_run"),
            ("last run", "last_run"), ("on complete", "on_complete")]))
        return
    if cmd == "run":
        run = data["run"]
        number = f"MIDAS run {run['run_number']}" if run["run_number"] else "not started yet"
        print(f"run id {run['id']}, {number}, status {run['status']}")
        if run["times_known"] and run["stopped"]:
            print(f"started {run['started']}, stopped {run['stopped']}, "
                  f"duration {run['duration_s']} s")
        elif run["times_known"]:
            print(f"started {run['started']}, no end-of-run entry yet")
        print(f"requested {run['requested_events']} events")
        if run["config_note"]:
            print(run["config_note"])
        for item in data["configs"]:
            print(f"\nconfiguration {item['config_id']} ({item['config_type']}): {item['summary']}")
            if item["values"] is None:
                print("  no table for this configuration type")
                continue
            for key, value in item["values"].items():
                print(f"  {key:20s} {value}")
        if data["files"]:
            print("\nfiles")
            print(_table(data["files"], [("id", "id"), ("base", "filebase"),
                                         ("ext", "fileext"), ("producer", "producer"),
                                         ("status", "status")]))
        if data["jobs"]:
            print("\nnearline jobs")
            print(_table(data["jobs"], [("id", "id"), ("type", "job_type"),
                                        ("prio", "priority"), ("status", "status")]))
        if data["sequence"]:
            sequence = data["sequence"]
            print(f"\nsequence {sequence['id']} ({sequence['status']}), "
                  f"{len(sequence['runs'])} runs: {_counts_text(sequence['counts'])}, "
                  f"on complete: {sequence['on_complete']}")
            print(_table(sequence["runs"], [("id", "id"), ("run", "run_number"),
                                            ("status", "status")]))
        return
    if cmd == "config":
        item = data["config"]
        print(f"configuration {item['config_id']} ({item['config_type']}), "
              f"do not use: {item['do_not_use']}")
        if item["values"] is None:
            print("no table for this configuration type")
            return
        for key, value in item["values"].items():
            print(f"  {key:20s} {value}")


def main(argv=None) -> int:
    """The manual path: the same six commands, from a shell, with no MIDAS.

    Everything goes through `commands.dispatch`, so `--json` prints byte for
    byte what the custom page would have received over jrpc.
    """
    from pioneer.rundb import commands

    parser = argparse.ArgumentParser(
        prog="python -m pioneer.rundb.view",
        description="Read the PIONEER run database from a shell.",
    )
    parser.add_argument("command", choices=sorted(commands.CLI_COMMANDS),
                        help="which view to print")
    parser.add_argument("id", nargs="?", type=int,
                        help="database id, for the run and config commands")
    parser.add_argument("--limit", type=int, default=None,
                        help="how many rows (runlog, queue, sequences)")
    parser.add_argument("--before-id", type=int, default=None,
                        help="runlog: show runs older than this database id")
    parser.add_argument("--dsn", default=None,
                        help=f"libpq connection string; default ${pg.DSN_ENV} "
                             "or the readonly role on the configured host")
    parser.add_argument("--timeout-ms", type=int, default=pg.DEFAULT_TIMEOUT_MS,
                        help="statement timeout for every query")
    parser.add_argument("--json", action="store_true",
                        help="print the reply envelope the custom page gets")
    args = parser.parse_args(argv)

    payload = {}
    if args.command in ("run", "config"):
        if args.id is None:
            parser.error(f"the {args.command} command needs a database id")
        payload["id"] = args.id
    if args.limit is not None:
        payload["limit"] = args.limit
    if args.before_id is not None:
        payload["before_id"] = args.before_id

    view = RunDbView(dsn=args.dsn, timeout_ms=args.timeout_ms)
    try:
        reply = commands.dispatch(view, None, args.command, json.dumps(payload))
    finally:
        view.close()

    if args.json:
        print(reply)
    envelope = json.loads(reply)
    if not envelope["ok"]:
        error = envelope["error"]
        if not args.json:
            # Not "usage: ...", which argparse already uses for its own errors.
            print(f"error ({error['kind']}): {error['message']}", file=sys.stderr)
            if error.get("hint"):
                print(error["hint"], file=sys.stderr)
        return 1
    if not args.json:
        _print_data(args.command, envelope["data"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
