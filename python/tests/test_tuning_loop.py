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
    assert loop2.resume_claimed() == [seq_id]
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
