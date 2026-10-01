"""Rendering nearline_job.py, light mode, the conditions source, and the file
names around it.

Runs without Gaudi, MIDAS or a database. The rendered job is executed with
stand-in Configurables that only record what the job file sets on them, which
is enough to see what light mode and the conditions source change in the job's
configuration; whether Gaudi accepts that configuration is for a real
gaudirun.py. The libpq service the job defaults to comes from a service file
in tmp_path (PGSERVICEFILE), never from the caller's ~/.pg_service.conf.
"""

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

from pioneer.nearline import render
from pioneer.conddb.pgservice import ServiceNotFound
from pioneer.nearline.render import (PLACEHOLDERS, hists_file_name, registered_file_name,
                                     render_job)

JOB = Path(render.__file__).with_name("nearline_job.py")

# What the test service file expands the job's default service into.
CONNINFO = "host=pg.example port=5432 dbname=conditions user=cond_viewer"


@pytest.fixture(autouse=True)
def _pg_service(tmp_path, monkeypatch):
    """A service file defining pioneer-conditions (with a password, which must never
    reach a rendered job), and no NL_CONDITIONS from the caller."""
    path = tmp_path / "pg_service.conf"
    path.write_text("[pioneer-conditions]\nhost=pg.example\nport=5432\ndbname=conditions\n"
                    "user=cond_viewer\npassword=hunter2\n")
    monkeypatch.setenv("PGSERVICEFILE", str(path))
    monkeypatch.delenv("PGSYSCONFDIR", raising=False)
    for var in ("NL_CONDITIONS", "NL_CONDITIONS_DIR"):
        monkeypatch.delenv(var, raising=False)
    return path


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
    # the database connection is part of the conditions source, not a list of its own
    assert "conditions" in PLACEHOLDERS and "pg" not in PLACEHOLDERS
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

class _EveryName:
    """A property table that has every property, as an up-to-date build does."""

    def __contains__(self, name):
        return True


class _Conf:
    """Records what the job file sets; an unset list property reads as []."""

    @classmethod
    def getDefaultProperties(cls):
        return _EveryName()

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
    for var in ("NL_MIDAS", "NL_OUT", "NL_EVTMAX", "NL_CONDITIONS", "NL_OVERRIDES", "NL_LIGHT"):
        monkeypatch.delenv(var, raising=False)
    settings = _settings()
    # named like the real tree: db mode refuses a directory of containers that is not one
    cond = tmp_path / "reco_testbeam" / "conditions"
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
    assert job["musip"].correctFineOffsets is True
    assert job["musip"].skipFirstBank is True      # run00790_00000 is subrun 0
    assert "[nearline] light      off" in capsys.readouterr().out


def test_stale_first_frame_is_skipped_only_for_subrun_0(job_env):
    tmp_path, _ = job_env
    for name, expected in (("run00790_00000", True), ("run00790_00001", False),
                           ("run00790", False)):
        midas = tmp_path / f"{name}.mid.lz4"
        midas.write_bytes(b"")
        job = _run(render_job(midas, tmp_path / f"{name}.root", light=False))
        assert job["musip"].skipFirstBank is expected, name
        assert job["musip"].correctFineOffsets is True


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
    # the SMA fine-time correction and the stale-frame skip change the hits, so
    # light mode leaves them on
    assert job["musip"].correctFineOffsets is True and job["musip"].skipFirstBank is True
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
               "evt_max": "-1", "conditions_dir": os.environ["NL_CONDITIONS_DIR"],
               "conditions": "db:" + CONNINFO,
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


def test_the_scint_window_reaches_the_track_reco(job_env):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    job = _run(target)
    assert job["PSM_SCINT_WINDOW_NS"] == 5.0
    assert job["all_reco"].thrScint == 5.0


def test_the_lpair_ownership_settings_reach_the_track_reco(job_env):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    job = _run(target)
    assert job["all_reco"].AggregateOwnersOnly == 1
    assert job["all_reco"].lPairOwnerOffsetNs == 0.0


@pytest.mark.parametrize("value", ["float('nan')", "'x'", "None"])
def test_an_owner_offset_that_is_not_a_finite_number_is_rejected(job_env, value):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    text = target.read_text().replace("PSM_LPAIR_OWNER_OFFSET_NS = 0.0", f"PSM_LPAIR_OWNER_OFFSET_NS = {value}")
    with pytest.raises(SystemExit, match="PSM_LPAIR_OWNER_OFFSET_NS"):
        _run(target, text)


@pytest.mark.parametrize("value", ["2", "-1", "'yes'"])
def test_an_owners_only_value_other_than_0_or_1_is_rejected(job_env, value):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    text = target.read_text().replace("PSM_AGGREGATE_OWNERS_ONLY = 1", f"PSM_AGGREGATE_OWNERS_ONLY = {value}")
    with pytest.raises(SystemExit, match="PSM_AGGREGATE_OWNERS_ONLY"):
        _run(target, text)


@pytest.mark.parametrize("value", ["0.0", "-1.0", "20.0", "25.0"])
def test_a_scint_window_that_is_empty_or_reaches_the_delayed_window_is_rejected(job_env, value):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    text = target.read_text().replace("PSM_SCINT_WINDOW_NS = 5.0", f"PSM_SCINT_WINDOW_NS = {value}")
    with pytest.raises(SystemExit, match="PSM_SCINT_WINDOW_NS"):
        _run(target, text)


def test_rf_and_current_channels_come_from_the_map_by_default(job_env):
    tmp_path, midas = job_env
    job = _run(render_job(midas, tmp_path / "run00790_00000.root", light=False))
    assert job["PSM_RF_CHANNEL"] is None and job["PSM_CURRENT_CHANNEL"] is None
    musip = job["musip"].__dict__
    assert "rf_channel" not in musip and "current_channel" not in musip
    # the snap list is the decoder's default: its resolved RF channel
    assert "fineOffsetSnapChannels" not in musip
    # whether a run has RF is decided by its map, so the consumers always get it
    assert job["sma_monitor"].RFInput == "/Event/rf"
    assert job["all_reco"].RFInput == "/Event/rf"


def test_reco_and_sma_monitor_read_the_pairing_sidecar(job_env):
    tmp_path, midas = job_env
    job = _run(render_job(midas, tmp_path / "run00790_00000.root", light=False))
    # the sidecar is index-parallel to /Event/mutrig_cal, which both read
    assert job["all_reco"].S_hits == "/Event/mutrig_cal"
    assert job["all_reco"].ScintHitsInput == "/Event/sma_hits"
    assert job["sma_monitor"].input == "/Event/mutrig_cal"
    assert job["sma_monitor"].ScintHitsInput == "/Event/sma_hits"
    assert job["sma_cal"].hitsOutput == "/Event/sma_hits"


def test_rf_and_current_channels_override_the_map_when_set(job_env):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    text = (target.read_text().replace("PSM_RF_CHANNEL = None", "PSM_RF_CHANNEL = 5")
            .replace("PSM_CURRENT_CHANNEL = None", "PSM_CURRENT_CHANNEL = 9"))
    job = _run(target, text)
    assert job["musip"].rf_channel == 5 and job["musip"].current_channel == 9
    assert "fineOffsetSnapChannels" not in job["musip"].__dict__


def test_rf_and_current_channels_can_be_switched_off(job_env):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    text = (target.read_text().replace("PSM_RF_CHANNEL = None", 'PSM_RF_CHANNEL = "off"')
            .replace("PSM_CURRENT_CHANNEL = None", 'PSM_CURRENT_CHANNEL = "off"'))
    job = _run(target, text)
    assert job["musip"].rf_channel == -2 and job["musip"].current_channel == -2


def test_the_nim_lag_is_on_with_the_decoders_roles_by_default(job_env):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    job = _run(target)
    assert job["PSM_SMA_NIM_LAG"] is True and job["PSM_SMA_NIM_NOMINAL_DELAY_NS"] == {}
    musip = job["musip"].__dict__
    # the roles by detector id are the decoder's defaults; no raw-channel lists
    for prop in ("fineOffsetLagVids", "fineOffsetLagNominalNs", "fineOffsetReferenceVid",
                 "fineOffsetVoteVids", "fineOffsetHalvedVids", "fineOffsetVoteChannels",
                 "fineOffsetHalvedChannels", "fineOffsetLagChannels", "fineOffsetReferenceChannel"):
        assert prop not in musip, prop
    text = (target.read_text().replace("PSM_SMA_NIM_LAG = True", "PSM_SMA_NIM_LAG = False")
            .replace("PSM_SMA_NIM_NOMINAL_DELAY_NS = {}", "PSM_SMA_NIM_NOMINAL_DELAY_NS = {2025: 42}"))
    musip = _run(target, text)["musip"]
    assert musip.fineOffsetLagVids == []
    assert musip.fineOffsetLagNominalNs == {2025: 42.0}
    assert isinstance(musip.fineOffsetLagNominalNs[2025], float)


@pytest.mark.parametrize("old, new, match", [
    ("PSM_SMA_NIM_LAG = True", "PSM_SMA_NIM_LAG = 1", "PSM_SMA_NIM_LAG"),
    ("PSM_SMA_NIM_LAG = True", "PSM_SMA_NIM_LAG = 'True'", "PSM_SMA_NIM_LAG"),
    ("PSM_SMA_NIM_NOMINAL_DELAY_NS = {}", "PSM_SMA_NIM_NOMINAL_DELAY_NS = {'2025': 42}",
     "PSM_SMA_NIM_NOMINAL_DELAY_NS"),
    ("PSM_SMA_NIM_NOMINAL_DELAY_NS = {}", "PSM_SMA_NIM_NOMINAL_DELAY_NS = {2025: '42'}",
     "PSM_SMA_NIM_NOMINAL_DELAY_NS"),
    ("PSM_SMA_NIM_NOMINAL_DELAY_NS = {}", "PSM_SMA_NIM_NOMINAL_DELAY_NS = {2025: 2**20}",
     "PSM_SMA_NIM_NOMINAL_DELAY_NS"),
    ("PSM_SMA_NIM_NOMINAL_DELAY_NS = {}", "PSM_SMA_NIM_NOMINAL_DELAY_NS = {2025: float('nan')}",
     "PSM_SMA_NIM_NOMINAL_DELAY_NS"),
    ("PSM_SMA_NIM_NOMINAL_DELAY_NS = {}", "PSM_SMA_NIM_NOMINAL_DELAY_NS = [(2025, 42)]",
     "PSM_SMA_NIM_NOMINAL_DELAY_NS"),
])
def test_a_bad_nim_lag_knob_is_rejected(job_env, old, new, match):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    text = target.read_text()
    assert old in text
    with pytest.raises(SystemExit, match=match):
        _run(target, text.replace(old, new))


@pytest.mark.parametrize("rf, current, match", [
    ("16", "None", "PSM_RF_CHANNEL is 16"),
    ("'OFF'", "None", "PSM_RF_CHANNEL is 'OFF'"),
    ("-2", "None", "PSM_RF_CHANNEL is -2"),
    ("None", "'none'", "PSM_CURRENT_CHANNEL is 'none'"),
    ("-1", "None", "PSM_RF_CHANNEL is -1"),
    ("'6'", "None", "PSM_RF_CHANNEL is '6'"),
    ("True", "None", "PSM_RF_CHANNEL is True"),
    ("None", "16", "PSM_CURRENT_CHANNEL is 16"),
    ("None", "6.0", "PSM_CURRENT_CHANNEL is 6.0"),
    ("6", "6", "are both 6"),
])
def test_a_bad_rf_or_current_channel_is_rejected(job_env, rf, current, match):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    text = (target.read_text().replace("PSM_RF_CHANNEL = None", f"PSM_RF_CHANNEL = {rf}")
            .replace("PSM_CURRENT_CHANNEL = None", f"PSM_CURRENT_CHANNEL = {current}"))
    with pytest.raises(SystemExit, match=match):
        _run(target, text)


def test_the_scaler_monitor_counts_the_current_of_the_role_table(job_env):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    job = _run(target)
    assert job["scaler_monitor"].RoleTable == "wd_channel_map"
    assert "RoleTag" not in job["scaler_monitor"].__dict__
    job = _run(target, target.read_text().replace('WD_ROLE_TABLE = "wd_channel_map"',
                                                  'WD_ROLE_TABLE = ""'))
    assert "RoleTable" not in job["scaler_monitor"].__dict__


def test_sma_nim_pairing_knobs_reach_the_calibration_layer(job_env):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    job = _run(target)
    cal = job["sma_cal"]
    assert cal.input == "/Event/mutrig" and cal.output == "/Event/mutrig_cal"
    assert cal.hitsOutput == "/Event/sma_hits"
    assert cal.NimPairing is True and cal.PairWindowNs == 20.0
    assert cal.TimeSource == "tot" and cal.NimOnlyTot == 1.0
    # the development override is not set unless asked for
    assert "OffsetOverrideNs" not in cal.__dict__
    text = (target.read_text().replace("PSM_SMA_NIM_PAIRING = True", "PSM_SMA_NIM_PAIRING = False")
            .replace("PSM_SMA_PAIR_WINDOW_NS = 20.0", "PSM_SMA_PAIR_WINDOW_NS = 12")
            .replace('PSM_SMA_TIME_SOURCE = "tot"', 'PSM_SMA_TIME_SOURCE = "nim"')
            .replace("PSM_SMA_NIM_ONLY_TOT = 1.0", "PSM_SMA_NIM_ONLY_TOT = 2")
            .replace("PSM_SMA_OFFSET_OVERRIDE_NS = {}", "PSM_SMA_OFFSET_OVERRIDE_NS = {2024: -153522}"))
    cal = _run(target, text)["sma_cal"]
    assert cal.NimPairing is False and cal.PairWindowNs == 12.0 and cal.TimeSource == "nim"
    assert cal.NimOnlyTot == 2.0 and isinstance(cal.NimOnlyTot, float)
    assert cal.OffsetOverrideNs == {2024: -153522.0}
    assert isinstance(cal.OffsetOverrideNs[2024], float)


@pytest.mark.parametrize("mode, drops", [("raw", ["drop /Event/mutrig_cal"]),
                                         ("both", []),
                                         ("calibrated", ["drop /Event/mutrig"])])
def test_sma_hits_is_kept_in_every_sma_cal_ntuple_mode(job_env, mode, drops, capsys):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    text = target.read_text().replace('PSM_SMA_CAL_NTUPLE = "raw"', f'PSM_SMA_CAL_NTUPLE = "{mode}"')
    rules = list(_run(target, text)["output"].SelectionRules)
    # the default PSM_TWC_NTUPLE rule first, then the SMA mode's; nothing drops the sidecar
    assert rules == ["drop /Event/muquad"] + drops
    # "calibrated" drops what the sidecar's raw indices point into, and says so
    warned = "raw TOT/NIM indices of /Event/sma_hits" in capsys.readouterr().out
    assert warned == (mode == "calibrated")


def test_sma_hits_ntuple_false_drops_the_sidecar_last(job_env):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    text = (target.read_text().replace("PSM_SMA_HITS_NTUPLE = True", "PSM_SMA_HITS_NTUPLE = False")
            .replace("NTUPLE_RULES = []", 'NTUPLE_RULES = ["keep /Event/*"]'))
    rules = list(_run(target, text)["output"].SelectionRules)
    assert rules[0] == "keep /Event/*" and rules[-1] == "drop /Event/sma_hits"


def test_users_rules_can_drop_the_sidecar(job_env):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    text = target.read_text().replace("NTUPLE_RULES = []", 'NTUPLE_RULES = ["drop /Event/sma_hits"]')
    rules = list(_run(target, text)["output"].SelectionRules)
    assert rules[0] == "drop /Event/sma_hits"
    assert not any(r.startswith("keep") and "sma_hits" in r for r in rules)


@pytest.mark.parametrize("old, new, match", [
    ("PSM_SMA_NIM_PAIRING = True", "PSM_SMA_NIM_PAIRING = 'False'", "PSM_SMA_NIM_PAIRING"),
    ("PSM_SMA_NIM_PAIRING = True", "PSM_SMA_NIM_PAIRING = 1", "PSM_SMA_NIM_PAIRING"),
    ("PSM_SMA_PAIR_WINDOW_NS = 20.0", "PSM_SMA_PAIR_WINDOW_NS = 0", "PSM_SMA_PAIR_WINDOW_NS"),
    ("PSM_SMA_PAIR_WINDOW_NS = 20.0", "PSM_SMA_PAIR_WINDOW_NS = -5.0", "PSM_SMA_PAIR_WINDOW_NS"),
    ("PSM_SMA_PAIR_WINDOW_NS = 20.0", "PSM_SMA_PAIR_WINDOW_NS = 5000.0", "PSM_SMA_PAIR_WINDOW_NS"),
    ("PSM_SMA_PAIR_WINDOW_NS = 20.0", "PSM_SMA_PAIR_WINDOW_NS = '20'", "PSM_SMA_PAIR_WINDOW_NS"),
    ("PSM_SMA_PAIR_WINDOW_NS = 20.0", "PSM_SMA_PAIR_WINDOW_NS = True", "PSM_SMA_PAIR_WINDOW_NS"),
    ("PSM_SMA_PAIR_WINDOW_NS = 20.0", "PSM_SMA_PAIR_WINDOW_NS = float('nan')", "PSM_SMA_PAIR_WINDOW_NS"),
    ('PSM_SMA_TIME_SOURCE = "tot"', 'PSM_SMA_TIME_SOURCE = "TOT"', "PSM_SMA_TIME_SOURCE"),
    ('PSM_SMA_TIME_SOURCE = "tot"', "PSM_SMA_TIME_SOURCE = None", "PSM_SMA_TIME_SOURCE"),
    ("PSM_SMA_NIM_ONLY_TOT = 1.0", "PSM_SMA_NIM_ONLY_TOT = -1.0", "PSM_SMA_NIM_ONLY_TOT"),
    ("PSM_SMA_NIM_ONLY_TOT = 1.0", "PSM_SMA_NIM_ONLY_TOT = 300", "PSM_SMA_NIM_ONLY_TOT"),
    ("PSM_SMA_NIM_ONLY_TOT = 1.0", "PSM_SMA_NIM_ONLY_TOT = '1'", "PSM_SMA_NIM_ONLY_TOT"),
    # at or below PSM_LAYER_THR (0.2) a NIM-only hit would fire no layer
    ("PSM_SMA_NIM_ONLY_TOT = 1.0", "PSM_SMA_NIM_ONLY_TOT = 0", "PSM_SMA_NIM_ONLY_TOT"),
    ("PSM_SMA_NIM_ONLY_TOT = 1.0", "PSM_SMA_NIM_ONLY_TOT = 0.2", "PSM_SMA_NIM_ONLY_TOT"),
    ("PSM_SMA_HITS_NTUPLE = True", "PSM_SMA_HITS_NTUPLE = 1", "PSM_SMA_HITS_NTUPLE"),
    ("PSM_SMA_OFFSET_OVERRIDE_NS = {}", "PSM_SMA_OFFSET_OVERRIDE_NS = {'2024': 5.0}",
     "PSM_SMA_OFFSET_OVERRIDE_NS"),
    ("PSM_SMA_OFFSET_OVERRIDE_NS = {}", "PSM_SMA_OFFSET_OVERRIDE_NS = {2024: '5'}",
     "PSM_SMA_OFFSET_OVERRIDE_NS"),
    ("PSM_SMA_OFFSET_OVERRIDE_NS = {}", "PSM_SMA_OFFSET_OVERRIDE_NS = [(2024, 5.0)]",
     "PSM_SMA_OFFSET_OVERRIDE_NS"),
])
def test_a_bad_sma_pairing_knob_is_rejected(job_env, old, new, match):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root", light=False)
    text = target.read_text()
    assert old in text
    with pytest.raises(SystemExit, match=match):
        _run(target, text.replace(old, new))


# -- the conditions source ----------------------------------------------------

def _containers(settings):
    return (list(settings["WD_CONDITIONS_FILES"]) + list(settings["PSM_GEOMETRY_FILES"])
            + [settings["PSM_CHANNEL_MAP_FILE"]])


def test_job_default_is_the_database_by_service_name():
    assert render.job_conditions(JOB.read_text()) == "db:service=pioneer-conditions"


def test_render_bakes_the_explicit_password_free_conninfo(tmp_path):
    _, text = _render(tmp_path, False)
    assert f'"conditions": "db:{CONNINFO}"' in text
    block = text[text.index("_RENDERED = {"):text.index("# Which of the two ways")]
    assert "service=" not in block and "password" not in block
    assert "hunter2" not in text


def test_render_bakes_json_as_an_absolute_resolved_directory(tmp_path):
    real = tmp_path / "snapshots" / "20260930T120000Z"
    real.mkdir(parents=True)
    (tmp_path / "snapshots" / "latest").symlink_to(real)
    _, text = _render(tmp_path, False, conditions=f"json:{tmp_path}/snapshots/latest")
    assert f'"conditions": "json:{real}"' in text
    _, text = _render(tmp_path, False, conditions="json")
    assert '"conditions": "json"' in text


def test_render_takes_nl_conditions_and_the_argument_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("NL_CONDITIONS", f"json:{tmp_path}")
    _, text = _render(tmp_path, False)
    assert f'"conditions": "json:{tmp_path}"' in text
    _, text = _render(tmp_path, False, conditions="db")
    assert f'"conditions": "db:{CONNINFO}"' in text


def test_render_with_an_undefined_service_raises_and_writes_nothing(tmp_path):
    with pytest.raises(ServiceNotFound, match="'nope'"):
        _render(tmp_path, False, conditions="db:nope")
    assert not (tmp_path / "out" / "run00790_00000.py").exists()


def test_render_rejects_an_unknown_source(tmp_path):
    with pytest.raises(ValueError, match="sqlite"):
        _render(tmp_path, False, conditions="sqlite:x.db")


def test_a_quoted_conninfo_survives_the_render(tmp_path, monkeypatch):
    spec = "db:host=h dbname=d options='-c search_path=cond' application_name='a\\'b'"
    target, text = _render(tmp_path, False, conditions=spec)
    compile(text, str(target), "exec")
    # the literal in the rendered file reads back as the resolved conninfo
    scope = {}
    block = text[text.index("_RENDERED = {"):text.index("# Which of the two ways")]
    exec(block, scope)
    assert scope["_RENDERED"]["conditions"] == "db:" + render.resolve_conninfo(spec[3:])


@pytest.mark.parametrize("spec", ["db", "db:", "db: ", "DB", "db:pioneer-conditions-admin",
                                  "db:service=x", "db: host=h port=1 dbname=d ",
                                  "json", "json:", "json:/a/b", "json:~/snap", " json : /x ",
                                  "postgres", "sqlite:x password=hunter2"])
def test_the_job_and_the_renderer_split_a_source_alike(job_env, spec):
    tmp_path, midas = job_env
    job = _run(render_job(midas, tmp_path / "run00790_00000.root", conditions="json"))

    def outcome(split):
        try:
            return split(spec)
        except ValueError as exc:
            assert "hunter2" not in str(exc)
            return ValueError
    assert outcome(job["_split_conditions"]) == outcome(render.split_conditions)


@pytest.mark.parametrize("spec", ["db:", "json:", "db:  "])
def test_a_colon_with_nothing_after_it_is_not_the_default(tmp_path, spec):
    with pytest.raises(ValueError, match="nothing after the colon"):
        _render(tmp_path, False, conditions=spec)


def test_the_job_describes_a_conninfo_as_pgservice_does(job_env):
    from pioneer.conddb.pgservice import describe, format_conninfo
    tmp_path, midas = job_env
    job = _run(render_job(midas, tmp_path / "run00790_00000.root", conditions="json"))
    for keys in ({"host": "h", "port": "5432", "dbname": "c", "user": "u"},
                 {"hostaddr": "10.0.0.1", "dbname": "c"}, {"dbname": "c"}):
        text = format_conninfo(keys)
        assert job["_describe_conninfo"](text) == describe(text)


def test_rendered_db_job_reads_only_the_database(job_env, capsys):
    tmp_path, midas = job_env
    job = _run(render_job(midas, tmp_path / "run00790_00000.root"))
    assert job["condSvc"].PgConnections == [CONNINFO]
    assert job["condSvc"].JsonFiles == []
    cond = Path(os.environ["NL_CONDITIONS_DIR"])
    assert job["condSvc"].OdbTables == [str(cond / f) for f in job["ODB_SPECS"]]
    out = capsys.readouterr().out
    assert "[nearline] conditions db host=pg.example port=5432 dbname=conditions\n" in out
    assert f"[nearline] odb specs  {cond}" in out
    assert "cond_viewer" not in out


def test_db_job_keeps_an_overrides_file_as_its_only_json(job_env):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root")
    (Path(os.environ["NL_CONDITIONS_DIR"]) / "ov.json").write_text("{}")
    text = target.read_text().replace('ODB_OVERRIDES = ""', 'ODB_OVERRIDES = "ov.json"')
    job = _run(target, text)
    assert job["condSvc"].JsonFiles == [str(Path(os.environ["NL_CONDITIONS_DIR"]) / "ov.json")]
    assert job["condSvc"].PgConnections == [CONNINFO]


_EMPTIED = (('WD_CONDITIONS_FILES = ["bt2026_wavedream_timebase.json", '
             '"bt2026_wavedream_calibration.json"]', "WD_CONDITIONS_FILES = []"),
            ('PSM_GEOMETRY_FILES = ["bt2026_psm_geometry.json", '
             '"bt2026_psm_readout_map.json"]', "PSM_GEOMETRY_FILES = []"))


def test_db_job_needs_no_container_files(job_env):
    # The container-file checks are json-mode only.
    tmp_path, midas = job_env
    cond = Path(os.environ["NL_CONDITIONS_DIR"])
    for name in _containers(_settings()):
        (cond / name).unlink()
    job = _run(render_job(midas, tmp_path / "run00790_00000.root"))
    assert job["condSvc"].JsonFiles == []
    with pytest.raises(SystemExit, match="conditions container does not exist"):
        _run(render_job(midas, tmp_path / "run00790_00000.root", conditions="json"))


def test_the_committed_container_settings_are_the_blocks(job_env):
    tmp_path, midas = job_env
    job = _run(render_job(midas, tmp_path / "run00790_00000.root"))
    for name, value in job["_JSON_ONLY_DEFAULTS"].items():
        assert _settings()[name] == value, name


def test_db_job_refuses_changed_container_settings(job_env):
    tmp_path, midas = job_env
    text = render_job(midas, tmp_path / "run00790_00000.root").read_text()
    for old, new in _EMPTIED:
        assert text.count(old) == 1
        text = text.replace(old, new)
    with pytest.raises(SystemExit, match="WD_CONDITIONS_FILES, PSM_GEOMETRY_FILES differ") as err:
        _run(tmp_path / "x.py", text)
    assert "--conditions json:DIR" in str(err.value)
    # and json mode still says what is wrong with them
    with pytest.raises(SystemExit, match="PSM_GEOMETRY_FILES is empty"):
        _run(tmp_path / "x.py", text.replace(f'"db:{CONNINFO}"', '"json"'))


def test_db_job_refuses_changed_container_settings_from_an_overrides_file(job_env, monkeypatch):
    tmp_path, midas = job_env
    overrides = tmp_path / "ov.py"
    overrides.write_text('PSM_CHANNEL_MAP_FILE = "/scratch/my_channel_map.json"\n')
    monkeypatch.setenv("NL_MIDAS", str(midas))
    monkeypatch.setenv("NL_OUT", str(tmp_path / "run00790_00000.root"))
    monkeypatch.setenv("NL_OVERRIDES", str(overrides))
    with pytest.raises(SystemExit, match="PSM_CHANNEL_MAP_FILE differ"):
        _run(JOB)
    monkeypatch.setenv("NL_CONDITIONS", "json")
    overrides.write_text("")
    _run(JOB)


def test_db_job_refuses_a_conditions_dir_holding_its_own_containers(job_env, monkeypatch):
    tmp_path, midas = job_env
    copy = tmp_path / "mask-trial"
    (copy / "odb").mkdir(parents=True)
    cond = Path(os.environ["NL_CONDITIONS_DIR"])
    for name in _containers(_settings()) + list(_settings()["ODB_SPECS"]):
        (copy / name).write_text("{}")
    monkeypatch.setenv("NL_CONDITIONS_DIR", str(copy))
    target = render_job(midas, tmp_path / "run00790_00000.root")
    with pytest.raises(SystemExit, match="mask-trial holds bt2026_") as err:
        _run(target)
    assert "reco_testbeam/conditions checkout" in str(err.value)
    # json mode reads them, and a directory with odb/ only is fine for db mode
    _run(render_job(midas, tmp_path / "run00790_00000.root", conditions="json"))
    for name in _containers(_settings()):
        (copy / name).unlink()
    _run(render_job(midas, tmp_path / "run00790_00000.root"))
    assert cond.is_dir()


def test_a_bad_conninfo_is_reported_without_echoing_it(job_env, monkeypatch):
    tmp_path, midas = job_env
    monkeypatch.setenv("NL_MIDAS", str(midas))
    monkeypatch.setenv("NL_OUT", str(tmp_path / "run00790_00000.root"))
    for spec in ("db:host=h password=hunter2 'oops", "db:dbname=c password=hunter2",
                 "db:host=h password=hunter2"):
        monkeypatch.setenv("NL_CONDITIONS", spec)
        with pytest.raises(SystemExit) as err:
            _run(JOB)
        assert "hunter2" not in str(err.value) and "CONDITIONS names a database" in str(err.value)


def test_rendered_json_job_is_the_old_job(job_env, capsys):
    tmp_path, midas = job_env
    cond = Path(os.environ["NL_CONDITIONS_DIR"])
    job = _run(render_job(midas, tmp_path / "run00790_00000.root", conditions="json"))
    settings = _settings()
    assert job["condSvc"].JsonFiles == [str(cond / f) for f in _containers(settings)]
    assert "PgConnections" not in job["condSvc"].__dict__
    assert f"[nearline] conditions json {cond}\n" in capsys.readouterr().out


def test_json_dir_moves_the_containers_but_not_the_odb_specs(job_env, tmp_path):
    _, midas = job_env
    snap = tmp_path / "snap"
    snap.mkdir()
    for name in _containers(_settings()):
        (snap / name).write_text("{}")
    job = _run(render_job(midas, tmp_path / "run00790_00000.root", conditions=f"json:{snap}"))
    cond = Path(os.environ["NL_CONDITIONS_DIR"])
    assert job["condSvc"].JsonFiles == [str(snap / f) for f in _containers(_settings())]
    assert job["condSvc"].OdbTables == [str(cond / f) for f in job["ODB_SPECS"]]


def test_rendered_job_ignores_nl_conditions(job_env, monkeypatch):
    tmp_path, midas = job_env
    target = render_job(midas, tmp_path / "run00790_00000.root")
    monkeypatch.setenv("NL_CONDITIONS", "json")
    assert _run(target)["condSvc"].PgConnections == [CONNINFO]


def test_unrendered_job_expands_the_service_and_takes_nl_conditions(job_env, monkeypatch):
    tmp_path, midas = job_env
    monkeypatch.setenv("NL_MIDAS", str(midas))
    monkeypatch.setenv("NL_OUT", str(tmp_path / "run00790_00000.root"))
    job = _run(JOB)
    assert job["RENDERED"] is False and job["condSvc"].PgConnections == [CONNINFO]
    monkeypatch.setenv("NL_CONDITIONS", "json")
    assert "PgConnections" not in _run(JOB)["condSvc"].__dict__


def test_unrendered_job_with_an_undefined_service_points_at_the_snapshot(job_env, monkeypatch):
    tmp_path, midas = job_env
    monkeypatch.setenv("NL_MIDAS", str(midas))
    monkeypatch.setenv("NL_OUT", str(tmp_path / "run00790_00000.root"))
    monkeypatch.setenv("NL_CONDITIONS", "db:nope")
    with pytest.raises(SystemExit) as err:
        _run(JOB)
    assert "'nope'" in str(err.value) and "--conditions json:" in str(err.value)
    assert "Conditions DB down" in str(err.value)


def test_a_bad_source_is_rejected_by_check(job_env, monkeypatch):
    tmp_path, midas = job_env
    monkeypatch.setenv("NL_MIDAS", str(midas))
    monkeypatch.setenv("NL_OUT", str(tmp_path / "run00790_00000.root"))
    monkeypatch.setenv("NL_CONDITIONS", "postgres")
    with pytest.raises(SystemExit, match="CONDITIONS starts with 'postgres'"):
        _run(JOB)


def test_process_takes_conditions(tmp_path, capsys):
    from pioneer.nearline import process
    midas = tmp_path / "run00790_00000.mid.lz4"
    midas.write_bytes(b"")
    out = tmp_path / "out"
    assert process.main([str(midas), "--out-dir", str(out), "--render-only",
                         "--conditions", f"json:{tmp_path}"]) == 0
    assert f'"conditions": "json:{tmp_path}"' in (out / "run00790_00000.py").read_text()
    assert process.main([str(midas), "--out-dir", str(out), "--render-only"]) == 0
    assert f'"conditions": "db:{CONNINFO}"' in (out / "run00790_00000.py").read_text()


def test_process_with_an_undefined_service_says_what_to_do(tmp_path, capsys):
    from pioneer.nearline import process
    midas = tmp_path / "run00790_00000.mid.lz4"
    midas.write_bytes(b"")
    assert process.main([str(midas), "--out-dir", str(tmp_path / "o"), "--render-only",
                         "--conditions", "db:nope"]) == 2
    out = capsys.readouterr().out
    assert "'nope'" in out and "--conditions json:~/bt2026/conddb-snapshots/latest" in out
    assert not (tmp_path / "o" / "run00790_00000.py").exists()


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


def test_daemon_announces_the_database(daemon_module, monkeypatch):
    msg, bad = daemon_module.conditions_announcement(environ={})
    assert bad is False
    assert msg == ("Nearline daemon: conditions from the database host=pg.example port=5432 "
                   "dbname=conditions (the job's default)")


def test_daemon_warns_loudly_about_json(daemon_module, tmp_path):
    msg, bad = daemon_module.conditions_announcement(environ={"NL_CONDITIONS": f"json:{tmp_path}"})
    assert bad is True and "NOT from the database" in msg and str(tmp_path) in msg
    assert "NL_CONDITIONS in the daemon's environment" in msg


def test_daemon_says_up_front_when_the_source_does_not_resolve(daemon_module):
    msg, bad = daemon_module.conditions_announcement(environ={"NL_CONDITIONS": "db:nope"})
    assert bad is True and "every nearline job will fail to start" in msg and "'nope'" in msg


def test_a_job_that_fails_to_start_is_marked_failed(daemon_module, monkeypatch, tmp_path):
    class BrokenJob:
        def start(self):
            raise ValueError("libpq service 'pioneer-conditions' is not defined")

    class Db:
        def __init__(self):
            self.status = []

        def update_status(self, table, job_id, status):
            self.status.append((table, job_id, status))

    messages = []
    monkeypatch.setattr(daemon_module.nl_jobs, "create_job", lambda cfg, db: BrokenJob())
    d = object.__new__(daemon_module.NearlineDaemon)
    d.light = False
    d.midas_logger_path = d.backup_path = d.remote_path = tmp_path
    d.nearline_output_path = tmp_path
    d.db_interface = Db()
    d.message = lambda msg, is_error=False, send_to_slack=False: messages.append((msg, is_error))
    queue = daemon_module.NearlineQueue("nearline", 1)
    d.dispatch_job(queue, {"midas_run_number": 790, "job_id": 12, "job_type": "nearline"})
    assert d.db_interface.status == [("postproc_job", 12, "FAILED")]
    assert queue.active == []
    assert messages == [("Job 12 failed to start: libpq service 'pioneer-conditions' is not "
                         "defined", True)]
