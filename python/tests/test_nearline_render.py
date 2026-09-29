"""Rendering nearline_job.py, light mode, and the file names around it.

Runs without Gaudi, MIDAS or a database. The rendered job is executed with
stand-in Configurables that only record what the job file sets on them, which
is enough to see what light mode changes in the job's configuration; whether
Gaudi accepts that configuration is for a real gaudirun.py.
"""

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

from pioneer.nearline import render
from pioneer.nearline.render import (PLACEHOLDERS, hists_file_name, registered_file_name,
                                     render_job)

JOB = Path(render.__file__).with_name("nearline_job.py")


# -- rendering -----------------------------------------------------------------

def _render(tmp_path, light, **kwargs):
    out = tmp_path / "out" / "run00790_00000.root"
    out.parent.mkdir(parents=True, exist_ok=True)
    target = render_job(tmp_path / "run00790_00000.mid.lz4", out, job_id=7, run_id=11,
                        light=light, **kwargs)
    return target, target.read_text()


def test_the_job_file_has_one_dollar_sign_per_placeholder():
    # nearline_job.py spells its own dollar sign chr(36) so that the only ones
    # in the unrendered file are the placeholders; the render step relies on it.
    source = JOB.read_text()
    assert source.count("$") == len(PLACEHOLDERS) == 12
    for name in PLACEHOLDERS:
        assert source.count("${" + name + "}") == 1, name


@pytest.mark.parametrize("light, value", [(False, "0"), (True, "1")])
def test_render_fills_every_placeholder_and_compiles(tmp_path, light, value):
    target, text = _render(tmp_path, light)
    assert target == tmp_path / "out" / "run00790_00000.py"
    for name in PLACEHOLDERS:
        assert "${" + name + "}" not in text, name
    assert "$" not in text
    compile(text, str(target), "exec")
    assert f'"light": "{value}"' in text
    assert '"job_id": "7"' in text and '"run_id": "11"' in text


def test_render_default_is_the_full_job(tmp_path):
    out = tmp_path / "run00001.root"
    text = render_job(tmp_path / "run00001.mid.lz4", out).read_text()
    assert '"light": "0"' in text


def test_render_cli_takes_light(tmp_path, capsys):
    out = tmp_path / "run00002.root"
    assert render.main([str(tmp_path / "run00002.mid.lz4"), str(out), "--light"]) == 0
    written = Path(capsys.readouterr().out.strip())
    assert '"light": "1"' in written.read_text()


def test_render_ignores_nl_light_in_the_callers_environment(tmp_path, monkeypatch):
    # light is the caller's argument; a stray NL_LIGHT must not change a job
    monkeypatch.setenv("NL_LIGHT", "1")
    _, text = _render(tmp_path, False)
    assert '"light": "0"' in text


# -- the rendered job, executed with stand-in Configurables -------------------

class _Conf:
    """Records what the job file sets; an unset list property reads as []."""

    def __init__(self, *args, **kwargs):
        self.__dict__["_name"] = args[0] if args else type(self).__name__
        self.__dict__.update(kwargs)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return []


def _gaudi_stubs():
    def module(name, *classes, **values):
        mod = types.ModuleType(name)
        for cls in classes:
            setattr(mod, cls, type(cls, (_Conf,), {}))
        for key, value in values.items():
            setattr(mod, key, value)
        return mod

    config = module("Gaudi.Configuration", "AuditorSvc", "ChronoAuditor", "ApplicationMgr",
                    DEBUG=2, INFO=3, WARNING=4, ERROR=5)
    config.__all__ = ["AuditorSvc", "ChronoAuditor", "ApplicationMgr",
                      "DEBUG", "INFO", "WARNING", "ERROR"]
    return {
        "Configurables": module("Configurables", "EvtDataSvc", "EvtPersistencySvc",
                                "Gaudi__Sequencer"),
        "Gaudi": module("Gaudi"),
        "Gaudi.Configuration": config,
        "shared": module("shared"),
        "shared.PiGaudiSharedSvcConf": module(
            "shared.PiGaudiSharedSvcConf", "PIAOutputStream", "PIConditionsSvc",
            "PIDataModelSvc", "PIHeaderSvc", "PIHistogramSvc"),
        "reco_testbeam": module("reco_testbeam"),
        "reco_testbeam.pi_testbeam_servicesConf": module(
            "reco_testbeam.pi_testbeam_servicesConf", "PIGeometrySvc"),
        "pi_midas": module("pi_midas"),
        "pi_midas.PIONEER_MIDAS_READERConf": module(
            "pi_midas.PIONEER_MIDAS_READERConf", "PIMidasSelector", "PIMidasConversionSvc",
            "PIMidasDecoder", "PITMidasMusip", "PITMidasWaveDream"),
        "reco_testbeam.pi_wdalgConf": module(
            "reco_testbeam.pi_wdalgConf", "PIWDCalibrator", "PIWDRFPhase",
            "PIWDScalerMonitor", "PIWDSettingsSummary", "PIWDWaveformAnalysis"),
        "reco_testbeam.pi_psmalg_expConf": module(
            "reco_testbeam.pi_psmalg_expConf", "PIPSMComputeWeight",
            "PIPSMDelayedCoincidence", "PIPSMMuPixMonitor", "PIPSMMuPixTimewalkCorrection",
            "PIPSMPatternReco", "PIPSMSMACalibration", "PIPSMSMAMonitor",
            "PIPSMSimpleTrackReco"),
    }


def _settings():
    """The job's settings block (and the rendered/light block after it), executed
    alone: enough to read the container names check() looks for."""
    source = JOB.read_text()
    start = source.index("# ===== RENDERED BY THE DAEMON")
    stop = source.index("# Measured, not guessed")
    scope = {"os": os, "Path": Path}
    exec(compile(source[start:stop], str(JOB), "exec"), scope)
    return scope


@pytest.fixture
def job_env(tmp_path, monkeypatch):
    """Gaudi stand-ins, an input file, an output directory and a conditions
    directory holding (empty) every container the job checks for."""
    for name, mod in _gaudi_stubs().items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setenv("PIONEERSYS", str(tmp_path / "pioneersys"))
    for var in ("NL_MIDAS", "NL_OUT", "NL_EVTMAX", "NL_PG", "NL_OVERRIDES", "NL_LIGHT"):
        monkeypatch.delenv(var, raising=False)
    settings = _settings()
    cond = tmp_path / "conditions"
    for name in (list(settings["WD_CONDITIONS_FILES"]) + list(settings["PSM_GEOMETRY_FILES"])
                 + [settings["PSM_CHANNEL_MAP_FILE"]] + list(settings["ODB_SPECS"])):
        (cond / name).parent.mkdir(parents=True, exist_ok=True)
        (cond / name).write_text("{}")
    monkeypatch.setenv("NL_CONDITIONS_DIR", str(cond))
    midas = tmp_path / "run00790_00000.mid.lz4"
    midas.write_bytes(b"")
    return tmp_path, midas


def _run(path, text=None):
    text = path.read_text() if text is None else text
    scope = {"__name__": "__main__", "__file__": str(path)}
    exec(compile(text, str(path), "exec"), scope)
    return scope


def test_rendered_full_job_keeps_everything(job_env, capsys):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    job = _run(target)
    assert job["RENDERED"] is True and job["LIGHT"] is False
    assert job["WRITE_NTUPLE"] is True and len(job["out_streams"]) == 1
    assert job["PSM_TIMEWALK"] is True and job["all_reco"].Timewalk == 1
    assert "MaxWidePairsPerFrame" not in job["sma_monitor"].__dict__
    assert job["musip"].smaDiagnostics is True
    assert "[nearline] light      off" in capsys.readouterr().out


def test_rendered_light_job_switches_off_the_four(job_env, capsys):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=True)
    job = _run(target)
    assert job["RENDERED"] is True and job["LIGHT"] is True
    assert job["WRITE_NTUPLE"] is False and job["out_streams"] == []
    assert job["PSM_TIMEWALK"] is False and job["all_reco"].Timewalk == 0
    assert "CounterInput" not in job["mupix_monitor"].__dict__
    assert "CounterInput" not in job["twc"].__dict__
    assert job["PSM_SMA_WIDE_DT"] is False
    assert job["sma_monitor"].MaxWidePairsPerFrame == 0
    assert job["PSM_SMA_DIAGNOSTICS"] is False and job["musip"].smaDiagnostics is False
    # the correction itself is not part of light mode
    assert job["twc"].applyTimewalkCorrection is True
    out = capsys.readouterr().out
    assert ("[nearline] light      on, switched off: "
            "WRITE_NTUPLE PSM_TIMEWALK PSM_SMA_WIDE_DT PSM_SMA_DIAGNOSTICS") in out
    assert "[nearline] rntuple    no RNTuple" in out


def test_rendered_job_ignores_nl_light(job_env, monkeypatch):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    monkeypatch.setenv("NL_LIGHT", "1")
    assert _run(target)["LIGHT"] is False


def test_a_job_from_a_renderer_without_light_is_the_full_job(job_env):
    # A daemon started before light mode keeps its old renderer in memory, which
    # fills every placeholder but light, and reads the job file from disk: the
    # light placeholder survives into the rendered job and must mean "0".
    tmp_path, midas = job_env
    from string import Template
    mapping = {"in_file": str(midas), "out_file": str(tmp_path / "runNNNNN_SSSSS.root"),
               "evt_max": "-1", "conditions_dir": os.environ["NL_CONDITIONS_DIR"], "pg": "",
               "rendered_at": "2026-01-01T00:00:00+00:00", "rendered_by": "old@daemon",
               "job_source": str(JOB), "job_git": "unknown", "job_id": "1", "run_id": "2"}
    assert set(mapping) == set(PLACEHOLDERS) - {"light"}
    text = Template(JOB.read_text()).safe_substitute(mapping)
    assert "${light}" in text
    target = tmp_path / "old_renderer.py"
    target.write_text(text)
    job = _run(target)
    assert job["RENDERED"] is True and job["LIGHT"] is False
    assert job["WRITE_NTUPLE"] is True and len(job["out_streams"]) == 1


def test_unrendered_job_takes_nl_light(job_env, monkeypatch):
    tmp_path, midas = job_env
    monkeypatch.setenv("NL_MIDAS", str(midas))
    monkeypatch.setenv("NL_OUT", str(tmp_path / "run00790_00000.root"))
    monkeypatch.setenv("NL_LIGHT", "1")
    job = _run(JOB)
    assert job["RENDERED"] is False and job["LIGHT"] is True
    assert job["out_streams"] == [] and job["sma_monitor"].MaxWidePairsPerFrame == 0


@pytest.mark.parametrize("value", ["yes", "true", "on"])
def test_a_light_value_other_than_1_or_0_is_rejected(job_env, monkeypatch, value):
    tmp_path, midas = job_env
    monkeypatch.setenv("NL_MIDAS", str(midas))
    monkeypatch.setenv("NL_OUT", str(tmp_path / "run00790_00000.root"))
    monkeypatch.setenv("NL_LIGHT", value)
    with pytest.raises(SystemExit, match="LIGHT is"):
        _run(JOB)


def test_a_non_bool_wide_dt_is_rejected(job_env):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    text = target.read_text().replace("PSM_SMA_WIDE_DT = True", "PSM_SMA_WIDE_DT = 'False'")
    with pytest.raises(SystemExit, match="PSM_SMA_WIDE_DT is"):
        _run(target, text)


# -- file names in the run database --------------------------------------------

def test_registered_file_name():
    assert registered_file_name("run00790_00000") == "run00790_00000.root"
    assert registered_file_name("run00790_00000", light=True) == "run00790_00000_hists.root"


def test_hists_file_name_takes_both_kinds_of_row():
    assert hists_file_name("run00790_00000") == "run00790_00000_hists.root"
    assert hists_file_name("run00790_00000_hists") == "run00790_00000_hists.root"


def test_registered_name_round_trips_through_the_run_database_split():
    # rundb.interface.open_file splits on the FIRST dot
    for light in (False, True):
        filebase, _, ext = registered_file_name("run00790_00000", light).partition(".")
        assert ext == "root"
        assert hists_file_name(filebase) == "run00790_00000_hists.root"


@pytest.fixture
def jobs_module(monkeypatch):
    """pioneer.nearline.jobs; its run-database import needs psycopg, which a
    stand-in replaces where it is not installed."""
    if importlib.util.find_spec("psycopg") is None:
        stub = types.ModuleType("pioneer.rundb.interface")
        stub.interface = type("interface", (), {})
        monkeypatch.setitem(sys.modules, "pioneer.rundb.interface", stub)
        monkeypatch.delitem(sys.modules, "pioneer.nearline.jobs", raising=False)
    import pioneer.nearline.jobs as jobs
    return jobs


def test_merge_input_files_maps_both_kinds_of_row(jobs_module):
    run_dir = Path("/nearline/run00790")
    got = jobs_module.merge_input_files(run_dir, ["run00790_00000", "run00790_00001_hists"])
    assert got == [str(run_dir / "run00790_00000_hists.root"),
                   str(run_dir / "run00790_00001_hists.root")]


def test_merge_input_files_lists_a_file_once(jobs_module):
    run_dir = Path("/nearline/run00790")
    got = jobs_module.merge_input_files(
        run_dir, ["run00790_00000", "run00790_00000_hists", "run00790_00001",
                  "run00790_00000"])
    assert got == [str(run_dir / "run00790_00000_hists.root"),
                   str(run_dir / "run00790_00001_hists.root")]


class _HistFilesDb:
    def __init__(self, rows):
        self.rows = rows

    def get_midas_run_number(self, run_id):
        return {3: 790, 4: 791}[run_id]

    def find_files(self, run_ids, ext):
        return [r for r in self.rows if r["run_id"] in run_ids]


def test_tuning_hist_files_lists_a_file_once():
    from pioneer.nearline.tuning import hist_files
    db = _HistFilesDb([
        {"run_id": 3, "filebase": "run00790_00000", "status": "DONE"},
        {"run_id": 3, "filebase": "run00790_00000_hists", "status": "DONE"},
        {"run_id": 3, "filebase": "run00790_00001", "status": "FAILED"},
        {"run_id": 3, "filebase": "run00790_00001", "status": "DONE"},
        {"run_id": 3, "filebase": "run00790_00002", "status": "RUNNING"},
        {"run_id": 4, "filebase": "run00791_00000", "status": "DONE"},
    ])
    found, skipped, numbers = hist_files(db, [3, 4], "/nearline/")
    assert [f["local"] for f in found] == ["/nearline/run00790/run00790_00000_hists.root",
                                           "/nearline/run00790/run00790_00001_hists.root",
                                           "/nearline/run00791/run00791_00000_hists.root"]
    # a failed row for a file another row finished is not reported
    assert skipped == [("/nearline/run00790/run00790_00002_hists.root", "RUNNING", 790)]
    assert numbers == [790, 791]


class _FakeDb:
    def __init__(self):
        self.opened = []

    def find_job_file(self, job_id):
        return {"filebase": "run00790_00003", "fileext": "mid.lz4"}

    def open_file(self, writer, run_id, name):
        self.opened.append((writer, run_id, name))
        return 99

    def update_status(self, *args):
        pass


@pytest.mark.parametrize("light, name", [(False, "run00790_00003.root"),
                                         (True, "run00790_00003_hists.root")])
def test_gaudi_job_renders_and_registers_by_light(jobs_module, tmp_path, monkeypatch,
                                                  light, name):
    class FakeProc:
        def poll(self):
            return 0

    # the job's own subprocess reference only: the render step runs git
    monkeypatch.setattr(jobs_module, "subprocess",
                        types.SimpleNamespace(Popen=lambda *a, **k: FakeProc(), STDOUT=-2))
    db = _FakeDb()
    cfg = {"job_id": 5, "run_id": 3, "job_type": "nearline", "midas_run_number": 790,
           "input": tmp_path, "output": tmp_path / "run00790"}
    if light:
        cfg["light"] = True
    job = jobs_module.GaudiJob(cfg, db)
    job.start()
    job.logfile.close()
    assert db.opened == [("nearline", 3, name)]
    rendered = (tmp_path / "run00790" / "run00790_00003.py").read_text()
    assert f'"light": "{int(light)}"' in rendered


# -- the daemon's command line -------------------------------------------------

@pytest.fixture
def daemon_module(monkeypatch, jobs_module):
    midas = types.ModuleType("midas")
    midas.TR_START, midas.TR_STOP = 1, 2
    midas.status_codes = {"SUCCESS": 1}
    client = types.ModuleType("midas.client")
    client.MidasClient = object
    midas.client = client
    monkeypatch.setitem(sys.modules, "midas", midas)
    monkeypatch.setitem(sys.modules, "midas.client", client)
    monkeypatch.delitem(sys.modules, "pioneer.nearline.daemon", raising=False)
    import pioneer.nearline.daemon as daemon
    return daemon


def test_daemon_parser_takes_light(daemon_module):
    parser = daemon_module.build_parser()
    assert parser.parse_args(["--midas-expt", "bt2026"]).light is False
    assert parser.parse_args(["--midas-expt", "bt2026", "--light"]).light is True


def test_start_command_keeps_light(daemon_module):
    parser = daemon_module.build_parser()
    args = parser.parse_args(["--midas-client", "NearlineDaemon", "--midas-host", "localhost",
                              "--midas-expt", "bt2026", "--light"])
    cmd = daemon_module.start_command(args, executable="/usr/bin/python",
                                      script="/home/pinky/daemon.py")
    assert cmd == ("/usr/bin/python /home/pinky/daemon.py --midas-client NearlineDaemon "
                   "--midas-host localhost --midas-expt bt2026 --light")
    # and the command it writes parses back to the same mode
    assert parser.parse_args(cmd.split()[2:]).light is True


def test_start_command_without_light(daemon_module):
    args = daemon_module.build_parser().parse_args(["--midas-expt", "bt2026"])
    cmd = daemon_module.start_command(args, executable="py", script="d.py")
    assert "--light" not in cmd


def test_dispatch_job_passes_light(daemon_module, monkeypatch, tmp_path):
    seen = []

    class FakeJob:
        def __init__(self, cfg):
            seen.append(dict(cfg))

        def start(self):
            pass

    monkeypatch.setattr(daemon_module.nl_jobs, "create_job", lambda cfg, db: FakeJob(cfg))
    d = object.__new__(daemon_module.NearlineDaemon)
    d.light = True
    d.midas_logger_path = d.backup_path = d.remote_path = tmp_path
    d.nearline_output_path = tmp_path
    d.db_interface = None
    queue = daemon_module.NearlineQueue("nearline", 1)
    d.dispatch_job(queue, {"midas_run_number": 790, "job_id": 1})
    assert seen[0]["light"] is True and len(queue.active) == 1


def test_daemon_jobs_has_no_default(daemon_module):
    parser = daemon_module.build_parser()
    assert parser.parse_args(["--midas-expt", "bt2026"]).jobs is None
    assert parser.parse_args(["--midas-expt", "bt2026", "-j", "5"]).jobs == 5


def test_num_jobs_to_write(daemon_module):
    write = daemon_module.num_jobs_to_write
    # first start: creates /Nearline/config, with -j or the default
    assert write(None, config_exists=False) == daemon_module.kDefaultNumJobs
    assert write(5, config_exists=False) == 5
    # later starts: only an explicit -j touches the ODB
    assert write(None, config_exists=True) is None
    assert write(2, config_exists=True) == 2


def test_start_command_has_no_jobs(daemon_module):
    args = daemon_module.build_parser().parse_args(["--midas-expt", "bt2026", "-j", "5"])
    cmd = daemon_module.start_command(args, executable="py", script="d.py")
    assert "-j" not in cmd.split() and "--jobs" not in cmd
