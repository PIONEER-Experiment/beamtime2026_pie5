"""The raw-data rsync job carrying the run's ODB dump.

Runs without MIDAS or a database: the job's database interface is a stand-in
that knows one run's raw files.
"""

import sys
import types
from pathlib import Path

import pytest

from pioneer.nearline.jobs import CleanJob, RsyncJob, odb_dump_path


class FakeDb:
    """The two lookups a raw-data job makes: its own file (none, for a
    run-level job) and the run's MIDAS files."""

    def __init__(self, filebases):
        self.filebases = filebases

    def find_job_file(self, job_id):
        return None

    def find_files(self, run_id, ext):
        return [{"filebase": fb} for fb in self.filebases]


def raw_job(cls, data_dir, **extra):
    config = {
        "job_id": 1, "job_type": "remote", "run_id": 7, "midas_run_number": 1070,
        "source_path": str(data_dir), "destination_path": "analysis:/home/pioneer/inbox",
        "log_path": str(data_dir / "logs"),
    }
    config.update(extra)
    return cls(config, FakeDb(["run01070_00000", "run01070_00001"]))


def test_odb_dump_path_formats_the_run_number_into_the_data_dir():
    assert odb_dump_path("/home/pinky/online/", "run%05d.json", 1070) == \
        Path("/home/pinky/online/run01070.json")


def test_odb_dump_path_keeps_an_absolute_name_and_a_fixed_name():
    assert odb_dump_path("/data", "/elsewhere/run%05d.json", 5) == Path("/elsewhere/run00005.json")
    assert odb_dump_path("/data", "last.json", 5) == Path("/data/last.json")


def test_odb_dump_path_is_none_without_a_dump_file():
    assert odb_dump_path("/data", "", 5) is None
    assert odb_dump_path("/data", None, 5) is None


def test_rsync_sends_the_dump_after_the_raw_files(tmp_path):
    dump = tmp_path / "run01070.json"
    dump.write_text("{}")
    cmd = raw_job(RsyncJob, tmp_path, odb_dump_path=str(dump)).build_command()
    assert cmd[0:2] == ["rsync", "-av"]
    assert cmd[-1] == "analysis:/home/pioneer/inbox"
    assert cmd[-2] == dump
    assert tmp_path / "run01070_00001.mid.lz4" in cmd


def test_rsync_without_the_dump_file_sends_the_raw_files_only(tmp_path):
    cmd = raw_job(RsyncJob, tmp_path, odb_dump_path=str(tmp_path / "run01070.json")).build_command()
    assert not any(str(f).endswith(".json") for f in cmd)
    assert len(cmd) == 2 + 2 * 3 + 1


def test_rsync_without_a_dump_named_is_unchanged(tmp_path):
    (tmp_path / "run01070.json").write_text("{}")
    cmd = raw_job(RsyncJob, tmp_path).build_command()
    assert not any(str(f).endswith(".json") for f in cmd)


def test_cleanup_never_removes_the_dump(tmp_path):
    dump = tmp_path / "run01070.json"
    dump.write_text("{}")
    cmd = raw_job(CleanJob, tmp_path, job_type="cleanup", odb_dump_path=str(dump)).build_command()
    assert cmd[0:2] == ["rm", "-rf"]
    assert dump not in cmd


# -- the daemon names the dump for raw-data jobs only ---------------------------

@pytest.fixture
def daemon(monkeypatch, tmp_path):
    midas = types.ModuleType("midas")
    midas.TR_START, midas.TR_STOP = 1, 2
    midas.status_codes = {"SUCCESS": 1}
    client = types.ModuleType("midas.client")
    client.MidasClient = object
    midas.client = client
    monkeypatch.setitem(sys.modules, "midas", midas)
    monkeypatch.setitem(sys.modules, "midas.client", client)
    monkeypatch.delitem(sys.modules, "pioneer.nearline.daemon", raising=False)
    import pioneer.nearline.daemon as module

    seen = []

    class FakeJob:
        def __init__(self, cfg):
            seen.append(dict(cfg))

        def start(self):
            pass

    monkeypatch.setattr(module.nl_jobs, "create_job", lambda cfg, db: FakeJob(cfg))
    d = object.__new__(module.NearlineDaemon)
    d.light = False
    d.midas_logger_path = tmp_path / "online"
    d.backup_path = tmp_path / "backup"
    d.remote_path = "analysis:/home/pioneer/inbox"
    d.nearline_output_path = tmp_path / "nearline"
    d.odb_dump_file = "run%05d.json"
    d.db_interface = None
    d.seen = seen
    d.queue = module.NearlineQueue("remote", 1)
    return d


@pytest.mark.parametrize("job_type", ["remote", "backup"])
def test_raw_jobs_name_the_dump(daemon, tmp_path, job_type):
    daemon.dispatch_job(daemon.queue, {"job_type": job_type, "midas_run_number": 1070,
                                       "job_id": 1, "producer": None})
    assert daemon.seen[0]["odb_dump_path"] == str(tmp_path / "online" / "run01070.json")


@pytest.mark.parametrize("job_type, producer", [("remote", "nearline"), ("backup", "nearline"),
                                                ("cleanup", None)])
def test_other_jobs_do_not(daemon, job_type, producer):
    daemon.dispatch_job(daemon.queue, {"job_type": job_type, "midas_run_number": 1070,
                                       "job_id": 1, "producer": producer})
    assert "odb_dump_path" not in daemon.seen[0]


def test_no_dump_without_a_logger_dump_file(daemon):
    daemon.odb_dump_file = ""
    daemon.dispatch_job(daemon.queue, {"job_type": "remote", "midas_run_number": 1070,
                                       "job_id": 1, "producer": None})
    assert "odb_dump_path" not in daemon.seen[0]
