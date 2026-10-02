
import pioneer.rundb.interface
import pioneer.nearline.jobs as nl_jobs
import pioneer.nearline.render as nl_render
from pioneer.conddb.pgservice import describe as describe_conninfo
import pioneer.nearline.run as nl_run
import pioneer.nearline.tuning as nl_tuning
from pioneer.nearline.miniTwinInterface import miniTwinInterface as mt_iface
from pioneer.nearline.queue import NearlineQueue

import midas.client        # connect to MIDAS ODB
import argparse            # parsing command line arguments
import os                  # os.path.join
import sys                 # identify executable and obtain complete list of arguments passed
import shlex               # To parse the start_cmd
import time
import pathlib

# define some default parameters
kMidasClientName = "NearlineDaemon"
kMidasHostName   = os.environ.get('MIDAS_SERVER_HOST', 'localhost')
kMidasExptName   = os.environ.get('MIDAS_EXPT_NAME'  , None)
if kMidasExptName is None:
    # Try the expttab file
    exptab = os.environ.get('MIDAS_EXPTAB', None)
    if exptab is not None:
        exptab_path = pathlib.Path(exptab)
        if exptab_path.exists():
            with exptab_path.open("r") as f:
                lines = f.readlines()
            n_lines = len(lines)
            if n_lines == 1:
                # there is exactly one unique line, which now shall provide a default value.
                kMidasExptName = lines[0].split()[0]


kDefaultNumJobs  = 3

# define DB credentials
kDbUser = "bot"
kDbPwd  = "bot"

def build_parser() -> argparse.ArgumentParser:
    """The daemon's command line; start_command() writes it back into the ODB."""
    parser = argparse.ArgumentParser(description="Good Luck Have Fun - I did not yet write documentation for this")
    parser.add_argument("--midas-client", default=kMidasClientName, help="Midas client name")
    parser.add_argument("--midas-host", default=kMidasHostName, help="Midas host name")
    parser.add_argument("--midas-expt", default=kMidasExptName, help="Midas experiment name")
    # No default here: /Nearline/config/Num parallel jobs is the setting, and -j
    # only overwrites it when given (see num_jobs_to_write).
    parser.add_argument("-j", "--jobs", default=None, type = int,
                        help="Number of nearline analysis processes to run in parallel. Written "
                             "to /Nearline/config/Num parallel jobs, which is what the daemon "
                             f"uses; without -j the ODB value stands ({kDefaultNumJobs} when "
                             "the daemon creates /Nearline)")
    parser.add_argument("--light", action="store_true",
                        help="run the light nearline job: histograms only, no RNTuple, "
                             "no timewalk histograms, no wide SMA dt plot, no SMA raw-word "
                             "diagnostics. Without it the full job runs. Not for pinky until "
                             "another host runs the full job for every subrun (see the README)")
    return parser


def num_jobs_to_write(jobs, config_exists: bool):
    """What to write into /Nearline/config/Num parallel jobs at start-up, or None
    to leave the ODB as it is.

    The ODB value is the setting. The first start, which creates /Nearline/config,
    writes -j or kDefaultNumJobs; a later start writes only a -j given on its
    command line. The Start command the daemon registers for itself carries no -j,
    so a restart from the MIDAS Programs page keeps whatever the ODB says.
    """
    if not config_exists:
        return jobs if jobs is not None else kDefaultNumJobs
    return jobs


def conditions_announcement(environ = None, job_source = None):
    """(message, is_error): where this daemon's jobs will take their constants from.

    Resolved the way render_job will resolve it for every job: NL_CONDITIONS in the
    daemon's environment if set, else the job file's CONDITIONS. The database is the
    normal answer. Anything else is announced as an error, because a daemon reading
    JSON is usually one that inherited an NL_CONDITIONS it was not meant to have
    (from mhttpd's environment, say), and its files would be made from whatever
    snapshot that names. A source that does not resolve means every job will fail to
    start, which is said up front rather than once per job.
    """
    env = os.environ if environ is None else environ
    job_source = pathlib.Path(job_source) if job_source else \
        pathlib.Path(nl_render.__file__).with_name("nearline_job.py")
    origin = ("NL_CONDITIONS in the daemon's environment" if env.get("NL_CONDITIONS")
              else "the job's default")
    try:
        spec = env.get("NL_CONDITIONS") or nl_render.job_conditions(job_source.read_text())
        resolved = nl_render.resolve_conditions(spec)
    except (ValueError, RuntimeError, OSError) as e:
        return (f"Nearline daemon: the conditions source ({origin}) does not resolve, so "
                f"every nearline job will fail to start: {e}", True)
    kind, arg = nl_render.split_conditions(resolved)
    if kind == "db":
        return (f"Nearline daemon: conditions from the database {describe_conninfo(arg)} "
                f"({origin})", False)
    return (f"Nearline daemon: WARNING: conditions from JSON {arg or 'in CONDITIONS_DIR'}, "
            f"NOT from the database ({origin}). Every job will record that source. If this "
            "is not deliberate, unset NL_CONDITIONS and restart the daemon", True)


def start_command(args, executable = None, script = None) -> str:
    """The command line /Programs/<client>/Start command gets, so that a
    restart from the MIDAS Programs page brings the daemon back as it is now.
    --light is part of it: a light host restarted without it would quietly
    switch to the full job."""

    invoking_call = [
        "tmux", "new-session",
        "-d",
        "-s", "pioneer-nearline",
        "--",
        sys.executable,
        "-m", "pioneer.nearline.daemon",
        "--midas-client", args.midas_client,
        "--midas-host", args.midas_host,
        "--midas-expt", args.midas_expt
    ]
    if getattr(args, "light", False):
        invoking_call.append("--light")
    return " ".join(shlex.quote(arg) for arg in invoking_call)


class NearlineDaemon:
    def __init__(self, args):
        # replaced by the ODB value once /Nearline/config is in place, below
        self.queues = {
            "nearline": NearlineQueue("nearline", kDefaultNumJobs),
            "backup"  : NearlineQueue("backup",   1),
            "remote"  : NearlineQueue("remote",   1),
            "cleanup" : NearlineQueue("cleanup",  1)
        }

        self.sequence_queue = NearlineQueue("merge", 1)

        # Create the MIDAS client for status monitoring, warnings and restarting
        # this is technically not required but considered a neat feature.
        self.client = midas.client.MidasClient(args.midas_client, host_name = args.midas_host, expt_name = args.midas_expt)

        # Light or full nearline job, for every GaudiJob this process starts.
        self.light = bool(getattr(args, "light", False))
        self.message("Nearline daemon: " + ("LIGHT nearline job (histograms only, no RNTuple, "
                                            "no timewalk, no wide SMA dt, no SMA diagnostics)"
                                            if self.light else "full nearline job"))
        # Where the constants come from, as every job this daemon renders will resolve it.
        conditions_msg, conditions_bad = conditions_announcement()
        self.message(conditions_msg, is_error = conditions_bad)

        odb_start_cmd_path = f"/Programs/{args.midas_client}/Start command"
        if not self.client.odb_exists(odb_start_cmd_path):
            start_cmd = start_command(args)
            self.client.odb_set(odb_start_cmd_path, start_cmd)
        config_exists = self.client.odb_exists("/Nearline")
        njobs = num_jobs_to_write(args.jobs, config_exists)
        # Main Nearline config in ODB
        self.client.odb_set("/Nearline", {
            "Config" : {
                "Backup path" : os.environ.get("NEARLINE_BACKUP_DIR", "/home/pinky/backup/pim1_epics"),
                "Remote path" : os.environ.get("NEARLINE_REMOTE", "analysis:/home/pioneer/inbox"),
                "Output path" : os.environ.get("NEARLINE_DIR", "/home/pinky/nearline"),
                "Num parallel jobs" : njobs if njobs else kDefaultNumJobs,
                "MiniTwin URL" : "http://127.0.0.1:8420",
                "MiniTwin updates" : "pim1_epics",
                "MiniTwin enable" : True
                },
            "Info" : {
                "Operator" : "",
                "Description" : "",
                "Quality" : "",
                "Run DB PK" : 0
                }
            }, update_structure_only=True)

        # Linking
        self.client.odb_link("/Experiment/Edit on Start/Operator",    "/Nearline/Info/Operator")
        self.client.odb_link("/Experiment/Edit on Start/Description", "/Nearline/Info/Description")
        self.client.odb_link("/Experiment/Edit on Start/Quality",     "/Nearline/Info/Quality")
        self.client.odb_set("/Experiment/Edit on Start/Options Quality", ["Debug", "NL Test"])

        # keys of the tuning loop, created with their defaults when missing
        nl_tuning.ensure_odb_keys(self.client)

        self.queues['nearline'].maxJobs = self.client.odb_get("/Nearline/Config/Num parallel jobs")
        self.midas_logger_path     = pathlib.Path(self.client.odb_get("/Logger/Data dir"))
        self.backup_path           = pathlib.Path(self.client.odb_get("/Nearline/Config/Backup path"))
        self.nearline_output_path  = pathlib.Path(self.client.odb_get("/Nearline/Config/Output path"))
        self.remote_path           = self.client.odb_get("/Nearline/Config/Remote path")
        self.minitwin_update_table = self.client.odb_get("/Nearline/Config/MiniTwin updates")
        self.minitwin_enabled      = self.client.odb_get("/Nearline/Config/MiniTwin enable")

        self.client.register_transition_callback(
            transition = midas.TR_START,
            sequence = 100,
            callback = self.start_of_run_callback
        )

        # after the frontends (sequence 500) have reset their statistics
        self.client.register_transition_callback(
            transition = midas.TR_START,
            sequence = 600,
            callback = self.record_run_start_callback
        )

        self.client.register_transition_callback(
            transition = midas.TR_STOP,
            sequence = 900,
            callback = self.end_of_run_callback
        )

        for log_channel in self.client.odb_get("/Logger/Channels", just_key_list = True):
            self.client.odb_watch(
                path = f"/Logger/Channels/{log_channel}/Settings/Current filename",
                callback = self.filename_change_callback
            )

        self.sleep_time = 1000
        self.db_interface = pioneer.rundb.interface.interface(user = kDbUser, password = kDbPwd)

        # Proper mini twin initialisation goes here.
        self.mt_interface = mt_iface(
            base_url = self.client.odb_get("/Nearline/Config/MiniTwin URL"),
            config_type = self.minitwin_update_table
        )
        self.tuning = nl_tuning.TuningLoop(
            db = self.db_interface,
            mt = self.mt_interface,
            odb = self.client,
            message = self.message
        )
        # last proposal id and the step in flight survive a restart, and a
        # context that was still queued when the daemon stopped is sent again
        self.tuning.restore()
        self.tuning.resume_claimed()
        self.minitwin_enabled = self.tuning.refresh_enable()


    def message(self, msg, is_error = False, send_to_slack = False):
        self.client.msg(msg, is_error = is_error)
        print(msg)

        if (send_to_slack):
            pass
            # todo: get slack hook and set it up

    def dispatch_job(self, queue : NearlineQueue, job_cfg):
        job_type = job_cfg['job_type']
        if job_type == "nearline":
            job_cfg['source_path'] = self.midas_logger_path
            job_cfg['destination_path'] = str(self.nearline_output_path / f"run{job_cfg['midas_run_number']:05d}")
        elif job_type in ("backup", "remote", "cleanup"):
            if (job_cfg.get('producer', None) == "nearline"):
                job_cfg['source_path'] = self.nearline_output_path / f"run{job_cfg['midas_run_number']:05d}"
            else:
                job_cfg['source_path'] = self.midas_logger_path

            if job_type == "backup":
                job_cfg['destination_path'] = str(self.backup_path)
            elif job_type == "remote":
                if (job_cfg.get('producer', None) == "nearline"):
                    job_cfg['destination_path'] = str(f"{self.remote_path}/run{job_cfg['midas_run_number']:05d}")
                else:
                    job_cfg['destination_path'] = str(f"{self.remote_path}")
            elif job_type == "cleanup":
                job_cfg['destination_path'] = None

        job_cfg['log_path'] = self.nearline_output_path / f"run{job_cfg['midas_run_number']:05d}"


        theJob = nl_jobs.create_job(job_cfg, self.db_interface)
        try:
            theJob.start()
        except Exception as e:
            msg = f"Job {job_cfg['job_id']} failed to start: {e}"
            self.message(msg, is_error = True, send_to_slack = True)
            # FAILED, not left CLAIMED: a job that never started (its render failed,
            # say, on a conditions service this host does not define) must show the
            # same symptom as one that failed, and be requeued the same way.
            try:
                self.db_interface.update_status(job_cfg.get('table', 'postproc_job'),
                                                job_cfg['job_id'], 'FAILED')
            except Exception as db_e:
                self.message(f"Job {job_cfg['job_id']} could not be marked FAILED either: "
                             f"{db_e}", is_error = True)
        else:
            queue.add(theJob)

    def build_and_dispatch_seq(self, seq_cfg : dict):
        on_complete = seq_cfg['on_complete'].split()
        if "merge" in on_complete:
            seq_cfg['source_path'] = self.nearline_output_path
            seq_cfg['job_type'] = "merge"
            seq_cfg['job_id'] = seq_cfg['id']
            seq_cfg['table'] = 'run_sequence'
            seq_cfg['midas_run_ids'] = self.db_interface.get_all_runs_in_sequence(seq_cfg['id'])
            theJob = nl_jobs.create_job(seq_cfg, self.db_interface)
            theJob.start()
            self.sequence_queue.add(theJob)
        elif "mt_add" in on_complete:
            # This sequence does not merge: the run's histogram files are
            # posted as they are. As this operation is fast, we'll do it
            # right here. The sequence ends up DONE, or FAILED if it raised.
            # Posted even while "MiniTwin enable" is off: the pause stops new
            # proposals, not the result of a run that was already taken.
            # Posted after "MiniTwin post delay" seconds (see post_due).
            self.tuning.claim(seq_cfg['id'])

    def communicate_with_midas(self):
        if (self.client):
                self.client.communicate(self.sleep_time)

                # Paths where things shall be going to
                self.midas_logger_path    = pathlib.Path(self.client.odb_get("/Logger/Data dir"))
                self.backup_path          = pathlib.Path(self.client.odb_get("/Nearline/Config/Backup path"))
                self.nearline_output_path = pathlib.Path(self.client.odb_get("/Nearline/Config/Output path"))
                self.remote_path          = self.client.odb_get("/Nearline/Config/Remote path")

                # Update max number of jobs in nearline queue
                self.queues['nearline'].maxJobs = self.client.odb_get("/Nearline/Config/Num parallel jobs")

                # "MiniTwin enable" is the tuning loop's pause switch
                self.minitwin_enabled = self.tuning.refresh_enable()

    def iterate_nearline_queues(self):
        for aQueue in self.queues.values():

            # Step 2.1: Identify finished jobs in all queues
            finished_jobs = aQueue.get_finshed()
            for aJob in finished_jobs:
                aJob.finalise()

            # Step 2.2: Dispatch new jobs should there be open slots.
            numOpen = aQueue.getOpenSlots()
            if (numOpen > 0):
                newConfigs = self.db_interface.find_pending_postproc_jobs(job_type = aQueue.job_types, client = 'nearline', max_jobs = numOpen)
                for aConfig in newConfigs:
                    self.dispatch_job(aQueue, aConfig)

    def iterate_sequences(self):
        finished_jobs = self.sequence_queue.get_finshed()
        for aJob in finished_jobs:
            print("Finalising job")
            aJob.finalise()
            if "mt_add" in aJob.config['on_complete'].split() and self.minitwin_enabled:
                step = self.tuning.step_for_sequence(aJob.config['id'])
                self.mt_interface.AddContext(aJob.config['output_file'], step = step,
                                             exposure = self.tuning.exposure_of(
                                                 aJob.config.get('midas_run_ids') or [], step))

        numOpen = self.sequence_queue.getOpenSlots()
        if numOpen > 0:
            newSeq = self.db_interface.claim_sequences(limit = numOpen)
            for seq in newSeq:
                self.build_and_dispatch_seq(seq)

        # mt_add sequences whose post delay is over
        self.tuning.post_due()

    def check_for_updates(self):
        if not self.minitwin_enabled:
            # paused: no proposals, no scheduling; queued contexts still go out
            self.mt_interface.Flush()
            return
        # 'iter': one run at the target config (the stage centre), posted
        # without a merge. 'final': five-point x degrader scan, merge only.
        # The logic is in tuning.py, shared with the manual CLI.
        try:
            self.tuning.poll_and_schedule()
        except nl_tuning.ScheduleError:
            # already sent as a MIDAS error and a 'failed' DAQ report
            pass

    def filename_change_callback(self, client : midas.client.MidasClient, path, value):
        # path should be
        # /Logger/Channels/<log_channel>/Settings/Current filename
        log_channel = path.split("/")[3]
        run_number = client.odb_get("/Runinfo/Run number")
        run_db_pk  = client.odb_get("/Nearline/Info/Run DB PK")
        self.finish_file(log_channel)
        is_valid = self.db_interface.validate_run_number(run_id = run_db_pk, run_number = run_number)
        if not is_valid:
            client.trigger_internal_alarm("RunDB Corrupted", "ODB run number and rundb primary key don't match the rundatabase entry")
            self.db_interface.update_status("state.midas_run", run_db_pk, "ERROR") # That id is bugged
            run_id = self.db_interface.get_run_id(run_number)
            #If this id exists, it is likely bugged too.
            if run_id: # None or 0 are both annotating an illegal run id
                self.db_interface.update_status("state.midas_run", run_id, "ERROR") # That id is bugged
            # create a new run id, better safe than sorry

            auth      = client.odb_get("/Nearline/Info/Operator")
            desc      = client.odb_get("/Nearline/Info/Description")

            author = "AutoRecovery"
            if auth:
                author += ", " + auth

            description =  "RunID created by auto-recovery"
            if desc:
                description += "\n" + desc
            run_db_pk = self.db_interface.register_run(
                status = "RUNNING",
                author = author,
                note = description,
                quality= "check"
            )
            client.odb_set("/Nearline/Info/Run DB PK", run_db_pk)

        self.db_interface.open_file(f"logger_{log_channel}", run_db_pk, value)

    # Small sub-routine to properly close out a file writing for a
    # specific channel. May be called either from filename_change_callback
    # in a sub-run setting where the next subrun started or as the
    # run terminates.
    def finish_file(self, channel):
        ids = self.db_interface.close_files_in_channel(channel)
        for i in ids:
            # Schedule the nearline analysis job right now as we finished
            # writing the file. This may give a head start in cases where
            # multiple subruns are produced.
            self.db_interface.schedule_postproc_job_on_file(i, task = 'nearline', client = 'nearline')

    def start_of_run_callback(self, client : midas.client.MidasClient , run_number):
        run_db_pk = client.odb_get("/Nearline/Info/Run DB PK")
        run_start = client.odb_get("/Runinfo/Start time")
        quality = None

        if run_db_pk == 0:
            # if MIDAS is unaware of a run in the table, register a new run
            # This is likely going to happen if someone started a run manually.
            author      = client.odb_get("/Nearline/Info/Operator")
            description = client.odb_get("/Nearline/Info/Description")
            quality     = client.odb_get("/Nearline/Info/Quality")
            if not author or not description:
                client.msg("Insufficient Run Description: Provide at least operator and description", is_error=True)
                return midas.status_codes["CM_INVALID_TRANSITION"], "Insufficient run description"

            run_db_pk = self.db_interface.register_run(
                status  = "CLAIMED",
                author  = author,
                note    = description,
                quality = quality
                )
            client.odb_set("/Nearline/Info/Operator", "")
            client.odb_set("/Nearline/Info/Description", "")
            client.odb_set("/Nearline/Info/Run DB PK", run_db_pk)

        self.db_interface.start_of_midas_run(
            run_id      = run_db_pk,
            run_number  = run_number,
            start_time  = run_start
        )
        return midas.status_codes['SUCCESS']

    def record_run_start_callback(self, client, run_number):
        # the tuning step's start time and WaveDREAM events, for
        # measurement.exposure; never fails the transition
        try:
            run_db_pk = 0
            if client.odb_exists("/Nearline/Info/Run DB PK"):
                run_db_pk = client.odb_get("/Nearline/Info/Run DB PK")
            tuning = getattr(self, "tuning", None)
            if tuning is not None:
                tuning.record_run_start(run_db_pk, run_number)
        except Exception as e:
            self.message(f"Tuning: start of run {run_number} not recorded: {e}")
        return midas.status_codes['SUCCESS']

    def end_of_run_callback(self, client, run_number):
        run_db_pk = client.odb_get("/Nearline/Info/Run DB PK")
        run_stop = client.odb_get("/Runinfo/Stop time")
        # the tuning step's stop time and WaveDREAM events (never raises)
        tuning = getattr(self, "tuning", None)
        if tuning is not None:
            tuning.record_run_stop(run_db_pk, run_number)
        logger_channels = self.client.odb_get("/Logger/Channels", just_key_list = True)
        nEv = 0
        for log_channel in logger_channels:
            self.finish_file(log_channel)
            nEv += self.client.odb_get(f"/Logger/Channels/{log_channel}/Statistics/Events written")
        client.odb_set("/Nearline/Info/Run DB PK", 0)
        qual =str(client.odb_get("/Nearline/Info/Quality")).lower()
        self.db_interface.end_of_midas_run(run_db_pk, stop_time = run_stop, recorded_events = nEv, schedule_post_processing= (qual != 'debug'))
        return midas.status_codes['SUCCESS']


    def mainloop(self):
        while True:
            try:
                # Step 1: Communicate with midas
                self.communicate_with_midas()

                # Step 2: Iterate nearline job queues
                self.iterate_nearline_queues()

                # Step 3: Iterate on sequences, identifying the ones that are completed.
                self.iterate_sequences()

                # Step 4: Poll update strategies for new configuration
                self.check_for_updates()

                # Step 5: Report the tuning step's DAQ progress (never raises)
                self.tuning.monitor()

            except Exception as e:
                self.client.msg(f"Nearline Error {e}", is_error= True)

if __name__ == "__main__":
    NLD = NearlineDaemon(build_parser().parse_args())
    NLD.mainloop()

