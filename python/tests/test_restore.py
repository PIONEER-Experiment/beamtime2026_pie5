"""The ConfigDB page's "restore beamline settings of run N": preview, load,
groups, refusals, warnings, and the schedulable configuration.

A stand-in MIDAS client holds the live ODB in a dict, and the old run's ODB
dump is a runNNNNN.json in a tmp data directory that /Logger/Data dir points
at, so none of this needs an experiment -- only the `midas` python package,
which midas_commands imports.  The last tests use two real end-of-run dumps
from the online machine, trimmed to the EPICS keys (tests/restore_dumps).
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("midas", reason="needs /software/midas/python on PYTHONPATH")

from pioneer.rundb import goto, midas_commands, restore   # noqa: E402

EPICS = "/Equipment/EPICS"
DUMPS = Path(__file__).resolve().parent / "restore_dumps"

# The real channel layout in miniature: a read-only value, three magnets of
# which one is the SEP41 coil, a slit, the beam blocker and its read-back
# (sharing a CA name), and the separator HV with its current read-back.
#         CA name      suffix     type thr   now Demand  now Meas.
CHANNELS = [
    ("AHSW41",    "",        6, 0.5,   0.0,   97.0),
    ("QSF41",     ":SOL:2",  1, 0.5, -220.0, -220.0),
    ("HSC41",     ":SOL:2",  1, 0.5,  -40.0,  -40.0),
    ("SEP41",     ":SOL:2",  1, 0.5,  -51.0,  -51.0),
    ("FS42-V",    ":SOL",    5, 2.0,   50.0,   50.2),
    ("KSF41",     ":COM:2",  2, 0.5,    0.0,    0.0),
    ("KSF41",     "",        3, 0.5,  120.0,    1.0),
    ("SEP41VHVN", ":SOLV:2", 4, 0.5,  190.0,  190.0),
    ("SEP41VHVN", ":SOLI:2", 6, 0.5,   50.0,    8.5),
]
IDX = {"QSF41": 1, "HSC41": 2, "SEP41": 3, "FS42-V": 4, "KSF41": 5, "SEP41VHVN": 7}

# The old run: every writeable channel differs from now except HSC41.
OLD = {"AHSW41": (0.0, 230.0), "QSF41": (-93.27, -93.25), "HSC41": (-40.0, -40.02),
       "SEP41": (-19.2, -19.22), "FS42-V": (10.0, 9.8), "KSF41": (1.0, 1.0),
       "SEP41VHVN": (150.0, 150.0)}


def dump(run, old=OLD, channels=CHANNELS):
    """An ODB dump dict of run `run`, Demand/Measured from `old` by CA name."""
    names = [c[0] for c in channels]
    vals = [old.get(c[0], (c[4], c[5])) for c in channels]
    return {"Runinfo": {"Run number": run},
            "Equipment": {"EPICS": {
                "Settings": {"CA Name": names, "CA Demand": [c[1] for c in channels],
                             "Device type": [c[2] for c in channels],
                             "Warning Threshold": [c[3] for c in channels],
                             "Unit": ["A"] * len(channels), "Allow write access": True},
                "Variables": {"Demand": [v[0] for v in vals], "Measured": [v[1] for v in vals]}}}}


class StubClient:
    def __init__(self, odb):
        self.odb = dict(odb)
        self.writes = []
        self.messages = []
        self.reads = {}

    def odb_get(self, path):
        if path not in self.odb:
            raise KeyError(path)
        self.reads[path] = self.reads.get(path, 0) + 1
        v = self.odb[path]
        return list(v) if isinstance(v, list) else v

    def odb_set(self, path, value):
        self.writes.append((path, value))
        self.odb[path] = value

    def msg(self, message, is_error=False, facility="midas"):
        self.messages.append((message, is_error))


def live_odb(data_dir, channels=CHANNELS, **extra):
    odb = {
        "/Runinfo/State": goto.STATE_STOPPED,
        "/PySequencer/State/Running": False,
        "/Logger/Data dir": str(data_dir),
        EPICS + "/Settings/CA Name": [c[0] for c in channels],
        EPICS + "/Settings/CA Demand": [c[1] for c in channels],
        EPICS + "/Settings/Device type": [c[2] for c in channels],
        EPICS + "/Settings/Warning Threshold": [c[3] for c in channels],
        EPICS + "/Settings/Unit": ["A"] * len(channels),
        EPICS + "/Settings/Allow write access": True,
        EPICS + "/Variables/Demand": [c[4] for c in channels],
        EPICS + "/Variables/Measured": [c[5] for c in channels],
    }
    odb.update(extra)
    return odb


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "run00510.json").write_text(json.dumps(dump(510)))
    return tmp_path


def client_for(data_dir, **extra):
    return StubClient(live_odb(data_dir, **extra))


def written(client):
    """The Demand array of the one write, or None."""
    if not client.writes:
        return None
    ((path, demand),) = client.writes
    assert path == EPICS + "/Variables/Demand"
    return demand


PRE_IMAGE = [c[4] for c in CHANNELS]      # the live Demand before any write


def assert_only(demand, expected, pre=PRE_IMAGE):
    """The whole written array is the pre-image except at `expected` (name -> value),
    read-only twins and the blocker included."""
    want = list(pre)
    for name, value in expected.items():
        want[IDX[name]] = value
    assert demand == want


# ---------------------------------------------------------------- preview / load

def test_preview_writes_nothing_and_tags_groups(data_dir):
    client = client_for(data_dir)
    p = restore.preview(client, 510)
    assert client.writes == [] and client.messages == []
    assert p["dump"] == "run00510.json (end of run)" and p["source"] == "auto"
    groups = {r["name"]: (r["group"], r["included"]) for r in p["rows"]}
    assert groups == {"QSF41": ("magnets", True), "HSC41": ("magnets", True),
                      "SEP41": ("sep41", False), "FS42-V": ("slits", False),
                      "SEP41VHVN": ("sep41_hv", False)}
    # only the included rows count; HSC41 is already there
    assert p["n_changed"] == 1 and p["n_included"] == 2
    assert p["warnings"] == []


def test_default_restores_magnets_but_not_sep41_slits_or_hv(data_dir):
    client = client_for(data_dir)
    done = restore.load(client, 510)
    demand = written(client)
    assert_only(demand, {"QSF41": -93.27})
    assert demand[IDX["QSF41"]] == -93.27
    assert demand[IDX["SEP41"]] == -51.0            # coil left alone
    assert demand[IDX["FS42-V"]] == 50.0            # slit left alone
    assert demand[IDX["SEP41VHVN"]] == 190.0        # HV left alone
    assert demand[IDX["KSF41"]] == 0.0              # blocker never
    assert done["written"] == ["QSF41"]
    (line, is_error), = client.messages
    assert not is_error
    assert "1 EPICS channel set from run 510" in line and line.endswith("restored magnets")


@pytest.mark.parametrize("group, name, value", [
    ("slits", "FS42-V", 10.0), ("sep41", "SEP41", -19.2), ("sep41_hv", "SEP41VHVN", 150.0)])
def test_each_group_written_only_when_included(data_dir, group, name, value):
    client = client_for(data_dir)
    done = restore.load(client, 510, [group])
    demand = written(client)
    assert_only(demand, {"QSF41": -93.27, name: value})
    assert demand[IDX[name]] == value
    others = {"FS42-V": 50.0, "SEP41": -51.0, "SEP41VHVN": 190.0}
    del others[name]
    assert all(demand[IDX[n]] == v for n, v in others.items())
    assert sorted(done["written"]) == sorted(["QSF41", name])
    assert restore.LABELS[group] in client.messages[0][0]


def test_all_groups(data_dir):
    client = client_for(data_dir)
    done = restore.load(client, 510, ["sep41_hv", "slits", "sep41"])
    assert done["include"] == ["slits", "sep41", "sep41_hv"]       # GROUPS order
    assert len(done["written"]) == 4
    assert_only(written(client), {"QSF41": -93.27, "FS42-V": 10.0, "SEP41": -19.2, "SEP41VHVN": 150.0})
    assert client.messages[0][0].endswith("restored magnets, slits, SEP41, SEP41-HV")


@pytest.mark.parametrize("include", [["bogus"], "slits", 3])
def test_unknown_group_refused(data_dir, include):
    client = client_for(data_dir)
    with pytest.raises(goto.GotoError) as err:
        restore.preview(client, 510, include)
    assert err.value.kind == "usage"
    with pytest.raises(goto.GotoError):
        restore.load(client, 510, include)
    assert client.writes == []


def test_auto_uses_measured_where_demand_was_off(tmp_path):
    # QSF41's Demand 1.4x its Measured in the old run, as has been seen
    old = dict(OLD, QSF41=(-130.6, -93.25))
    (tmp_path / "run00510.json").write_text(json.dumps(dump(510, old)))
    p = restore.preview(client_for(tmp_path), 510)
    row = next(r for r in p["rows"] if r["name"] == "QSF41")
    assert row["overruled"] and row["target"] == -93.25
    hsc = next(r for r in p["rows"] if r["name"] == "HSC41")
    assert not hsc["overruled"] and hsc["target"] == -40.0      # within threshold: Demand


def test_unchanged_run_writes_nothing(tmp_path):
    same = {c[0]: (c[4], c[5]) for c in reversed(CHANNELS)}     # first of a shared CA name wins
    (tmp_path / "run00510.json").write_text(json.dumps(dump(510, same)))
    client = client_for(tmp_path)
    p = restore.preview(client, 510, ["slits", "sep41", "sep41_hv"])
    assert p["n_changed"] == 0 and not any(r["changes"] for r in p["rows"])
    done = restore.load(client, 510, ["slits"])
    assert client.writes == [] and client.messages == []
    assert done["n_changed"] == 0 and done["arrival"] == []


def test_channel_missing_on_one_side(tmp_path):
    old_channels = [c for c in CHANNELS if c[0] != "HSC41"] + [("QSF99", ":SOL:2", 1, 0.5, 1.0, 1.0)]
    (tmp_path / "run00510.json").write_text(json.dumps(dump(510, channels=old_channels)))
    client = client_for(tmp_path)
    p = restore.preview(client, 510)
    assert p["only_old"] == ["QSF99"] and p["only_cur"] == ["HSC41"]
    assert "HSC41" not in [r["name"] for r in p["rows"]]
    restore.load(client, 510)
    assert_only(written(client), {"QSF41": -93.27})


def test_write_access_off_warns_in_preview_and_refuses_load(data_dir):
    client = client_for(data_dir, **{EPICS + "/Settings/Allow write access": False})
    p = restore.preview(client, 510)
    assert any("Allow write access is off" in w for w in p["warnings"])
    with pytest.raises(goto.GotoError) as err:
        restore.load(client, 510)
    assert err.value.kind == "denied"
    assert client.writes == []


def test_running_run_and_sequencer_warn_but_do_not_stop_the_load(data_dir):
    client = client_for(data_dir, **{"/Runinfo/State": goto.STATE_RUNNING,
                                     "/PySequencer/State/Running": True})
    assert len(restore.preview(client, 510)["warnings"]) == 2
    done = restore.load(client, 510)
    assert len(done["warnings"]) == 2 and client.writes
    assert "run is in progress" in client.messages[0][0]


def test_wrong_run_in_dump_is_a_usage_error(tmp_path):
    (tmp_path / "run00510.json").write_text(json.dumps(dump(511)))
    with pytest.raises(goto.GotoError) as err:
        restore.preview(client_for(tmp_path), 510)
    assert err.value.kind == "usage" and "belongs to run 511" in str(err.value)


def test_no_dump_is_a_usage_error(data_dir):
    with pytest.raises(goto.GotoError) as err:
        restore.preview(client_for(data_dir), 9999)
    assert err.value.kind == "usage" and "no ODB dump for run 9999" in str(err.value)


def test_device_type_changed_is_a_usage_error(tmp_path):
    changed = [c if c[0] != "QSF41" else ("QSF41", ":SOL", 5, 2.0, 0.0, 0.0) for c in CHANNELS]
    (tmp_path / "run00510.json").write_text(json.dumps(dump(510, channels=changed)))
    with pytest.raises(goto.GotoError) as err:
        restore.preview(client_for(tmp_path), 510)
    assert err.value.kind == "usage" and "device type" in str(err.value)


class MovingClient(StubClient):
    """Someone changes HSC41's Demand after the plan read it, before the write."""

    def odb_get(self, path):
        v = super().odb_get(path)
        if path == EPICS + "/Variables/Demand" and self.reads[path] == 1:
            self.odb[path] = list(self.odb[path])
            self.odb[path][IDX["HSC41"]] = -12.0
        return v


def test_demand_is_read_again_just_before_the_write(data_dir):
    client = MovingClient(live_odb(data_dir))
    restore.load(client, 510)
    demand = written(client)
    assert_only(demand, {"QSF41": -93.27, "HSC41": -12.0})
    assert demand[IDX["HSC41"]] == -12.0       # not reset to what the plan saw
    assert demand[IDX["QSF41"]] == -93.27


def test_arrival_in_goto_format(data_dir):
    done = restore.load(client_for(data_dir), 510, ["slits"])
    assert done["label"] == "the beamline settings of run 510"
    assert done["stable_for"] == 5 and done["timeout"] == 120
    assert sorted((a["path"], a["op"], a["target"], a["upper"]) for a in done["arrival"]) == [
        (EPICS + "/Variables/Measured[1]", "range", -93.77, -92.77),
        (EPICS + "/Variables/Measured[4]", "range", 8.0, 12.0)]


# ---------------------------------------------------------------- the RPC envelopes

def test_rpc_envelopes(data_dir):
    client = client_for(data_dir)
    ok = json.loads(midas_commands.call(client, "restore_preview", json.dumps({"run": 510, "include": ["slits"]})))
    assert ok["ok"] and ok["data"]["n_changed"] == 2

    bad = json.loads(midas_commands.call(client, "restore_run", json.dumps({"run": "x"})))
    assert not bad["ok"] and bad["error"]["kind"] == "usage"

    unknown = json.loads(midas_commands.call(client, "restore_run", json.dumps({"run": 510, "include": ["nope"]})))
    assert not unknown["ok"] and unknown["error"]["kind"] == "usage"

    missing = json.loads(midas_commands.call(client, "restore_preview", {"run": 9999}))
    assert not missing["ok"] and "no ODB dump" in missing["error"]["message"]
    assert client.writes == []

    client.odb[EPICS + "/Settings/Allow write access"] = False
    denied = json.loads(midas_commands.call(client, "restore_run", json.dumps({"run": 510})))
    assert not denied["ok"] and denied["error"]["kind"] == "denied"
    assert client.writes == []

    client.odb[EPICS + "/Settings/Allow write access"] = True
    done = json.loads(midas_commands.call(client, "restore_run", json.dumps({"run": 510})))
    assert done["ok"] and done["data"]["written"] == ["QSF41"]


def test_restore_commands_are_routed_to_the_midas_client():
    assert {"restore_preview", "restore_run"} <= set(midas_commands.MidasCommands)


# ---------------------------------------------------------------- config_values

def test_config_values_is_complete_and_keyed_like_write_epics(data_dir):
    client = client_for(data_dir)
    values, comment = restore.config_values(client, 510, ["slits"])
    assert values == {"QSF41:SOL:2": -93.27, "HSC41:SOL:2": -40.0,   # from the run
                      "FS42-V:SOL": 10.0,                           # ticked: from the run
                      "SEP41:SOL:2": -51.0, "SEP41VHVN:SOLV:2": 190.0}   # live Demand
    assert comment == "from run 510 (run00510.json (end of run), auto; SEP41/SEP41-HV kept)"
    assert client.writes == []


def test_config_values_all_groups_comment(data_dir):
    _, comment = restore.config_values(client_for(data_dir), 510, ["slits", "sep41", "sep41_hv"])
    assert comment == "from run 510 (run00510.json (end of run), auto)"


# ---------------------------------------------------------------- real dumps

def real_client(now_run):
    d = json.loads((DUMPS / f"run{now_run:05d}.json").read_text())
    s, v = d["Equipment"]["EPICS"]["Settings"], d["Equipment"]["EPICS"]["Variables"]
    odb = {"/Runinfo/State": goto.STATE_STOPPED, "/PySequencer/State/Running": False,
           "/Logger/Data dir": str(DUMPS)}
    odb.update({f"{EPICS}/Settings/{k}": val for k, val in s.items()})
    odb.update({f"{EPICS}/Variables/{k}": val for k, val in v.items()})
    return StubClient(odb)


def test_real_dumps_page_default():
    p = restore.preview(real_client(1842), 1569)
    # 22 type-1 channels less SEP41; HSC42 is 0 in both runs
    assert p["n_included"] == 21 and p["n_changed"] == 20
    excluded = {r["name"]: r["group"] for r in p["rows"] if not r["included"]}
    assert excluded["SEP41"] == "sep41" and excluded["SEP41VHVP"] == "sep41_hv"
    assert sum(g == "slits" for g in excluded.values()) == 7
    assert "KSF41" not in excluded and "AHSW41" not in excluded


def test_real_dumps_same_run_changes_nothing():
    p = restore.preview(real_client(1842), 1842, ["slits", "sep41", "sep41_hv"])
    assert p["n_changed"] == 0 and p["n_included"] == 31


def test_real_dumps_config_values_through_the_sequencer_loader_match_load():
    """What the sequencer would load from the stored configuration is what a
    restore now writes, for no group and for every group."""
    from pioneer.sequencer import config_loader

    class StubSeq(StubClient):
        pass

    for include in ([], ["slits", "sep41", "sep41_hv"]):
        values, _ = restore.config_values(real_client(1842), 1569, include)
        seq = StubSeq(real_client(1842).odb)
        config_loader.load_beam_config(seq, "pie5_epics", values)
        ((_, via_sequencer),) = seq.writes

        now = real_client(1842)
        restore.load(now, 1569, include)
        via_restore = written(now)
        assert via_sequencer == pytest.approx(via_restore, rel=1e-6, abs=1e-9), include


# ---------------------------------------------------------------- strictness

@pytest.mark.parametrize("value, run", [(510, 510), (510.0, 510), ("510", 510), (" 510 ", 510)])
def test_parse_run_accepts(value, run):
    assert restore.parse_run(value) == run


@pytest.mark.parametrize("value", [True, False, 510.5, "510.0", "510abc", "", "-5", 0, -3, None, [510]])
def test_parse_run_refuses(value):
    with pytest.raises(goto.GotoError) as err:
        restore.parse_run(value)
    assert err.value.kind == "usage"


@pytest.mark.parametrize("run", [True, 510.5, "510abc", 0])
def test_rpc_refuses_a_bad_run(data_dir, run):
    client = client_for(data_dir)
    reply = json.loads(midas_commands.call(client, "restore_preview", {"run": run}))
    assert not reply["ok"] and reply["error"]["kind"] == "usage"


def test_group_of_refuses_a_type_in_no_group():
    assert restore.group_of({"name": "QSF41", "type": 1}) == "magnets"
    assert restore.group_of({"name": "SEP41", "type": 1}) == "sep41"
    with pytest.raises(goto.GotoError):
        restore.group_of({"name": "NEW41", "type": 7})


class MutedClient(StubClient):
    def msg(self, message, is_error=False, facility="midas"):
        raise RuntimeError("message log down")


def test_a_failing_message_log_does_not_fail_a_done_write(data_dir, capsys):
    client = MutedClient(live_odb(data_dir))
    done = restore.load(client, 510)
    assert done["written"] == ["QSF41"] and written(client) is not None
    assert "message log down" in capsys.readouterr().err


# ---------------------------------------------------------------- the manual path

def test_cli_arguments():
    a = restore._parse_args(["1569", "--include", "sep41_hv, slits"])
    assert a.run == 1569 and a.include == ("slits", "sep41_hv") and not a.store_config
    a = restore._parse_args(["1569", "--store-config", "--write-dsn", "dbname=x", "--table", "pim1_epics"])
    assert a.store_config and a.table == "pim1_epics" and a.include == ()


@pytest.mark.parametrize("argv", [
    ["15x"], ["0"], ["1569", "--include", "bogus"], ["1569", "--store-config"],
    ["1569", "--write-dsn", "dbname=x"], ["1569", "--store-config", "--write-dsn", "x", "--table", "target_position"]])
def test_cli_refusals(argv):
    with pytest.raises(SystemExit) as err:
        restore._parse_args(argv)
    assert err.value.code == 2


def test_preview_text_lists_both_tables(data_dir):
    text = restore.preview_text(restore.preview(client_for(data_dir), 510, ["slits"]))
    assert "Restoring: magnets, slits." in text
    assert "2 of 3 restored channel(s) change." in text
    assert "[SEP41-HV] differs, kept as now" in text


def test_config_values_comment_is_what_the_page_hides(data_dir):
    """The ConfigDB page hides "from run N" configurations by FROM_RUN_COMMENT
    (custom/js/cfgdb.js); the comment written here has to keep matching it."""
    import re
    js = (Path(__file__).resolve().parents[2] / "custom" / "js" / "cfgdb.js").read_text()
    pattern = re.search(r"const FROM_RUN_COMMENT = /(.*)/;", js).group(1)
    for include in ([], ["slits", "sep41", "sep41_hv"]):
        _, comment = restore.config_values(client_for(data_dir), 510, include)
        assert re.match(pattern, comment), (pattern, comment)
