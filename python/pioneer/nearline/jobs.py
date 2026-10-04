import subprocess
import sys
import json

from pathlib import Path

from pioneer.rundb.interface import interface as db_interface

from pioneer.nearline.render import hists_file_name, registered_file_name, render_job

# Set this flag to True for debugging purpose only. It will
# print the shell command instead of executing it and execute
# a sleep command instead.
dry_run_all_jobs = False

def merge_input_files(run_dir, filebases) -> list[str]:
    """The histogram files of one run's nearline "root" rows, for the merge.

    run_dir is the run's output directory (<output path>/run<N>) and filebases
    the rows' filebase column, full-job rows (runNNNNN_SSSSS) and light-job
    rows (runNNNNN_SSSSS_hists) alike: each becomes <run_dir>/<...>_hists.root.
    A file is listed once however many rows name it -- a subrun processed again,
    or once by each kind of job, has more than one row -- because the merge
    adds up what it is given and would count it twice. The order is the rows'.
    """
    paths = []
    for filebase in filebases:
        path = str(Path(run_dir) / hists_file_name(filebase))
        if path not in paths:
            paths.append(path)
    return paths

def assert_list(obj) -> list:
    if isinstance(obj, list):
        return obj
    elif isinstance(obj, (str, bytes)):
        return [obj]
    try:
        return list(obj)
    except TypeError:
        return [obj]

class BaseJob:
    """
    This is the base class for all job types to be used.
    It provides the implementation of all common functionalities
    and defines the interface to `build_command` which has to
    be implemented by the specialised class.
    """
    def __init__(self, config, iface : db_interface):
        self.config = config
        self.source = Path(config['source_path'])
        self.logpath = Path(config['log_path'])

        # Destinaion can either be a path or a remote location description for rsync.
        self.destination = str(config['destination_path'])
        self.db = iface
        self.proc = None
        self.rc = None
        self.table = config.get("table", "postproc_job")
        self.infile = self.db.find_job_file(config['job_id'])

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
        self.logpath.mkdir(parents = True, exist_ok= True)

        cmd = self.build_command()
        if self.infile:
            log_name = self.logpath / f"{self.infile['filebase']}_{self.config['job_type']}.log"
        elif 'midas_run_number' in self.config.keys():
            log_name = self.logpath / f"run{self.config['midas_run_number']:05d}_{self.config['job_type']}.log"
        elif self.config.get("job_type", "") == "merge":
            log_name = self.logpath / f"seq{self.config['job_id']:05d}_{self.config['job_type']}.log"
        else:
            log_name = self.logpath / f"job{self.config['job_id']:05d}_{self.config['job_type']}.log"

        self.logfile = log_name.open("w")
        self.logfile.write(f"Job ID: {self.config['job_id']}, Run ID: {self.config.get('run_id', '---')}, Job Type: {self.config['job_type']}\n\n")
        self.logfile.write(" ".join([str(c) for c in cmd]))
        self.logfile.write("\n\n")
        self.logfile.flush()
        if (dry_run_all_jobs):
            print(" ".join([str(c) for c in cmd]))
            self.proc = subprocess.Popen(['sleep', '2'])
        else:
            self.proc = subprocess.Popen(cmd, start_new_session = True, stdout = self.logfile, stderr = subprocess.STDOUT)
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
        self.logfile.close()
        return status

    def raw_midas_files(self, include_sidecars = False):
        run_id = self.config['run_id']
        file_list = self.db.find_files(run_id, "mid.lz4")
        files = list()
        for aFile in file_list:
            files.append(self.source / f"{aFile['filebase']}.mid.lz4")
            if include_sidecars:
                files.extend([
                    self.source / f"{aFile['filebase']}.mid.crc32c",
                    self.source / f"{aFile['filebase']}.mid.lz4.crc32c",
                ])
        return files

    def get_files(self, include_sidecars = True):
        if self.infile is None:
            return self.raw_midas_files(include_sidecars = include_sidecars)

        producer = self.config.get("producer", None)
        file_list = [
            f"{self.infile['filebase']}.{self.infile['fileext']}"
        ]

        if producer is None:
            raise ValueError(f"No producer for file {self.infile['filebase']}.{self.infile['fileext']} registered")
        if producer in ("nearline", "farline"):
            if include_sidecars:
                fb = self.infile['filebase']
                if fb.endswith("_hists"):
                    file_list.extend([
                        f"{fb[:-6]}.py" # for uniquiness, we assign the python config file as a sidecar to the histogram root file.
                    ])
        elif producer.startswith("logger"):
            if include_sidecars:
                file_list.extend([
                    # list sidecar files here
                    f"{self.infile['filebase']}.mid.crc32c",
                    f"{self.infile['filebase']}.mid.lz4.crc32c"
                ])
        else:
            raise ValueError(f"Unknown producer {producer}")
        return [self.source  / f for f in file_list]


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

def odb_dump_path(data_dir, dump_file, run_number):
    """Where mlogger wrote the ODB dump of run `run_number`, or None.

    `dump_file` is /Logger/ODB Dump File (run%05d.json): mlogger formats it
    with the run number when it holds a %, and places it in /Logger/Data dir
    unless it is an absolute path. An empty setting means no dump.
    """
    if not dump_file:
        return None
    name = dump_file % run_number if "%" in dump_file else dump_file
    return Path(data_dir) / name


class RsyncJob(BaseJob):
    """
    Synchronise data between different locations.
    It can either be between two local locations (SSD to HDD transfer)
    or to a remote machine (DAQ machine to Analysis machine). It is assumed
    that SSH keys are configured for remote transfers.

    A raw-data job also carries the run's ODB dump when the daemon names one
    in `odb_dump_path`, so the analysis host can show a run's ODB without its
    reconstruction output. It is added only if it exists: a run without a dump
    must not fail the transfer of its data.
    """
    def get_files(self, include_sidecars = True):
        files = super().get_files(include_sidecars = include_sidecars)
        dump = self.config.get('odb_dump_path')
        if dump and Path(dump).is_file() and Path(dump) not in files:
            files.append(Path(dump))
        return files

    def build_command(self):
        return ['rsync', '-av', *self.get_files(), self.destination]

class GaudiJob(BaseJob):
    """
    This launches nearline/farline processing on a midas file and represents
    the backbone of the nearline software.
    """
    def __init__(self, config, iface):
        super().__init__(config, iface)
        self.out_file_ids = {}

    def format_config_file(self) -> Path:
        # `nearline_job.py` is itself the template: rendering it writes the
        # complete job next to the outputs as <filebase>.py. That file names
        # its own input, output and event limit, so it ignores every NL_*
        # variable and re-running it reproduces this run exactly. The
        # conditions source is resolved at this moment -- the job's default,
        # the database by service name, expanded through this user's
        # ~/.pg_service.conf, or NL_CONDITIONS if the daemon was started with
        # it -- and baked in with the daemon's NL_CONDITIONS_DIR, which is
        # what makes the artefact valid on a host with no /simulation. A
        # service this host does not define makes the render, and so start(),
        # raise.
        if self.infile is None:
            raise RuntimeError("input file not found in database")

        input_file_path  = self.source / f"{self.infile['filebase']}.{self.infile['fileext']}"
        output_file_path = Path(self.destination) / f"{self.infile['filebase']}.root"
        hist_only = (self.job_type == 'nearline')

        return render_job(input_file_path, output_file_path,
                          job_id = self.config['job_id'],
                          run_id = self.config['run_id'],
                          light = hist_only)

    def build_command(self):
        opt_file = self.format_config_file()
        return ['gaudirun.py', str(opt_file)]

    def start(self):
        # start job first, then register the file to the database.
        # if job start throws, the file is not entered to the database.
        Path(self.destination).mkdir(parents=True, exist_ok=True)
        result = super().start()
        self.out_file_ids = {
            "hist" : self.db.open_file(self.job_type, self.config['run_id'], f"{self.infile['filebase']}_hists.root")
        }
        if self.job_type == 'farline':
            self.out_file_ids['tuple'] = self.db.open_file(self.job_type, self.config['run_id'], f"{self.infile['filebase']}.root")
        return result

    def finalise(self):
        status = super().finalise()
        for key, out_file_id in self.out_file_ids.items():
            self.db.update_file_status(out_file_id, status)
            if status == 'DONE':
                # Job succeeded.
                job_id = self.db.schedule_postproc_job_on_file(
                    file_id = out_file_id,
                    task = "backup",
                    client = self.job_type
                )
                if self.job_type == 'nearline':
                    self.db.schedule_postproc_job_on_file(
                        file_id = out_file_id,
                        task = "remote",
                        client = self.job_type,
                        )
                elif key == 'tuple':
                    self.db.schedule_postproc_job_on_file(
                        file_id = out_file_id,
                        task = "cleanup",
                        client = self.job_type,
                        dependencies = [job_id]
                    )
        return status

class CleanJob(BaseJob):
    """
    Call a simple cleanup routine that removes the input files.
    Its design purpose is to free space on the SSD after a first pass of the data was
    completed and the raw data was backed up to HDD and remote locations.
    """
    def build_command(self):
        return ['rm', '-rf', *self.get_files()]


class MergeJob(BaseJob):
    """
    Combine nearline ROOT files belonging to a run sequence.
    The exact logic is detailed in `combine_files.py`
    """

    def build_job_description_file(self):
        dest_path = Path(self.destination)
        dest_path.mkdir(parents=True, exist_ok=True)
        outfile = dest_path / f"seq{self.config['id']:05d}.root"
        self.config['output_file'] = outfile
        config = {
            "output" : str(outfile),
            "runs"   : {
                f"{run_id}" : merge_input_files(self.source / f"run{self.db.get_midas_run_number(run_id):05d}",
                                                [f['filebase'] for f in self.db.find_files([run_id], "root")])
                for run_id in self.config['midas_run_ids']
            }
        }
        cfg_file_path = dest_path / f"seq{self.config['id']:05d}.json"
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
        "farline" : GaudiJob,
        "nearline": GaudiJob,
        "cleanup" : CleanJob,
        "merge"   : MergeJob
    }
    alloc_name = config['job_type'].lower()
    if alloc_name not in job_list.keys():
        raise RuntimeError(f"Can't allocate job with job_type {config['job_type']} aka {alloc_name}. Options are " + ", ".join(job_list.keys()))
    return job_list[alloc_name](config, iface)
