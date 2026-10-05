"""The ConfigDB page's "Submit new Sequences": target x degrader x beamline,
with any level kept at its current setting.

A stand-in run-DB interface records what would be scheduled, so none of this
needs a database -- only the `midas` python package, which midas_commands
imports.
"""

import json

import pytest

pytest.importorskip("midas", reason="needs /software/midas/python on PYTHONPATH")

from pioneer.rundb import midas_commands      # noqa: E402
from pioneer.rundb.commands import CommandError  # noqa: E402


class StubIface:
    def __init__(self):
        self.runs = []          # (configs, note) per scheduled run
        self.sequences = []

    def load_config(self, name, config_id):
        return {"id": int(config_id), "table": name}

    def add_new_configuration(self, key, config):
        raise AssertionError("every configuration comes from the database")

    def schedule_new_run(self, num_ev, configs, author, note, quality=None):
        if not configs:
            raise ValueError("No configuration for run provided")
        self.runs.append((sorted(configs), note))
        return len(self.runs)

    def register_sequence(self, run_ids, on_complete):
        self.sequences.append(list(run_ids))
        return len(self.sequences)


def request(config, current=()):
    return {"config": list(config), "current": list(current), "events": 1000,
            "operator": "tester", "description": "test scan"}


def schedule(config, current=()):
    iface = StubIface()
    data = midas_commands.schedule_configuration(request(config, current), iface=iface)
    assert data["runs"] == list(range(1, len(iface.runs) + 1))
    return iface


def test_full_product_unchanged():
    iface = schedule(["target_position:1", "target_position:2",
                      "degrader_position:10", "pie5_epics:100", "pie5_epics:101"])
    assert len(iface.runs) == 4
    assert all(len(cfgs) == 3 for cfgs, _ in iface.runs)
    assert {tuple(c) for c, _ in iface.runs} == {(1, 10, 100), (1, 10, 101), (2, 10, 100), (2, 10, 101)}


def test_current_beamline_leaves_epics_out():
    iface = schedule(["target_position:1", "target_position:2", "degrader_position:10"],
                     current=["beamline"])
    assert [c for c, _ in iface.runs] == [[1, 10], [2, 10]]
    # the description lives on the beam level and must survive it being current
    assert all(note.startswith("test scan\nSequence: Beam (current)") for _, note in iface.runs)


def test_current_degrader_and_target():
    iface = schedule(["pie5_epics:100", "pie5_epics:101"], current=["degrader", "target"])
    assert [c for c, _ in iface.runs] == [[100], [101]]
    assert "Degrader (current)" in iface.runs[0][1] and "XY (current)" in iface.runs[0][1]


def test_current_target_only():
    iface = schedule(["degrader_position:10", "degrader_position:11", "pim1_epics:100"],
                     current=["target"])
    assert [c for c, _ in iface.runs] == [[10, 100], [11, 100]]


@pytest.mark.parametrize("config,current,match", [
    (["target_position:1", "degrader_position:10"], [], "beamline: select at least one"),
    (["target_position:1", "degrader_position:10", "pie5_epics:100"], ["target"], "target: configurations selected and marked"),
    ([], ["target", "degrader", "beamline"], "nothing to schedule"),
    (["target_position:1"], ["degrader", "bogus"], "Unknown level"),
    (["foo:1"], ["degrader", "beamline"], "Unknown table"),
])
def test_refusals(config, current, match):
    with pytest.raises(CommandError, match=match):
        midas_commands.schedule_configuration(request(config, current), iface=StubIface())


def test_refusal_reaches_the_page_as_an_envelope():
    reply = json.loads(midas_commands.call(None, "generate_sequence",
                                           json.dumps(request(["target_position:1"], ["degrader"]))))
    assert reply["ok"] is False
    assert reply["error"]["kind"] == "usage"
    assert "beamline" in reply["error"]["message"]


# ---------------------------------------------------------------------------
# "Settings from run N": a beamline configuration made at schedule time
# ---------------------------------------------------------------------------

from test_restore import client_for, dump    # noqa: E402  (the restore stand-ins)


class RecordingIface(StubIface):
    """Also stores new configurations, with ids from 900 up, and records which
    were marked do_not_use.  Configurations made here are never read back."""

    def __init__(self, fail_after_runs=None):
        super().__init__()
        self.added = []
        self.do_not_use = []
        self.fail_after_runs = fail_after_runs

    def add_new_configuration(self, table, values, comment="Mystery Configuration"):
        self.added.append((table, dict(values), comment))
        return 900 + len(self.added) - 1

    def load_config(self, name, config_id):
        assert int(config_id) < 900, "a configuration made just now is scheduled by id"
        return super().load_config(name, config_id)

    def schedule_new_run(self, num_ev, configs, author, note, quality=None):
        if self.fail_after_runs is not None and len(self.runs) >= self.fail_after_runs:
            raise RuntimeError("database went away")
        return super().schedule_new_run(num_ev, configs, author, note, quality)

    def set_do_not_use(self, config_id, do_not_use=True):
        self.do_not_use.append(config_id)


@pytest.fixture
def epics_client(tmp_path):
    (tmp_path / "run00510.json").write_text(json.dumps(dump(510)))
    return client_for(tmp_path)


def from_run_request(config, current=(), run=510, include=(), table="pie5_epics"):
    req = request(config, current)
    req["from_run"] = {"run": run, "include": list(include), "table": table}
    return req


def test_from_run_is_stored_and_scheduled_as_a_beamline_config(epics_client):
    iface = RecordingIface()
    data = midas_commands.schedule_configuration(
        from_run_request(["target_position:1", "target_position:2", "degrader_position:10"],
                         include=["slits"]),
        iface=iface, client=epics_client)
    ((table, values, comment),) = iface.added
    assert table == "pie5_epics"
    assert values == {"QSF41:SOL:2": -93.27, "HSC41:SOL:2": -40.0, "FS42-V:SOL": 10.0,
                      "SEP41:SOL:2": -51.0, "SEP41VHVN:SOLV:2": 190.0}
    assert comment.startswith("from run 510 (") and comment.endswith("; SEP41/SEP41-HV kept)")
    assert data["from_run_config"] == 900
    assert [c for c, _ in iface.runs] == [[1, 10, 900], [2, 10, 900]]
    assert epics_client.writes == []                  # scheduled, not applied


def test_from_run_sits_alongside_ticked_beamline_configs(epics_client):
    iface = RecordingIface()
    midas_commands.schedule_configuration(
        from_run_request(["pie5_epics:100"], current=["target", "degrader"]),
        iface=iface, client=epics_client)
    assert sorted(c for c, _ in iface.runs) == [[100], [900]]


def test_from_run_excluded_groups_take_the_live_demand_at_schedule_time(epics_client):
    epics_client.odb["/Equipment/EPICS/Variables/Demand"][4] = 33.0     # FS42-V moved since
    iface = RecordingIface()
    midas_commands.schedule_configuration(from_run_request([], current=["target", "degrader"]),
                                          iface=iface, client=epics_client)
    ((_, values, _),) = iface.added
    assert values["FS42-V:SOL"] == 33.0 and values["QSF41:SOL:2"] == -93.27


@pytest.mark.parametrize("change, match", [
    ({"current": ["target", "degrader", "beamline"]}, "settings from run 510 ticked and marked current"),
    ({"from_run": {"run": 9999, "include": [], "table": "pie5_epics"}}, "no ODB dump for run 9999"),
    ({"from_run": {"run": "", "include": [], "table": "pie5_epics"}}, "give the run number"),
    ({"from_run": {"run": True, "include": [], "table": "pie5_epics"}}, "give the run number"),
    ({"from_run": {"run": 1569.5, "include": [], "table": "pie5_epics"}}, "give the run number"),
    ({"from_run": {"run": 510, "include": ["bogus"], "table": "pie5_epics"}}, "unknown group"),
    ({"from_run": {"run": 510, "include": [], "table": "target_position"}}, "unknown beamline table"),
])
def test_from_run_refusals_write_no_configuration(epics_client, change, match):
    req = from_run_request([], current=["target", "degrader"])
    req.update(change)
    iface = RecordingIface()
    with pytest.raises(CommandError, match=match):
        midas_commands.schedule_configuration(req, iface=iface, client=epics_client)
    assert iface.added == [] and iface.runs == []


def test_from_run_refusal_reaches_the_page_as_an_envelope(epics_client):
    req = from_run_request([], current=["target", "degrader"], run=9999)
    reply = json.loads(midas_commands.call(epics_client, "generate_sequence", json.dumps(req)))
    assert reply["ok"] is False and reply["error"]["kind"] == "usage"
    assert "no ODB dump for run 9999" in reply["error"]["message"]


def test_from_run_config_is_scheduled_like_a_selected_row(epics_client):
    """Selected rows reach the run as the dict load_config returns; only its id is
    used (midas_run.schedule), so the new row is added as {"id": ...}."""
    iface = RecordingIface()
    midas_commands.schedule_configuration(
        from_run_request(["pie5_epics:100"], current=["target", "degrader"]),
        iface=iface, client=epics_client)
    assert sorted(c for c, _ in iface.runs) == [[100], [900]]
    assert iface.do_not_use == []


def test_failure_before_any_run_names_the_row_and_marks_it_do_not_use(epics_client):
    iface = RecordingIface(fail_after_runs=0)
    with pytest.raises(CommandError) as err:
        midas_commands.schedule_configuration(
            from_run_request(["target_position:1"], current=["degrader"]),
            iface=iface, client=epics_client)
    assert err.value.kind == "internal"
    assert "stored as configuration 900 but not scheduled" in err.value.message
    assert "marked do_not_use" in err.value.message and "database went away" in err.value.message
    assert iface.do_not_use == [900] and iface.runs == []


def test_failure_after_some_runs_names_the_row_but_leaves_it_usable(epics_client):
    iface = RecordingIface(fail_after_runs=1)
    with pytest.raises(CommandError) as err:
        midas_commands.schedule_configuration(
            from_run_request(["target_position:1", "target_position:2"], current=["degrader"]),
            iface=iface, client=epics_client)
    assert "stored as configuration 900 but not scheduled (1 run(s) were scheduled" in err.value.message
    assert iface.do_not_use == [] and len(iface.runs) == 1
