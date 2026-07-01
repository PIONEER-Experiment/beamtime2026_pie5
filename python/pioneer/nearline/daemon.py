
import pioneer.rundb.interface
import pioneer.nearline.jobs as nl_jobs

import midas.client        # connect to MIDAS ODB
import argparse            # parsing command line arguments
import os                  # os.path.join
import sys                 # identify executable and obtain complete list of arguments passed
import shlex               # To parse the start_cmd
import subprocess          # To dispatch nearline analysis processes
import time

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

        if (args.midas):
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
            self.midas_logger_path    = self.client.odb_get("/Logger/Data dir")
            self.backup_path          = self.client.odb_get("/Nearline/config/Backup path")
            self.remote_path          = self.client.odb_get("/Nearline/config/Remote path")
            self.nearline_output_path = self.client.odb_get("/Nearline/config/Output path")
        else:
            self.client = None;
            self.midas_logger_path = "/Users/patrick/phasespace2026/sequencer"
            self.backup_path = "/Users/patrick/phasespace2026/playground/backup"
            self.remote_path = "/Users/patrick/phasespace2026/playground/remote"
            self.nearline_output_path = "/Users/patrick/phasespace2026/playground/nearline"

        self.sleep_time = 10
        self.db_interface = pioneer.rundb.interface.interface(user = kDbUser, password = kDbPwd)

    def message(self, msg, is_error = False, send_to_slack = False):
        if self.client:
            self.client.msg(msg, is_error = is_error)
        else:
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



    def mainloop(self):
        while True:
            # Step 0: Communicate with midas if available
            if (self.client):
                self.client.communicate(10)

                # Paths where things shall be going to
                self.midas_logger_path    = self.client.odb_get("/Logger/Data dir")
                self.backup_path          = self.client.odb_get("/Nearline/config/Backup path")
                self.remote_path          = self.client.odb_get("/Nearline/config/Remote path")
                self.nearline_output_path = self.client.odb_get("/Nearline/config/Output path")

                # Update max number of jobs in nearline queue
                self.queues['nearline'].maxJobs = self.client.odb_get("/Nearline/config/Num parallel jobs")
                # all midas communication and midas-related stuff goes here.

            # Iterate queues
            for aQueue in self.queues.values():

                # Step 1: Identify finished jobs in all queues
                finished_jobs = aQueue.get_finshed()
                for aJob in finished_jobs:
                    aJob.finalise()

                # Step 2: Dispatch new jobs should there be open slots.
                numOpen = aQueue.getOpenSlots()
                if (numOpen > 0):
                    newConfigs = self.db_interface.find_pending_postproc_jobs(job_type = aQueue.name, max_jobs = numOpen)
                    for aConfig in newConfigs:
                        self.dispatch_job(aQueue, aConfig)

            time.sleep(self.sleep_time)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Good Luck Have Fun - I did not yet write documentation for this")
    parser.add_argument("--midas", action = "store_true")
    parser.add_argument("--midas-client", default=kMidasClientName, help="Midas client name")
    parser.add_argument("--midas-host", default=kMidasHostName, help="Midas host name")
    parser.add_argument("--midas-expt", default=kMidasExptName, help="Midas experiment name")
    parser.add_argument("-j", "--jobs", type = int, help="Number of nearline analysis processes to run in parallel")


    NLD = NearlineDaemon(parser.parse_args())
    NLD.mainloop()

