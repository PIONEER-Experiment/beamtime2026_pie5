"""The ConfigDB page's "go to": preview, load, refusals, warnings.

A stand-in MIDAS client holds a small ODB in a dict and a stand-in view hands
out configuration rows, so none of this needs an experiment or a database --
only the `midas` python package, which `config_loader` imports.
"""

import json

import pytest

pytest.importorskip("midas", reason="needs /software/midas/python on PYTHONPATH")

from pioneer.rundb import goto, midas_commands      # noqa: E402
from pioneer.rundb.view import ViewError            # noqa: E402

EPICS = "/Equipment/EPICS"


class StubClient:
    def __init__(self, odb):
        self.odb = dict(odb)
        self.writes = []
        self.messages = []

    def odb_get(self, path):
        if path not in self.odb:
            raise KeyError(path)
        return self.odb[path]

    def odb_set(self, path, value):
        self.writes.append((path, value))
        self.odb[path] = value

    def msg(self, message, is_error=False, facility="midas"):
        self.messages.append((message, is_error))


class StubView:
    def __init__(self, rows):
        self.rows = rows

    def config(self, config_id):
        if config_id not in self.rows:
            raise ViewError("usage", f"no configuration with id {config_id}")
        return {"config": self.rows[config_id]}


def row(config_id, config_type, values, do_not_use=False):
    return {"config_id": config_id, "config_type": config_type, "do_not_use": do_not_use,
            "known_type": True, "values": dict(values, id=config_id, seq_id=0)}


ROWS = {
    3: row(3, "target_position", {"xpos": 5.0, "ypos": -5.0}),
    17: row(17, "degrader_position", {"xpos": 12.5}),
    460: row(460, "pie5_epics", {"QSK41:SOL:2": 101.0, "QSK42:SOL:2": 50.0,
                                 "FS41-L:SOL": 30.0, "KSF41:SOL": 1.0}),
    461: row(461, "pie5_epics", {"QSK41:SOL:2": 101.0}),       # misses channels
    18: row(18, "degrader_position", {"xpos": 1.0}, do_not_use=True),
}


def stopped_odb(**extra):
    odb = {
        "/Runinfo/State": goto.STATE_STOPPED,
        "/PySequencer/State/Running": False,
        "/Equipment/XYTable/Variables/Demand": [0.0, 0.0],
        "/Equipment/Degrader/Variables/Demand": 4.0,
        # magnet, magnet, slit, beam blocker
        EPICS + "/Settings/CA Name": ["QSK41:SOL", "QSK42:SOL", "FS41-L:SOL", "KSF41:SOL"],
        EPICS + "/Settings/CA Demand": [":2", ":2", "", ""],
        EPICS + "/Settings/Device type": [1, 1, 5, 2],
        EPICS + "/Settings/Warning Threshold": [0.5, 0.5, 1.0, 0.1],
        EPICS + "/Variables/Demand": [100.0, 50.0, 20.0, 0.0],
    }
    odb.update(extra)
    return odb


def test_preview_writes_nothing_and_lists_changes():
    client = StubClient(stopped_odb())
    p = goto.preview(client, StubView(ROWS), 3)
    assert client.writes == []
    assert p["warnings"] == []
    assert [(c["now"], c["new"], c["changes"]) for c in p["changes"]] == [
        (0.0, 5.0, True), (0.0, -5.0, True)]


def test_load_target_sets_demand_and_returns_arrival():
    client = StubClient(stopped_odb())
    done = goto.load(client, StubView(ROWS), 3)
    assert client.writes == [("/Equipment/XYTable/Variables/Demand", (5.0, -5.0))]
    assert [(a["path"], a["op"], a["target"]) for a in done["arrival"]] == [
        ("/Equipment/XYTable/Variables/Measured[0]", "==", 5.0),
        ("/Equipment/XYTable/Variables/Measured[1]", "==", -5.0)]
    assert done["timeout"] == 60
    assert "loaded configuration 3" in client.messages[0][0]


def test_load_degrader():
    client = StubClient(stopped_odb())
    done = goto.load(client, StubView(ROWS), 17)
    assert client.writes == [("/Equipment/Degrader/Variables/Demand", 12.5)]
    (a,) = done["arrival"]
    assert a["op"] == "between" and a["target"] < 12.5 < a["upper"]


def test_beamline_never_touches_the_beam_blocker():
    client = StubClient(stopped_odb())
    p = goto.preview(client, StubView(ROWS), 460)
    assert [c["name"] for c in p["changes"]] == ["QSK41:SOL:2", "QSK42:SOL:2", "FS41-L:SOL"]
    assert [c["changes"] for c in p["changes"]] == [True, False, True]

    done = goto.load(client, StubView(ROWS), 460)
    ((path, demand),) = client.writes
    assert path == EPICS + "/Variables/Demand"
    assert demand == [101.0, 50.0, 30.0, 0.0]          # blocker left at 0
    assert len(done["arrival"]) == 3 and done["stable_for"] == 5
    assert "2 settings changed" in client.messages[0][0]


def test_beamline_with_missing_channels_is_refused_and_logged():
    client = StubClient(stopped_odb())
    with pytest.raises(goto.GotoError) as err:
        goto.load(client, StubView(ROWS), 461)
    assert err.value.kind == "usage"
    assert client.writes == []
    assert client.messages[0][1] is True


@pytest.mark.parametrize("config_id, kind", [(18, "denied"), (999, "usage")])
def test_refusals(config_id, kind):
    client = StubClient(stopped_odb())
    with pytest.raises(goto.GotoError) as err:
        goto.preview(client, StubView(ROWS), config_id)
    assert err.value.kind == kind


def test_running_run_and_sequencer_warn_but_do_not_stop_the_load():
    client = StubClient(stopped_odb(**{"/Runinfo/State": goto.STATE_RUNNING,
                                       "/PySequencer/State/Running": True}))
    done = goto.load(client, StubView(ROWS), 17)
    assert len(done["warnings"]) == 2
    assert client.writes                                 # loaded anyway
    assert "run is in progress" in client.messages[0][0]


def test_rpc_envelopes():
    client = StubClient(stopped_odb())
    view = StubView(ROWS)
    ok = json.loads(midas_commands.call(client, "goto_preview", json.dumps({"config_id": 3}), view=view))
    assert ok["ok"] and ok["data"]["config_type"] == "target_position"

    bad = json.loads(midas_commands.call(client, "goto_config", json.dumps({"config_id": "x"}), view=view))
    assert not bad["ok"] and bad["error"]["kind"] == "usage"

    denied = json.loads(midas_commands.call(client, "goto_config", json.dumps({"config_id": 18}), view=view))
    assert not denied["ok"] and denied["error"]["kind"] == "denied"
    assert client.writes == []
