
import pioneer.rundb.interface
import pioneer.nearline.jobs as nl_jobs
import pioneer.nearline.run as nl_run
from pioneer.nearline.miniTwinInterface import miniTwinInterface as mt_iface

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

class NearlineQueue:
    def __init__(self, name : str = None, maxJobs : int = 1):
        self.name : str  = name
        self.maxJobs :int = maxJobs
        self.active : list[nl_jobs.BaseJob] = list()

    def get_finshed(self) -> list[nl_jobs.BaseJob]:
        completed = list()
        for aJob in self.active:
            rc = aJob.poll()
            if rc is None:
                # This job is still running
                continue
            completed.append(aJob)
        for j in completed:
            self.active.remove(j)
        return completed

    def getOpenSlots(self) -> int:
        return self.maxJobs - len(self.active)

    def add(self, aJob : nl_jobs.BaseJob) -> None:
        self.active.append(aJob)

class NearlineDaemon:
    def __init__(self, args):
        if (args.jobs):
            njobs = args.jobs
        else:
            njobs = kDefaultNumJobs

        self.queues = {
            "nearline": NearlineQueue("nearline", njobs),
            "backup"  : NearlineQueue("backup",   1),
            "remote"  : NearlineQueue("remote",   1),
            "cleanup" : NearlineQueue("cleanup",  1)
        }

        self.sequence_queue = NearlineQueue("merge", 1)

        # Create the MIDAS client for status monitoring, warnings and restarting
        # this is technically not required but considered a neat feature.
        self.client = midas.client.MidasClient(args.midas_client, host_name = args.midas_host, expt_name = args.midas_expt)

        invoking_call = [
            sys.executable,
            os.path.realpath(sys.argv[0]),
            "--midas-client", args.midas_client,
            "--midas-host", args.midas_host,
            "--midas-expt", args.midas_expt
        ]

        start_cmd = " ".join(shlex.quote(arg) for arg in invoking_call)
        self.client.odb_set(f"/Programs/{args.midas_client}/Start command", start_cmd)
        if not self.client.odb_exists("/Nearline"):
            self.client.odb_set("/Nearline", {
                "config" : {
                    "Backup path" : os.environ.get("NEARLINE_BACKUP_DIR", "/home/pinky/backup"),
                    "Remote path" : os.environ.get("NEARLINE_REMOTE", "analysis:/home/pioneer/inbox"),
                    "Output path" : os.environ.get("NEARLINE_DIR", "/home/pinky/nearline"),
                    "Num parallel jobs" : njobs,
                    "MiniTwin URL" : "http://127.0.0.1:8420",
                    "MiniTwin updates" : "pim1_epics"
                    }
            })
        elif (args.jobs):
            self.client.odb_set("/Nearline/config/Num parallel jobs", njobs)

        self.queues['nearline'].maxJobs = self.client.odb_get("/Nearline/config/Num parallel jobs")
        self.midas_logger_path    = pathlib.Path(self.client.odb_get("/Logger/Data dir"))
        self.backup_path          = pathlib.Path(self.client.odb_get("/Nearline/config/Backup path"))
        self.remote_path          = pathlib.Path(self.client.odb_get("/Nearline/config/Remote path"))
        self.nearline_output_path = pathlib.Path(self.client.odb_get("/Nearline/config/Output path"))
        self.minitwin_update_table = self.client.odb_get("/Nearline/config/MiniTwin updates")

        self.client.register_transition_callback(
            transition = midas.TR_START,
            sequence = 100,
            callback = self.start_of_run_callback
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
            base_url = self.client.odb_get("/Nearline/config/MiniTwin URL")
        )


    def message(self, msg, is_error = False, send_to_slack = False):
        self.client.msg(msg, is_error = is_error)
        print(msg)

        if (send_to_slack):
            pass
            # todo: get slack hook and set it up

    def dispatch_job(self, queue : NearlineQueue, job_cfg):
        job_cfg['job_type'] = queue.name
        job_cfg['input']    = self.midas_logger_path
        job_cfg['backup']   = self.backup_path
        job_cfg['remote']   = self.remote_path
        job_cfg['output']   = self.nearline_output_path / f"run{job_cfg['midas_run_number']:05d}"

        theJob = nl_jobs.create_job(job_cfg, self.db_interface)
        try:
            theJob.start()
        except Exception as e:
            msg = f"Job {job_cfg['job_id']} failed to start: {e}"
            self.message(msg, is_error = True, send_to_slack = True)
        else:
            queue.add(theJob)

    def build_and_dispatch_seq(self, seq_cfg : dict):
        on_complete = seq_cfg['on_complete'].split()
        if "merge" in on_complete:
            seq_cfg['input'] = self.nearline_output_path
            seq_cfg['cfg_file'] = self.nearline_output_path / f"seq{seq_cfg['id']:05d}.json"
            seq_cfg['output'] = self.nearline_output_path / f"seq{seq_cfg['id']:05d}.root"
            seq_cfg['job_type'] = "merge"
            seq_cfg['job_id'] = seq_cfg['id']
            seq_cfg['table'] = 'run_sequence'
            seq_cfg['midas_run_ids'] = self.db_interface.get_all_runs_in_sequence(seq_cfg['id'])
            theJob = nl_jobs.create_job(seq_cfg, self.db_interface)
            theJob.start()
            self.sequence_queue.add(theJob)
        elif "mt_add" in on_complete:
            # This sequence does not merge but adds all runs
            # As this operation is fast, we'll do it right here
            run_ids = self.db_interface.get_all_runs_in_sequence(seq_cfg['id'])
            files = self.db_interface.find_files(run_ids, "root")
            self.mt_interface.AddContextFiles(files)

    def communicate_with_midas(self):
        if (self.client):
                self.client.communicate(self.sleep_time)

                # Paths where things shall be going to
                self.midas_logger_path    = pathlib.Path(self.client.odb_get("/Logger/Data dir"))
                self.backup_path          = pathlib.Path(self.client.odb_get("/Nearline/config/Backup path"))
                self.remote_path          = pathlib.Path(self.client.odb_get("/Nearline/config/Remote path"))
                self.nearline_output_path = pathlib.Path(self.client.odb_get("/Nearline/config/Output path"))

                # Update max number of jobs in nearline queue
                self.queues['nearline'].maxJobs = self.client.odb_get("/Nearline/config/Num parallel jobs")

    def iterate_nearline_queues(self):
        for aQueue in self.queues.values():

            # Step 2.1: Identify finished jobs in all queues
            finished_jobs = aQueue.get_finshed()
            for aJob in finished_jobs:
                aJob.finalise()

            # Step 2.2: Dispatch new jobs should there be open slots.
            numOpen = aQueue.getOpenSlots()
            if (numOpen > 0):
                newConfigs = self.db_interface.find_pending_postproc_jobs(job_type = aQueue.name, max_jobs = numOpen)
                for aConfig in newConfigs:
                    self.dispatch_job(aQueue, aConfig)

    def iterate_sequences(self):
        finished_jobs = self.sequence_queue.get_finshed()
        for aJob in finished_jobs:
            print("Finalising job")
            aJob.finalise()
            if "mt_add" in aJob.config['on_complete'].split():
                self.mt_interface.AddContext(aJob.config['output'])

        numOpen = self.sequence_queue.getOpenSlots()
        if numOpen > 0:
            newSeq = self.db_interface.claim_sequences(limit = numOpen)
            for seq in newSeq:
                self.build_and_dispatch_seq(seq)

    def check_for_updates(self):
        new_configs = self.mt_interface.NextConfiguration()
        if len(new_configs) > 0:
            for aConfig in new_configs:
                mrs = nl_run.midas_run_sequence(self.db_interface)
                mrs.set_config_list(self.minitwin_update_table, aConfig['currents'])
                if aConfig['type'] == 'iter':
                    # default 5 point sequence triggers file merging.
                    mrs.set_subsequence(nl_run.five_point_sequence(self.db_interface))
                    mrs.num_ev = 1e6
                elif aConfig['type'] == 'final':
                    fiveScan = nl_run.five_point_sequence(self.db_interface)
                    fiveScan.set_on_complete("merge") # it shall only merge and not submit to minitwin.
                    dscan = nl_run.degrader_scan(self.db_interface)
                    dscan.set_subsequence(fiveScan)
                    mrs.set_subsequence(dscan)
                    mrs.num_ev = 1e7

                mrs.schedule()

    def filename_change_callback(self, client, path, value):
        # path should be
        # /Logger/Channels/<log_channel>/Settings/Current filename
        log_channel = path.split("/")[3]
        self.finish_file(log_channel)
        run_db_pk = 0
        if client.odb_exists("/Runinfo/Run DB PK"):
            run_db_pk = client.odb_get("/Runinfo/Run DB PK")
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
            self.db_interface.schedule_postproc_job_on_file(i, 'nearline')

    def start_of_run_callback(self, client, run_number):
        run_db_pk = 0
        if client.odb_exists("/Runinfo/Run DB PK"):
            run_db_pk = client.odb_get("/Runinfo/Run DB PK")

        if run_db_pk == 0:
            # if MIDAS is unaware of a run in the table, register a new run
            run_db_pk = self.db_interface.register_run(status = "RUNNING")
            client.odb_set("/Runinfo/Run DB PK", run_db_pk)

        self.db_interface.start_of_midas_run(
            run_id = run_db_pk,
            run_number =  run_number
        )
        return midas.status_codes['SUCCESS']

    def end_of_run_callback(self, client, run_number):
        run_db_pk = client.odb_get("/Runinfo/Run DB PK")
        logger_channels = self.client.odb_get("/Logger/Channels", just_key_list = True)
        for log_channel in logger_channels:
            self.finish_file(log_channel)
        client.odb_set("/Runinfo/Run DB PK", 0)
        self.db_interface.end_of_midas_run(run_db_pk)
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

            except Exception as e:
                self.client.msg("Nearline Error" + e, is_error= True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Good Luck Have Fun - I did not yet write documentation for this")
    parser.add_argument("--midas-client", default=kMidasClientName, help="Midas client name")
    parser.add_argument("--midas-host", default=kMidasHostName, help="Midas host name")
    parser.add_argument("--midas-expt", default=kMidasExptName, help="Midas experiment name")
    parser.add_argument("-j", "--jobs", default=kDefaultNumJobs, type = int, help="Number of nearline analysis processes to run in parallel")

    NLD = NearlineDaemon(parser.parse_args())
    NLD.mainloop()

