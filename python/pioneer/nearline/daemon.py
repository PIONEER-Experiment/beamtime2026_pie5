
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
kMidasHostName   = "localhost"
kMidasExptName   = "test"
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

        invoking_call = [sys.executable, os.path.realpath(sys.argv[0]), "--midas"]
        if (args.midas_client):
            invoking_call += ["--midas-client", args.midas_client]
        if (args.midas_host):
            invoking_call += ["--midas-host", args.midas_host]
        if (args.midas_expt):
            invoking_call += ["--midas-expt", args.midas_expt]

        start_cmd = " ".join(shlex.quote(arg) for arg in invoking_call)
        self.client.odb_set(f"/Programs/{args.midas_client}/Start command", start_cmd)
        if not self.client.odb_exists("/Nearline"):
            self.client.odb_set("/Nearline", {
                "config" : {
                    "Backup path" : "/Users/patrick/phasespace2026/playground/backup",
                    "Remote path" : "/Users/patrick/phasespace2026/playground/remote",
                    "Output path" : "/Users/patrick/phasespace2026/playground/nearline",
                    "Num parallel jobs" : njobs
                    }
            })
        elif (args.jobs):
            self.client.odb_set("/Nearline/config/Num parallel jobs", njobs)

        self.queues['nearline'].maxJobs = self.client.odb_get("/Nearline/config/Num parallel jobs")
        self.midas_logger_path    = pathlib.Path(self.client.odb_get("/Logger/Data dir"))
        self.backup_path          = pathlib.Path(self.client.odb_get("/Nearline/config/Backup path"))
        self.remote_path          = pathlib.Path(self.client.odb_get("/Nearline/config/Remote path"))
        self.nearline_output_path = pathlib.Path(self.client.odb_get("/Nearline/config/Output path"))

        self.sleep_time = 10
        self.db_interface = pioneer.rundb.interface.interface(user = kDbUser, password = kDbPwd)

        # Proper mini twin initialisation goes here.
        self.mt_interface = mt_iface()


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
        job_cfg['output']   = self.nearline_output_path

        theJob = nl_jobs.create_job(job_cfg, self.db_interface)
        theJob.start()
        queue.add(theJob)

    def build_and_dispatch_seq(self, seq_cfg : dict):
        on_complete = seq_cfg['on_complete'].split()
        if "merge" in on_complete:
            seq_cfg['input'] = self.nearline_output_path
            seq_cfg['output'] = self.nearline_output_path / f"seq{seq_cfg['id']:05d}.root"
            seq_cfg['job_type'] = "merge"
            seq_cfg['job_id'] = seq_cfg['id']
            seq_cfg['table'] = 'run_sequence'
            seq_cfg['midas_run_numbers'] = self.db_interface.get_all_runs_in_sequence(seq_cfg['id'])
            theJob = nl_jobs.create_job(seq_cfg, self.db_interface)
            theJob.start()
            self.sequence_queue.add(theJob)
        elif "mt_add" in on_complete:
            # This sequence does not merge but adds all runs
            # As this operation is fast, we'll do it right here
            for mrn in self.db_interface.get_all_runs_in_sequence(seq_cfg['id']):
                self.mt_interface.AddContext(self.nearline_output_path / f"run{mrn:05d}.root")

    def communicate_with_midas(self):
        if (self.client):
                self.client.communicate(10)

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
        print("iterate_sequences")
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
            mrs = nl_run.midas_run_sequence(self.db_interface)
            mrs.set_config_list("dummy", new_configs)
            mrs.set_subsequence(nl_run.five_point_sequence(self.db_interface))
            mrs.schedule()



    def mainloop(self):
        while True:
            # Step 1: Communicate with midas if available
            self.communicate_with_midas()

            # Step 2: Iterate nearline job queues
            self.iterate_nearline_queues()

            # Step 3: Iterate on sequences, identifying the ones that are completed.
            self.iterate_sequences()

            # Step 4: Poll update strategies for new configuration
            self.check_for_updates()

            time.sleep(self.sleep_time)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Good Luck Have Fun - I did not yet write documentation for this")
    parser.add_argument("--midas-client", default=kMidasClientName, help="Midas client name")
    parser.add_argument("--midas-host", default=kMidasHostName, help="Midas host name")
    parser.add_argument("--midas-expt", default=kMidasExptName, help="Midas experiment name")
    parser.add_argument("-j", "--jobs", default=kDefaultNumJobs, type = int, help="Number of nearline analysis processes to run in parallel")

    NLD = NearlineDaemon(parser.parse_args())
    NLD.mainloop()

