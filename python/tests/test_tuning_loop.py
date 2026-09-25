"""The tuning loop (pioneer.nearline.tuning) and the daemon's use of it.

Runs without MIDAS, without a database and without network: the run
database, the ODB and the service are the fakes in tuning_fakes.py.
"""

import sys
import types

import pytest

from pathlib import Path

from tuning_fakes import FakeDb, FakeHttp, FakeOdb

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


#: a beam header as tuning.read_beamline_header returns it: two knobs, a
#: type-2 device that is not one, and a read-only type-3 channel
HEADER = {
    "names": ["ASM12:SOL:2", "QTB12", "KSD11", "BEAM:CURRENT"],
    "demand": [90.44, 56.12, 1.0, 0.0],
    "measured": [89.40, 55.22, 1.0, 2.2],
    "types": [1, 4, 2, 3],
}


class HeaderReader:
    def __init__(self, header=HEADER):
        self.header = header
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        return self.header


def make_loop(db=None, mt=None, odb=None, header_reader=None):
    db = db or FakeDb()
    odb = odb if odb is not None else FakeOdb({"/Nearline/config/MiniTwin updates": "pim1_epics"})
    messages = []
    loop = tuning.TuningLoop(db=db, mt=mt or FakeMt(), odb=odb,
                             message=lambda m, is_error=False: messages.append((m, is_error)),
                             header_reader=header_reader or HeaderReader())
    return loop, db, odb, messages


def real_mt(http=None):
    from pioneer.nearline.miniTwinInterface import miniTwinInterface
    mt = miniTwinInterface(config_type="pim1_epics", logger=lambda m: None)
    mt.client = http or FakeHttp()
    return mt


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


# -- A2: a context made of file paths -----------------------------------------

DATA = Path(__file__).resolve().parents[4] / "runplan-loop-poc" / "data"


@pytest.mark.skipif(not (DATA / "run00588").is_dir(), reason="run00588 test files not present")
def test_context_from_the_real_run588_files():
    db = FakeDb()
    run_id = db.add_run(588, subruns=3)
    reader = HeaderReader()
    odb = FakeOdb({"/Nearline/config/MiniTwin updates": "pim1_epics",
                   "/Nearline/config/Output path": str(DATA),
                   "/Nearline/config/MiniTwin local prefix": str(DATA) + "/"})
    loop, _, _, _ = make_loop(db=db, odb=odb, header_reader=reader, mt=real_mt())
    context = loop.build_context([run_id])

    # the header comes from the first subrun's file on this machine
    assert reader.paths == [str(DATA / "run00588" / "run00588_00000_hists.root")]
    assert Path(reader.paths[0]).is_file()
    for i in range(3):
        assert (DATA / "run00588" / ("run00588_%05d_hists.root" % i)).is_file()

    assert context["context_id"] == "run00588"
    assert context["measurement"]["files"] == [
        {"path": "/home/pioneer/nearline/histograms/run00588/run00588_%05d_hists.root" % i,
         "role": "hist_root"} for i in range(3)]
    assert context["measurement"]["kind"] == "psm_nearline"
    assert context["measurement"]["valid"] is True
    assert "inline" not in context["measurement"]


def test_context_shape_matches_the_contract():
    db = FakeDb()
    run_id = db.add_run(604, subruns=2)
    loop, _, _, _ = make_loop(db=db, mt=real_mt())
    context = loop.build_context([run_id])
    assert context["schema"] == "beamtune.context/v1"
    assert context["context_id"] == "run00604"
    # default prefixes: pinky's output tree -> piana's mirror
    assert context["measurement"]["files"][0]["path"] == \
        "/home/pioneer/nearline/histograms/run00604/run00604_00000_hists.root"
    assert context["setting"] == {
        "units": "A",
        "knobs": {"ASM12:SOL:2": 90.44, "QTB12": 56.12},
        "readback": {"ASM12:SOL:2": 89.40, "QTB12": 55.22},
    }
    assert context["provenance"] == {"run_ids": [604], "config_type": "pim1_epics",
                                     "source": "nearline-daemon"}
    # no step known: no responds_to at all
    assert "responds_to" not in context


def test_context_carries_the_step_when_given():
    db = FakeDb()
    run_id = db.add_run(604, subruns=1)
    loop, _, _, _ = make_loop(db=db, mt=real_mt())
    step = {"proposal_id": 5, "step_id": "ASM12_90.44", "attempt": 0,
            "plan": "quick_run00588_ASM12", "seq_id": 57}
    context = loop.build_context([run_id], step=step)
    assert context["responds_to"] == {"proposal_id": 5}
    assert context["provenance"]["step_id"] == "ASM12_90.44"
    assert context["provenance"]["attempt"] == 0
    assert context["provenance"]["plan"] == "quick_run00588_ASM12"


def test_failed_subruns_are_left_out():
    db = FakeDb()
    run_id = db.add_run(604, subruns=2)
    db.files[1]["status"] = "FAILED"
    loop, _, _, messages = make_loop(db=db, mt=real_mt())
    context = loop.build_context([run_id])
    assert len(context["measurement"]["files"]) == 1
    assert any("FAILED" in m for m, _ in messages)


def test_no_files_is_an_error():
    db = FakeDb()
    run_id = db.add_run(604, subruns=0)
    loop, _, _, _ = make_loop(db=db, mt=real_mt())
    with pytest.raises(RuntimeError, match="no finished"):
        loop.build_context([run_id])


def test_a_header_without_knobs_is_an_error():
    db = FakeDb()
    run_id = db.add_run(604, subruns=1)
    reader = HeaderReader({"names": ["X"], "demand": [1.0], "measured": [1.0], "types": [3]})
    loop, _, _, _ = make_loop(db=db, mt=real_mt(), header_reader=reader)
    with pytest.raises(ValueError, match="no knobs"):
        loop.build_context([run_id])


def test_path_outside_the_local_prefix_is_posted_unchanged():
    assert tuning.remote_path("/data/run00604/x_hists.root", "/home/pinky/nearline/",
                              "/home/pioneer/nearline/histograms/") is None
    assert tuning.remote_path("/home/pinky/nearline/run00604/x_hists.root", "/home/pinky/nearline",
                              "/remote") == "/remote/run00604/x_hists.root"


def test_post_sequence_delivers_the_context():
    http = FakeHttp()
    db = FakeDb()
    db.add_run(604, subruns=3, seq_id=57)
    loop, _, _, _ = make_loop(db=db, mt=real_mt(http))
    loop.post_sequence(57)
    assert [c["context_id"] for c in http.contexts] == ["run00604"]
    assert len(http.contexts[0]["measurement"]["files"]) == 3
    assert db.sequences[57]["status"] == "DONE"


def test_post_sequence_with_the_service_down_queues_the_context():
    http = FakeHttp(fail=True)
    db = FakeDb()
    db.add_run(604, subruns=1, seq_id=57)
    mt = real_mt(http)
    loop, _, _, _ = make_loop(db=db, mt=mt)
    loop.post_sequence(57)
    assert mt.pending == 1
    assert db.sequences[57]["status"] == "DONE"
    http.fail = False
    mt._muted_until = 0.0
    mt._flush()
    assert mt.pending == 0 and len(http.contexts) == 1


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
