import subprocess
import sys
import json

from pathlib import Path
from string import Template

from pioneer.rundb.interface import interface as db_interface

# This flag is set if the list of input files shall be determined
# by using glob. Otherwise, an explicit list is used.
glob_input_files = False

# Set this flag to True for debugging purpose only. It will
# print the shell command instead of executing it and execute
# a sleep command instead.
dry_run_all_jobs = True

class BaseJob:
    """
    This is the base class for all job types to be used.
    It provides the implementation of all common functionalities
    and defines the interface to `build_command` which has to
    be implemented by the specialised class.
    """
    def __init__(self, config, iface : db_interface):
        self.config = config
        self.db = iface
        self.proc = None
        self.rc = None
        self.table = config.get("table", "postproc_job")

    def build_command(self):
        # This function should be overwritten by the actual job description
        raise NotImplementedError

    @property
    def processing_status(self):
        # You can overwrite this for advanced multi-step jobs
        # end of sequence jobs may use PPROC to mark the
        # post-processing stage.
        return "RUNNING"

    def start(self):
        cmd = self.build_command()
        if (dry_run_all_jobs):
            print(" ".join([str(c) for c in cmd]))
            self.proc = subprocess.Popen(['sleep', '2'])
        else:
            self.proc = subprocess.Popen(cmd, start_new_session = True)
        self.db.update_status(self.table, self.config['job_id'], self.processing_status)
        return self.proc

    def poll(self):
        if (self.proc is None):
            raise RuntimeError("A job has to be started before polling")
        self.rc = self.proc.poll()
        return self.rc

    def finalise(self):
        if self.rc is None:
            raise RuntimeError("Finalise called before job was finished")
        status = 'DONE' if self.rc == 0 else 'FAILED'
        self.db.update_status(self.table, self.config['job_id'], status)
        return status

    def raw_midas_files(self, include_sidecars = False):
        parent_path = Path(self.config['input'])
        run_id = self.config['job_id']
        file_list = self.db.find_files(run_id, "mid.lz4")
        files = list()
        for aFile in file_list:
            files.append(parent_path / f"{aFile['filebase']}.mid.lz4")
            if include_sidecars:
                files.extend([
                    parent_path / f"{aFile['filebase']}.mid.crc32c",
                    parent_path / f"{aFile['filebase']}.mid.lz4.crc32c",
                ])
        return files

    @property
    def job_type(self):
        return self.config['job_type']

class DummyJob(BaseJob):
    """
    This is a dummy job that only sleeps for 3 seconds and has no
    other effects. Use for testing purposes only.
    """
    def build_command(self):
        return ['sleep', '3']

class RsyncJob(BaseJob):
    """
    Synchronise data between different locations.
    It can either be between two local locations (SSD to HDD transfer)
    or to a remote machine (DAQ machine to Analysis machine). It is assumed
    that SSH keys are configured for remote transfers.
    """
    def build_command(self):
        return ['rsync', '-av', *self.raw_midas_files(include_sidecars = True), self.config[self.config['job_type'].lower()]]

class GaudiJob(BaseJob):
    """
    This launches nearline processing on a midas file and represents
    the backbone of the nearline software.
    """
    def __init__(self, config, iface):
        super().__init__(config, iface)
        self.infile = self.db.find_job_file(config['job_id'])
        self.out_file_id = None

    def format_config_file(self) -> Path:
        template_path = Path(__file__).resolve().parent / "template_config.py"
        if self.infile is None:
            raise RuntimeError("input file not found in database")

        input_file_path  = Path(self.config['input'])  / f"{self.infile['filebase']}.{self.infile['fileext']}"
        out_file_name = f"{self.infile['filebase']}.{self.infile['fileext']}"
        output_file_path = Path(self.config['output']) / out_file_name
        cfg_file_name = f"{self.infile['filebase']}.py"

        cfg_template = Template(template_path.read_text())
        cfg_str = cfg_template.substitute(
            author   = "Me",
            in_file  = input_file_path,
            out_file = output_file_path
        )

        opt_file = Path(self.config['output']) / cfg_file_name
        opt_file.write_text(cfg_str)

        return opt_file

    def build_command(self):
        opt_file = self.format_config_file()
        return ['gaudirun.py', str(opt_file)]

    def start(self):
        # start job first, then register the file to the database.
        # if job start throws, the file is not entered to the database.
        result = super().start()
        self.out_file_id = self.db.open_file('nearline', self.config['run_id'], f"{self.infile['filebase']}.root")
        return result

    def finalise(self):
        status = super().finalise()
        self.db.update_file_status(self.out_file_id, status)
        return status

class CleanJob(BaseJob):
    """
    Call a simple cleanup routine that removes the input files.
    Its design purpose is to free space on the SSD after a first pass of the data was
    completed and the raw data was backed up to HDD and remote locations.
    """
    def build_command(self):
        input_files = self.raw_midas_files(include_sidecars = True)
        return ['rm', '-rf', *input_files]


class MergeJob(BaseJob):
    """
    Combine nearline ROOT files belonging to a run sequence.
    The exact logic is detailed in `combine_files.py`
    """

    def build_job_description_file(self):
        input_path = Path(self.config["input"])
        config = {
            "output" : str(self.config["output"]),
            "runs"   : {
                f"{run_id}" : [str(input_path / f"{f['filebase']}.root") for f in self.db.find_files([run_id], "root")]
                for run_id in self.config['midas_run_ids']
            }
        }
        cfg_file_path = Path(self.config['cfg_file'])
        with cfg_file_path.open("w") as f:
            json.dump(config, f, indent = 2)

        return cfg_file_path

    def build_command(self):
        cmd = [sys.executable, "-m", "pioneer.nearline.combine_files", str(self.build_job_description_file())]
        return cmd;

    @property
    def processing_status(self):
        return 'PPROC'



def create_job(config, iface) -> BaseJob:
    """
    Job allocation factory

    It will check the job_type retrieved from the configuration and return
    an appropriate job class for further processing.
    """
    job_list = {
        "dummy"   : DummyJob,

        # Note that 'rsync', 'remote' and 'backup' all refer to the same job class.
        # The distinction is due to different resource requirements in scheduling.
        "rsync"   : RsyncJob,
        "remote"  : RsyncJob,
        "backup"  : RsyncJob,
        "gaudi"   : GaudiJob,
        "nearline": GaudiJob,
        "cleanup" : CleanJob,
        "merge"   : MergeJob
    }
    alloc_name = config['job_type'].lower()
    if alloc_name not in job_list.keys():
        raise RuntimeError(f"Can't allocate job with job_type {config['job_type']} aka {alloc_name}. Options are " + ", ".join(job_list.keys()))
    return job_list[alloc_name](config, iface)
