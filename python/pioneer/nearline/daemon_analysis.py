
import pioneer.rundb.interface
import pioneer.nearline.jobs as nl_jobs
from pioneer.nearline.queue import NearlineQueue


import midas.client        # connect to MIDAS ODB
import argparse            # parsing command line arguments
import os                  # os.path.join
import pathlib

# define some default parameters
kMidasClientName = "AnaDaemon"
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

class FarlineDaemon:
    def __init__(self, args):
        if (args.jobs):
            njobs = args.jobs
        else:
            njobs = kDefaultNumJobs

        self.queues = {
            "farline"  : NearlineQueue("farline", njobs),       # Actual Gaudi job
            "rsync"    : NearlineQueue(["rsync", "backup"], 1), # Data transfer SSD <-> HDD
            "cleanup"  : NearlineQueue("cleanup", 1)            # cleanup
        }

        # Create the MIDAS client for status monitoring, warnings and restarting
        # this is technically not required but considered a neat feature.
        self.client = midas.client.MidasClient(args.midas_client, host_name = args.midas_host, expt_name = args.midas_expt)

        self.client.odb_set("/Farline", {
            "Config" : {
                "SSD midas path"     : "/home/pioneer/inbox",
                "SSD output path"    : "/home/pioneer/inbox",
                "Midas backup path"  : "/home/pioneer/backup/raw",
                "Output backup path" : "/home/pioneer/backup/rec",
                "Log path"           : "/home/pioneer/farline_logs",
                "Num parallel jobs" : njobs,
                }
        }, update_structure_only = True)
        if (args.jobs):
            self.client.odb_set("/Nearline/config/Num parallel jobs", njobs)
        # keys of the tuning loop, created with their defaults when missing

        self.queues['farline'].maxJobs =     self.client.odb_get("/Farline/Config/Num parallel jobs")

        # Some paths may not be local.
        self.ssd_midas_path   = str(self.client.odb_get("/Farline/Config/SSD midas path"))
        self.ssd_output_path  = str(self.client.odb_get("/Farline/Config/SSD output path"))
        self.backup_mpath     = str(self.client.odb_get("/Farline/Config/Midas backup path"))
        self.backup_opath     = str(self.client.odb_get("/Farline/Config/Output backup path"))
        self.log_path         = str(self.client.odb_get("/Farline/Config/Log path"))

        self.sleep_time = 1000
        self.db_interface = pioneer.rundb.interface.interface(user = kDbUser, password = kDbPwd)


    def message(self, msg, is_error = False, send_to_slack = False):
        self.client.msg(msg, is_error = is_error)
        print(msg)

        if (send_to_slack):
            pass
            # todo: get slack hook and set it up

    def dispatch_job(self, queue : NearlineQueue, job_cfg):
        job_type = job_cfg['job_type']

        if job_type == "farline":
            # Analyse midas files and write root files to output
            job_cfg['source_path']      = self.ssd_midas_path # read midas files
            job_cfg['destination_path'] = f"{self.ssd_output_path}/run{job_cfg['midas_run_number']:05d}" # write root files
        elif job_type == 'rsync':
            # Restore a midas file from backup to SSD for reprocessing
            job_cfg['source_path']      = self.backup_mpath
            job_cfg['destination_path'] = self.ssd_midas_path
        elif job_type in ('backup', 'cleanup'):
            if job_cfg.get('producer', None) in ("nearline", "farline"):
                # a reco artefact
                job_cfg['source_path']      = f"{self.ssd_output_path}/run{job_cfg['midas_run_number']:05d}"
                job_cfg['destination_path'] = f"{self.backup_opath}/run{job_cfg['midas_run_number']:05d}"
            else:
                # Either a single midas file or a bulk backup/cleanup of all midas files on the farline machine
                job_cfg['source_path']      = f"{self.ssd_midas_path}/run{job_cfg['midas_run_number']:05d}"
                job_cfg['destination_path'] = f"{self.backup_mpath}/run{job_cfg['midas_run_number']:05d}"

        job_cfg['log_path'] = self.log_path

        theJob = nl_jobs.create_job(job_cfg, self.db_interface)
        try:
            theJob.start()
        except Exception as e:
            msg = f"Job {job_cfg['job_id']} failed to start: {e}"
            self.message(msg, is_error = True, send_to_slack = True)
        else:
            queue.add(theJob)

    def communicate_with_midas(self):
        if (self.client):
                self.client.communicate(self.sleep_time)

                # Paths where things shall be going to

                self.ssd_midas_path   = str(self.client.odb_get("/Farline/Config/SSD midas path"))
                self.ssd_output_path  = str(self.client.odb_get("/Farline/Config/SSD output path"))
                self.backup_mpath     = str(self.client.odb_get("/Farline/Config/Midas backup path"))
                self.backup_opath     = str(self.client.odb_get("/Farline/Config/Output backup path"))
                self.log_path         = str(self.client.odb_get("/Farline/Config/Log path"))

                # Update max number of jobs in nearline queue
                self.queues['farline'].maxJobs = self.client.odb_get("/Farline/Config/Num parallel jobs")

    def iterate_farline_queues(self):
        for aQueue in self.queues.values():

            # Step 2.1: Identify finished jobs in all queues
            finished_jobs = aQueue.get_finshed()
            for aJob in finished_jobs:
                aJob.finalise()

            # Step 2.2: Dispatch new jobs should there be open slots.
            numOpen = aQueue.getOpenSlots()
            if (numOpen > 0):
                newConfigs = self.db_interface.find_pending_postproc_jobs(job_type = aQueue.job_types, max_jobs = numOpen)
                for aConfig in newConfigs:
                    self.dispatch_job(aQueue, aConfig)


    def mainloop(self):
        while True:
            try:
                # Step 1: Communicate with midas
                self.communicate_with_midas()

                # Step 2: Iterate nearline job queues
                self.iterate_farline_queues()


            except Exception as e:
                self.client.msg(f"Farline Error {e}", is_error= True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Good Luck Have Fun - I did not yet write documentation for this")
    parser.add_argument("--midas-client", default=kMidasClientName, help="Midas client name")
    parser.add_argument("--midas-host", default=kMidasHostName, help="Midas host name")
    parser.add_argument("--midas-expt", default=kMidasExptName, help="Midas experiment name")
    parser.add_argument("-j", "--jobs", default=kDefaultNumJobs, type = int, help="Number of nearline analysis processes to run in parallel")

    NLD = FarlineDaemon(parser.parse_args())
    NLD.mainloop()

