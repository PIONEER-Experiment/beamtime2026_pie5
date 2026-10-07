"""The daemon's logger-file bookkeeping: the Current filename watch and the
stop transition.  No MIDAS, no Postgres."""

import sys
import types

import pytest

from tuning_fakes import FakeDb, FakeOdb

CHANNEL = "/Logger/Channels/0"
FILENAME = CHANNEL + "/Settings/Current filename"


@pytest.fixture
def daemon_module(monkeypatch):
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


class AlarmOdb(FakeOdb):
    def __init__(self, values=None):
        super().__init__(values)
        self.alarms = []

    def trigger_internal_alarm(self, name, text):
        self.alarms.append((name, text))


def running(daemon_module, run_number=2788):
    """A daemon with run `run_number` open and one subrun file written."""
    db = FakeDb()
    run_id = db.add_run(run_number, status="RUNNING")
    odb = AlarmOdb({
        "/Runinfo/Run number": run_number,
        "/Runinfo/Stop time": "Wed Oct  7 08:57:54 2026",
        "/Nearline/Info/Run DB PK": run_id,
        "/Nearline/Info/Quality": "iter",
        CHANNEL + "/Statistics/Events written": 14323,
        FILENAME: "",
    })
    d = object.__new__(daemon_module.NearlineDaemon)
    d.client = odb
    d.db_interface = db
    d.tuning = None
    new_file(d, odb, "run%05d_00014.mid.lz4" % run_number)
    return d, db, odb, run_id


def new_file(d, odb, name):
    """mlogger opens `name` and the watch notification is handled."""
    odb.values[FILENAME] = name
    d.filename_change_callback(odb, FILENAME, name)


def logger_files(db):
    return {f["filebase"]: (f["run_id"], f["status"]) for f in db.files}


def test_run_number_of(daemon_module):
    run_number_of = daemon_module.run_number_of
    assert run_number_of("run02788_00015.mid.lz4") == 2788
    assert run_number_of("/home/pinky/online/run00042.mid") == 42
    assert run_number_of("") is None and run_number_of("debug.mid") is None


def test_a_subrun_closes_the_previous_file(daemon_module):
    d, db, odb, run_id = running(daemon_module)
    new_file(d, odb, "run02788_00015.mid.lz4")
    assert logger_files(db) == {"run02788_00014": (run_id, "DONE"),
                                "run02788_00015": (run_id, "RUNNING")}
    assert [j["job_type"] for j in db.jobs] == ["nearline"]
    assert odb.alarms == []


def test_a_notification_handled_after_the_stop_is_not_an_alarm(daemon_module):
    # mlogger opened the last subrun, but the daemon was busy and handled
    # the stop transition before the watch notification
    d, db, odb, run_id = running(daemon_module)
    odb.values[FILENAME] = "run02788_00015.mid.lz4"
    assert d.end_of_run_callback(odb, 2788) == 1
    assert logger_files(db) == {"run02788_00014": (run_id, "DONE"),
                                "run02788_00015": (run_id, "DONE")}
    assert odb.values["/Nearline/Info/Run DB PK"] == 0

    d.filename_change_callback(odb, FILENAME, "run02788_00015.mid.lz4")
    assert odb.alarms == []
    assert db.runs[run_id]["status"] == "DONE"
    assert len(db.runs) == 1 and len(db.files) == 2
    assert [j["job_type"] for j in db.jobs] == ["nearline", "nearline"]


def test_the_stop_ignores_a_file_of_another_run(daemon_module):
    # the logger wrote nothing this run: Current filename is still the last run's
    d, db, odb, run_id = running(daemon_module)
    odb.values[FILENAME] = "run02787_00003.mid.lz4"
    d.end_of_run_callback(odb, 2788)
    assert "run02787_00003" not in logger_files(db)


def test_an_unregistered_file_after_the_stop_goes_to_its_run(daemon_module):
    d, db, odb, run_id = running(daemon_module)
    d.end_of_run_callback(odb, 2788)
    d.filename_change_callback(odb, FILENAME, "run02788_00016.mid.lz4")
    assert odb.alarms == [] and len(db.runs) == 1
    assert db.runs[run_id]["status"] == "DONE"
    assert logger_files(db)["run02788_00016"] == (run_id, "DONE")
    assert any(is_error for _, is_error in odb.messages)


def test_a_wrong_primary_key_still_raises_the_alarm(daemon_module):
    d, db, odb, run_id = running(daemon_module)
    other = db.add_run(2700, status="DONE")
    odb.values["/Nearline/Info/Run DB PK"] = other
    odb.values["/Nearline/Info/Operator"] = ""
    odb.values["/Nearline/Info/Description"] = ""
    db.register_run = lambda **kw: db.add_run(None, status=kw["status"])
    new_file(d, odb, "run02788_00015.mid.lz4")
    assert [name for name, _ in odb.alarms] == ["RunDB Corrupted"]
