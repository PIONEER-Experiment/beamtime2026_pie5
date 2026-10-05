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
