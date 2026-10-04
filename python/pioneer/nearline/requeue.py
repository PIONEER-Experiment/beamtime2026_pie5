"""Put a run the nearline daemon missed back into the normal flow.

The daemon writes a run's rows in the run database only from its MIDAS
callbacks: the run row at the start transition, a file row and a `nearline`
job for every subrun file it sees closed, and at the stop transition the raw
backup, the remote copy, the cleanup and piana's `farline` jobs. A run taken
while the daemon was down has none of them, and neither daemon will ever
process it.

    python -m pioneer.nearline.requeue RUN [RUN ...]            # dry run
    python -m pioneer.nearline.requeue RUN [RUN ...] --apply    # write

writes the rows the callbacks would have written, so that pinky (`nearline`
jobs, histograms only) and piana (`farline` jobs, the full job) process the
run exactly as if the daemon had been up. Per run:

1. the run MIDAS is taking now is refused;
2. the run row is found by its number, or attached with --run-id, or created
   from mlogger's `runNNNNN.json` (author `requeue`, status DONE);
3. a row left RUNNING (the daemon missed the stop) is closed with the stop
   time and event count from that file; without the file (mlogger writes it
   at the stop) the run may still be going, and it is refused;
4. raw files on disk with no row are registered, unless the run already has
   its transfer jobs, which would not copy them (refused); a row whose file
   is gone gets nothing queued on pinky, and is set to ERROR if left open;
5. the jobs are queued in the order the callbacks queue them: the per-file
   `nearline` jobs first, then the run-level stages. A run of quality Debug
   gets only `nearline` jobs, as at the stop transition, unless --stage says
   otherwise.

Nothing is written without --apply, and the dry run connects as the
read-only role. --apply needs the ODB (to refuse the run being taken) unless
--no-midas-check is given. Jobs that are already there are left alone and
counted; --again puts the DONE and FAILED ones of the chosen stages back to
PENDING, and refuses a run where a running job waits for one of them.
The ODB is never written: a stale /Nearline/Info/Run DB PK is reported with
the odbedit line that clears it. See "A run the daemon missed" in README.md.
"""

import argparse
import json
import os
import pathlib
import sys
from dataclasses import dataclass, field

import pioneer.rundb.interface
from pioneer.nearline.jobs import odb_dump_path

# Same role as the daemon (daemon.py:40-42) for --apply; the dry run reads as
# `readonly`, so the database itself refuses a write it might attempt.
kDbUser = "bot"
kDbPwd  = "bot"
kDbReadUser = "readonly"
kDbReadPwd  = "readonly"

kMidasClientName  = "NearlineRequeue"
kDefaultDataDir   = "/home/pinky/online"
kDefaultDumpFile  = "run%05d.json"

# --stage choices. 'nearline' is the per-file job pinky runs; the other two
# are interface.RUN_STAGES.
kStages = ("nearline", "transfer", "farline")

# (client, job_type) of the run-level jobs of each stage, in queueing order.
kRunJobs = {
    "transfer": [("nearline", "backup"), ("nearline", "remote"), ("nearline", "cleanup")],
    "farline" : [("farline", "backup")],
}

# Job statuses a reset (or the database, for a dependent) turns back into a
# job that waits and runs again.
kRerunStatus = ("DONE", "FAILED", "BLOCKED")

# Run-row statuses that mean "the daemon saw the start and never the stop".
kOpenRunStatus = ("CLAIMED", "RUNNING")

# MIDAS /Runinfo/State values while a run is being taken (running, paused).
kStateTaking = (2, 3)


# ---------------------------------------------------------------------------
# mlogger's ODB dump
# ---------------------------------------------------------------------------

def odb_value(tree, path):
    """The value at `path` ("Logger/Channels") in an ODB JSON dump, or None.

    ODB key names are not case sensitive (the daemon writes /Nearline/Config,
    older dumps hold /Nearline/config), and every key has a "<name>/key"
    sibling with its type, which is skipped.
    """
    node = tree
    for part in path.strip("/").split("/"):
        if not isinstance(node, dict):
            return None
        if part in node:
            node = node[part]
            continue
        lower = part.lower()
        matches = [k for k in node if k.lower() == lower]
        if not matches:
            return None
        node = node[matches[0]]
    return node


def odb_number(value):
    """An ODB JSON number: a plain number, or the decimal or "0x..." string
    MIDAS writes for some integer types. None when it is neither."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return None
    return None


def odb_text(tree, *paths):
    """The first non-empty string among `paths`. A value starting with "/" is
    an ODB link written as its target (the daemon links /Experiment/Edit on
    Start/Quality to /Nearline/Info/Quality) and is skipped."""
    for path in paths:
        value = odb_value(tree, path)
        if isinstance(value, str) and value.strip() and not value.startswith("/"):
            return value.strip()
    return None


@dataclass
class RunJson:
    """What the requeue takes from mlogger's ODB dump of one run.

    mlogger writes the dump at the stop transition, so it holds the stop
    time and the logger's final event count. The times are the /Runinfo
    strings ("Sat Oct  3 22:50:19 2026", local time), passed to the database
    exactly as the daemon passes them. `stop` is None when the dump has no
    stop time later than the start (a dump written at the start only).
    """
    path : pathlib.Path
    number : int | None
    start : str | None
    stop : str | None
    events : int | None
    quality : str | None
    description : str | None
    operator : str | None


def read_run_json(path) -> RunJson | None:
    """Read mlogger's ODB dump at `path`; None when there is no such file.

    Tolerant on purpose: a key that is missing gives None for its field, and
    the caller says what that means for the run.
    """
    path = pathlib.Path(path)
    if not path.is_file():
        return None
    with path.open() as f:
        tree = json.load(f)

    number = odb_number(odb_value(tree, "Runinfo/Run number"))
    start = odb_text(tree, "Runinfo/Start time")
    stop = odb_text(tree, "Runinfo/Stop time")
    start_bin = odb_number(odb_value(tree, "Runinfo/Start time binary"))
    stop_bin = odb_number(odb_value(tree, "Runinfo/Stop time binary"))
    if stop_bin is not None and (stop_bin <= 0 or (start_bin is not None and stop_bin < start_bin)):
        # MIDAS zeroes the binary stop time at the start; the string is then
        # the previous run's
        stop = None

    events = None
    channels = odb_value(tree, "Logger/Channels")
    if isinstance(channels, dict):
        for name, channel in channels.items():
            if name.endswith("/key") or not isinstance(channel, dict):
                continue
            n = odb_number(odb_value(channel, "Statistics/Events written"))
            if n is not None:
                events = (events or 0) + int(n)

    return RunJson(
        path = path,
        number = int(number) if number is not None else None,
        start = start,
        stop = stop,
        events = events,
        # where the stop callback reads it, then the Edit-on-Start form
        quality = odb_text(tree, "Nearline/Info/Quality", "Experiment/Edit on Start/Quality"),
        description = odb_text(tree, "Nearline/Info/Description", "Experiment/Edit on Start/Description"),
        operator = odb_text(tree, "Nearline/Info/Operator", "Experiment/Edit on Start/Operator"),
    )


def raw_files_on_disk(data_dir, run_number : int) -> list[str]:
    """Names of run `run_number`'s raw files in `data_dir`: runNNNNN.mid.lz4
    and the subrun files runNNNNN_*.mid.lz4, sorted."""
    data_dir = pathlib.Path(data_dir)
    names = {p.name for p in data_dir.glob(f"run{run_number:05d}.mid.lz4")}
    names |= {p.name for p in data_dir.glob(f"run{run_number:05d}_*.mid.lz4")}
    return sorted(names)


def split_file_name(name : str) -> tuple[str, str]:
    """(filebase, fileext), split on the first dot as interface.open_file does."""
    base, _, ext = name.partition(".")
    return base, ext


# ---------------------------------------------------------------------------
# MIDAS, optional
# ---------------------------------------------------------------------------

class MidasState:
    """The four ODB values the requeue reads, from a MIDAS client that is
    disconnected again straight away. Never writes."""

    def __init__(self, state, run_number, data_dir, run_db_pk, dump_file):
        self.state = state
        self.run_number = run_number
        self.data_dir = data_dir
        self.run_db_pk = run_db_pk
        self.dump_file = dump_file


def read_midas(host = None, expt = None) -> tuple[MidasState | None, str | None]:
    """(MidasState, None), or (None, why not) when the MIDAS python package
    is missing or the experiment does not answer."""
    try:
        import midas.client
    except ImportError as e:
        return None, f"no MIDAS python package ({e})"
    try:
        client = midas.client.MidasClient(kMidasClientName, host_name = host, expt_name = expt)
    except Exception as e:
        return None, f"cannot connect to MIDAS ({e})"
    try:
        dump_file = ""
        try:
            if client.odb_get("/Logger/ODB Dump"):
                dump_file = str(client.odb_get("/Logger/ODB Dump File"))
        except Exception:
            dump_file = ""
        run_db_pk = 0
        if client.odb_exists("/Nearline/Info/Run DB PK"):
            run_db_pk = client.odb_get("/Nearline/Info/Run DB PK")
        return MidasState(
            state = client.odb_get("/Runinfo/State"),
            run_number = client.odb_get("/Runinfo/Run number"),
            data_dir = str(client.odb_get("/Logger/Data dir")),
            run_db_pk = run_db_pk,
            dump_file = dump_file,
        ), None
    except Exception as e:
        return None, f"cannot read the ODB ({e})"
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """What was (or, in a dry run, would be) done for one run."""
    number : int
    run_id : int | None = None
    row : str = "found"                 # found / create / attach / close / -
    files_registered : int = 0
    files_closed : int = 0              # file rows left RUNNING, now DONE
    files_errored : int = 0             # file rows left RUNNING whose file is gone, now ERROR
    jobs : list = field(default_factory = list)   # [client, job_type, file, action, status]
    warnings : list = field(default_factory = list)
    refused : str | None = None
    failed : str | None = None
    run_json : RunJson | None = None
    row_status : str | None = None

    def count(self, action):
        return sum(1 for j in self.jobs if j[3] == action)


def wanted_stages(args, quality) -> list[str]:
    """--stage if given; otherwise every stage, or only 'nearline' for a run
    of quality Debug (case-insensitive, as daemon.py does at the stop)."""
    if args.stage:
        return [s for s in kStages if s in args.stage]
    if str(quality or "").lower() == "debug":
        return ["nearline"]
    return list(kStages)


def requeue_run(number, args, iface, midas_state, apply) -> RunResult:
    """Fill run `number`'s gap in the run database. With `apply` False only
    reads, and the result says what would be written."""
    res = RunResult(number = number)

    # 1. never the run MIDAS is taking
    if midas_state is not None and midas_state.state in kStateTaking and midas_state.run_number == number:
        res.refused = "MIDAS is taking this run now; the daemon's callbacks will handle it"
        return res

    data_dir = pathlib.Path(args.data_dir)
    dump_file = (midas_state.dump_file if midas_state is not None and midas_state.dump_file
                 else kDefaultDumpFile)
    json_path = odb_dump_path(data_dir, dump_file, number)
    run_json = read_run_json(json_path) if json_path is not None else None
    res.run_json = run_json
    if run_json is not None and run_json.number not in (None, number):
        res.refused = f"{json_path} is the dump of run {run_json.number}, not {number}"
        return res

    # 2. the run row
    row = None
    run_id = iface.get_run_id(number)
    if run_id is not None:
        found = iface.get_midas_run(run_id)
        if found is not None and found["status"] == "ERROR":
            # filename_change_callback marks a row ERROR when the ODB's primary
            # key does not match it, and moves the run's next files to a new
            # row registered without a run number
            res.refused = (f"row {run_id} of run {number} is ERROR: the daemon's auto-recovery marks a "
                           "row ERROR and registers the run's later files under a new row (author "
                           "AutoRecovery) that has no run number, so they may already be registered "
                           "there. Registering them again would make duplicates; an expert has to "
                           "sort out which row the run's files belong to first")
            return res
    if args.run_id is not None:
        if midas_state is not None and midas_state.run_db_pk and args.run_id == midas_state.run_db_pk:
            res.refused = (f"--run-id {args.run_id} is /Nearline/Info/Run DB PK, the row the next start "
                           "will use; attaching another run to it would break that start")
            return res
        if run_id is not None and run_id != args.run_id:
            res.refused = f"run {number} already has row {run_id}; --run-id {args.run_id} would make a second"
            return res
        row = iface.get_midas_run(args.run_id)
        if row is None:
            res.refused = f"--run-id {args.run_id}: no such row"
            return res
        if row["midas_run_number"] is None:
            if row["status"] not in ("PENDING", "CLAIMED"):
                res.refused = (f"--run-id {args.run_id} is {row['status']} without a run number; "
                               "only a PENDING or CLAIMED row can be attached")
                return res
            res.row = "attach"
        elif row["midas_run_number"] != number:
            res.refused = f"--run-id {args.run_id} is run {row['midas_run_number']}, not {number}"
            return res
        run_id = args.run_id
    elif run_id is not None:
        row = iface.get_midas_run(run_id)
    if row is not None:
        res.row_status = row["status"]

    if row is None:
        if run_json is None:
            res.refused = f"no run row for run {number} and no {json_path} to create one from"
            return res
        res.row = "create"
    elif res.row != "attach" and row["status"] in kOpenRunStatus:
        # 3. the daemon saw the start and not the stop
        res.row = "close"
    res.run_id = run_id

    # mlogger writes the dump at the stop: without one (or without a stop
    # time in it) the run may still be going, and is not closed
    if res.row in ("attach", "close") and (run_json is None or run_json.stop is None
                                           or (res.row == "attach" and run_json.start is None)):
        what = (f"there is no {json_path}" if run_json is None else
                f"{json_path.name} has no {'stop' if run_json.stop is None else 'start'} time")
        res.refused = (f"row {run_id} would be {'attached and closed' if res.row == 'attach' else 'closed'}, "
                       f"but {what}. mlogger writes it when a run stops, so this run may still be "
                       "being taken")
        return res
    if res.row == "create" and run_json.stop is None:
        res.warnings.append(f"{json_path.name} has no stop time: the row gets none")
    if res.row in ("create", "attach", "close") and run_json.events is None:
        res.warnings.append(f"{json_path.name} has no 'Events written': the row gets no event count")

    # 4. raw files
    registered = iface.find_files(run_id, ["mid.lz4"]) if run_id is not None else []
    registered_names = {f"{f['filebase']}.{f['fileext']}": f for f in registered}
    on_disk = raw_files_on_disk(data_dir, number)
    to_register = [n for n in on_disk if n not in registered_names]
    missing = sorted(n for n in registered_names if n not in on_disk)
    to_close = [registered_names[n]["id"] for n in on_disk
                if n in registered_names and registered_names[n]["status"] in kOpenRunStatus]
    # an open row of a file that is gone: the live daemon's close of its
    # channel would close it and queue a nearline job on a missing file
    to_error = [registered_names[n]["id"] for n in missing
                if registered_names[n]["status"] in kOpenRunStatus]
    res.files_closed = len(to_close)
    res.files_errored = len(to_error)
    if missing:
        res.warnings.append(f"{len(missing)} registered raw file(s) not in {data_dir}, nothing queued "
                            f"for them on pinky: {', '.join(missing)}")
    no_files = not on_disk and not registered
    if no_files:
        res.warnings.append(f"no raw files of run {number} in {data_dir} and none registered")
        if res.row == "create":
            res.refused = "nothing to process: no raw files and no run row"
            return res

    existing = iface.find_postproc_jobs(run_id) if run_id is not None else []
    by_key = {(j["client"], j["job_type"], j["file_id"]): j for j in existing}

    # The run-level transfer copies, and the cleanup deletes, the raw files
    # registered when it runs. A file registered after those jobs were queued
    # could be deleted by the cleanup without having been copied.
    transfer_jobs = [by_key[(c, t, None)] for c, t in kRunJobs["transfer"] if (c, t, None) in by_key]
    if to_register and transfer_jobs:
        jobs = ", ".join(f"{j['job_type']} {j['id']} ({j['status']})" for j in transfer_jobs)
        res.refused = (
            f"{len(to_register)} raw file(s) on disk have no row ({to_register[0]}"
            f"{' .. ' + to_register[-1] if len(to_register) > 1 else ''}), but the run already has its "
            f"run-level transfer jobs: {jobs}. Those copy and delete only the files registered when "
            "they run, so a file registered now could be deleted by the cleanup without a backup. "
            "Expert steps: put the cleanup on HOLDING if it has not run; register the files and queue "
            "their nearline jobs; reset the backup and the remote copy to PENDING so that they copy "
            "every file; make the cleanup depend on the new nearline jobs; queue farline jobs for the "
            "new files depending on the remote copy; then release (or reset) the cleanup")
        return res

    # quality: what the stop callback would read (the dump), else the row's
    quality = (run_json.quality if run_json is not None and run_json.quality is not None
               else (row or {}).get("quality"))
    stages = [] if no_files else wanted_stages(args, quality)
    if not no_files and not args.stage and stages == ["nearline"]:
        res.warnings.append("quality Debug: only nearline jobs, as at the stop transition "
                            "(--stage to queue more)")

    # a nearline job queued now on a registered file would not be waited for
    # by a cleanup that is already there (it waits for the jobs queued before it)
    if "nearline" in stages and ("nearline", "cleanup", None) in by_key and any(
            ("nearline", "nearline", registered_names[n]["id"]) not in by_key
            for n in on_disk if n in registered_names):
        res.warnings.append("the cleanup job is already there and will not wait for the nearline "
                            "jobs queued now on files that had none")

    # a run-level stage reads the run's raw files on pinky: with one missing,
    # the backup and the remote copy would fail, and then everything after them
    if "transfer" in stages and missing:
        all_there = all((c, t, None) in by_key for c, t in kRunJobs["transfer"])
        if args.again or not all_there:
            stages.remove("transfer")
            res.warnings.append("transfer not queued: the raw backup and remote copy need every "
                                "registered raw file on disk")
    if "farline" in stages and "transfer" not in stages and ("nearline", "remote", None) not in by_key:
        stages.remove("farline")
        res.warnings.append("farline not queued: it waits for the remote copy, and the run has no "
                            "remote copy job")

    # --again: the finished jobs of the chosen stages (nearline ones only on
    # files that are on disk). The database then recomputes every job that
    # waits for them (state.recompute_job_state), so a finished dependent
    # goes back to waiting and runs again, and one a daemon is running now
    # would be pulled from under it: that run is refused.
    file_names = {f["id"]: f"{f['filebase']}.{f['fileext']}" for f in registered}
    to_reset = []
    if args.again:
        present_file_ids = {registered_names[n]["id"] for n in on_disk if n in registered_names}
        for j in existing:
            if j["status"] not in ("DONE", "FAILED"):
                continue
            if (("nearline" in stages and (j["client"], j["job_type"]) == ("nearline", "nearline")
                 and j["file_id"] in present_file_ids)
                    or ("transfer" in stages and j["file_id"] is None
                        and (j["client"], j["job_type"]) in kRunJobs["transfer"])
                    or ("farline" in stages and j["client"] == "farline")):
                to_reset.append(j)
    dependents = iface.find_dependents([j["id"] for j in to_reset]) if to_reset else []
    busy = [d for d in dependents if d["status"] in ("CLAIMED", "RUNNING")]
    if busy:
        names = ", ".join(f"{d['client']}/{d['job_type']} {d['id']}"
                          f"{' (' + file_names[d['file_id']] + ')' if d['file_id'] in file_names else ''}"
                          f" {d['status']}" for d in busy)
        res.refused = (f"--again would reset jobs that these running jobs wait for: {names}. The "
                       "database would put them back to waiting while a daemon runs them; wait until "
                       "they are DONE or FAILED, then run this again")
        return res

    # dry run: say what would happen, from the rows as they are
    if not apply:
        res.files_registered = len(to_register)
        # in the order the apply queues them: by file name
        nearline_files = sorted([(n, registered_names[n]["id"]) for n in on_disk if n in registered_names]
                                + [(n, None) for n in to_register])
        all_files = sorted([(n, registered_names[n]["id"]) for n in registered_names]
                           + [(n, None) for n in to_register])
        planned = []
        if "nearline" in stages:
            planned += [("nearline", "nearline", n, fid) for n, fid in nearline_files]
        for stage in ("transfer", "farline"):
            if stage not in stages:
                continue
            if stage == "farline":
                planned += [("farline", "farline", n, fid) for n, fid in all_files]
            planned += [(c, t, "-", None) for c, t in kRunJobs[stage]]
        reset_ids = {j["id"] for j in to_reset}
        rerun_ids = reset_ids | {d["id"] for d in dependents if d["status"] in kRerunStatus}
        listed = set()
        for client, job_type, name, fid in planned:
            job = by_key.get((client, job_type, fid)) if (fid is not None or name == "-") else None
            if job is None:
                res.jobs.append([client, job_type, name, "create", None])
                continue
            listed.add(job["id"])
            action = "reset" if job["id"] in rerun_ids else "present"
            res.jobs.append([client, job_type, name, action, job["status"]])
        # finished dependents of stages not asked for, which run again too
        for d in dependents:
            if d["id"] in rerun_ids and d["id"] not in listed:
                res.jobs.append([d["client"], d["job_type"], file_names.get(d["file_id"], "-"),
                                 "reset", d["status"]])
        return res

    # --- from here on, writes ---------------------------------------------
    try:
        if res.row == "create":
            note = f"Created by pioneer.nearline.requeue from {run_json.path.name}"
            if run_json.description:
                note += "\n" + run_json.description
            author = "requeue" + (f", {run_json.operator}" if run_json.operator else "")
            # an empty quality is stored as '', as the start callback stores it
            run_id = iface.create_finished_run(number, run_json.start, run_json.stop, run_json.events,
                                               run_json.quality or "", author, note)
            res.run_id = run_id
        elif res.row == "attach":
            iface.start_of_midas_run(run_id, number, run_json.start)
        if res.row in ("attach", "close"):
            iface.end_of_midas_run(run_id,
                                   recorded_events = run_json.events if run_json else None,
                                   stop_time = run_json.stop if run_json else None,
                                   schedule_post_processing = False)

        names = {f["id"]: f"{f['filebase']}.{f['fileext']}" for f in registered}
        for name in to_register:
            base, ext = split_file_name(name)
            names[iface.register_logger_file(run_id, base, ext, args.channel)] = name
            res.files_registered += 1
        for file_id in to_close:
            # what the stop transition's close does, for this run's files only
            iface.update_file_status(file_id, "DONE")
        for file_id in to_error:
            iface.update_file_status(file_id, "ERROR")
        present_ids = sorted((fid for fid, n in names.items() if n in on_disk), key = lambda i: names[i])

        if args.again:
            if "nearline" in stages and present_ids:
                iface.reset_jobs(run_id, ["nearline"], ["nearline"], present_ids)
            if "transfer" in stages:
                iface.reset_jobs(run_id, ["backup", "remote", "cleanup"], ["nearline"])
            if "farline" in stages:
                iface.reset_jobs(run_id, ["farline", "backup"], ["farline"])

        recorded = []
        def record(client, job_type, file_id, job_id, created):
            recorded.append((client, job_type, file_id, job_id, created))

        # 5. per-file nearline jobs first: the cleanup waits for every job
        # queued before it, as at the stop transition (finish_file, then
        # end_of_midas_run)
        if "nearline" in stages:
            for file_id in present_ids:
                job_id = iface.schedule_postproc_job_on_file(file_id, task = 'nearline', client = 'nearline')
                if job_id != -1:
                    record("nearline", "nearline", file_id, job_id, True)
                    continue
                job = iface.find_postproc_job(run_id, "nearline", "nearline", file_id)
                if job is None:
                    res.failed = f"nearline job for {names[file_id]} could be neither queued nor found"
                    return res
                record("nearline", "nearline", file_id, job["id"], False)

        run_stages = [s for s in iface.RUN_STAGES if s in stages]
        if run_stages:
            outcome = []
            ok = iface.schedule_run_post_processing(run_id, stages = run_stages, existing_ok = True,
                                                    outcome = outcome)
            for o in outcome:
                record(o["client"], o["job_type"], o["file_id"], o["job_id"], o["created"])
            if not ok:
                res.failed = "a run-level job could be neither queued nor found"
        # A job counts as reset when it had finished and is now waiting again:
        # the ones reset_jobs reset, and their finished dependents the database
        # put back to waiting (see the dry run's cascade above).
        status_before = {j["id"]: j["status"] for j in existing}
        after = {j["id"]: j for j in iface.find_postproc_jobs(run_id)}
        def was_reset(job_id):
            before = status_before.get(job_id)
            return before in kRerunStatus and after.get(job_id, {}).get("status") != before
        def name_of(file_id):
            return names.get(file_id, "-") if file_id is not None else "-"
        for client, job_type, file_id, job_id, created in recorded:
            action = "create" if created else ("reset" if was_reset(job_id) else "present")
            res.jobs.append([client, job_type, name_of(file_id), action,
                             None if created else status_before.get(job_id)])
        seen = {r[3] for r in recorded}
        for job_id, j in after.items():
            if job_id not in seen and was_reset(job_id):
                res.jobs.append([j["client"], j["job_type"], name_of(j["file_id"]), "reset",
                                 status_before[job_id]])

        # a trace on the run of the hand-made rows (a created row says so in its note)
        if res.row != "create" and (res.row != "found" or res.files_registered or res.files_closed
                                    or res.files_errored or res.count("create") or res.count("reset")):
            iface.annotate_run_id(run_id, "requeue",
                                  f"pioneer.nearline.requeue: row {res.row}, {res.files_registered} "
                                  f"raw file(s) registered, {res.count('create')} job(s) queued, "
                                  f"{res.count('reset')} reset")
    except Exception as e:
        res.failed = f"{type(e).__name__}: {e}"
    return res


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def describe_row(res) -> str:
    rj = res.run_json
    rid = res.run_id if res.run_id is not None else "new"
    if res.row == "create":
        return (f"row {rid}: create from {rj.path.name} (DONE, start {rj.start!r}, stop {rj.stop!r}, "
                f"events {rj.events}, quality {rj.quality or ''!r})")
    if res.row == "attach":
        return f"row {rid}: attach to run {res.number}, then close it (DONE)"
    if res.row == "close":
        stop = rj.stop if rj else None
        events = rj.events if rj else None
        return f"row {rid}: left open by the daemon; close it (DONE, stop {stop!r}, events {events})"
    return f"row {rid} ({res.row_status})"


def job_action(action, status):
    """'create', 'present (DONE)' or 'reset (was FAILED)'."""
    if not status:
        return action
    return f"{action} (was {status})" if action == "reset" else f"{action} ({status})"


def print_run(res, verbose, apply, out):
    print(f"run {res.number}", file = out)
    if res.refused:
        print(f"  REFUSED: {res.refused}", file = out)
        return
    print(f"  {describe_row(res)}", file = out)
    closed = (f", open file rows {'closed' if apply else 'to close'}: {res.files_closed}"
              if res.files_closed else "")
    if res.files_errored:
        closed += (f", open rows of gone files {'marked' if apply else 'to mark'} ERROR: "
                   f"{res.files_errored}")
    print(f"  raw files {'registered' if apply else 'to register'}: {res.files_registered}{closed}", file = out)
    for w in res.warnings:
        print(f"  warning: {w}", file = out)
    if not res.jobs:
        print("  no jobs", file = out)
    elif verbose:
        for client, job_type, name, action, status in res.jobs:
            print(f"    {client:9s} {job_type:9s} {name:28s} {job_action(action, status)}", file = out)
    else:
        # one line per client/job type: counts of each action and the files
        groups = {}
        for client, job_type, name, action, status in res.jobs:
            groups.setdefault((client, job_type), []).append((name, action, status))
        for (client, job_type), items in groups.items():
            actions = {}
            for _, action, status in items:
                key = job_action(action, status)
                actions[key] = actions.get(key, 0) + 1
            files = [n for n, _, _ in items if n != "-"]
            span = f"  {files[0]} .. {files[-1]}" if len(files) > 1 else (f"  {files[0]}" if files else "")
            print(f"    {client:9s} {job_type:9s} " + ", ".join(f"{n} {a}" for a, n in actions.items())
                  + span, file = out)
    if res.failed:
        print(f"  FAILED: {res.failed}", file = out)


def print_table(results, out):
    header = ("run", "row", "row action", "files registered", "jobs created", "reset", "present", "result")
    rows = []
    for r in results:
        result = "refused" if r.refused else ("FAILED" if r.failed else "ok")
        rows.append((str(r.number), str(r.run_id) if r.run_id is not None else ("new" if r.row == "create" and not r.refused else "-"),
                     "-" if r.refused or r.row == "found" else r.row,
                     str(r.files_registered), str(r.count("create")), str(r.count("reset")),
                     str(r.count("present")), result))
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(header)]
    print("  ".join(h.ljust(w) for h, w in zip(header, widths)), file = out)
    for row in rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)), file = out)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog = "python -m pioneer.nearline.requeue",
        description = "Write the run-database rows the nearline daemon writes from its MIDAS "
                      "callbacks (run row, raw-file rows, post-processing jobs) for runs it "
                      "missed, so that the pinky and piana daemons process them as usual. "
                      "Dry run unless --apply.")
    parser.add_argument("runs", metavar = "RUN", type = int, nargs = "+", help = "MIDAS run number(s)")
    parser.add_argument("--apply", action = "store_true",
                        help = "write the rows; without it nothing is written and the dry run "
                               "connects as the read-only role")
    parser.add_argument("--again", action = "store_true",
                        help = "also put the DONE and FAILED jobs of the chosen stages back to "
                               "PENDING (never CLAIMED or RUNNING ones)")
    parser.add_argument("--stage", choices = kStages, action = "append",
                        help = "queue only this stage (repeatable): nearline = per-file job on "
                               "pinky, transfer = raw backup, remote copy and cleanup on pinky, "
                               "farline = full job per file and backup on piana. Default: all, or "
                               "only nearline for a run of quality Debug")
    parser.add_argument("--run-id", type = int, default = None,
                        help = "attach the run to this existing run-database row (a PENDING or "
                               "CLAIMED row without a run number) instead of creating one; one RUN only")
    parser.add_argument("--data-dir", default = None,
                        help = "where the raw files and runNNNNN.json are (default: the ODB's "
                               f"/Logger/Data dir, else {kDefaultDataDir})")
    parser.add_argument("--channel", type = int, default = 0,
                        help = "MIDAS logger channel the files came from, recorded as producer "
                               "logger_<channel> (default 0)")
    parser.add_argument("--midas-host", default = os.environ.get("MIDAS_SERVER_HOST", None),
                        help = "MIDAS server (default $MIDAS_SERVER_HOST, else local)")
    parser.add_argument("--midas-expt", default = os.environ.get("MIDAS_EXPT_NAME", None),
                        help = "MIDAS experiment (default $MIDAS_EXPT_NAME, else MIDAS's default)")
    parser.add_argument("--no-midas-check", action = "store_true",
                        help = "allow --apply when the ODB cannot be read, so that the run MIDAS is "
                               "taking now cannot be checked. Rows left RUNNING are still closed only "
                               "when the run's runNNNNN.json is there")
    parser.add_argument("-v", "--verbose", action = "store_true",
                        help = "list every job instead of one line per job type")
    return parser


_UNSET = object()


def main(argv = None, iface = None, midas_state = _UNSET, out = None) -> int:
    """The command line. `iface` and `midas_state` are for the tests: a
    database interface to use instead of the bot/readonly one, and the ODB
    values (None: no MIDAS) instead of asking MIDAS."""
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    if args.run_id is not None and len(args.runs) != 1:
        print("--run-id needs exactly one RUN", file = sys.stderr)
        return 2

    if midas_state is _UNSET:
        midas_state, why = read_midas(args.midas_host, args.midas_expt)
    else:
        why = "no MIDAS"
    if midas_state is None:
        print(f"warning: {why}: the run MIDAS is taking now is NOT checked, and the data "
              f"directory is {args.data_dir or kDefaultDataDir}", file = out)
        if args.apply and not args.no_midas_check:
            print("refusing --apply without the ODB: the run being taken cannot be told apart from "
                  "one the daemon missed. Fix the MIDAS connection, or add --no-midas-check if you "
                  "are sure no listed run is being taken", file = out)
            return 2
    if args.data_dir is None:
        args.data_dir = (midas_state.data_dir if midas_state is not None and midas_state.data_dir
                         else kDefaultDataDir)

    if iface is None:
        iface = (pioneer.rundb.interface.interface(user = kDbUser, password = kDbPwd) if args.apply
                 else pioneer.rundb.interface.interface(user = kDbReadUser, password = kDbReadPwd))

    print(("APPLY: writing to the run database" if args.apply
           else "DRY RUN: nothing is written (add --apply to write)") + f"; data directory {args.data_dir}",
          file = out)

    results = []
    for number in args.runs:
        res = requeue_run(number, args, iface, midas_state, args.apply)
        print_run(res, args.verbose, args.apply, out)
        results.append(res)

    print(file = out)
    print_table(results, out)

    # The ODB is not touched here: a stale primary key only gets the line
    # that clears it. While stopped, the stop callback leaves it at 0.
    if midas_state is not None and midas_state.state == 1 and midas_state.run_db_pk:
        print(f"\nwarning: /Nearline/Info/Run DB PK is {midas_state.run_db_pk} while no run is being "
              "taken, so the next start would try to reuse that row and fail. Clear it with\n"
              "  odbedit -c 'set \"/Nearline/Info/Run DB PK\" 0'", file = out)

    if not args.apply:
        print("\nDry run: nothing was written. Re-run with --apply to write these rows.", file = out)
    return 1 if any(r.refused or r.failed for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
