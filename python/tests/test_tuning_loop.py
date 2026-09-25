"""The tuning loop (pioneer.nearline.tuning) and the daemon's use of it.

Runs without MIDAS, without a database and without network: the run
database, the ODB and the service are the fakes in tuning_fakes.py.
"""

import sys
import types

import pytest

from pathlib import Path

from tuning_fakes import FakeDb, FakeHttp, FakeOdb, proposal

from pioneer.nearline import tuning


class FakeMt:
    """A miniTwinInterface that hands out prepared configs once."""

    def __init__(self, configs=None, add_raises=None):
        self.configs = list(configs or [])
        self.added = []
        self.add_raises = add_raises
        self.last_proposal_id = 0
        self.last_run_hints = None

    def NextConfiguration(self):                       # noqa: N802
        configs, self.configs = self.configs, []
        return configs

    def BuildContextFiles(self, context_id, *args, **kwargs):  # noqa: N802
        if self.add_raises:
            raise self.add_raises
        return {"context_id": context_id}

    def Enqueue(self, context):                        # noqa: N802
        self.added.append(context)
        if self.on_delivered:
            self.on_delivered(context)
        return True

    pending = 0
    on_delivered = None
    on_rejected = None


def iter_config(**currents):
    return {"type": "iter", "currents": [dict(currents or {"ASM12:SOL:2": 90.44})]}


#: a beam header as tuning.read_beamline_header returns it, with real channel
#: names: two knobs (QTB12 given type 4 to cover that type), the type-2 KSD11
#: that is not one, and the read-only type-6 MHC3
HEADER = {
    "names": ["ASM12", "QTB12", "KSD11", "MHC3"],
    "demand": [90.44, 56.12, 1.0, 0.0],
    "measured": [89.40, 55.22, 1.0, 2142.3],
    "types": [1, 4, 2, 6],
}


class HeaderReader:
    def __init__(self, header=HEADER):
        self.header = header
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        return self.header


def make_loop(db=None, mt=None, odb=None, header_reader=None, maps_reader=None):
    db = db or FakeDb()
    odb = odb if odb is not None else FakeOdb({"/Nearline/config/MiniTwin updates": "pim1_epics"})
    messages = []
    loop = tuning.TuningLoop(db=db, mt=mt or FakeMt(), odb=odb,
                             message=lambda m, is_error=False: messages.append((m, is_error)),
                             header_reader=header_reader or HeaderReader(),
                             maps_reader=maps_reader or (lambda paths: None))
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
    assert sorted(run["configs"]) == [1, 100]
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
    with pytest.raises(tuning.ScheduleError, match="99"):
        loop.poll_and_schedule()
    assert db.runs == {} and db.sequences == {}


def test_dry_run_writes_nothing():
    loop, db, _, _ = make_loop(mt=FakeMt([iter_config()]))
    scheduled = loop.poll_and_schedule(dry_run=True)
    assert db.runs == {} and db.sequences == {} and db.written == []
    assert scheduled[0]["target_position"]["id"] == 1
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
    assert odb.values["/Nearline/config/MiniTwin target config"] == 1


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
        "knobs": {"ASM12": 90.44, "QTB12": 56.12},
        "readback": {"ASM12": 89.40, "QTB12": 55.22},
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
    # not DONE until the service took it
    assert db.sequences[57]["status"] == "CLAIMED"
    http.fail = False
    mt._muted_until = 0.0
    mt._flush()
    assert mt.pending == 0 and len(http.contexts) == 1
    assert db.sequences[57]["status"] == "DONE"


# -- A3: persistence and the pause switch ------------------------------------

def loop_with_service(proposals, odb=None, db=None):
    http = FakeHttp(proposals)
    odb = odb or FakeOdb({"/Nearline/config/MiniTwin updates": "pim1_epics",
                          "/Nearline/config/MiniTwin enable": True})
    tuning.ensure_odb_keys(odb)
    loop, db, odb, messages = make_loop(db=db, mt=real_mt(http), odb=odb)
    loop.restore()
    return loop, db, odb, http, messages


def test_schedule_keeps_the_sequence_id():
    import pioneer.nearline.run as nl_run
    db = FakeDb()
    mrs = nl_run.midas_run_sequence(db, num_ev=1e6)
    mrs.set_config_id("target_position", 1)
    mrs.schedule()
    assert mrs.seq_id in db.sequences


def test_scheduling_stores_proposal_and_step_in_the_odb():
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    scheduled = loop.poll_and_schedule()
    seq_id = scheduled[0]["seq_id"]
    assert db.sequences[seq_id]["on_complete"] == "mt_add"
    v = odb.values
    assert v["/Nearline/MiniTwin/Last proposal id"] == 5
    assert v["/Nearline/MiniTwin/Active step/Proposal id"] == 5
    assert v["/Nearline/MiniTwin/Active step/Step id"] == "ASM12_90.44"
    assert v["/Nearline/MiniTwin/Active step/Attempt"] == 0
    assert v["/Nearline/MiniTwin/Active step/Plan"] == "quick_run00588_ASM12"
    assert v["/Nearline/MiniTwin/Active step/Seq id"] == seq_id


def test_a_restart_does_not_schedule_the_outstanding_proposal_again():
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    loop.poll_and_schedule()
    assert len(db.runs) == 1

    # a new daemon on the same ODB and run database, service unchanged
    loop2, _, _, http2, _ = loop_with_service([proposal(5)], odb=odb, db=db)
    assert loop2.mt.last_proposal_id == 5
    assert loop2.active["proposal_id"] == 5
    assert loop2.poll_and_schedule() == []
    assert http2.since == [5]
    assert len(db.runs) == 1


def test_restored_step_is_posted_with_the_context_and_then_cleared():
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    seq_id = loop.poll_and_schedule()[0]["seq_id"]
    (run_id,) = db.sequences[seq_id]["runs"]
    db.runs[run_id]["midas_run_number"] = 604
    db.files.append({"run_id": run_id, "filebase": "run00604_00000", "fileext": "root",
                     "status": "DONE"})

    loop2, _, _, http2, _ = loop_with_service([], odb=odb, db=db)
    loop2.post_sequence(seq_id)
    (context,) = http2.contexts
    assert context["responds_to"] == {"proposal_id": 5}
    assert context["provenance"]["step_id"] == "ASM12_90.44"
    assert loop2.active is None
    assert odb.values["/Nearline/MiniTwin/Active step/Proposal id"] == 0
    assert odb.values["/Nearline/MiniTwin/Last proposal id"] == 5


def test_another_sequence_is_posted_without_the_step():
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    loop.poll_and_schedule()
    db.add_run(700, subruns=1, seq_id=999)
    loop.post_sequence(999)
    assert "responds_to" not in http.contexts[-1]
    assert "step_id" not in http.contexts[-1]["provenance"]
    assert loop.active["proposal_id"] == 5


def test_a_failed_schedule_still_consumes_the_proposal():
    odb = FakeOdb({"/Nearline/config/MiniTwin updates": "pim1_epics",
                   "/Nearline/config/MiniTwin target config": 99})
    loop, db, odb, http, _ = loop_with_service([proposal(5)], odb=odb)
    with pytest.raises(RuntimeError):
        loop.poll_and_schedule()
    assert odb.values["/Nearline/MiniTwin/Last proposal id"] == 5
    assert loop.active is None


def test_dry_run_stores_nothing():
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    loop.poll_and_schedule(dry_run=True)
    assert odb.values["/Nearline/MiniTwin/Last proposal id"] == 0
    assert loop.active is None and db.runs == {}


def test_enable_is_re_read_every_time():
    loop, db, odb, http, messages = loop_with_service([])
    assert loop.refresh_enable() is True
    odb.values["/Nearline/config/MiniTwin enable"] = False
    assert loop.refresh_enable() is False
    assert loop.refresh_enable() is False
    odb.values["/Nearline/config/MiniTwin enable"] = True
    assert loop.refresh_enable() is True
    texts = [m for m, _ in messages if "paused" in m or "resumed" in m]
    assert len(texts) == 2


# -- A4: DAQ progress reports -------------------------------------------------

class Clock:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def monitored_loop(proposals=None):
    loop, db, odb, http, messages = loop_with_service(proposals or [proposal(5)])
    loop.clock = Clock()
    loop.refresh_enable()
    return loop, db, odb, http, messages


def test_post_daq_goes_to_v1_daq(monkeypatch):
    import json
    import urllib.request
    from pioneer.nearline import beamtune_client

    seen = {}

    class Response:
        status = 200

        def read(self):
            return b'{"accepted": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["body"] = json.loads(request.data.decode())
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = beamtune_client.BeamTuneClient("http://127.0.0.1:1122")
    report = tuning.daq_report(5, "running", step={"step_id": "ASM12_90.44"}, seq_id=57)
    assert client.post_daq(report) == {"accepted": True}
    assert seen["url"] == "http://127.0.0.1:1122/v1/daq"
    assert seen["method"] == "POST"
    assert seen["body"]["schema"] == "beamtune.daq/v1"
    assert set(seen["body"]) == {"schema", "proposal_id", "step_id", "stage", "seq_id", "runs",
                                 "events", "subruns", "message", "sent_utc"}


def test_post_daq_failure_never_raises_and_trips_the_breaker():
    mt = real_mt(FakeHttp(fail=True))
    report = tuning.daq_report(5, "running")
    assert [mt.PostDaq(report) for _ in range(3)] == [False, False, False]
    assert mt.daq_muted
    # DAQ reports have their own breaker: contexts and proposals go on
    assert not mt.muted


def test_progress_through_the_stages():
    loop, db, odb, http, _ = monitored_loop()
    seq_id = loop.poll_and_schedule()[0]["seq_id"]
    (run_id,) = db.sequences[seq_id]["runs"]

    # scheduling reports at once
    (first,) = http.daq
    assert first["stage"] == "scheduled" and first["proposal_id"] == 5
    assert first["step_id"] == "ASM12_90.44" and first["seq_id"] == seq_id
    assert first["runs"] == [{"run_db_id": run_id, "run_number": None, "status": "PENDING"}]
    assert first["sent_utc"].endswith("Z")

    # nothing changed: nothing sent, and not looked at before 10 s anyway
    loop.clock.t += 10
    loop.monitor()
    assert len(http.daq) == 1

    # the run starts
    db.runs[run_id].update(status="RUNNING", midas_run_number=604)
    odb.values["/Runinfo/Run DB PK"] = run_id
    odb.values["/Equipment/WDWaveforms/Statistics/Events sent"] = 100000
    loop.clock.t += 5
    loop.monitor()
    assert len(http.daq) == 1          # only 5 s since the last look
    loop.clock.t += 5
    loop.monitor()
    assert http.daq[-1]["stage"] == "running"
    assert http.daq[-1]["events"] == {"sent": 100000, "requested": 1000000}
    assert http.daq[-1]["runs"][0]["run_number"] == 604

    # 5 % more events: not worth a report; 10 % more: reported
    odb.values["/Equipment/WDWaveforms/Statistics/Events sent"] = 150000
    loop.clock.t += 10
    loop.monitor()
    assert len(http.daq) == 2
    odb.values["/Equipment/WDWaveforms/Statistics/Events sent"] = 200000
    loop.clock.t += 10
    loop.monitor()
    assert len(http.daq) == 3 and http.daq[-1]["events"]["sent"] == 200000

    # run done, nearline 1 of 2 subruns
    db.runs[run_id]["status"] = "DONE"
    for i, status in enumerate(["DONE", "RUNNING"]):
        db.files.append({"run_id": run_id, "filebase": "run00604_%05d" % i, "fileext": "root",
                         "status": status})
        db.jobs.append({"midas_run_id": run_id, "job_type": "nearline", "status": status})
    loop.clock.t += 10
    loop.monitor()
    assert http.daq[-1]["stage"] == "nearline"
    assert http.daq[-1]["subruns"] == {"done": 1, "total": 2}
    assert http.daq[-1]["events"] is None

    # all subruns done, the daemon posts the context
    db.jobs[-1]["status"] = "DONE"
    db.files[-1]["status"] = "DONE"
    loop.post_sequence(seq_id)
    assert http.daq[-1]["stage"] == "posted"
    assert "run00604" in http.daq[-1]["message"]
    assert http.contexts[-1]["responds_to"] == {"proposal_id": 5}
    n = len(http.daq)
    loop.clock.t += 60
    loop.monitor()
    assert len(http.daq) == n          # no active step, no more reports


def test_a_failed_run_is_reported_once():
    loop, db, odb, http, _ = monitored_loop()
    seq_id = loop.poll_and_schedule()[0]["seq_id"]
    (run_id,) = db.sequences[seq_id]["runs"]
    db.runs[run_id].update(status="FAILED", midas_run_number=604)
    for _ in range(3):
        loop.clock.t += 10
        loop.monitor()
    failed = [r for r in http.daq if r["stage"] == "failed"]
    assert len(failed) == 1 and "604 FAILED" in failed[0]["message"]


def test_a_failed_post_is_reported():
    loop, db, odb, http, messages = monitored_loop()
    loop.header_reader = HeaderReader({"names": [], "demand": [], "measured": [], "types": []})
    seq_id = loop.poll_and_schedule()[0]["seq_id"]
    (run_id,) = db.sequences[seq_id]["runs"]
    db.runs[run_id].update(status="DONE", midas_run_number=604)
    db.files.append({"run_id": run_id, "filebase": "run00604_00000", "fileext": "root", "status": "DONE"})
    loop.post_sequence(seq_id)
    assert db.sequences[seq_id]["status"] == "FAILED"
    assert http.daq[-1]["stage"] == "failed" and "no knobs" in http.daq[-1]["message"]
    assert any(is_error and "no knobs" in m for m, is_error in messages)


def test_a_failed_schedule_is_reported():
    odb = FakeOdb({"/Nearline/config/MiniTwin updates": "pim1_epics",
                   "/Nearline/config/MiniTwin enable": True,
                   "/Nearline/config/MiniTwin target config": 99})
    loop, db, odb, http, messages = loop_with_service([proposal(5)], odb=odb)
    with pytest.raises(tuning.ScheduleError):
        loop.poll_and_schedule()
    assert http.daq[-1]["stage"] == "failed" and http.daq[-1]["proposal_id"] == 5
    assert "99" in http.daq[-1]["message"]
    assert any(is_error for _, is_error in messages)


def test_pause_is_reported_once_and_stops_the_monitor():
    loop, db, odb, http, _ = monitored_loop()
    seq_id = loop.poll_and_schedule()[0]["seq_id"]
    odb.values["/Nearline/config/MiniTwin enable"] = False
    loop.refresh_enable()
    loop.refresh_enable()
    assert [r["stage"] for r in http.daq] == ["scheduled", "paused"]
    assert http.daq[-1]["proposal_id"] == 5 and http.daq[-1]["seq_id"] == seq_id
    (run_id,) = db.sequences[seq_id]["runs"]
    db.runs[run_id].update(status="RUNNING", midas_run_number=604)
    loop.clock.t += 30
    loop.monitor()
    assert len(http.daq) == 2
    # back on: the next look reports the real state
    odb.values["/Nearline/config/MiniTwin enable"] = True
    loop.refresh_enable()
    loop.monitor()
    assert http.daq[-1]["stage"] == "running"


def test_resume_without_a_step_repeats_the_last_report():
    loop, db, odb, http, _ = monitored_loop()
    seq_id = loop.poll_and_schedule()[0]["seq_id"]
    loop.set_active(None)
    odb.values["/Nearline/config/MiniTwin enable"] = False
    loop.refresh_enable()
    odb.values["/Nearline/config/MiniTwin enable"] = True
    loop.refresh_enable()
    assert [r["stage"] for r in http.daq] == ["scheduled", "paused", "scheduled"]
    assert http.daq[-1]["message"] == "loop resumed"


def test_monitor_never_raises():
    loop, db, odb, http, messages = monitored_loop()
    loop.poll_and_schedule()

    def boom(seq_id):
        raise RuntimeError("database gone")
    db.get_sequence_progress = boom
    for _ in range(3):
        loop.clock.t += 10
        assert loop.monitor() is False
    assert sum("database gone" in m for m, _ in messages) == 1


def test_the_cli_writing_the_odb_is_picked_up():
    loop, db, odb, http, _ = monitored_loop()
    loop.poll_and_schedule()
    # another process posted the step and took proposal 6
    tuning.save_step(odb, None)
    tuning.save_last_id(odb, 6)
    loop.refresh_enable()
    assert loop.active is None and loop.mt.last_proposal_id == 6


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
    odb.values["/Nearline/config/MiniTwin post delay"] = 0
    d = bare_daemon(daemon_module, loop, db, odb)
    d.build_and_dispatch_seq({"id": 57, "on_complete": "mt_add", "status": "CLAIMED"})
    assert db.sequences[57]["status"] == "DONE"
    assert len(loop.mt.added) == 1


def test_daemon_does_not_poll_while_paused(daemon_module):
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    d = bare_daemon(daemon_module, loop, db, odb)
    odb.values["/Nearline/config/MiniTwin enable"] = False
    d.minitwin_enabled = loop.refresh_enable()
    d.check_for_updates()
    assert http.since == [] and db.runs == {}
    odb.values["/Nearline/config/MiniTwin enable"] = True
    d.minitwin_enabled = loop.refresh_enable()
    d.check_for_updates()
    assert http.since == [0] and len(db.runs) == 1


def test_daemon_posts_a_finished_step_even_while_paused(daemon_module):
    loop, db, odb, http, _ = loop_with_service([])
    db.add_run(604, subruns=1, seq_id=57)
    odb.values["/Nearline/config/MiniTwin post delay"] = 0
    d = bare_daemon(daemon_module, loop, db, odb)
    d.minitwin_enabled = False
    d.build_and_dispatch_seq({"id": 57, "on_complete": "mt_add", "status": "CLAIMED"})
    assert len(http.contexts) == 1 and db.sequences[57]["status"] == "DONE"


def test_daemon_check_for_updates_schedules_the_centre(daemon_module):
    loop, db, odb, _ = make_loop(mt=FakeMt([iter_config()]))
    d = bare_daemon(daemon_module, loop, db, odb)
    d.check_for_updates()
    assert len(db.runs) == 1
    assert "mt_add" in [s["on_complete"] for s in db.sequences.values()]


# -- A5: the manual path -------------------------------------------------------

def cli_setup(proposals=None):
    odb = FakeOdb({"/Nearline/config/MiniTwin updates": "pim1_epics",
                   "/Nearline/config/MiniTwin enable": False})
    tuning.ensure_odb_keys(odb)
    return FakeDb(), odb, FakeHttp(proposals or [])


def test_cli_schedule_dry_run_writes_nothing(capsys):
    db, odb, http = cli_setup([proposal(5)])
    rc = tuning.main(["schedule", "--dry-run"], db=db, odb=odb, http=http)
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry run" in out and '"on_complete": "mt_add"' in out
    assert db.runs == {} and odb.values["/Nearline/MiniTwin/Last proposal id"] == 0


def test_cli_schedule_takes_the_proposal_once(capsys):
    db, odb, http = cli_setup([proposal(5)])
    assert tuning.main(["schedule"], db=db, odb=odb, http=http) == 0
    assert len(db.runs) == 1
    assert odb.values["/Nearline/MiniTwin/Last proposal id"] == 5
    assert odb.values["/Nearline/MiniTwin/Active step/Step id"] == "ASM12_90.44"
    assert http.daq[-1]["stage"] == "scheduled"
    # again: the ODB remembers proposal 5
    assert tuning.main(["schedule"], db=db, odb=odb, http=http) == 1
    assert len(db.runs) == 1
    assert "no proposal newer than 5" in capsys.readouterr().out
    # --since 4 retakes it
    assert tuning.main(["schedule", "--since", "4"], db=db, odb=odb, http=http) == 0
    assert len(db.runs) == 2


def schedule_and_finish(db, odb, http):
    tuning.main(["schedule"], db=db, odb=odb, http=http)
    seq_id = odb.values["/Nearline/MiniTwin/Active step/Seq id"]
    (run_id,) = db.sequences[seq_id]["runs"]
    db.runs[run_id].update(status="DONE", midas_run_number=604)
    db.files.append({"run_id": run_id, "filebase": "run00604_00000", "fileext": "root", "status": "DONE"})
    db.sequences[seq_id]["status"] = "RUNSDONE"       # what the run database trigger does
    return seq_id


def test_cli_post_dry_run_prints_the_context(capsys):
    db, odb, http = cli_setup([proposal(5)])
    schedule_and_finish(db, odb, http)
    capsys.readouterr()
    rc = tuning.main(["post", "--run", "604", "--dry-run"], db=db, odb=odb, http=http,
                     header_reader=HeaderReader())
    out = capsys.readouterr().out
    assert rc == 0 and http.contexts == []
    assert '"context_id": "run00604"' in out and '"proposal_id": 5' in out
    assert "/home/pioneer/nearline/histograms/run00604/run00604_00000_hists.root" in out
    assert odb.values["/Nearline/MiniTwin/Active step/Proposal id"] == 5


def test_cli_post_sends_the_step_and_closes_it():
    db, odb, http = cli_setup([proposal(5)])
    seq_id = schedule_and_finish(db, odb, http)
    rc = tuning.main(["post", "--run", "604"], db=db, odb=odb, http=http,
                     header_reader=HeaderReader())
    assert rc == 0
    assert http.contexts[-1]["responds_to"] == {"proposal_id": 5}
    assert http.daq[-1]["stage"] == "posted"
    assert odb.values["/Nearline/MiniTwin/Active step/Proposal id"] == 0
    assert db.sequences[seq_id]["status"] == "DONE"


def test_cli_post_with_the_service_down_fails_and_keeps_the_step(capsys):
    db, odb, http = cli_setup([proposal(5)])
    schedule_and_finish(db, odb, http)
    http.fail = True
    rc = tuning.main(["post", "--run", "604"], db=db, odb=odb, http=http,
                     header_reader=HeaderReader())
    assert rc == 2
    assert "not posted" in capsys.readouterr().out
    assert odb.values["/Nearline/MiniTwin/Active step/Proposal id"] == 5


def test_cli_post_unknown_run():
    db, odb, http = cli_setup()
    assert tuning.main(["post", "--run", "999"], db=db, odb=odb, http=http) == 2


@pytest.mark.skipif(not (DATA / "run00588").is_dir(), reason="run00588 test files not present")
def test_cli_post_without_midas(capsys):
    db = FakeDb()
    db.add_run(588, subruns=3)
    reader = HeaderReader()
    rc = tuning.main(["post", "--run", "588", "--dry-run", "--no-odb", "--output-path", str(DATA)],
                     db=db, http=FakeHttp(), header_reader=reader)
    assert rc == 0
    assert reader.paths == [str(DATA / "run00588" / "run00588_00000_hists.root")]
    assert '"context_id": "run00588"' in capsys.readouterr().out


# -- proposal in_reply_to ------------------------------------------------------

from tuning_fakes import in_reply_to  # noqa: E402


def test_last_context_id_key_is_created_and_kept():
    odb = FakeOdb()
    tuning.ensure_odb_keys(odb)
    assert odb.values["/Nearline/MiniTwin/Last context id"] == ""
    odb.values["/Nearline/MiniTwin/Last context id"] = "run00604"
    tuning.ensure_odb_keys(odb)
    assert odb.values["/Nearline/MiniTwin/Last context id"] == "run00604"


def test_a_delivered_context_is_remembered():
    loop, db, odb, http, _ = loop_with_service([])
    db.add_run(604, subruns=1, seq_id=57)
    loop.post_sequence(57)
    assert odb.values["/Nearline/MiniTwin/Last context id"] == "run00604"


def test_an_undelivered_context_is_remembered_only_once_delivered():
    loop, db, odb, http, _ = loop_with_service([])
    http.fail = True
    db.add_run(604, subruns=1, seq_id=57)
    loop.post_sequence(57)
    assert odb.values["/Nearline/MiniTwin/Last context id"] == ""
    http.fail = False
    loop.mt._muted_until = 0.0
    loop.mt.Flush()
    assert odb.values["/Nearline/MiniTwin/Last context id"] == "run00604"


def scheduled_reply(http):
    (report,) = [r for r in http.daq if r["stage"] == "scheduled"]
    return report["reply"]


def test_old_service_without_in_reply_to():
    loop, db, odb, http, messages = loop_with_service([proposal(5)])
    odb.values["/Nearline/MiniTwin/Last context id"] = "run00604"
    loop.poll_and_schedule()
    assert len(db.runs) == 1
    assert scheduled_reply(http) == {"expected": "run00604", "got": None, "outcome": None, "ok": True}


def test_null_in_reply_to_is_none():
    loop, db, odb, http, messages = loop_with_service([proposal(5, in_reply_to=None)])
    loop.poll_and_schedule()
    assert scheduled_reply(http) == {"expected": None, "got": None, "outcome": None, "ok": True}
    assert not [m for m in messages if "warning" in m[0].lower() or m[1]]


def test_matching_reply_is_ok():
    loop, db, odb, http, messages = loop_with_service(
        [proposal(6, in_reply_to=in_reply_to("run00604"))])
    odb.values["/Nearline/MiniTwin/Last context id"] = "run00604"
    loop.poll_and_schedule()
    assert scheduled_reply(http) == {"expected": "run00604", "got": "run00604",
                                     "outcome": "done", "ok": True}
    assert any("answers context run00604" in m for m, _ in messages)


def test_mismatch_warns_and_still_schedules():
    loop, db, odb, http, messages = loop_with_service(
        [proposal(6, in_reply_to=in_reply_to("run00603"))])
    odb.values["/Nearline/MiniTwin/Last context id"] = "run00604"
    loop.poll_and_schedule()
    assert len(db.runs) == 1
    assert scheduled_reply(http) == {"expected": "run00604", "got": "run00603",
                                     "outcome": "done", "ok": False}
    warnings = [m for m, is_error in messages if "warning" in m.lower() and not is_error]
    assert warnings and "run00603" in warnings[0] and "run00604" in warnings[0]


def test_retake_is_an_info_message():
    loop, db, odb, http, messages = loop_with_service(
        [proposal(6, attempt=1, in_reply_to=in_reply_to("run00604", outcome="retake"))])
    odb.values["/Nearline/MiniTwin/Last context id"] = "run00604"
    loop.poll_and_schedule()
    assert ("Tuning: step ASM12_90.44 is retaken, attempt 1", False) in messages


def test_failed_is_an_error_message():
    loop, db, odb, http, messages = loop_with_service(
        [proposal(6, step_id="ASM12_95.20",
                  in_reply_to=in_reply_to("run00604", outcome="failed", attempt=2))])
    odb.values["/Nearline/MiniTwin/Last context id"] = "run00604"
    loop.poll_and_schedule()
    assert ("Tuning: step ASM12_90.44 given up after 3 attempts", True) in messages
    assert len(db.runs) == 1


def test_off_plan_is_a_warning():
    loop, db, odb, http, messages = loop_with_service(
        [proposal(6, in_reply_to=in_reply_to("run00604", outcome="off_plan", step_id=None))])
    odb.values["/Nearline/MiniTwin/Last context id"] = "run00604"
    loop.poll_and_schedule()
    assert any("warning" in m.lower() and "off_plan" in m and not e for m, e in messages)


def test_cli_schedule_dry_run_prints_the_reply(capsys):
    db, odb, http = cli_setup([proposal(6, in_reply_to=in_reply_to("run00603"))])
    odb.values["/Nearline/MiniTwin/Last context id"] = "run00604"
    assert tuning.main(["schedule", "--dry-run"], db=db, odb=odb, http=http) == 0
    out = capsys.readouterr().out
    assert '"reply"' in out and '"got": "run00603"' in out and '"ok": false' in out


def test_cli_post_remembers_the_context():
    db, odb, http = cli_setup([proposal(5)])
    schedule_and_finish(db, odb, http)
    assert tuning.main(["post", "--run", "604"], db=db, odb=odb, http=http,
                       header_reader=HeaderReader()) == 0
    assert odb.values["/Nearline/MiniTwin/Last context id"] == "run00604"


# -- review blocker 1: a context the service refuses must not stall the queue --

def finished_step(loop, db, run_number):
    """Schedule the loop's proposal and finish its run with one subrun."""
    seq_id = loop.poll_and_schedule()[0]["seq_id"]
    (run_id,) = db.sequences[seq_id]["runs"]
    db.runs[run_id].update(status="DONE", midas_run_number=run_number)
    db.files.append({"run_id": run_id, "filebase": "run%05d_00000" % run_number,
                     "fileext": "root", "status": "DONE"})
    return seq_id


def test_a_rejected_context_fails_its_sequence_and_the_next_one_is_delivered():
    loop, db, odb, http, messages = loop_with_service([proposal(5)])
    http.reject = {"run00604": 400}
    seq_id = finished_step(loop, db, 604)
    loop.post_sequence(seq_id)
    assert db.sequences[seq_id]["status"] == "FAILED"
    assert any(e and "rejected context run00604" in m for m, e in messages)
    assert http.daq[-1]["stage"] == "failed" and "rejected" in http.daq[-1]["message"]
    assert loop.mt.pending == 0
    # a valid context after it goes straight through
    db.add_run(605, subruns=1, seq_id=58)
    loop.post_sequence(58)
    assert [c["context_id"] for c in http.contexts] == ["run00605"]
    assert db.sequences[58]["status"] == "DONE"


def test_a_queued_context_rejected_later_does_not_hold_up_the_queue():
    loop, db, odb, http, messages = loop_with_service([])
    http.fail = True
    db.add_run(604, subruns=1, seq_id=57)
    db.add_run(605, subruns=1, seq_id=58)
    loop.post_sequence(57)
    loop.post_sequence(58)
    assert loop.mt.pending == 2
    http.fail = False
    http.reject = {"run00604": 400}
    loop.mt._muted_until = 0.0
    loop.mt.Flush()
    assert loop.mt.pending == 0
    assert [c["context_id"] for c in http.contexts] == ["run00605"]
    assert db.sequences[57]["status"] == "FAILED"
    assert any(e and "run00604" in m for m, e in messages)


def test_an_auth_error_is_retried_not_dropped():
    loop, db, odb, http, messages = loop_with_service([])
    http.reject = {"run00604": 401}
    db.add_run(604, subruns=1, seq_id=57)
    loop.post_sequence(57)
    assert loop.mt.pending == 1
    http.reject = {}
    loop.mt._muted_until = 0.0
    loop.mt.Flush()
    assert [c["context_id"] for c in http.contexts] == ["run00604"]


def test_daq_successes_do_not_reset_the_context_breaker():
    mt = real_mt(FakeHttp())
    mt.client.fail = True
    mt._enqueue({"context_id": "run00604"})       # failure 1
    mt._flush()                                     # failure 2
    mt.client.fail = False
    assert mt.PostDaq(tuning.daq_report(5, "running"))
    mt.client.fail = True
    mt._flush()                                     # failure 3: muted
    assert mt.muted


def test_client_raises_with_the_status_for_a_4xx_body(monkeypatch):
    from pioneer.nearline import beamtune_client
    client = beamtune_client.BeamTuneClient("http://127.0.0.1:1")
    monkeypatch.setattr(client, "_call", lambda method, path, body=None:
                        (422, {"error": {"type": "SchemaError", "message": "bad knob"}}))
    with pytest.raises(beamtune_client.BeamTuneError) as err:
        client.post_context({"context_id": "run00604"})
    assert err.value.status == 422
    assert beamtune_client.is_permanent_rejection(err.value)
    assert not beamtune_client.is_permanent_rejection(beamtune_client.BeamTuneError("x", status=429))
    assert not beamtune_client.is_permanent_rejection(beamtune_client.BeamTuneError("x"))


def test_non_finite_header_values_are_refused():
    from pioneer.nearline.miniTwinInterface import knobs_from_header
    header = dict(HEADER, measured=[float("nan")] + HEADER["measured"][1:])
    with pytest.raises(ValueError, match="non-finite"):
        knobs_from_header(header)


# -- review blocker 2: a queued context must survive a restart -----------------

def test_a_queued_context_keeps_its_sequence_and_step_open():
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    seq_id = finished_step(loop, db, 604)
    db.sequences[seq_id]["status"] = "CLAIMED"
    http.fail = True
    loop.post_sequence(seq_id)
    assert db.sequences[seq_id]["status"] == "CLAIMED"
    assert loop.active["proposal_id"] == 5
    assert odb.values["/Nearline/MiniTwin/Active step/Proposal id"] == 5
    assert not [r for r in http.daq if r["stage"] == "posted"]
    # delivered later by the same process: closed then
    http.fail = False
    loop.mt._muted_until = 0.0
    loop.mt.Flush()
    assert db.sequences[seq_id]["status"] == "DONE"
    assert loop.active is None
    assert http.daq[-1]["stage"] == "posted"


def test_a_restart_posts_a_claimed_sequence_again_with_its_step():
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    seq_id = finished_step(loop, db, 604)
    db.sequences[seq_id]["status"] = "CLAIMED"
    http.fail = True
    loop.post_sequence(seq_id)
    # the daemon stops with the context in its in-memory queue
    del loop

    loop2, _, _, http2, _ = loop_with_service([], odb=odb, db=db)
    loop2.clock = Clock()
    assert loop2.resume_claimed() == [seq_id]
    assert http2.contexts == []          # waits out the post delay like any claim
    loop2.clock.t += 60
    loop2.post_due()
    (context,) = http2.contexts
    assert context["context_id"] == "run00604"
    assert context["responds_to"] == {"proposal_id": 5}
    assert db.sequences[seq_id]["status"] == "DONE"
    assert loop2.active is None


def test_resume_claimed_leaves_other_sequences_alone():
    loop, db, odb, http, _ = loop_with_service([])
    db.add_run(604, subruns=1, seq_id=57)
    db.sequences[57]["on_complete"] = "merge mt_add"
    db.add_run(605, subruns=1, seq_id=58)
    db.sequences[58]["status"] = "DONE"
    assert loop.resume_claimed() == []
    assert http.contexts == []


# -- review blocker 3 and should-fix 4/5: the proposal watermark ---------------

def test_schedule_since_never_lowers_the_stored_watermark():
    db, odb, http = cli_setup([proposal(4), proposal(5)])
    assert tuning.main(["schedule"], db=db, odb=odb, http=http) == 0       # takes 4
    assert tuning.main(["schedule"], db=db, odb=odb, http=http) == 0       # takes 5
    assert odb.values["/Nearline/MiniTwin/Last proposal id"] == 5
    # a shifter retakes proposal 4 by hand
    assert tuning.main(["schedule", "--since", "3"], db=db, odb=odb, http=http) == 0
    assert odb.values["/Nearline/MiniTwin/Last proposal id"] == 5
    assert len(db.runs) == 3
    # a daemon (re)started now does not take proposal 5 again
    loop, _, _, http2, _ = loop_with_service([proposal(4), proposal(5)], odb=odb, db=db)
    assert loop.poll_and_schedule() == []
    assert len(db.runs) == 3


def test_a_failing_watermark_write_still_schedules_and_says_so_once():
    class BrokenOdb(FakeOdb):
        def odb_set(self, path, value):
            if path.endswith("/Last proposal id"):
                raise OSError("ODB full")
            super().odb_set(path, value)

    odb = BrokenOdb({"/Nearline/config/MiniTwin updates": "pim1_epics",
                     "/Nearline/config/MiniTwin enable": True,
                     "/Nearline/MiniTwin/Last proposal id": 0})
    loop, db, odb, http, messages = loop_with_service([proposal(5), proposal(6)], odb=odb)
    loop.poll_and_schedule()
    loop.poll_and_schedule()
    assert len(db.runs) == 2
    errors = [m for m, e in messages if e and "Last proposal id" in m]
    assert len(errors) == 1 and "ODB full" in errors[0]


def test_a_service_below_the_watermark_is_one_error():
    odb = FakeOdb({"/Nearline/config/MiniTwin updates": "pim1_epics",
                   "/Nearline/config/MiniTwin enable": True,
                   "/Nearline/MiniTwin/Last proposal id": 5})
    loop, db, odb, http, messages = loop_with_service([proposal(1), proposal(2)], odb=odb)
    for _ in range(3):
        assert loop.poll_and_schedule() == []
    errors = [m for m, e in messages if e and "below" in m]
    assert len(errors) == 1
    assert "#2" in errors[0] and "#5" in errors[0] and "/Nearline/MiniTwin/Last proposal id" in errors[0]
    assert odb.values["/Nearline/MiniTwin/Last proposal id"] == 5


# -- review should-fix 6: reading the header without PIONEER dictionaries -----

REAL_FILE = DATA / "run00588" / "run00588_00000_hists.root"


@pytest.mark.skipif(not REAL_FILE.is_file(), reason="run00588 test files not present")
def test_real_header_through_uproot():
    pytest.importorskip("uproot")
    from pioneer.nearline.miniTwinInterface import knobs_from_header
    header = tuning.read_beamline_header_uproot(REAL_FILE)
    knobs, readback = knobs_from_header(header)
    assert len(knobs) == 28
    assert "QTA11" in knobs and "ASM12" in knobs and "FS12-U" in knobs
    assert knobs["QTB12"] == pytest.approx(56.1219, abs=1e-3)
    assert set(knobs) == set(readback)
    assert "KSD11" not in knobs            # type 2


@pytest.mark.skipif(not REAL_FILE.is_file(), reason="run00588 test files not present")
def test_header_falls_back_to_uproot_without_root(monkeypatch):
    pytest.importorskip("uproot")
    monkeypatch.setitem(sys.modules, "ROOT", None)
    header = tuning.read_beamline_header(REAL_FILE)
    assert len(header["names"]) == len(header["types"]) == 34


def test_header_without_root_or_uproot_names_what_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "ROOT", None)
    monkeypatch.setitem(sys.modules, "uproot", None)
    with pytest.raises(RuntimeError, match="PIONEER.*dictionaries.*uproot"):
        tuning.read_beamline_header("/nonexistent/run00604_00000_hists.root")


def test_header_from_an_emulated_root_object_uses_uproot(monkeypatch):
    class Emulated:                         # what ROOT gives without dictionaries
        pass

    class FakeFile:
        def IsZombie(self):
            return False

        def Get(self, name):
            return Emulated()

        def Close(self):
            pass

    root = types.ModuleType("ROOT")
    root.TFile = types.SimpleNamespace(Open=lambda path: FakeFile())
    monkeypatch.setitem(sys.modules, "ROOT", root)
    monkeypatch.setattr(tuning, "read_beamline_header_uproot", lambda path: {"via": "uproot"})
    assert tuning.read_beamline_header("x_hists.root") == {"via": "uproot"}


# -- review should-fix 7: no proposal taken without the column map -------------

def test_no_proposal_is_taken_while_the_column_map_is_unavailable():
    from pioneer.nearline.beamtune_client import BeamTuneError
    loop, db, odb, http, messages = loop_with_service(
        [proposal(5, currents={"ASM12": 90.44, "QTB12": 56.12})])
    http.config_answer = BeamTuneError("GET /v1/config failed: timed out")
    assert loop.poll_and_schedule() == []
    assert loop.poll_and_schedule() == []
    assert db.runs == {}
    assert loop.mt.last_proposal_id == 0
    assert odb.values["/Nearline/MiniTwin/Last proposal id"] == 0
    errors = [m for m, e in messages if e and "knobs.columns" in m]
    assert len(errors) == 1
    # the service answers again: fetched now, proposal taken with column names
    http.config_answer = {"config": {"knobs": {"columns": {"ASM12": "ASM12:SOL:2", "QTB12": "QTB12:SOL:2"}}}}
    assert len(loop.poll_and_schedule()) == 1
    assert db.written == [("pim1_epics", {"ASM12:SOL:2": 90.44, "QTB12:SOL:2": 56.12})]
    assert odb.values["/Nearline/MiniTwin/Last proposal id"] == 5


# -- review should-fix 8: CLI schedule while the daemon loop is on -------------

def test_cli_schedule_refuses_while_the_loop_is_enabled(capsys):
    db, odb, http = cli_setup([proposal(5)])
    odb.values["/Nearline/config/MiniTwin enable"] = True
    assert tuning.main(["schedule"], db=db, odb=odb, http=http) == 2
    assert "MiniTwin enable" in capsys.readouterr().out
    assert db.runs == {} and http.since == []
    # a dry run only reads
    assert tuning.main(["schedule", "--dry-run"], db=db, odb=odb, http=http) == 0
    assert db.runs == {}
    assert tuning.main(["schedule", "--force"], db=db, odb=odb, http=http) == 0
    assert len(db.runs) == 1


# -- review should-fix 9: wait for the mirror before posting -------------------

def test_a_claimed_sequence_is_posted_after_the_post_delay(daemon_module):
    loop, db, odb, http, _ = loop_with_service([])
    loop.clock = Clock()
    assert odb.values["/Nearline/config/MiniTwin post delay"] == 0     # the default
    odb.values["/Nearline/config/MiniTwin post delay"] = 60            # a positive value still works
    db.add_run(604, subruns=1, seq_id=57)
    d = bare_daemon(daemon_module, loop, db, odb)
    d.build_and_dispatch_seq({"id": 57, "on_complete": "mt_add", "status": "CLAIMED"})
    assert http.contexts == [] and db.sequences[57]["status"] == "CLAIMED"
    loop.clock.t += 59
    loop.post_due()
    assert http.contexts == []
    loop.clock.t += 1
    loop.post_due()
    assert [c["context_id"] for c in http.contexts] == ["run00604"]
    assert db.sequences[57]["status"] == "DONE"
    loop.clock.t += 100
    loop.post_due()
    assert len(http.contexts) == 1


def test_cli_post_ignores_the_post_delay():
    db, odb, http = cli_setup([proposal(5)])
    schedule_and_finish(db, odb, http)
    assert tuning.main(["post", "--run", "604"], db=db, odb=odb, http=http,
                       header_reader=HeaderReader()) == 0
    assert len(http.contexts) == 1


# -- review nits ----------------------------------------------------------------

def test_save_step_writes_proposal_id_last():
    class Recording(FakeOdb):
        def __init__(self):
            super().__init__()
            self.calls = []

        def odb_set(self, path, value):
            self.calls.append((path.rsplit("/", 1)[1], value))
            super().odb_set(path, value)

    odb = Recording()
    tuning.save_step(odb, {"proposal_id": 5, "step_id": "S", "attempt": 0, "plan": "P", "seq_id": 57})
    assert odb.calls[0] == ("Proposal id", 0)
    assert odb.calls[-1] == ("Proposal id", 5)
    odb.calls.clear()
    tuning.save_step(odb, None)
    assert [c for c in odb.calls if c[0] == "Proposal id"] == [("Proposal id", 0)]


def test_cli_post_leaves_a_running_sequence_alone(capsys):
    db, odb, http = cli_setup([proposal(5)])
    seq_id = schedule_and_finish(db, odb, http)
    db.sequences[seq_id]["status"] = "RUNNING"
    assert tuning.main(["post", "--run", "604"], db=db, odb=odb, http=http,
                       header_reader=HeaderReader()) == 0
    assert db.sequences[seq_id]["status"] == "RUNNING"
    assert "left as it is" in capsys.readouterr().out


def test_scheduled_message_names_the_target_position():
    loop, db, odb, http, messages = loop_with_service([proposal(5)])
    loop.poll_and_schedule()
    assert any("target_position 1 at (0.0, 0.0)" in m for m, _ in messages)
    assert not any("not the stage centre" in m for m, _ in messages)
    assert http.daq[-1]["message"] == "target_position 1 at (0.0, 0.0)"


def test_a_target_off_centre_is_a_warning_not_a_refusal():
    odb = FakeOdb({"/Nearline/config/MiniTwin updates": "pim1_epics",
                   "/Nearline/config/MiniTwin enable": True,
                   "/Nearline/config/MiniTwin target config": 3})
    loop, db, odb, http, messages = loop_with_service([proposal(5)], odb=odb)
    loop.poll_and_schedule()
    assert len(db.runs) == 1
    assert any("warning" in m.lower() and "(17.0, 17.0)" in m and not e for m, e in messages)


# -- inline MuPix maps (planner item) -------------------------------------------

from tuning_fakes import FakeTH2, fake_root  # noqa: E402

XXP, YYP, XY = ("histograms/PIPSMMuPixMonitor/xxp", "histograms/PIPSMMuPixMonitor/yyp",
                "histograms/PIPSMMuPixMonitor/track_xy")
X_RANGE, PX_RANGE = (-37.48, 3.48), (-1365.0, 1365.0)
Y_RANGE, PY_RANGE = (-20.48, 20.48), (-1333.0, 1333.0)


def mupix_maps(counts=1000.0, at_x=-10.0):
    return {
        XXP: FakeTH2.blob(128, 128, X_RANGE, PX_RANGE, (at_x, 100.0), counts),
        YYP: FakeTH2.blob(128, 128, Y_RANGE, PY_RANGE, (5.0, -200.0), counts),
        XY: FakeTH2.blob(128, 128, X_RANGE, Y_RANGE, (at_x, 5.0), counts),
    }


@pytest.mark.parametrize("bins", [64, 128, 320])
def test_serialise_rebins_multiples_of_64(bins):
    from pioneer.nearline.miniTwinInterface import serialise_hist
    hist = FakeTH2.blob(bins, bins, (-1, 1), (-1, 1), (0.1, 0.1))
    out = serialise_hist(hist)
    assert len(out) == 64 and all(len(row) == 64 for row in out)
    assert sum(map(sum, out)) == 1000.0


def test_serialise_refuses_260_bins():
    from pioneer.nearline.miniTwinInterface import serialise_hist
    with pytest.raises(ValueError, match="not a multiple of 64"):
        serialise_hist(FakeTH2.blob(260, 77, (-41.6, 41.6), (-102.7, 102.7), (0, 0)))


def test_axes_come_from_the_histograms():
    from pioneer.nearline.miniTwinInterface import inline_maps
    maps = mupix_maps()
    inline = inline_maps([maps[XXP], maps[YYP], maps[XY]])
    assert inline["axes"] == {"x": list(X_RANGE), "px": list(PX_RANGE),
                              "y": list(Y_RANGE), "py": list(PY_RANGE)}
    assert [len(m) for m in inline["maps"]] == [64, 64, 64]


def test_an_x_y_map_on_other_axes_is_refused():
    from pioneer.nearline.miniTwinInterface import inline_maps
    maps = mupix_maps()
    maps[XY] = FakeTH2.blob(128, 128, (-3.48, 37.48), Y_RANGE, (5.0, 5.0))
    with pytest.raises(ValueError, match="x-y map"):
        inline_maps([maps[XXP], maps[YYP], maps[XY]])


def _mean_along_rows(plane, lo, hi):
    """psm_maps.map_moments: the row variable's mean is from plane.sum(axis=1)."""
    import numpy as np
    plane = np.asarray(plane)
    n = plane.shape[0]
    edges = np.linspace(lo, hi, n + 1)
    centres = 0.5 * (edges[:-1] + edges[1:])
    weights = plane.sum(axis=1)
    return float((weights * centres).sum() / weights.sum())


def test_orientation_matches_psm_maps():
    """A beam offset in x only: rows must be x, as psm_maps reads them."""
    import numpy as np
    from pioneer.nearline.miniTwinInterface import inline_maps
    xxp = FakeTH2.blob(128, 128, (-40.0, 40.0), (-1000.0, 1000.0), (20.3, 0.1))
    yyp = FakeTH2.blob(128, 128, (-40.0, 40.0), (-1000.0, 1000.0), (0.3, 0.1))
    xy = FakeTH2.blob(128, 128, (-40.0, 40.0), (-40.0, 40.0), (20.3, 0.3))
    inline = inline_maps([xxp, yyp, xy])
    axes = inline["axes"]
    assert _mean_along_rows(inline["maps"][0], *axes["x"]) == pytest.approx(20.3, abs=0.7)
    assert _mean_along_rows(np.asarray(inline["maps"][0]).T, *axes["px"]) == pytest.approx(0.0, abs=20)
    assert _mean_along_rows(inline["maps"][2], *axes["x"]) == pytest.approx(20.3, abs=0.7)
    # and through beam-tuning-client's own map_moments, when it is next door
    btc = Path(__file__).resolve().parents[3] / "beam-tuning-client-runplan-loop"
    if (btc / "beamtune" / "adapters" / "psm_maps.py").is_file():
        sys.path.insert(0, str(btc))
        try:
            from beamtune.adapters.psm_maps import map_moments
        except Exception:                              # noqa: BLE001 -- optional cross-check
            map_moments = None
        finally:
            sys.path.remove(str(btc))
        if map_moments is not None:
            moments = map_moments(inline["maps"], {k: tuple(v) for k, v in axes.items()})
            assert moments["mean_x"] == pytest.approx(20.3, abs=0.7)
            assert abs(moments["mean_px"]) < 20
            assert moments["mean_y"] == pytest.approx(0.3, abs=0.7)


def test_read_inline_maps_sums_the_subruns(monkeypatch):
    files = {"a_hists.root": mupix_maps(100.0), "b_hists.root": mupix_maps(50.0)}
    monkeypatch.setitem(sys.modules, "ROOT", fake_root(files))
    inline = tuning.read_inline_maps(["a_hists.root", "b_hists.root"])
    assert sum(map(sum, inline["maps"][0])) == 150.0
    assert inline["axes"]["x"] == list(X_RANGE)


def test_read_inline_maps_refuses_differently_binned_subruns(monkeypatch):
    other = mupix_maps()
    other[XXP] = FakeTH2.blob(128, 128, (-3.48, 37.48), PX_RANGE, (5.0, 0.0))
    monkeypatch.setitem(sys.modules, "ROOT", fake_root({"a": mupix_maps(), "b": other}))
    with pytest.raises(ValueError, match="binned differently"):
        tuning.read_inline_maps(["a", "b"])


def test_posted_context_carries_the_maps_inline_and_the_files(monkeypatch):
    db = FakeDb()
    db.add_run(604, subruns=2, seq_id=57)
    local = ["/home/pinky/nearline/run00604/run00604_%05d_hists.root" % i for i in range(2)]
    monkeypatch.setitem(sys.modules, "ROOT", fake_root({local[0]: mupix_maps(10.0),
                                                        local[1]: mupix_maps(20.0)}))
    http = FakeHttp()
    loop, _, _, _ = make_loop(db=db, mt=real_mt(http), maps_reader=tuning.read_inline_maps)
    loop.post_sequence(57)
    (context,) = http.contexts
    measurement = context["measurement"]
    assert len(measurement["files"]) == 2
    assert sum(map(sum, measurement["inline"]["maps"][1])) == 30.0
    assert measurement["inline"]["axes"]["py"] == list(PY_RANGE)
    assert db.sequences[57]["status"] == "DONE"


def test_unreadable_maps_still_post_the_files():
    db = FakeDb()
    db.add_run(604, subruns=1, seq_id=57)
    http = FakeHttp()

    def broken(paths):
        raise OSError("no ROOT here")
    loop, _, _, messages = make_loop(db=db, mt=real_mt(http), maps_reader=broken)
    loop.post_sequence(57)
    (context,) = http.contexts
    assert "inline" not in context["measurement"]
    assert len(context["measurement"]["files"]) == 1
    assert db.sequences[57]["status"] == "DONE"
    assert any(e and "no ROOT here" in m and "mirror" in m for m, e in messages)


def test_merge_path_add_context_takes_axes_from_the_file(monkeypatch):
    header = types.SimpleNamespace(GetNames=lambda: HEADER["names"], GetDemand=lambda: HEADER["demand"],
                                   GetMeasured=lambda: HEADER["measured"], GetTypes=lambda: HEADER["types"])
    objects = dict(mupix_maps(), beamline=header)
    root = fake_root({"/n/seq00057/seq00057.root": objects})
    monkeypatch.setitem(sys.modules, "ROOT", root)
    http = FakeHttp()
    mt = real_mt(http)
    step = {"proposal_id": 5, "step_id": "ASM12_90.44", "attempt": 0, "plan": "P", "seq_id": 57}
    mt.AddContext(Path("/n/seq00057/seq00057.root"), step=step)
    (context,) = http.contexts
    # never a path: the service makes a directory of the context id
    assert context["context_id"] == "seq00057"
    assert context["measurement"]["inline"]["axes"]["px"] == list(PX_RANGE)
    assert context["setting"]["knobs"] == {"ASM12": 90.44, "QTB12": 56.12}
    assert context["responds_to"] == {"proposal_id": 5}
    assert context["provenance"]["step_id"] == "ASM12_90.44"
    assert root.opened and all(f.closed for f in root.opened)


# -- combine_files: no current pulses means no normalisation --------------------

class FakeHeader:
    def __bool__(self):
        return True

    def Clone(self):
        return self

    def MergeHeader(self, other):
        return True


def _combine_inputs(current_counts):
    files = {}
    for name in ("a", "b"):
        objects = dict(mupix_maps(10.0), beamline=FakeHeader())
        if current_counts is not None:
            objects["histograms/musip/current"] = FakeTH2([[current_counts]], (0, 1), (0, 1))
        files[name] = objects
    return files


@pytest.mark.parametrize("current_counts", [None, 0.0])
def test_combine_without_current_pulses_is_not_normalised(monkeypatch, capsys, current_counts):
    monkeypatch.setitem(sys.modules, "ROOT", fake_root(_combine_inputs(current_counts)))
    from pioneer.nearline import combine_files
    headers, histos = combine_files.merge_sub_runs(["a", "b"])
    assert histos[XXP].Integral() == 20.0
    assert histos[XXP].scaled is None
    out = capsys.readouterr().out
    assert out.count("warning") == 1 and "not normalised" in out


def test_combine_normalises_by_the_current_pulses(monkeypatch):
    monkeypatch.setitem(sys.modules, "ROOT", fake_root(_combine_inputs(5.0)))
    from pioneer.nearline import combine_files
    headers, histos = combine_files.merge_sub_runs(["a", "b"])
    assert histos[XXP].scaled == pytest.approx(0.1)
    assert histos[XXP].Integral() == pytest.approx(2.0)


# -- second review S1 / N3: a context that always fails must not block others --

def test_a_context_that_always_gets_a_5xx_lets_the_others_through():
    from pioneer.nearline.beamtune_client import BeamTuneError
    loop, db, odb, http, messages = loop_with_service([])
    original = http.post_context

    def post_context(context):
        if context["context_id"] == "run00604":
            raise BeamTuneError("POST /v1/context -> 500: boom", status=500)
        return original(context)
    http.post_context = post_context
    db.add_run(604, subruns=1, seq_id=57)
    db.add_run(605, subruns=1, seq_id=58)
    loop.post_sequence(57)
    loop.post_sequence(58)
    # the daemon's loop: a proposal poll (which succeeds) every iteration
    for _ in range(6):
        loop.mt.NextConfiguration()
    assert [c["context_id"] for c in http.contexts] == ["run00605"]
    assert db.sequences[58]["status"] == "DONE"
    stuck = [m for m, e in messages if e and "keeps failing" in m]
    assert len(stuck) == 1 and "run00604" in stuck[0]
    # given up after 20 failures: dropped, sequence FAILED
    for _ in range(30):
        loop.mt.NextConfiguration()
    assert loop.mt.pending == 0
    assert db.sequences[57]["status"] == "FAILED"
    assert any(e and "dropped undelivered context run00604" in m for m, e in messages)


def test_a_service_that_is_down_costs_no_context_anything():
    loop, db, odb, http, _ = loop_with_service([])
    http.fail = True
    db.add_run(604, subruns=1, seq_id=57)
    db.add_run(605, subruns=1, seq_id=58)
    loop.post_sequence(57)
    loop.post_sequence(58)
    for _ in range(50):
        loop.mt._muted_until = 0.0
        loop.mt.NextConfiguration()
    assert loop.mt.pending == 2
    assert db.sequences[57]["status"] == "CLAIMED"
    http.fail = False
    loop.mt._muted_until = 0.0
    loop.mt.Flush()
    assert [c["context_id"] for c in http.contexts] == ["run00604", "run00605"]


def test_a_full_queue_fails_the_dropped_sequence():
    loop, db, odb, http, messages = loop_with_service([])
    http.fail = True
    loop.mt._pending = __import__("collections").deque(maxlen=2)
    for i, seq in enumerate((57, 58, 59)):
        db.add_run(604 + i, subruns=1, seq_id=seq)
        loop.post_sequence(seq)
    assert loop.mt.pending == 2
    assert db.sequences[57]["status"] == "FAILED"
    assert any(e and "run00604" in m and "full" in m for m, e in messages)


# -- second review S2 / N4 / N1 --------------------------------------------------

def test_an_odb_failure_in_the_reply_check_does_not_lose_the_proposal():
    class FlakyOdb(FakeOdb):
        armed = False

        def odb_get(self, path):
            if self.armed and path.endswith("Last context id"):
                raise RuntimeError("odb timeout")
            return super().odb_get(path)

    odb = FlakyOdb({"/Nearline/config/MiniTwin updates": "pim1_epics",
                    "/Nearline/config/MiniTwin enable": True})
    loop, db, odb, http, messages = loop_with_service(
        [proposal(5, in_reply_to=in_reply_to("run00604"))], odb=odb)
    odb.armed = True
    assert len(loop.poll_and_schedule()) == 1
    assert len(db.runs) == 1
    assert scheduled_reply(http) == {"expected": None, "got": None, "outcome": None, "ok": True}
    assert sum(1 for m, e in messages if e and "reply check" in m) == 1


def test_a_failed_step_write_keeps_the_step_in_memory():
    class FlakyOdb(FakeOdb):
        armed = False

        def odb_set(self, path, value):
            if self.armed and "/Active step/" in path and path.endswith("Seq id"):
                raise RuntimeError("odb timeout")
            super().odb_set(path, value)

    odb = FlakyOdb({"/Nearline/config/MiniTwin updates": "pim1_epics",
                    "/Nearline/config/MiniTwin enable": True})
    loop, db, odb, http, messages = loop_with_service([proposal(5)], odb=odb)
    odb.armed = True
    seq_id = loop.poll_and_schedule()[0]["seq_id"]
    assert len(db.runs) == 1
    assert loop.active["proposal_id"] == 5 and loop.active["seq_id"] == seq_id
    # the half-written step is never taken up (Proposal id was written last)
    assert odb.values["/Nearline/MiniTwin/Active step/Proposal id"] == 0
    loop.refresh_enable()
    assert loop.active["proposal_id"] == 5
    assert sum(1 for m, e in messages if e and "Active step" in m) == 1
    # the ODB recovers: written on the next iteration
    odb.armed = False
    loop.refresh_enable()
    assert odb.values["/Nearline/MiniTwin/Active step/Proposal id"] == 5
    assert odb.values["/Nearline/MiniTwin/Active step/Seq id"] == seq_id


def test_a_reply_without_a_context_id_is_none():
    check, reply = tuning.reply_check({"context_id": None, "outcome": "accepted"}, "run00604")
    assert check == "none" and reply["ok"] is True


# -- second review S4 / N8: the step is taken at claim time ----------------------

def test_a_kick_while_waiting_out_the_delay_keeps_the_old_steps_provenance():
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    loop.clock = Clock()
    odb.values["/Nearline/config/MiniTwin post delay"] = 60
    seq_id = finished_step(loop, db, 604)
    db.sequences[seq_id]["status"] = "CLAIMED"
    loop.claim(seq_id)
    assert odb.values["/Nearline/MiniTwin/Pending/%d/Proposal id" % seq_id] == 5
    # a new proposal is taken before the post delay is over
    http.proposals.append(proposal(6, step_id="ASM12_95.20"))
    loop.poll_and_schedule()
    assert loop.active["proposal_id"] == 6
    loop.clock.t += 61
    loop.post_due()
    context = http.contexts[-1]
    assert context["context_id"] == "run00604"
    assert context["responds_to"] == {"proposal_id": 5}
    assert context["provenance"]["step_id"] == "ASM12_90.44"
    assert loop.active["proposal_id"] == 6        # the new step is untouched
    assert "/Nearline/MiniTwin/Pending/%d/Proposal id" % seq_id not in odb.values


def test_a_restart_during_the_delay_keeps_the_provenance():
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    loop.clock = Clock()
    odb.values["/Nearline/config/MiniTwin post delay"] = 60
    seq_id = finished_step(loop, db, 604)
    db.sequences[seq_id]["status"] = "CLAIMED"
    loop.claim(seq_id)
    del loop
    loop2, _, _, http2, _ = loop_with_service([], odb=odb, db=db)
    loop2.clock = Clock()
    assert loop2.resume_claimed() == [seq_id]
    loop2.clock.t += 60
    loop2.post_due()
    assert http2.contexts[-1]["responds_to"] == {"proposal_id": 5}


def test_resume_leaves_sequences_of_other_proposals_alone():
    loop, db, odb, http, messages = loop_with_service([])
    loop.clock = Clock()
    # a CLAIMED sequence nobody recorded a step for
    db.add_run(590, subruns=1, seq_id=40)
    # one recorded for proposal 3 while the watermark is at 6
    db.add_run(591, subruns=1, seq_id=41)
    tuning.save_pending(odb, 41, {"proposal_id": 3, "step_id": "S", "attempt": 0, "plan": "P"})
    loop.mt.last_proposal_id = 6
    assert loop.resume_claimed() == []
    loop.clock.t += 120
    loop.post_due()
    assert http.contexts == []
    (text,) = [m for m, _ in messages if "left CLAIMED" in m]
    assert "40" in text and "41" in text and "590" in text and "post --run" in text


def test_resume_reads_no_histograms():
    calls = []
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    loop.header_reader = lambda path: calls.append(path) or HEADER
    seq_id = finished_step(loop, db, 604)
    db.sequences[seq_id]["status"] = "CLAIMED"
    loop.clock = Clock()
    assert loop.resume_claimed() == [seq_id]
    assert calls == []


# -- second review S5: a run-database error while posting ------------------------

def test_a_db_error_while_posting_is_retried_not_stuck():
    loop, db, odb, http, messages = loop_with_service([proposal(5)])
    loop.clock = Clock()
    odb.values["/Nearline/config/MiniTwin post delay"] = 0
    seq_id = finished_step(loop, db, 604)
    db.sequences[seq_id]["status"] = "CLAIMED"
    runs, update = db.get_all_runs_in_sequence, db.update_status

    def boom(*args, **kwargs):
        raise RuntimeError("db down")
    db.get_all_runs_in_sequence = db.update_status = boom
    loop.claim(seq_id)
    loop.clock.t += 10
    loop.post_due()
    assert db.sequences[seq_id]["status"] == "CLAIMED"
    assert seq_id in loop._waiting
    assert sum(1 for m, e in messages if e and "db down" in m) == 1
    # the database is back: posted on the next retry
    db.get_all_runs_in_sequence, db.update_status = runs, update
    loop.clock.t += 5
    loop.post_due()
    assert http.contexts == []           # not before 30 s
    loop.clock.t += 20
    loop.post_due()
    assert [c["context_id"] for c in http.contexts] == ["run00604"]
    assert db.sequences[seq_id]["status"] == "DONE"
    assert http.contexts[0]["responds_to"] == {"proposal_id": 5}


def test_a_failed_update_after_a_context_error_is_retried():
    db = FakeDb()
    db.add_run(604, subruns=0, seq_id=57)        # no files: a context error
    loop, _, odb, messages = make_loop(db=db, mt=real_mt())
    loop.clock = Clock()
    update = db.update_status

    def boom(*args, **kwargs):
        raise RuntimeError("db down")
    db.update_status = boom
    loop.post_sequence(57)
    assert 57 in loop._waiting and db.sequences[57]["status"] == "CLAIMED"
    db.update_status = update
    loop.clock.t += 30
    loop.post_due()
    assert db.sequences[57]["status"] == "FAILED"


# -- second review N5: column map errors ----------------------------------------

def test_a_config_answer_with_an_error_is_not_cached():
    logged = []
    loop, db, odb, http, messages = loop_with_service(
        [proposal(5, currents={"ASM12": 90.44})])
    loop.mt.logger = logged.append
    http.config_answer = {"error": {"type": "Conflict", "message": "backend restarting"}}
    for _ in range(3):
        assert loop.poll_and_schedule() == []
    assert db.runs == {}
    assert sum("knobs.columns" in m for m in logged) == 1
    http.config_answer = {"config": {"knobs": {"columns": {"ASM12": "ASM12:SOL:2"}}}}
    assert len(loop.poll_and_schedule()) == 1
    assert db.written == [("pim1_epics", {"ASM12:SOL:2": 90.44})]


# -- second review N7: empty maps are not sent inline ----------------------------

def test_empty_maps_are_not_sent_inline():
    db = FakeDb()
    db.add_run(604, subruns=1, seq_id=57)
    http = FakeHttp()
    empty = {"maps": [[[0.0] * 64 for _ in range(64)]] * 3,
             "axes": {"x": [0, 1], "px": [0, 1], "y": [0, 1], "py": [0, 1]}}
    loop, _, _, messages = make_loop(db=db, mt=real_mt(http), maps_reader=lambda paths: empty)
    loop.post_sequence(57)
    (context,) = http.contexts
    assert "inline" not in context["measurement"]
    assert len(context["measurement"]["files"]) == 1
    assert any(e and "empty" in m and "mirror" in m for m, e in messages)


# -- second review N9: combine_files across runs ---------------------------------

def _run_files(prefix, current_counts):
    files = {}
    for sub in ("0", "1"):
        objects = dict(mupix_maps(10.0), beamline=FakeHeader())
        if current_counts is not None:
            objects["histograms/musip/current"] = FakeTH2([[current_counts]], (0, 1), (0, 1))
        files[prefix + sub] = objects
    return files


def test_combine_adds_several_runs(monkeypatch):
    files = {**_run_files("a", 5.0), **_run_files("b", 10.0)}
    monkeypatch.setitem(sys.modules, "ROOT", fake_root(files))
    from pioneer.nearline import combine_files
    headers, histos = combine_files.combine_runs([["a0", "a1"], ["b0", "b1"]])
    # run a: 20 counts / 10 pulses, run b: 20 counts / 20 pulses
    assert histos[XXP].Integral() == pytest.approx(2.0 + 1.0)


def test_combine_normalises_all_runs_or_none(monkeypatch, capsys):
    files = {**_run_files("a", 5.0), **_run_files("b", None)}
    monkeypatch.setitem(sys.modules, "ROOT", fake_root(files))
    from pioneer.nearline import combine_files
    headers, histos = combine_files.combine_runs([["a0", "a1"], ["b0", "b1"]])
    assert histos[XXP].Integral() == pytest.approx(40.0)      # raw counts of both runs
    assert "histograms/musip/current" not in histos
    out = capsys.readouterr().out
    assert out.count("warning") == 1 and "b0" in out


def test_a_failing_context_is_moved_aside_while_paused_too():
    """Paused, the daemon only flushes; a health check stands in for the poll."""
    from pioneer.nearline.beamtune_client import BeamTuneError
    loop, db, odb, http, messages = loop_with_service([])
    original = http.post_context

    def post_context(context):
        if context["context_id"] == "run00604":
            raise BeamTuneError("POST /v1/context -> 500: boom", status=500)
        return original(context)
    http.post_context = post_context
    db.add_run(604, subruns=1, seq_id=57)
    db.add_run(605, subruns=1, seq_id=58)
    loop.post_sequence(57)
    loop.post_sequence(58)
    for _ in range(10):
        loop.mt._muted_until = 0.0
        loop.mt.Flush()
    assert [c["context_id"] for c in http.contexts] == ["run00605"]


# -- planner N5: never knob names as run-database columns -------------------------

@pytest.mark.parametrize("config", [{"config": {}}, {"config": {"knobs": {"columns": {}}}}])
def test_a_config_without_columns_blocks_the_proposal(config):
    loop, db, odb, http, messages = loop_with_service([proposal(5)])
    http.config_answer = config
    for _ in range(3):
        assert loop.poll_and_schedule() == []
    assert db.runs == {} and loop.mt.last_proposal_id == 0
    assert odb.values["/Nearline/MiniTwin/Last proposal id"] == 0
    assert sum(1 for m, e in messages if e and "knobs.columns" in m) == 1
    http.config_answer = None                      # the default map
    assert len(loop.poll_and_schedule()) == 1
    assert db.written == [("pim1_epics", {"ASM12:SOL:2": 90.44, "QTB12:SOL:2": 56.12})]


def test_a_knob_without_a_column_blocks_the_proposal_and_refetches_the_map():
    loop, db, odb, http, messages = loop_with_service(
        [proposal(5, currents={"ASM12": 90.44, "QSL18": 20.0})])
    for _ in range(3):
        assert loop.poll_and_schedule() == []
    assert db.runs == {} and odb.values["/Nearline/MiniTwin/Last proposal id"] == 0
    errors = [m for m, e in messages if e and "QSL18" in m]
    assert len(errors) == 1
    # the service's beam file gains the column: taken on the next poll
    http.config_answer = {"config": {"knobs": {"columns": {"ASM12": "ASM12:SOL:2",
                                                           "QSL18": "QSL18:SOL:2"}}}}
    assert len(loop.poll_and_schedule()) == 1
    assert db.written == [("pim1_epics", {"ASM12:SOL:2": 90.44, "QSL18:SOL:2": 20.0})]


def test_a_done_proposal_needs_no_column_map():
    loop, db, odb, http, _ = loop_with_service([dict(proposal(5), done=True)])
    http.config_answer = {"config": {}}
    assert loop.poll_and_schedule() == []
    assert loop.mt.last_proposal_id == 5


# -- laptop integration test findings ---------------------------------------------

def test_a_last_proposal_id_lowered_by_hand_is_taken_at_once():
    loop, db, odb, http, messages = loop_with_service([proposal(5)])
    loop.poll_and_schedule()
    assert odb.values["/Nearline/MiniTwin/Last proposal id"] == 5
    # the service restarted with a new state directory: its proposals start at 1
    http.proposals = [proposal(1)]
    assert loop.poll_and_schedule() == []
    odb.values["/Nearline/MiniTwin/Last proposal id"] = 0      # the shifter's fix
    loop.refresh_enable()
    assert loop.mt.last_proposal_id == 0
    assert sum("lowered by hand from 5 to 0" in m for m, _ in messages) == 1
    assert len(loop.poll_and_schedule()) == 1
    assert odb.values["/Nearline/MiniTwin/Last proposal id"] == 1
    loop.refresh_enable()
    assert sum("lowered by hand" in m for m, _ in messages) == 1


def test_the_daemons_own_lower_odb_value_is_not_taken_for_a_hand_edit():
    class NoWatermarkWrites(FakeOdb):
        def odb_set(self, path, value):
            if path.endswith("/Last proposal id") and value:
                raise OSError("ODB full")
            super().odb_set(path, value)

    odb = NoWatermarkWrites({"/Nearline/config/MiniTwin updates": "pim1_epics",
                             "/Nearline/config/MiniTwin enable": True,
                             "/Nearline/MiniTwin/Last proposal id": 0})
    loop, db, odb, http, messages = loop_with_service([proposal(5)], odb=odb)
    loop.poll_and_schedule()            # stored write fails: ODB still 0
    loop.refresh_enable()
    assert loop.mt.last_proposal_id == 5
    assert not any("lowered by hand" in m for m, _ in messages)


def test_a_delivery_during_a_pause_keeps_the_page_on_paused():
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    seq_id = finished_step(loop, db, 604)
    db.sequences[seq_id]["status"] = "CLAIMED"
    odb.values["/Nearline/config/MiniTwin enable"] = False
    loop.refresh_enable()
    loop.post_sequence(seq_id)
    assert http.contexts[-1]["responds_to"] == {"proposal_id": 5}
    stages = [r["stage"] for r in http.daq]
    assert stages[-2:] == ["posted", "paused"]
    assert "paused" in http.daq[-2]["message"]
    assert http.daq[-1]["runs"][0]["run_number"] == 604


def test_the_paused_report_carries_the_active_steps_runs():
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    seq_id = loop.poll_and_schedule()[0]["seq_id"]
    (run_id,) = db.sequences[seq_id]["runs"]
    db.runs[run_id].update(status="RUNNING", midas_run_number=604)
    db.jobs.append({"midas_run_id": run_id, "job_type": "nearline", "status": "DONE"})
    odb.values["/Nearline/config/MiniTwin enable"] = False
    loop.refresh_enable()
    report = http.daq[-1]
    assert report["stage"] == "paused"
    assert report["runs"] == [{"run_db_id": run_id, "run_number": 604, "status": "RUNNING"}]
    assert report["subruns"] == {"done": 1, "total": 1}
    assert report["seq_id"] == seq_id


def test_a_retake_after_a_nominal_bracket_says_so():
    loop, db, odb, http, messages = loop_with_service(
        [proposal(6, step_id="nominal_1", attempt=0,
                  in_reply_to=in_reply_to("run00604", outcome="retake", attempt=0))])
    odb.values["/Nearline/MiniTwin/Last context id"] = "run00604"
    loop.poll_and_schedule()
    assert ("Tuning: step ASM12_90.44 will be retaken after a nominal bracket (attempt 1)", False) \
        in messages


# -- run length from the proposal's run.stop ---------------------------------------

def proposal_with_stop(pid, stop):
    p = proposal(pid)
    p["run"]["stop"] = stop
    return p


def test_run_stop_events_sets_the_requested_events():
    loop, db, odb, http, messages = loop_with_service(
        [proposal_with_stop(5, {"kind": "events", "value": 1000})])
    assert odb.values["/Nearline/config/MiniTwin max events"] == 10000000
    loop.poll_and_schedule()
    (run,) = db.runs.values()
    assert run["requested_events"] == 1000
    assert any("1000 events requested" in m for m, _ in messages)


@pytest.mark.parametrize("stop", [None, {"kind": "seconds", "value": 600},
                                  {"kind": "events", "value": 0},
                                  {"kind": "events", "value": -5},
                                  {"kind": "events", "value": "1000"},
                                  {"kind": "events", "value": True},
                                  {"kind": "events", "value": 12.5},
                                  "1000"])
def test_other_run_stops_keep_the_default(stop):
    p = proposal(5)
    if stop is not None:
        p["run"]["stop"] = stop
    loop, db, odb, http, messages = loop_with_service([p])
    loop.poll_and_schedule()
    (run,) = db.runs.values()
    assert run["requested_events"] == 1000000
    why = [m for m, e in messages if "requesting 1000000 events" in m]
    assert len(why) == 1 and not any(e for m, e in messages if "requesting" in m)


def test_run_stop_above_the_cap_is_an_error_and_capped():
    odb = FakeOdb({"/Nearline/config/MiniTwin updates": "pim1_epics",
                   "/Nearline/config/MiniTwin enable": True,
                   "/Nearline/config/MiniTwin max events": 5000})
    loop, db, odb, http, messages = loop_with_service(
        [proposal_with_stop(5, {"kind": "events", "value": 20000})], odb=odb)
    loop.poll_and_schedule()
    (run,) = db.runs.values()
    assert run["requested_events"] == 5000
    assert any(e and "20000" in m and "5000" in m for m, e in messages)


def test_cli_schedule_dry_run_shows_the_requested_events(capsys):
    db, odb, http = cli_setup([proposal_with_stop(5, {"kind": "events", "value": 1000})])
    assert tuning.main(["schedule", "--dry-run"], db=db, odb=odb, http=http) == 0
    out = capsys.readouterr().out
    assert '"num_ev": 1000' in out and db.runs == {}
    assert tuning.main(["schedule"], db=db, odb=odb, http=http) == 0
    assert list(db.runs.values())[0]["requested_events"] == 1000


# -- inline maps are the measurement: no wait, labelled, failures are errors -----

def test_the_default_post_delay_posts_at_claim(daemon_module):
    loop, db, odb, http, _ = loop_with_service([])
    loop.clock = Clock()
    assert odb.values["/Nearline/config/MiniTwin post delay"] == 0
    db.add_run(604, subruns=1, seq_id=57)
    d = bare_daemon(daemon_module, loop, db, odb)
    d.build_and_dispatch_seq({"id": 57, "on_complete": "mt_add", "status": "CLAIMED"})
    assert [c["context_id"] for c in http.contexts] == ["run00604"]


def test_an_existing_post_delay_is_kept():
    odb = FakeOdb({"/Nearline/config/MiniTwin post delay": 60})
    tuning.ensure_odb_keys(odb)
    assert odb.values["/Nearline/config/MiniTwin post delay"] == 60


def test_inline_maps_are_labelled(monkeypatch):
    files = {"a": mupix_maps(10.0), "b": mupix_maps(20.0), "c": mupix_maps(5.0)}
    monkeypatch.setitem(sys.modules, "ROOT", fake_root(files))
    inline = tuning.read_inline_maps(["a", "b", "c"])
    assert inline["names"] == [XXP, YYP, XY]
    assert inline["source"] == "daemon"
    assert inline["n_files"] == 3
    assert inline["rebin"] == 2
    assert set(inline) == {"maps", "axes", "names", "source", "n_files", "rebin"}
    assert inline["axes"] == {"x": list(X_RANGE), "px": list(PX_RANGE),
                              "y": list(Y_RANGE), "py": list(PY_RANGE)}
    assert sum(map(sum, inline["maps"][0])) == 35.0


def test_rebin_is_listed_per_map_when_the_maps_differ():
    from pioneer.nearline.miniTwinInterface import inline_maps
    maps = mupix_maps()
    maps[YYP] = FakeTH2.blob(320, 128, Y_RANGE, PY_RANGE, (5.0, 0.0))
    inline = inline_maps([maps[XXP], maps[YYP], maps[XY]])
    assert inline["rebin"] == [[2, 2], [5, 2], [2, 2]]


def test_inconsistent_axes_post_files_only_with_an_error(monkeypatch):
    db = FakeDb()
    db.add_run(604, subruns=1, seq_id=57)
    local = "/home/pinky/nearline/run00604/run00604_00000_hists.root"
    maps = mupix_maps()
    maps[XY] = FakeTH2.blob(128, 128, (-3.48, 37.48), Y_RANGE, (5.0, 5.0))
    monkeypatch.setitem(sys.modules, "ROOT", fake_root({local: maps}))
    http = FakeHttp()
    loop, _, _, messages = make_loop(db=db, mt=real_mt(http), maps_reader=tuning.read_inline_maps)
    loop.post_sequence(57)
    (context,) = http.contexts
    assert "inline" not in context["measurement"] and len(context["measurement"]["files"]) == 1
    assert any(e and "x-y map" in m and "piana's file mirror" in m for m, e in messages)
    assert db.sequences[57]["status"] == "DONE"


# -- WP10: run exposure (measurement.exposure) --------------------------------

class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if self.conn.fail:
            raise RuntimeError("connection lost")

    def fetchall(self):
        return list(self.conn.rows)


class _FakeConn:
    def __init__(self, rows=(), fail=False):
        self.rows, self.fail = list(rows), fail
        self.executed = []
        self.closed = False
        self.committed = False

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_get_run_times_reads_bor_and_eor_rows(monkeypatch):
    from datetime import datetime, timezone
    import pioneer.rundb.interface as rundb_iface
    bor = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    eor = datetime(2026, 9, 25, 10, 5, 12, tzinfo=timezone.utc)
    conn = _FakeConn(rows=[(604, bor, eor), (605, bor, None)])
    monkeypatch.setattr(rundb_iface, "connect", lambda *a, **k: conn)
    times = rundb_iface.interface().get_run_times([605, 604, 606])
    assert times == {604: {"bor": bor, "eor": eor}, 605: {"bor": bor, "eor": None},
                     606: {"bor": None, "eor": None}}
    (timeout, _), (sql, params) = conn.executed
    # the scan is bounded: 5 s by default, local to this read-only transaction
    assert timeout == "SET LOCAL statement_timeout = '5000ms'"
    assert "logs.slow_control" in sql and params == ([604, 605, 606],)
    # read-only, and the connection is closed
    assert conn.closed and not conn.committed
    assert not any(word in sql.upper() for word in ("INSERT", "UPDATE", "DELETE"))


def test_get_run_times_timeout_is_configurable(monkeypatch):
    import pioneer.rundb.interface as rundb_iface
    conn = _FakeConn()
    monkeypatch.setattr(rundb_iface, "connect", lambda *a, **k: conn)
    rundb_iface.interface().get_run_times([604], timeout_s=0.25)
    assert conn.executed[0][0] == "SET LOCAL statement_timeout = '250ms'"


def test_get_all_runs_in_sequence_closes_its_connection(monkeypatch):
    import pioneer.rundb.interface as rundb_iface
    conn = _FakeConn(rows=[(401,), (402,)])
    monkeypatch.setattr(rundb_iface, "connect", lambda *a, **k: conn)
    assert rundb_iface.interface().get_all_runs_in_sequence(57) == [401, 402]
    assert conn.closed
    failing = _FakeConn(fail=True)
    monkeypatch.setattr(rundb_iface, "connect", lambda *a, **k: failing)
    with pytest.raises(RuntimeError):
        rundb_iface.interface().get_all_runs_in_sequence(57)
    assert failing.closed


def test_get_run_times_closes_the_connection_on_error(monkeypatch):
    import pioneer.rundb.interface as rundb_iface
    conn = _FakeConn(fail=True)
    monkeypatch.setattr(rundb_iface, "connect", lambda *a, **k: conn)
    with pytest.raises(RuntimeError):
        rundb_iface.interface().get_run_times(604)
    assert conn.closed
    assert "statement_timeout" in conn.executed[0][0]


def test_get_run_times_of_nothing_does_not_connect(monkeypatch):
    import pioneer.rundb.interface as rundb_iface
    monkeypatch.setattr(rundb_iface, "connect", lambda *a, **k: pytest.fail("connected"))
    assert rundb_iface.interface().get_run_times([]) == {}


def test_get_run_times_against_the_scratch_database(fresh_db, seeded):
    """The real query, on the seeded runs (needs $PIONEER_RUNDB_TEST_DSN)."""
    import psycopg
    from rundb_seed import _patch_config, _restore_config
    from pioneer.rundb.interface import interface

    params = psycopg.conninfo.conninfo_to_dict(fresh_db)
    saved = _patch_config(fresh_db)
    try:
        numbers = list(seeded["finished_run_numbers"]) + [seeded["running_run_number"]]
        times = interface(user=params.get("user") or "postgres",
                          password=params.get("password") or "").get_run_times(numbers)
    finally:
        _restore_config(saved)
    for number in seeded["finished_run_numbers"]:
        entry = times[number]
        assert (entry["eor"] - entry["bor"]).total_seconds() == seeded["durations"][number]
    running = times[seeded["running_run_number"]]
    assert running["bor"] is not None and running["eor"] is None


EVENTS_SENT = "/Equipment/WDWaveforms/Statistics/Events sent"
START_TIME = "/Runinfo/Start time binary"
STOP_TIME = "/Runinfo/Stop time binary"
STEP_KEYS = "/Nearline/MiniTwin/Active step/"
RECORD_DEFAULTS = {"Recorded run": 0, "Run start": 0.0, "Run stop": 0.0,
                   "Events at BOR": -1, "Events at EOR": -1}
RECORD_NAMES = ("run_number", "run_start", "run_stop", "events_start", "events_stop")
#: a run start, Unix seconds (2026-09-25T10:00:00Z)
T0 = 1_790_330_400


def running_step(run_number=604):
    """A loop whose proposal 5 was scheduled and whose run is being taken:
    ``(loop, db, odb, http, run_id)``."""
    loop, db, odb, http, _ = loop_with_service([proposal(5)])
    seq_id = loop.poll_and_schedule()[0]["seq_id"]
    (run_id,) = db.sequences[seq_id]["runs"]
    db.runs[run_id].update(status="RUNNING", midas_run_number=run_number)
    odb.values["/Runinfo/Run DB PK"] = run_id
    return loop, db, odb, http, run_id


def start_run(loop, odb, run_id, run_number=604, start=T0, events=0):
    """What MIDAS shows at the start transition after the frontends reset."""
    odb.values.update({START_TIME: start, STOP_TIME: 0, EVENTS_SENT: events})
    return loop.record_run_start(run_id, run_number)


def stop_run(loop, odb, run_id, run_number=604, stop=T0 + 312, events=1_000_123):
    odb.values.update({STOP_TIME: stop, EVENTS_SENT: events})
    return loop.record_run_stop(run_id, run_number)


def test_run_record_keys_are_created_and_kept():
    odb = FakeOdb()
    tuning.ensure_odb_keys(odb)
    for key, default in RECORD_DEFAULTS.items():
        assert odb.values[STEP_KEYS + key] == default
    odb.values[STEP_KEYS + "Events at EOR"] = 1234
    tuning.ensure_odb_keys(odb)
    assert odb.values[STEP_KEYS + "Events at EOR"] == 1234


def test_a_new_step_starts_with_nothing_recorded():
    loop, db, odb, http, run_id = running_step()
    for key, default in RECORD_DEFAULTS.items():
        assert odb.values[STEP_KEYS + key] == default
    assert all(loop.active[name] is None for name in RECORD_NAMES)


def test_the_transitions_record_the_steps_run(daemon_module):
    loop, db, odb, http, run_id = running_step()
    odb.values["/Logger/Channels/0/Settings/Current filename"] = "run00604.mid.lz4"
    d = bare_daemon(daemon_module, loop, db, odb)
    odb.values.update({START_TIME: T0, STOP_TIME: 0, EVENTS_SENT: 12})
    assert d.record_run_start_callback(odb, 604) == 1
    odb.values.update({STOP_TIME: T0 + 312, EVENTS_SENT: 1_000_135})
    assert d.end_of_run_callback(odb, 604) == 1
    assert {k: odb.values[STEP_KEYS + k] for k in RECORD_DEFAULTS} == {
        "Recorded run": 604, "Run start": float(T0), "Run stop": float(T0 + 312),
        "Events at BOR": 12, "Events at EOR": 1_000_135}
    assert {n: loop.active[n] for n in RECORD_NAMES} == {
        "run_number": 604, "run_start": float(T0), "run_stop": float(T0 + 312),
        "events_start": 12, "events_stop": 1_000_135}
    assert db.runs[run_id]["status"] == "DONE"
    # the in-memory step and the ODB agree: nothing looks changed to sync_state
    loop._last_key = "kept"
    loop.sync_state()
    assert loop._last_key == "kept"


def test_a_new_start_forgets_the_previous_stop():
    loop, db, odb, http, run_id = running_step()
    start_run(loop, odb, run_id)
    stop_run(loop, odb, run_id)
    start_run(loop, odb, run_id, run_number=605, start=T0 + 400, events=3)
    assert loop.active["run_number"] == 605 and loop.active["run_stop"] is None
    assert loop.active["events_stop"] is None and odb.values[STEP_KEYS + "Events at EOR"] == -1


def test_another_runs_transitions_leave_the_record_alone(daemon_module):
    loop, db, odb, http, run_id = running_step()
    other = db.add_run(700, status="RUNNING")
    odb.values["/Runinfo/Run DB PK"] = other
    d = bare_daemon(daemon_module, loop, db, odb)
    odb.values.update({START_TIME: T0, EVENTS_SENT: 0})
    d.record_run_start_callback(odb, 700)
    d.end_of_run_callback(odb, 700)
    assert all(loop.active[name] is None for name in RECORD_NAMES)
    assert odb.values[STEP_KEYS + "Recorded run"] == 0


def test_the_transitions_never_fail_over_the_record(daemon_module):
    loop, db, odb, http, run_id = running_step()

    def boom(*args, **kwargs):
        raise RuntimeError("run database down")
    db.get_all_runs_in_sequence = boom
    d = bare_daemon(daemon_module, loop, db, odb)
    assert d.record_run_start_callback(odb, 604) == 1
    assert d.end_of_run_callback(odb, 604) == 1
    assert db.runs[run_id]["status"] == "DONE"

    # nor over the ODB, nor with no tuning loop at all
    loop2, db2, odb2, _, run_id2 = running_step()

    class BrokenOdb(FakeOdb):
        def odb_exists(self, path):
            raise RuntimeError("ODB gone")
    d2 = bare_daemon(daemon_module, loop2, db2, BrokenOdb())
    assert d2.record_run_start_callback(d2.client, 604) == 1
    del d2.tuning
    assert d2.record_run_start_callback(odb2, 604) == 1


def test_a_missing_counter_is_recorded_as_unknown():
    loop, db, odb, http, run_id = running_step()
    odb.values.update({START_TIME: T0, STOP_TIME: 0})
    loop.record_run_start(run_id, 604)
    assert loop.active["run_start"] == float(T0) and loop.active["events_start"] is None


def test_a_stop_without_a_recorded_start_takes_the_start_time_from_runinfo():
    loop, db, odb, http, run_id = running_step()
    odb.values[START_TIME] = T0            # the daemon was down at the start
    stop_run(loop, odb, run_id)
    assert loop.active["run_number"] == 604
    assert loop.active["run_start"] == float(T0) and loop.active["run_stop"] == float(T0 + 312)
    assert loop.active["events_start"] is None and loop.active["events_stop"] == 1_000_123


def test_a_stale_stop_time_is_replaced_by_the_clock():
    loop, db, odb, http, run_id = running_step()
    loop.clock = lambda: T0 + 100.5
    start_run(loop, odb, run_id)
    stop_run(loop, odb, run_id, stop=T0 - 50)
    assert loop.active["run_stop"] == T0 + 100.5


def test_the_record_survives_a_restart_and_a_claim():
    loop, db, odb, http, run_id = running_step()
    start_run(loop, odb, run_id, events=7)
    stop_run(loop, odb, run_id, events=987_661)
    seq_id = loop.active["seq_id"]
    recorded = {n: loop.active[n] for n in RECORD_NAMES}

    # a new daemon on the same ODB
    loop2, _, _, _, _ = loop_with_service([], odb=odb, db=db)
    assert {n: loop2.active[n] for n in RECORD_NAMES} == recorded
    # the claimed sequence keeps it, through the Pending record too
    loop2.claim(seq_id)
    pending = tuning.load_pending(odb, seq_id)
    assert {n: pending[n] for n in RECORD_NAMES} == recorded
    # a record written before these keys existed reads as unknown
    for key in RECORD_DEFAULTS:
        del odb.values["/Nearline/MiniTwin/Pending/%d/%s" % (seq_id, key)]
    assert all(tuning.load_pending(odb, seq_id)[n] is None for n in RECORD_NAMES)


def _utc(h, m, s):
    from datetime import datetime, timezone
    return datetime(2026, 9, 25, h, m, s, tzinfo=timezone.utc)


def posted_step_context(record=True, run_times=None, start=T0, stop=T0 + 312,
                        events=(12, 1_000_135)):
    """Take proposal 5's run (MIDAS 604) through its transitions (unless
    `record` is False: the daemon was down) and post it:
    ``(context, loop, db, odb, messages)``."""
    loop, db, odb, http, run_id = running_step()
    messages = []
    loop._message = lambda m, is_error=False: messages.append((m, is_error))
    if record:
        start_run(loop, odb, run_id, start=start, events=events[0])
        stop_run(loop, odb, run_id, stop=stop, events=events[1])
    db.runs[run_id]["status"] = "DONE"
    db.files.append({"run_id": run_id, "filebase": "run00604_00000", "fileext": "root",
                     "status": "DONE"})
    db.run_times = run_times
    loop.post_sequence(loop.active["seq_id"])
    (context,) = http.contexts
    return context, loop, db, odb, messages


def exposure_messages(messages):
    return [m for m, e in messages if "exposure" in m]


def test_the_context_carries_the_recorded_exposure():
    context, loop, db, odb, messages = posted_step_context()
    assert context["measurement"]["exposure"] == {
        "seconds": 312.0,
        "wd_events": 1_000_123,
        "per_run": [{"run": 604, "seconds": 312.0, "wd_events": 1_000_123,
                     "bor": "2026-09-25T10:00:00.000Z", "eor": "2026-09-25T10:05:12.000Z",
                     "time_source": "odb"}],
        "source": tuning.EXPOSURE_SOURCES,
    }
    assert set(context["measurement"]["exposure"]["source"]) == {"seconds", "wd_events"}
    # the run database is not asked
    assert db.run_times_calls == []
    # everything else about the context is as before
    assert context["measurement"]["kind"] == "psm_nearline"
    assert context["responds_to"] == {"proposal_id": 5}
    assert exposure_messages(messages) == []


def test_events_going_down_give_null_events():
    context, loop, db, odb, messages = posted_step_context(events=(500, 400))
    exposure = context["measurement"]["exposure"]
    assert exposure["wd_events"] is None and exposure["per_run"][0]["wd_events"] is None
    assert exposure["seconds"] == 312.0
    (text,) = exposure_messages(messages)
    assert "went down" in text


@pytest.mark.parametrize("stop", [T0, T0 - 5])
def test_seconds_that_are_not_positive_are_null(stop):
    # a stop time at or before the start is replaced by the clock; a clock
    # that says the same is not a duration
    loop, db, odb, http, run_id = running_step()
    loop.clock = lambda: float(T0)
    start_run(loop, odb, run_id)
    stop_run(loop, odb, run_id, stop=stop)
    exposure = loop.exposure([604], step=loop.active)
    assert exposure["seconds"] is None and exposure["per_run"][0]["seconds"] is None
    assert exposure["wd_events"] == 1_000_123


def test_run_db_seconds_that_are_not_positive_are_null():
    loop, db, _, messages = make_loop()
    db.run_times = {604: {"bor": _utc(10, 0, 0), "eor": _utc(10, 0, 0)}}
    exposure = loop.exposure([604])
    assert exposure["seconds"] is None and exposure["per_run"][0]["time_source"] == "run_db"
    assert "not after BOR" in exposure_messages(messages)[0]


def test_a_run_the_daemon_did_not_record_falls_back_to_the_run_db():
    context, loop, db, odb, messages = posted_step_context(
        record=False, run_times={604: {"bor": _utc(10, 0, 0), "eor": _utc(10, 5, 12)}})
    exposure = context["measurement"]["exposure"]
    assert exposure["seconds"] == 312.0 and exposure["wd_events"] is None
    assert exposure["per_run"][0]["time_source"] == "run_db"
    assert db.run_times_calls == [([604], tuning.RUN_TIMES_TIMEOUT_S)]
    # one info message, saying the daemon did not record the run
    (text,) = exposure_messages(messages)
    assert "not recorded by the daemon" in text
    assert not [m for m, e in messages if e]


def test_a_run_without_bor_has_null_seconds():
    context, loop, db, odb, messages = posted_step_context(
        record=False, run_times={604: {"bor": None, "eor": _utc(10, 5, 12)}})
    exposure = context["measurement"]["exposure"]
    assert exposure["seconds"] is None
    assert exposure["per_run"] == [{"run": 604, "seconds": None, "wd_events": None, "bor": None,
                                    "eor": "2026-09-25T10:05:12.000Z", "time_source": "run_db"}]
    (text,) = exposure_messages(messages)
    assert "no BOR row" in text


def test_a_fallback_timeout_still_posts_the_context():
    import psycopg
    context, loop, db, odb, messages = posted_step_context(
        record=False,
        run_times=psycopg.errors.QueryCanceled("canceling statement due to statement timeout"))
    exposure = context["measurement"]["exposure"]
    assert exposure["seconds"] is None and exposure["wd_events"] is None
    assert exposure["per_run"][0]["bor"] is None and exposure["per_run"][0]["eor"] is None
    assert db.sequences[loop_seq(db)]["status"] == "DONE"
    (text,) = exposure_messages(messages)
    assert "statement timeout" in text
    assert not [m for m, e in messages if e]


def loop_seq(db):
    (seq_id,) = [s for s, v in db.sequences.items() if v["on_complete"] == "mt_add"]
    return seq_id


def test_a_context_without_the_step_has_no_events():
    db = FakeDb()
    run_id = db.add_run(604, subruns=1)
    db.run_times = {604: {"bor": _utc(10, 0, 0), "eor": _utc(10, 1, 0)}}
    loop, _, _, messages = make_loop(db=db, mt=real_mt())
    exposure = loop.build_context([run_id])["measurement"]["exposure"]
    assert exposure["seconds"] == 60.0 and exposure["wd_events"] is None
    # no step, so nothing was expected to be recorded: no message
    assert exposure_messages(messages) == []


def test_several_runs_sum_their_seconds():
    db = FakeDb()
    a, b = db.add_run(604, subruns=1), db.add_run(605, subruns=1)
    db.run_times = {604: {"bor": _utc(10, 0, 0), "eor": _utc(10, 1, 0)},
                    605: {"bor": _utc(10, 2, 0), "eor": _utc(10, 2, 30)}}
    loop, _, _, _ = make_loop(db=db, mt=real_mt())
    exposure = loop.build_context([a, b])["measurement"]["exposure"]
    assert exposure["seconds"] == 90.0
    assert [r["seconds"] for r in exposure["per_run"]] == [60.0, 30.0]
    assert exposure["wd_events"] is None


def test_the_merge_path_carries_the_exposure(monkeypatch):
    header = types.SimpleNamespace(GetNames=lambda: HEADER["names"], GetDemand=lambda: HEADER["demand"],
                                   GetMeasured=lambda: HEADER["measured"], GetTypes=lambda: HEADER["types"])
    monkeypatch.setitem(sys.modules, "ROOT",
                        fake_root({"/n/seq00057/seq00057.root": dict(mupix_maps(), beamline=header)}))
    db = FakeDb()
    run_ids = [db.add_run(604), db.add_run(605)]
    db.run_times = {604: {"bor": _utc(10, 0, 0), "eor": _utc(10, 1, 0)}}
    http = FakeHttp()
    loop, _, _, _ = make_loop(db=db, mt=real_mt(http))
    exposure = loop.exposure_of(run_ids)
    loop.mt.AddContext(Path("/n/seq00057/seq00057.root"), exposure=exposure)
    (context,) = http.contexts
    got = context["measurement"]["exposure"]
    assert [r["run"] for r in got["per_run"]] == [604, 605]
    assert got["per_run"][0]["seconds"] == 60.0 and got["seconds"] is None


def test_exposure_of_unknown_runs_is_none():
    loop, db, _, messages = make_loop()
    assert loop.exposure_of([12345]) is None
    assert [m for m, e in messages if "exposure" in m]


# -- the scratch-database guards in conftest.py (no database touched) ---------

@pytest.mark.parametrize("dsn", [
    "dbname=pioneer_rundb_test",
    "host=localhost dbname=pioneer_rundb_test",
    "host=127.0.0.1 port=5432 dbname=pioneer_rundb_test",
    "host=::1 dbname=pioneer_rundb_test",
    "host=/var/run/postgresql dbname=pioneer_rundb_test",
    "postgresql://u:p@localhost/pioneer_rundb_test",
])
def test_scratch_dsn_on_the_local_default_port_is_refused(dsn):
    from conftest import local_default_port_refusal
    assert "live run database" in local_default_port_refusal(dsn, environ={})
    assert local_default_port_refusal(dsn, environ={"PIONEER_RUNDB_TEST_I_KNOW": "1"}) is None
    assert "live run database" in local_default_port_refusal(
        dsn, environ={"PIONEER_RUNDB_TEST_I_KNOW": "yes"})


@pytest.mark.parametrize("dsn", [
    "host=localhost port=55432 dbname=pioneer_rundb_test",
    "host=scratch-pg port=5432 dbname=pioneer_rundb_test",
])
def test_scratch_dsn_elsewhere_is_allowed(dsn):
    from conftest import local_default_port_refusal
    assert local_default_port_refusal(dsn, environ={}) is None


def test_a_server_with_the_live_database_is_refused():
    from conftest import live_database_refusal

    class Conn:
        def __init__(self, names):
            self.names = names

        def execute(self, sql, params):
            assert "pg_database" in sql
            found = params[0] in self.names
            return types.SimpleNamespace(fetchone=lambda: (1,) if found else None)

    assert "'pioneer'" in live_database_refusal(Conn({"postgres", "pioneer"}))
    assert live_database_refusal(Conn({"postgres", "pioneer_rundb_test"})) is None
