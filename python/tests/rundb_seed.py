"""Fill a scratch run database with the shape the viewer has to cope with.

Imported by the test fixtures and by the standalone seeding script, so both
build exactly the same database and a test failure can be reproduced by hand.

What it makes, and why each piece is there:

* three runs that are over, with no configuration attached -- the runs taken
  before the run database carried settings.  The page has to say so in words
  rather than showing an empty list;
* one run still going, with a begin-of-run log entry but no end-of-run one, so
  a start time without a stop time is exercised;
* a five-run sequence waiting in the queue, each run carrying a target
  position, a degrader position and a beamline setting -- what a scan looks
  like before it starts;
* one run on hold, carrying a configuration whose type names no table at all,
  which is what a typo or a device added to the database but not to the schema
  looks like from the page;
* files and post-processing jobs in a mixture of states.

Everything is built through the helpers the rest of the system uses --
`pioneer.rundb.interface` and `pioneer.nearline.run` -- so the rows come out
the way the sequencer and the nearline daemon really write them.  Only two
things are inserted directly: the begin/end-of-run rows in `logs.slow_control`,
which the slow-control frontend writes, and the configuration row with the
unknown type, which no helper would ever create.

Those helpers connect through `pioneer.rundb.config`, which hardcodes the
production host and database.  `seed` therefore points that module at the
scratch database for as long as it runs and puts it back afterwards; the
database name needs the default argument of `config.connect` patched as well,
because that default was bound when the module was imported.
"""

import re
from datetime import datetime, timedelta, timezone

import psycopg

from pioneer.nearline.run import five_point_sequence
from pioneer.rundb import config
from pioneer.rundb.interface import interface

# Run numbers used by the seeded data.  High enough not to be confused with
# anything a real experiment would have written into a scratch database.
FIRST_RUN_NUMBER = 9001

# How long each finished run lasted, in seconds.
FINISHED_DURATIONS = (300, 615, 120)

# A configuration type with no table behind it.
UNKNOWN_CONFIG_TYPE = "phantom_device"


def _patch_config(dsn: str):
    """Point `pioneer.rundb.config` at the scratch database.

    Returns what has to be given back afterwards.  `DB_HOST` and `DB_PORT` are
    read inside `connect` and so are enough on their own; `DB_NAME` is not,
    because `connect` takes it as a default argument that was evaluated at
    import time, so the function object's defaults are patched too.
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


def _log_boundary(conn, run_number: int, reason: str, when: datetime) -> None:
    """Write the slow-control rows that mark the start or the end of a run.

    These are what the run times on the page are computed from: the frontend
    logs every monitored channel once at each run boundary, and the viewer
    takes the earliest BOR and the latest EOR of a run number.
    """
    readings = [
        ("Degrader", "0", "degrader position", "34.0"),
        ("XYTable", "0", "target x", "0.0"),
        ("XYTable", "1", "target y", "0.0"),
    ]
    with conn.cursor() as cur:
        for equipment, channel, label, reading in readings:
            cur.execute(
                """
                INSERT INTO logs.slow_control
                    (midas_run_number, reason, log_time, upd_time,
                     equipment, channel, label, reading)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (run_number, reason, when, when, equipment, channel, label, reading),
            )
    conn.commit()


def _jobs_of(conn, run_id: int) -> dict:
    """The post-processing jobs of one run, by type."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, job_type FROM state.postproc_job WHERE midas_run_id = %s",
            (run_id,),
        )
        return {job_type: job_id for job_id, job_type in cur.fetchall()}


def seed(dsn: str, user: str | None = None, password: str | None = None) -> dict:
    """Build the whole shape in the database `dsn` names.

    Returns the ids and run numbers it created, so a test can ask about a
    specific row instead of guessing which one it got.
    """
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    user = user or params.get("user") or "postgres"
    password = password or params.get("password") or ""

    saved = _patch_config(dsn)
    conn = None
    try:
        # Inside the try: a connection that cannot be made must not leave
        # pioneer.rundb.config pointed at the scratch database.
        conn = psycopg.connect(dsn)
        iface = interface(user=user, password=password)
        out = _build(iface, conn)
    finally:
        if conn is not None:
            conn.close()
        _restore_config(saved)
    return out


def _build(iface: interface, conn: psycopg.Connection) -> dict:
    now = datetime.now(timezone.utc)
    out: dict = {
        "finished_run_ids": [],
        "finished_run_numbers": [],
        "durations": {},
        "file_ids": [],
    }

    # --- runs that are over, with no configuration attached -----------------
    for index, duration in enumerate(FINISHED_DURATIONS):
        run_number = FIRST_RUN_NUMBER + index
        started = now - timedelta(hours=len(FINISHED_DURATIONS) - index, minutes=10)
        stopped = started + timedelta(seconds=duration)

        run_id = iface.register_run("PENDING")
        iface.start_of_midas_run(run_id, run_number)
        _log_boundary(conn, run_number, "BOR", started)

        file_id = iface.open_file("logger_0", run_id, f"run{run_number:05d}.mid.lz4")
        _log_boundary(conn, run_number, "EOR", stopped)
        iface.end_of_midas_run(run_id)

        # The middle run is the one that went wrong: its file never closed
        # cleanly and its backup job failed.
        if index == 1:
            iface.update_file_status(file_id, "FAILED")
            jobs = _jobs_of(conn, run_id)
            iface.update_postproc_status(jobs["backup"], "FAILED")
            iface.update_postproc_status(jobs["remote"], "DONE")
        else:
            iface.update_file_status(file_id, "DONE")
            for job_id in _jobs_of(conn, run_id).values():
                iface.update_postproc_status(job_id, "DONE")

        out["finished_run_ids"].append(run_id)
        out["finished_run_numbers"].append(run_number)
        out["durations"][run_number] = duration
        out["file_ids"].append(file_id)

    # --- a run that is still going ------------------------------------------
    running_number = FIRST_RUN_NUMBER + len(FINISHED_DURATIONS)
    running_id = iface.register_run("PENDING")
    iface.start_of_midas_run(running_id, running_number)
    _log_boundary(conn, running_number, "BOR", now - timedelta(minutes=4))
    running_file = iface.open_file("logger_0", running_id, f"run{running_number:05d}.mid.lz4")
    iface.schedule_postproc_job(running_id, "nearline")
    out["running_run_id"] = running_id
    out["running_run_number"] = running_number
    out["file_ids"].append(running_file)

    # --- a five-point scan waiting in the queue -----------------------------
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM config.degrader_position ORDER BY id LIMIT 1")
        degrader_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM config.pim1_epics ORDER BY id LIMIT 1")
        pim1_id = cur.fetchone()[0]

    sequence = five_point_sequence(iface)
    sequence.num_ev = 2_000_000
    sequence.add_config_id("degrader_position", degrader_id)
    sequence.add_config_id("pim1_epics", pim1_id)
    out["sequence_run_ids"] = sequence.schedule()

    with conn.cursor() as cur:
        cur.execute("SELECT max(id) FROM state.run_sequence")
        out["sequence_id"] = cur.fetchone()[0]
    out["config_ids"] = {"degrader": degrader_id, "pim1_epics": pim1_id}

    # --- a run on hold, carrying a configuration type with no table ---------
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO config.configuration (config_type) VALUES (%s) RETURNING id",
            (UNKNOWN_CONFIG_TYPE,),
        )
        unknown_config_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM config.target_position ORDER BY id LIMIT 1")
        target_id = cur.fetchone()[0]
    conn.commit()

    # A target position with one coordinate missing.  Every position column is
    # nullable, and a half-filled row must not be able to break the runlog.
    half_target_id = iface.add_new_configuration("target_position", {"xpos": 5.0})

    holding_id = iface.schedule_new_run(
        500_000, [target_id, unknown_config_id, half_target_id])
    iface.update_status("midas_run", holding_id, "HOLDING")
    out["holding_run_id"] = holding_id
    out["unknown_config_id"] = unknown_config_id
    out["config_ids"]["target"] = target_id
    out["config_ids"]["half_target"] = half_target_id
    return out


# Databases this script will write to.  It schedules runs through the same
# interface the sequencer uses, so a production connection string pasted on the
# command line would put test runs into the experiment's queue.
SCRATCH_DATABASES = re.compile(r"^pioneer_rundb_(test|scratch|actions)$")


if __name__ == "__main__":
    import json
    import os
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PIONEER_RUNDB_TEST_DSN")
    if not target:
        print("usage: rundb_seed.py <dsn>", file=sys.stderr)
        raise SystemExit(2)

    name = psycopg.conninfo.conninfo_to_dict(target).get("dbname") or ""
    if not SCRATCH_DATABASES.match(name):
        print(f"refusing to seed database {name!r}: this writes runs, files and "
              f"jobs, and only scratch databases (pioneer_rundb_test, "
              f"pioneer_rundb_scratch, pioneer_rundb_actions) may be "
              f"seeded", file=sys.stderr)
        raise SystemExit(2)

    print(json.dumps(seed(target), indent=2, default=str))
