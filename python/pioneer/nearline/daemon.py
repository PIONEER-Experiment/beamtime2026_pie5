
import pioneer.rundb.interface

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


class NearlineDaemon:
    def __init__(self, args):
        if (args.jobs):
            njobs = args.jobs
        else:
            njobs = kDefaultNumJobs

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
                        "jobs" : njobs,
                        "schedule" : args.auto_schedule
                        }
                })
            elif (args.jobs):
                self.client.odb_set("/Nearline/config/jobs", njobs)

            self.maxJobs = self.client.odb_get("/Nearline/config/jobs")
            self.autoSchedule = self.client.odb_get("/Nearline/config/schedule")
        else:
            self.client = None;
            self.maxJobs = njobs
            self.autoSchedule = args.auto_schedule
        self.active_processes = dict()
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

    def check_active_process_count(self):
        finished = list()
        for pid, payload in self.active_processes.items():
            rc = payload["proc"].poll()
            if rc is None:
                # That one is still running
                continue

            if rc == 0:
                # Process terminated successfully
                self.message(f"Nearline: Run {payload['run_id']} post-processed successfully")
            else:
                # Process failed. Alarm the shifter
                self.message(f"Nearline: Run {payload['run_id']} postprocessing exited with RC {rc}", is_error = True, send_to_slack = True)
            finished.append(pid)

            # Update database
            payload["rc"] = rc
            self.finalise(payload)

        for pid in finished:
            del self.active_processes[pid]

        return len(self.active_processes)
    

    def finalise(self, payload : dict):
        # This method only updates the run state database.
        # it must not mutate internal state.
        new_status = "DONE" if payload.get("rc", 1) == 0 else "FAILED"
        job_id = payload.get("job_id")
        self.db_interface.update_postproc_status(job_id, new_status)

        if (payload['rc'] == 0):
            config_ids = []

            config_ids.append(self.db_interface.add_new_configuration(
                table = "dummy",
                values = {
                    "p1" : job_id,
                    "p2" : job_id + 34
                }
            ))

            if (self.autoSchedule):
                job_id = self.db_interface.schedule_new_run(config_ids)
                self.message(f"Created new run with ID {job_id}")

    

    def dispatch_job(self, job):
        job["proc"] = subprocess.Popen(["sleep", "5"])
        self.message(f"Dispatching job {job}")
        job_id = job.get("job_id")
        self.db_interface.update_postproc_status(job_id, "RUNNING")
        self.active_processes[job["proc"].pid] = job


    def mainloop(self):
        while True:
            # Step 0: Communicate with midas if available
            if (self.client):
                self.client.communicate(10)
                self.maxJobs = self.client.odb_get("/Nearline/config/jobs")
                self.autoSchedule = self.client.odb_get("/Nearline/config/schedule")
                # all midas communication and midas-related stuff goes here.

            # Step 1: Check all active subprocesses
            n_active = self.check_active_process_count()

            if (n_active < self.maxJobs):
                jobs_to_dispatch = self.db_interface.find_pending_postproc_jobs(max_jobs = self.maxJobs - n_active)
                for job in jobs_to_dispatch:
                    self.dispatch_job(job)

            time.sleep(self.sleep_time)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Minimal CLI for DB + MIDAS config")
    parser.add_argument("--midas", action = "store_true")
    parser.add_argument("--midas-client", default=kMidasClientName, help="Midas client name")
    parser.add_argument("--midas-host", default=kMidasHostName, help="Midas host name")
    parser.add_argument("--midas-expt", default=kMidasExptName, help="Midas experiment name")
    parser.add_argument("-j", "--jobs", type = int, help="Number of parallel nearline analysis processes to run in parallel")
    parser.add_argument("-s", "--auto-schedule", action = "store_true")


    NLD = NearlineDaemon(parser.parse_args())
    NLD.mainloop()

    