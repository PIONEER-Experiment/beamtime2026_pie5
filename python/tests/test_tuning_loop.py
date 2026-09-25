"""The tuning loop (pioneer.nearline.tuning) and the daemon's use of it.

Runs without MIDAS, without a database and without network: the run
database, the ODB and the service are the fakes in tuning_fakes.py.
"""

import sys
import types

import pytest

from tuning_fakes import FakeDb, FakeOdb

from pioneer.nearline import tuning


class FakeMt:
    """A miniTwinInterface that hands out prepared configs once."""

    def __init__(self, configs=None, add_raises=None):
        self.configs = list(configs or [])
        self.added = []
        self.add_raises = add_raises

    def NextConfiguration(self):                       # noqa: N802
        configs, self.configs = self.configs, []
        return configs

    def AddContextFiles(self, *args, **kwargs):        # noqa: N802
        if self.add_raises:
            raise self.add_raises
        self.added.append((args, kwargs))
        return {"context_id": "fake"}


def iter_config(**currents):
    return {"type": "iter", "currents": [dict(currents or {"ASM12:SOL:2": 90.44})]}


def make_loop(db=None, mt=None, odb=None):
    db = db or FakeDb()
    odb = odb if odb is not None else FakeOdb({"/Nearline/config/MiniTwin updates": "pim1_epics"})
    messages = []
    loop = tuning.TuningLoop(db=db, mt=mt or FakeMt(), odb=odb,
                             message=lambda m, is_error=False: messages.append((m, is_error)))
    return loop, db, odb, messages


# -- A1: one central run per proposal, no merge -------------------------------

def test_iter_schedules_one_run_at_the_centre():
    loop, db, odb, _ = make_loop(mt=FakeMt([iter_config()]))
    tuning.ensure_odb_keys(odb)
    scheduled = loop.poll_and_schedule()

    assert len(scheduled) == 1
    assert len(db.runs) == 1
    (run_id, run), = db.runs.items()
    assert run["requested_events"] == 1000000
    # the pim1 row was written as a new config, the centre was reused by id
    assert db.written == [("pim1_epics", {"ASM12:SOL:2": 90.44})]
    assert sorted(run["configs"]) == [2, 100]
    # the sequence around the run posts without a merge; the wrapper has no
    # on_complete and so goes straight to DONE in the run database
    on_completes = sorted((s["on_complete"] or "") for s in db.sequences.values())
    assert on_completes == ["", "mt_add"]
    mt_add = [s for s in db.sequences.values() if s["on_complete"] == "mt_add"]
    assert mt_add[0]["runs"] == [run_id]


def test_target_config_comes_from_the_odb():
    odb = FakeOdb({"/Nearline/config/MiniTwin updates": "pim1_epics",
                   "/Nearline/config/MiniTwin target config": 3})
    loop, db, _, _ = make_loop(mt=FakeMt([iter_config()]), odb=odb)
    loop.poll_and_schedule()
    (run,) = db.runs.values()
    assert sorted(run["configs"]) == [3, 100]


def test_missing_target_config_schedules_nothing():
    odb = FakeOdb({"/Nearline/config/MiniTwin target config": 99})
    loop, db, _, _ = make_loop(mt=FakeMt([iter_config()]), odb=odb)
    with pytest.raises(RuntimeError, match="99"):
        loop.poll_and_schedule()
    assert db.runs == {} and db.sequences == {}


def test_dry_run_writes_nothing():
    loop, db, _, _ = make_loop(mt=FakeMt([iter_config()]))
    scheduled = loop.poll_and_schedule(dry_run=True)
    assert db.runs == {} and db.sequences == {} and db.written == []
    assert scheduled[0]["target_position"]["id"] == 2
    assert scheduled[0]["on_complete"] == "mt_add"


def test_final_path_is_unchanged():
    loop, db, _, _ = make_loop(mt=FakeMt([{"type": "final", "currents": [{"ASM12:SOL:2": 90.0}]}]))
    loop.poll_and_schedule()
    # five positions x one degrader position, merged
    assert len(db.runs) == 5
    assert all(r["requested_events"] == 10000000 for r in db.runs.values())
    assert "merge" in [s["on_complete"] for s in db.sequences.values()]
    assert "mt_add" not in [s["on_complete"] for s in db.sequences.values()]


def test_ensure_odb_keys_keeps_existing_values():
    odb = FakeOdb({"/Nearline/config/MiniTwin target config": 4})
    tuning.ensure_odb_keys(odb)
    assert odb.values["/Nearline/config/MiniTwin target config"] == 4
    odb = FakeOdb()
    tuning.ensure_odb_keys(odb)
    assert odb.values["/Nearline/config/MiniTwin target config"] == 2


def test_post_sequence_marks_done():
    db = FakeDb()
    db.add_run(604, subruns=2, seq_id=57)
    loop, _, _, messages = make_loop(db=db)
    loop.post_sequence(57)
    assert db.sequences[57]["status"] == "DONE"
    assert not [m for m in messages if m[1]]


def test_post_sequence_marks_failed_with_a_message():
    db = FakeDb()
    db.add_run(604, subruns=2, seq_id=57)
    loop, _, _, messages = make_loop(db=db, mt=FakeMt(add_raises=ValueError("no header")))
    assert loop.post_sequence(57) is None
    assert db.sequences[57]["status"] == "FAILED"
    errors = [m for m, is_error in messages if is_error]
    assert errors and "no header" in errors[0] and "57" in errors[0]


# -- the daemon, with midas faked ----------------------------------------------

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


def bare_daemon(daemon_module, loop, db, odb):
    d = object.__new__(daemon_module.NearlineDaemon)
    d.client = odb
    d.db_interface = db
    d.mt_interface = loop.mt
    d.tuning = loop
    d.minitwin_enabled = True
    d.minitwin_update_table = "pim1_epics"
    return d


def test_daemon_mt_add_branch_posts_and_closes_the_sequence(daemon_module):
    db = FakeDb()
    db.add_run(604, subruns=1, seq_id=57)
    loop, _, odb, _ = make_loop(db=db)
    d = bare_daemon(daemon_module, loop, db, odb)
    d.build_and_dispatch_seq({"id": 57, "on_complete": "mt_add", "status": "CLAIMED"})
    assert db.sequences[57]["status"] == "DONE"
    assert len(loop.mt.added) == 1


def test_daemon_check_for_updates_schedules_the_centre(daemon_module):
    loop, db, odb, _ = make_loop(mt=FakeMt([iter_config()]))
    d = bare_daemon(daemon_module, loop, db, odb)
    d.check_for_updates()
    assert len(db.runs) == 1
    assert "mt_add" in [s["on_complete"] for s in db.sequences.values()]
