"""The one thing the run-database client may write: a five-point scan.

Everything else in this package reads.  This module is the exception, and it is
built so that it cannot be reached by accident:

* it is imported only when the client was started with `--allow-actions` and a
  `--write-dsn`, so a client started without them has no code path that writes;
* the command layer refuses every action unless the ODB flag
  `/RunDBView/Allow actions` is true as well, re-read on each call;
* the connection string is bound by the process that started the client
  (`rpc_server.ActionAdapter`), never by the caller;
* the command line here refuses any database that is not a scratch one, because
  scheduling into the experiment's own queue is not something this version
  offers.

What the action does is exactly what a person would do at a python prompt:
`pioneer.nearline.run.five_point_sequence` on the run-database `interface`, the
chosen settings applied on top of it, `schedule()`.  Nothing about the runs it
creates is special, so the sequencer treats them like any other queued runs and
a mistake is undone by cancelling them in the usual way.

Two pieces of the code underneath need working around, and both are handled
here rather than changed, because the sequencer and the nearline daemon depend
on them as they are:

* `pioneer.rundb.interface` connects through `pioneer.rundb.config`, which
  hardcodes host and port and binds the database name as a default argument at
  import time.  This module points that module at the write connection string
  for as long as the scheduling call runs, under a lock, and puts it back in a
  `finally`.  The read path does not go through `config` at all (`pg.py` builds
  its own connection strings), so a poll running at the same time is unaffected;
* `interface.register_sequence` returns `True`, not the id of the sequence it
  created.  The id is read back afterwards from `state.runs_in_sequence` for
  the runs that were just created.

`interface.load_config_sequence` has no `ORDER BY`, so the order in which the
five target positions come back is whatever the database feels like.  The reply
therefore lists the position each created run actually carries rather than
assuming the order, which is what the page shows the shifter.

One more thing follows from using those helpers: **scheduling is not atomic**.
`midas_run_sequence.schedule` commits each run on its own and registers the
sequence at the end, so an error part way through -- a constraint, a lost
connection, the database going away -- leaves runs in the queue and quite
possibly no sequence around them.  Rolling that back is not this module's to
do, so instead it takes the largest run and sequence ids before it starts and,
if the write fails, reports the rows that appeared anyway under
`created_anyway` with a hint to go and look at the queue.  A shifter must never
be told that nothing happened while five runs are waiting to be taken.

`preview_five_point` is the same checks with none of the writing, and it is a
read command: the page uses it to show what a button would do before the button
is armed.  It still needs this module, because the connection string it reads
belongs to the client, not to the caller.
"""

import re
import threading

import psycopg
import psycopg.sql

from pioneer.rundb import config

# The target-position sequence the five-point scan is made of.  Rows of
# `config.target_position` carrying this `seq_id` are the five points; see
# `pioneer.nearline.run.five_point_sequence`.
TARGET_SEQ_ID = 2

# The configuration type the five-point sequence owns.  A caller may not supply
# one: it would replace the five points with a single position, and the "scan"
# would be five identical runs.
TARGET_CONFIG_TYPE = "target_position"

# How many events a run gets when the caller does not say.
DEFAULT_REQUESTED_EVENTS = 1_000_000

# The most configurations one run may carry.  A run takes one configuration
# per device and there are a handful of devices; a list longer than this is a
# caller sending something that is not a list of chosen settings.
MAX_CONFIG_IDS = 16

# The largest number of events that may be asked for.  `requested_events` is a
# BIGINT and a plausible run is many orders of magnitude below this; the bound
# exists so that a slipped decimal point is refused rather than queued.
MAX_REQUESTED_EVENTS = 10**10

# Databases this module's command line will write to: the test suite's own
# (`test`), the standalone experiment the page is developed against (`scratch`)
# and the one the action tests rebuild (`actions`).  Scheduling runs into the
# experiment's own database from here is not offered in this version: the
# action has never been exercised against the DAQ machine, and a connection
# string pasted from somewhere else must not be obeyed.
SCRATCH_DATABASES = re.compile(r"^pioneer_rundb_(test|scratch|actions)$")

# `pioneer.rundb.config` is process-wide state, so only one action at a time may
# have it pointed somewhere.  The client answers RPCs from one thread, so this
# is never contended in practice; it is here so that it cannot be.
_CONFIG_LOCK = threading.Lock()

# What the action calls itself in pg_stat_activity.
APPLICATION_NAME = "rundb_actions"


class ActionError(Exception):
    """An action could not be carried out, and the caller should be told why.

    The same shape as `view.ViewError`: `kind` is one of the kinds the JSON
    contract names, and `commands.dispatch_envelope` reads it off the exception
    to build the error envelope.  `usage` means the request was wrong and
    nothing was written; `db` means the database refused or was unreachable.

    `data` is anything else the caller needs beside the message, and it is
    carried into the error envelope as it stands.  It exists for one case:
    scheduling gave up half way and some runs are in the queue anyway, and the
    reply has to say which.
    """

    def __init__(self, kind: str, message: str, hint: str | None = None,
                 data: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint
        self.data = data or {}


# --------------------------------------------------------------------------
# pointing `pioneer.rundb.config` at the write database
# --------------------------------------------------------------------------

def _patch_config(dsn: str):
    """Point `pioneer.rundb.config` at `dsn`; returns what to give back.

    `DB_HOST` and `DB_PORT` are read inside `connect` and so are enough on
    their own.  `DB_NAME` is not: `connect` takes the database name as a
    default argument, evaluated when the module was imported, so the function
    object's defaults have to be patched too.  This is the same treatment the
    test seeder gives it (`python/tests/rundb_seed.py`).
    """
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    saved = (config.DB_HOST, config.DB_PORT, config.DB_NAME, config.connect.__defaults__)

    config.DB_HOST = params.get("host", "localhost")
    config.DB_PORT = int(params.get("port", 5432))
    config.DB_NAME = params["dbname"]
    config.connect.__defaults__ = config.connect.__defaults__[:-1] + (config.DB_NAME,)
    return saved


def _restore_config(saved) -> None:
    config.DB_HOST, config.DB_PORT, config.DB_NAME, config.connect.__defaults__ = saved


def _credentials(dsn: str) -> tuple:
    """The role the runs are scheduled as, taken from the write connection string."""
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    return params.get("user") or "bot", params.get("password") or ""


def _scrub(text, dsn: str) -> str:
    """A database error with this client's connection details taken out of it.

    libpq quotes the host, the user and the database name in its own messages,
    and those messages go straight onto a page a shifter reads and screenshots.
    Where the client is pointed is not their business and not their problem, so
    every value from the connection string is replaced before the text goes
    anywhere.  Short values are left alone: replacing a port number of `5` in a
    sentence would make it unreadable rather than safe.
    """
    out = str(text)
    try:
        params = psycopg.conninfo.conninfo_to_dict(dsn or "")
    except psycopg.Error:
        return out
    for key in ("password", "sslpassword", "host", "hostaddr", "user", "dbname"):
        value = str(params.get(key) or "")
        if len(value) >= 3:
            out = out.replace(value, "***")
    return out


def database_name(dsn: str) -> str:
    """The database a connection string names, or "" if it names none."""
    try:
        return psycopg.conninfo.conninfo_to_dict(dsn).get("dbname") or ""
    except psycopg.Error:
        return ""


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _whole_number(value, what: str) -> int:
    """`value` as an int, or a usage error.

    A `float` has to be exactly a whole number: `2.5` is not a number of events
    anybody meant, and silently truncating it to 2 would schedule something
    other than what was asked for.  `True` is an int in Python and is refused
    here for the same reason.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ActionError("usage", f"{what} must be a whole number")
    if isinstance(value, float) and not value.is_integer():
        raise ActionError("usage", f"{what} must be a whole number, not {value}")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ActionError("usage", f"{what} must be a whole number") from None


def _check_events(requested_events) -> int:
    if requested_events is None:
        return DEFAULT_REQUESTED_EVENTS
    events = _whole_number(requested_events, "requested_events")
    if events < 1 or events > MAX_REQUESTED_EVENTS:
        raise ActionError(
            "usage",
            f"requested_events must be between 1 and {MAX_REQUESTED_EVENTS}, not {events}",
        )
    return events


def _check_ids(config_ids) -> list:
    if config_ids is None:
        raise ActionError("usage", "schedule_five_point needs config_ids")
    if isinstance(config_ids, (str, bytes)) or not isinstance(config_ids, (list, tuple)):
        raise ActionError("usage", "config_ids must be a list of configuration ids")
    if not config_ids:
        raise ActionError("usage", "config_ids must be a non-empty list of configuration ids")
    if len(config_ids) > MAX_CONFIG_IDS:
        raise ActionError(
            "usage",
            f"config_ids has {len(config_ids)} entries; at most {MAX_CONFIG_IDS} "
            f"configurations may be applied to a run",
            hint="one configuration per device, and there are not that many devices",
        )

    out = [_whole_number(item, "config_ids") for item in config_ids]

    duplicates = sorted({item for item in out if out.count(item) > 1})
    if duplicates:
        raise ActionError("usage",
                          "config_ids names the same configuration twice: "
                          + ", ".join(str(item) for item in duplicates))
    return out


def _connect(write_dsn: str) -> psycopg.Connection:
    """A connection to the database that will be written.

    Autocommit, because everything done through it here is a single-statement
    read: the validation before the write and the read-back afterwards, which
    has to see rows the scheduling call committed.  The writes themselves go
    through `interface`, which opens its own connections.
    """
    if not write_dsn:
        raise ActionError("internal", "this client has no write connection string",
                          hint="start it with --allow-actions and --write-dsn")
    if not database_name(write_dsn):
        raise ActionError("internal", "the write connection string names no database")
    try:
        return psycopg.connect(write_dsn, autocommit=True,
                               application_name=APPLICATION_NAME)
    except psycopg.Error as exc:
        raise ActionError("db",
                          f"cannot connect to the run database: {_scrub(exc, write_dsn)}",
                          hint="the database this client writes to is not answering"
                          ) from exc


def _validate_configs(conn, config_ids: list) -> list:
    """Check every configuration the caller chose, before anything is written.

    All of it is refused as `usage`: these are things the caller got wrong, and
    the run database is left exactly as it was.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, config_type, do_not_use FROM config.configuration "
                "WHERE id = ANY(%s) ORDER BY id",
                (config_ids,),
            )
            rows = cur.fetchall()
    except psycopg.Error as exc:
        raise ActionError("db", f"cannot read config.configuration: {exc}") from exc

    found = {row[0]: (row[1], row[2]) for row in rows}

    missing = [cfg_id for cfg_id in config_ids if cfg_id not in found]
    if missing:
        raise ActionError(
            "usage",
            "no such configuration: " + ", ".join(str(item) for item in missing),
            hint="the ids come from config.configuration",
        )

    blocked = [cfg_id for cfg_id in config_ids if found[cfg_id][1]]
    if blocked:
        raise ActionError(
            "usage",
            "configuration marked do not use: " + ", ".join(str(item) for item in blocked),
            hint="somebody set do_not_use on it; pick another configuration",
        )

    targets = [cfg_id for cfg_id in config_ids if found[cfg_id][0] == TARGET_CONFIG_TYPE]
    if targets:
        raise ActionError(
            "usage",
            f"configuration {', '.join(str(item) for item in targets)} is a "
            f"{TARGET_CONFIG_TYPE}, which the five-point sequence sets itself",
            hint="the five positions are the scan; choose only the other settings",
        )

    by_type: dict = {}
    for cfg_id in config_ids:
        by_type.setdefault(found[cfg_id][0], []).append(cfg_id)
    clashing = sorted(kind for kind, ids in by_type.items() if len(ids) > 1)
    if clashing:
        kind = clashing[0]
        raise ActionError(
            "usage",
            f"two configurations of type {kind}: "
            + ", ".join(str(item) for item in by_type[kind]),
            hint="a run carries one configuration per device",
        )

    chosen = [{"config_id": cfg_id, "config_type": found[cfg_id][0]}
              for cfg_id in config_ids]
    _check_typed_rows(conn, chosen)
    return chosen


def _check_typed_rows(conn, chosen: list) -> None:
    """Every chosen configuration must really have a row of its own type.

    `config.configuration` is only the parent: the settings themselves live in
    `config.<config_type>`, and the two can disagree -- a type naming no table
    at all (a typo, or a device added to the database but not to the schema),
    or a parent row whose child was never inserted.  Either way
    `interface.load_config` returns nothing, `midas_run_sequence.schedule`
    produces an empty product, and the caller would be told that five runs were
    scheduled when none were.  It is cheaper and far clearer to refuse here.
    """
    types = sorted({entry["config_type"] or "" for entry in chosen})
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'config' AND table_name = ANY(%s)",
                (types,),
            )
            tables = {row[0] for row in cur.fetchall()}

            present = set()
            for name in tables:
                query = psycopg.sql.SQL(
                    "SELECT id FROM config.{table} WHERE id = ANY({ids})"
                ).format(table=psycopg.sql.Identifier(name),
                         ids=psycopg.sql.Placeholder())
                cur.execute(query, ([entry["config_id"] for entry in chosen
                                     if entry["config_type"] == name],))
                present.update(row[0] for row in cur.fetchall())
    except psycopg.Error as exc:
        raise ActionError("db", f"cannot check the chosen configurations: {exc}") from exc

    unknown = [entry for entry in chosen if (entry["config_type"] or "") not in tables]
    if unknown:
        entry = unknown[0]
        raise ActionError(
            "usage",
            f"configuration {entry['config_id']} has type "
            f"{entry['config_type']!r}, and there is no table config."
            f"{entry['config_type']} to read it from",
            hint="the type names a table in the config schema; this one names none",
        )

    empty = [entry for entry in chosen if entry["config_id"] not in present]
    if empty:
        entry = empty[0]
        raise ActionError(
            "usage",
            f"configuration {entry['config_id']} has no row in "
            f"config.{entry['config_type']}, so it holds no settings",
            hint="the parent row exists but the settings were never written",
        )


def _target_positions(conn) -> list:
    """The five points, read for the reply and to check they can all be used.

    Read the way `interface.load_config_sequence` reads them -- everything with
    this `seq_id`, in no particular order -- so that what a dry run promises is
    what a real call creates.  `do_not_use` is not a filter here but a refusal:
    dropping a flagged point silently would turn a five-point scan into a
    four-point one without saying so.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.xpos, t.ypos, coalesce(c.do_not_use, false) "
                "FROM config.target_position AS t "
                "JOIN config.configuration AS c ON c.id = t.id "
                "WHERE t.seq_id = %s ORDER BY t.id",
                (TARGET_SEQ_ID,),
            )
            rows = cur.fetchall()
    except psycopg.Error as exc:
        raise ActionError("db", f"cannot read the target positions: {exc}") from exc

    if not rows:
        raise ActionError(
            "db",
            f"no target positions carry seq_id {TARGET_SEQ_ID}, so there is no "
            f"five-point sequence to schedule",
            hint="config.target_position is seeded by db_config.sql",
        )

    flagged = [row for row in rows if row[3]]
    if flagged:
        points = "; ".join(f"{row[0]} (x = {row[1]}, y = {row[2]})" for row in flagged)
        raise ActionError(
            "db",
            f"the five-point sequence cannot be used: target position {points} "
            f"is marked do not use",
            hint="clear do_not_use on that position, or fix the position it names",
        )

    return [{"config_id": row[0], "xpos": row[1], "ypos": row[2]} for row in rows]


# --------------------------------------------------------------------------
# the action
# --------------------------------------------------------------------------

def preview_five_point(config_ids=None, requested_events=None, write_dsn=None) -> dict:
    """What `schedule_five_point` would create, without creating it.

    Every check the real call makes is made here, so a dry run that comes back
    clean means the request itself is sound.  Nothing is written, which is why
    the command layer treats this as a read and does not put it behind the ODB
    flag: it is how the page shows a shifter what a button would do, on a
    client where the button itself is refused.
    """
    events = _check_events(requested_events)
    ids = _check_ids(config_ids)

    conn = _connect(write_dsn)
    try:
        configs = _validate_configs(conn, ids)
        positions = _target_positions(conn)
    finally:
        conn.close()

    return {
        "would_create_runs": len(positions),
        "target_seq_id": TARGET_SEQ_ID,
        "requested_events": events,
        "configs_applied": configs,
        "target_positions": positions,
        "message": (
            f"would create {len(positions)} runs of {events} events, one per "
            f"target position, with {len(configs)} configuration(s) applied"
        ),
    }


def schedule_five_point(config_ids=None, requested_events=None, write_dsn=None) -> dict:
    """Queue one five-point scan and say what was created.

    All three arguments are given by name: the command layer passes on the two
    the caller chose, and `rpc_server.ActionAdapter` binds `write_dsn` from the
    command line that started the client.

    Nothing is written until every check has passed, so a rejected request
    leaves the queue exactly as it was.

    Once the writing starts, though, it is not one transaction: each run is
    committed on its own and the sequence is registered last (see `_schedule`).
    A failure part way therefore leaves runs in the queue, and the error says
    which ones, so that a shifter is never told "that failed" about a queue
    that has just grown.
    """
    events = _check_events(requested_events)
    ids = _check_ids(config_ids)

    conn = _connect(write_dsn)
    try:
        configs = _validate_configs(conn, ids)
        _target_positions(conn)

        # What the database looked like before anything was written, so that a
        # failure can work out what it left behind.
        baseline = _high_water(conn)
        try:
            run_ids = _schedule(write_dsn, configs, events)
        except ActionError as exc:
            _attach_partial(conn, exc, baseline)
            raise
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            error = ActionError("internal", f"{exc.__class__.__name__}: {exc}")
            _attach_partial(conn, error, baseline)
            raise error from exc

        if not run_ids:
            # Nothing was scheduled and nothing complained: the product of the
            # configuration lists was empty.  Validation should have caught
            # every way that can happen, so this is a bug here rather than a
            # mistake by the caller -- and it must not be reported as success.
            raise ActionError(
                "internal",
                "nothing was scheduled: the five-point sequence produced no runs",
                hint="the target positions or a chosen configuration came back empty",
                data=_created_since(conn, baseline),
            )

        sequence_id = _sequence_of(conn, run_ids)
        runs = _runs_created(conn, run_ids)
    finally:
        conn.close()

    where = (f"sequence {sequence_id}" if sequence_id is not None
             else "no sequence (the membership rows could not be read back)")
    return {
        "sequence_id": sequence_id,
        "run_ids": run_ids,
        "target_seq_id": TARGET_SEQ_ID,
        "requested_events": events,
        "configs_applied": configs,
        "runs": runs,
        "message": (
            f"queued {len(run_ids)} runs of {events} events as {where}; "
            f"they start when the sequencer reaches them"
        ),
    }


def _high_water(conn) -> dict:
    """The largest ids in the two tables a scan adds rows to.

    Taken on the same connection as the validation, immediately before the
    first write, so that afterwards "what did this call create" is a question
    with an answer.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT coalesce(max(id), 0) FROM state.midas_run")
            runs = cur.fetchone()[0]
            cur.execute("SELECT coalesce(max(id), 0) FROM state.run_sequence")
            sequences = cur.fetchone()[0]
    except psycopg.Error as exc:
        raise ActionError("db", f"cannot read the run database: {exc}") from exc
    return {"runs": runs, "sequences": sequences}


def _created_since(conn, baseline: dict) -> dict:
    """Anything that appeared after `baseline`, ready to go into an envelope.

    Ids only, and only of rows newer than the mark: if somebody else was
    scheduling at the same moment their runs are in here too, which is why the
    reply says "check the queue" rather than "these are yours".  An empty
    result is `{}` so that a clean failure carries nothing.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM state.midas_run WHERE id > %s ORDER BY id",
                        (baseline["runs"],))
            run_ids = [int(row[0]) for row in cur.fetchall()]
            cur.execute("SELECT id FROM state.run_sequence WHERE id > %s ORDER BY id",
                        (baseline["sequences"],))
            sequence_ids = [int(row[0]) for row in cur.fetchall()]
    except psycopg.Error:
        # The database has just refused something; if it refuses this too there
        # is nothing useful to add, and the original error must still get out.
        return {}

    if not run_ids and not sequence_ids:
        return {}
    return {"created_anyway": {"run_ids": run_ids, "sequence_ids": sequence_ids}}


def _attach_partial(conn, error: ActionError, baseline: dict) -> None:
    """Say, on the error, what the half-finished write left in the queue."""
    created = _created_since(conn, baseline)
    if not created:
        return

    error.data = {**error.data, **created}
    runs = created["created_anyway"]["run_ids"]
    with_sequence = bool(created["created_anyway"]["sequence_ids"])
    error.hint = (
        f"check the queue: run(s) {', '.join(str(item) for item in runs)} were "
        f"created and are queued "
        f"{'with a sequence' if with_sequence else 'without a sequence'}; "
        f"cancel them if they were not meant to be there"
    )


def _schedule(write_dsn: str, configs: list, events: int) -> list:
    """The write itself, with `pioneer.rundb.config` pointed at the right database.

    The lock and the `finally` are the whole point of this function: the module
    being patched is shared process-wide state, and it has to be back the way it
    was even if scheduling raises.
    """
    from pioneer.nearline.run import five_point_sequence

    user, password = _credentials(write_dsn)

    with _CONFIG_LOCK:
        saved = _patch_config(write_dsn)
        try:
            from pioneer.rundb.interface import interface

            iface = interface(user=user, password=password)
            # Reads the five target positions, so it belongs inside the patch.
            sequence = five_point_sequence(iface)
            for entry in configs:
                sequence.set_config_id(entry["config_type"], entry["config_id"])
            sequence.num_ev = events
            run_ids = sequence.schedule()
        except psycopg.Error as exc:
            raise ActionError(
                "db",
                f"the run database refused the new runs: {_scrub(exc, write_dsn)}"
            ) from exc
        finally:
            _restore_config(saved)

    return [int(run_id) for run_id in run_ids]


def _sequence_of(conn, run_ids: list):
    """The sequence the new runs ended up in.

    `interface.register_sequence` returns `True` rather than the id it created,
    so the id is read back from the membership rows of the runs that were just
    scheduled.  More than one would mean somebody else was writing at the same
    moment; that is reported as no id rather than as a wrong one.
    """
    if not run_ids:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT seq_id FROM state.runs_in_sequence "
                "WHERE midas_run_id = ANY(%s)",
                (run_ids,),
            )
            found = [row[0] for row in cur.fetchall()]
    except psycopg.Error:
        return None
    return int(found[0]) if len(found) == 1 else None


def _runs_created(conn, run_ids: list) -> list:
    """One entry per created run, carrying the target position it will go to.

    `interface.load_config_sequence` has no `ORDER BY`, so which run got which
    of the five points is not predictable and has to be read back.
    """
    if not run_ids:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT mr.id, mr.priority, mr.status, mr.requested_events,
                       t.id, t.xpos, t.ypos
                FROM state.midas_run AS mr
                LEFT JOIN state.midas_run_config AS mrc ON mrc.run_id = mr.id
                LEFT JOIN config.configuration AS c
                       ON c.id = mrc.config_id AND c.config_type = %s
                LEFT JOIN config.target_position AS t ON t.id = c.id
                WHERE mr.id = ANY(%s)
                """,
                (TARGET_CONFIG_TYPE, run_ids),
            )
            rows = cur.fetchall()
    except psycopg.Error:
        return []

    by_run = {}
    for run_id, priority, status, requested, target_id, xpos, ypos in rows:
        entry = by_run.setdefault(run_id, {
            "run_id": int(run_id),
            "priority": priority,
            "status": status,
            "requested_events": int(requested) if requested is not None else None,
            "target_config_id": None,
            "xpos": None,
            "ypos": None,
        })
        if target_id is not None:
            entry["target_config_id"] = int(target_id)
            entry["xpos"] = xpos
            entry["ypos"] = ypos
    return [by_run[run_id] for run_id in run_ids if run_id in by_run]


# --------------------------------------------------------------------------
# the manual path
# --------------------------------------------------------------------------

def _print_preview(preview: dict) -> None:
    print(f"would create {preview['would_create_runs']} runs of "
          f"{preview['requested_events']} events each")
    print(f"target positions (seq_id {preview['target_seq_id']}):")
    for position in preview["target_positions"]:
        print(f"  config {position['config_id']:6d}  "
              f"x = {position['xpos']}  y = {position['ypos']}")
    print("with these settings on every run:")
    for entry in preview["configs_applied"]:
        print(f"  config {entry['config_id']:6d}  {entry['config_type']}")


def _print_result(result: dict) -> None:
    print(result["message"])
    for run in result["runs"]:
        print(f"  run {run['run_id']:6d}  priority {run['priority']}  "
              f"{run['status']}  x = {run['xpos']}  y = {run['ypos']}")


def main(argv=None) -> int:
    """Schedule a five-point scan from a shell.

    The same code the custom page reaches over RPC, so a scan can be queued,
    and a refusal understood, with nothing running but a terminal.  Two things
    stand in the way of doing it by accident: the database has to be a scratch
    one, and `--confirm` has to be given.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m pioneer.rundb.actions",
        description="Write to the PIONEER run database from a shell.",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    five = sub.add_parser("five-point",
                          help="queue a five-point target scan")
    five.add_argument("--config-id", type=int, action="append", default=[],
                      dest="config_ids", metavar="ID",
                      help="a configuration every run carries; repeat for more "
                           "than one, one per device")
    five.add_argument("--events", type=int, default=DEFAULT_REQUESTED_EVENTS,
                      help=f"events per run; default {DEFAULT_REQUESTED_EVENTS}")
    five.add_argument("--write-dsn", required=True,
                      help="libpq connection string of the database to write")
    five.add_argument("--confirm", action="store_true",
                      help="actually create the runs; without it nothing is written")
    args = parser.parse_args(argv)

    name = database_name(args.write_dsn)
    if not SCRATCH_DATABASES.match(name):
        print(f"refusing to write to database {name!r}: scheduling runs in the "
              f"experiment's own run database is not enabled in this version, "
              f"which has only ever been exercised against a scratch database "
              f"({SCRATCH_DATABASES.pattern})", file=sys.stderr)
        return 2

    try:
        if not args.confirm:
            _print_preview(preview_five_point(config_ids=args.config_ids or None,
                                              requested_events=args.events,
                                              write_dsn=args.write_dsn))
            print("nothing was written; add --confirm to create these runs",
                  file=sys.stderr)
            return 2

        _print_result(schedule_five_point(config_ids=args.config_ids or None,
                                          requested_events=args.events,
                                          write_dsn=args.write_dsn))
    except ActionError as exc:
        print(f"error ({exc.kind}): {exc.message}", file=sys.stderr)
        if exc.hint:
            print(exc.hint, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
