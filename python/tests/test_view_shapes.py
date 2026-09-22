"""Every view returns what the JSON contract says, against a seeded database.

The page draws whatever comes out of these methods, so what is checked here is
shape and order: a missing key or a queue in the wrong order is a page that
misleads a shifter.
"""

import json
import re
from pathlib import Path

import pytest

from pioneer.rundb.view import NO_CONFIG_NOTE, RunDbView, ViewError


def roundtrip(data):
    """Everything a view returns must survive json.dumps unchanged."""
    return json.loads(json.dumps(data))


def test_status(view, seeded):
    data = roundtrip(view.status(actions_allowed=False, actions_built=False))

    assert data["client"]["actions_allowed"] is False
    assert data["client"]["actions_built"] is False
    assert data["database"]["reachable"] is True
    assert data["database"]["read_only"] is True
    assert "password=pioneer" not in data["database"]["dsn"]

    counts = data["counts"]
    assert counts["runs_total"] >= 10
    assert counts["queue_pending"] >= 6      # five queued runs plus the one on hold
    assert counts["queue_running"] >= 1
    assert counts["sequences_open"] >= 1
    assert counts["jobs_failed"] >= 1

    # The status table is passed on as it stands: the name the database uses,
    # its description and the five flags.  Nothing is renamed here.
    statuses = {row["name"]: row for row in data["statuses"]}
    assert set(statuses) >= {"PENDING", "HOLDING", "RUNNING", "DONE",
                             "CANCELLED", "FAILED"}
    assert set(statuses["PENDING"]) == {"name", "description", "issuccess",
                                        "isfailure", "ispending", "isrunning",
                                        "isuser"}
    assert statuses["PENDING"]["ispending"] is True
    assert statuses["HOLDING"]["isuser"] is True
    assert statuses["RUNNING"]["isrunning"] is True
    assert statuses["DONE"]["issuccess"] is True
    assert statuses["FAILED"]["isfailure"] is True
    assert all("word" not in row for row in data["statuses"])


def test_runlog_rows(view, seeded):
    data = roundtrip(view.runlog(limit=200))
    runs = {row["id"]: row for row in data["runs"]}

    ids = [row["id"] for row in data["runs"]]
    assert ids == sorted(ids, reverse=True)

    for run_id in seeded["finished_run_ids"]:
        row = runs[run_id]
        assert row["configs"] == []
        assert row["config_note"] == NO_CONFIG_NOTE
        assert row["status"] == "DONE"
        assert row["times_known"] is True
        assert row["duration_s"] == seeded["durations"][row["run_number"]]
        assert row["started"] < row["stopped"]
        assert row["files"], "a finished run should have a file"

    running = runs[seeded["running_run_id"]]
    assert running["status"] == "RUNNING"
    assert running["times_known"] is True
    assert running["stopped"] is None
    assert running["duration_s"] is None

    queued = runs[seeded["sequence_run_ids"][0]]
    assert queued["config_note"] is None
    assert queued["run_number"] is None
    types = {item["config_type"] for item in queued["configs"]}
    assert types == {"target_position", "degrader_position", "pim1_epics"}
    summaries = {item["config_type"]: item["summary"] for item in queued["configs"]}
    assert summaries["target_position"].startswith("target x=")
    assert "mm" in summaries["degrader_position"]
    assert queued["sequence"]["id"] == seeded["sequence_id"]


def test_runlog_paging(view, seeded):
    first = view.runlog(limit=3)
    assert first["next_before_id"] == first["runs"][-1]["id"]

    second = view.runlog(limit=3, before_id=first["next_before_id"])
    first_ids = [row["id"] for row in first["runs"]]
    second_ids = [row["id"] for row in second["runs"]]

    assert not set(first_ids) & set(second_ids)
    assert max(second_ids) < min(first_ids)
    assert second_ids == sorted(second_ids, reverse=True)


def test_runlog_last_page_has_no_next(view):
    data = view.runlog(limit=200)
    assert data["next_before_id"] is None


def test_queue_order_and_counts(view, seeded):
    data = roundtrip(view.queue(limit=200))
    rows = data["runs"]

    assert [row["position"] for row in rows] == list(range(1, len(rows) + 1))
    assert all(row["status"] in ("PENDING", "RUNNING", "HOLDING") for row in rows)
    assert all("status_word" not in row for row in rows)

    # Running first, then by priority.
    running = [row for row in rows if row["status"] == "RUNNING"]
    assert rows[: len(running)] == running

    priorities = [row["priority"] for row in rows[len(running):]]
    assert priorities == sorted(priorities)

    # Counted under the names the database uses, not under words of ours.
    assert data["counts"]["RUNNING"] >= 1
    assert data["counts"]["HOLDING"] == 1
    assert data["counts"]["PENDING"] == len(seeded["sequence_run_ids"])

    assert data["next_up"] == seeded["sequence_run_ids"][0]
    on_hold = [row for row in rows if row["id"] == seeded["holding_run_id"]]
    assert on_hold and on_hold[0]["status"] == "HOLDING"


def test_run_detail_has_values(view, seeded):
    data = roundtrip(view.run(seeded["sequence_run_ids"][0]))

    by_type = {item["config_type"]: item for item in data["configs"]}
    target = by_type["target_position"]
    assert set(target["values"]) >= {"id", "seq_id", "xpos", "ypos"}
    assert target["values"]["seq_id"] == 2

    beam = by_type["pim1_epics"]
    assert beam["values"] is not None
    assert len(beam["values"]) > 5

    assert data["sequence"]["id"] == seeded["sequence_id"]
    member_ids = [member["id"] for member in data["sequence"]["runs"]]
    assert member_ids == sorted(seeded["sequence_run_ids"])
    assert data["sequence"]["counts"] == {"PENDING": len(member_ids)}


def test_run_detail_of_a_finished_run(view, seeded):
    data = roundtrip(view.run(seeded["finished_run_ids"][1]))

    assert data["run"]["config_note"] == NO_CONFIG_NOTE
    assert data["configs"] == []
    assert [item["status"] for item in data["files"]] == ["FAILED"]

    jobs = {item["job_type"]: item for item in data["jobs"]}
    assert jobs["backup"]["status"] == "FAILED"
    assert all("id" in item for item in data["jobs"])
    assert all("status_word" not in item for item in data["jobs"])


def test_unknown_config_type_gives_no_values(view, seeded):
    data = roundtrip(view.run(seeded["holding_run_id"]))
    by_id = {item["config_id"]: item for item in data["configs"]}

    unknown = by_id[seeded["unknown_config_id"]]
    assert unknown["values"] is None
    assert unknown["summary"].endswith(str(seeded["unknown_config_id"]))

    known = by_id[seeded["config_ids"]["target"]]
    assert known["values"] is not None


def test_a_half_filled_position_does_not_break_the_summary(view, seeded):
    """One missing coordinate is shown as a question mark, not an exception."""
    data = roundtrip(view.run(seeded["holding_run_id"]))
    by_id = {item["config_id"]: item for item in data["configs"]}

    half = by_id[seeded["config_ids"]["half_target"]]
    assert half["summary"] == "target x=5 y=? mm"
    assert half["values"]["ypos"] is None

    # And the same row goes through the runlog and the queue unharmed.
    for rows in (view.runlog(limit=200)["runs"], view.queue(limit=200)["runs"]):
        row = next(r for r in rows if r["id"] == seeded["holding_run_id"])
        assert "target x=5 y=? mm" in [item["summary"] for item in row["configs"]]


def test_missing_run_is_a_usage_error(view):
    with pytest.raises(ViewError) as caught:
        view.run(10_000_000)
    assert caught.value.kind == "usage"


def test_sequences(view, seeded):
    data = roundtrip(view.sequences(limit=20))
    rows = {row["id"]: row for row in data["sequences"]}

    row = rows[seeded["sequence_id"]]
    assert row["n_runs"] == len(seeded["sequence_run_ids"])
    # Members counted under the database's own status names.
    assert row["counts"] == {"PENDING": len(seeded["sequence_run_ids"])}
    assert row["first_run"] is None      # not one of them has started
    assert row["on_complete"] == "merge mt_add"
    assert row["status"] == "PENDING"
    assert not any(key.startswith("n_") and key != "n_runs" for key in row)


def test_config_view(view, seeded):
    data = roundtrip(view.config(seeded["config_ids"]["degrader"]))
    item = data["config"]

    assert item["config_type"] == "degrader_position"
    assert item["known_type"] is True
    assert item["do_not_use"] is False
    assert "xpos" in item["values"]

    unknown = roundtrip(view.config(seeded["unknown_config_id"]))["config"]
    assert unknown["known_type"] is False
    assert unknown["values"] is None

    with pytest.raises(ViewError) as caught:
        view.config(10_000_000)
    assert caught.value.kind == "usage"


def test_finished_run_times_are_cached(fresh_db, seeded):
    """The BOR/EOR lookup is a scan, so a run that is over is asked about once.

    A run that is over and has its end-of-run row is kept for good; a run that
    is still going is kept for a few seconds only, because its times move.
    """
    own = RunDbView(dsn=fresh_db)
    try:
        own.runlog(limit=200)
        for number in seeded["finished_run_numbers"]:
            assert own._times[number][2] is None

        running = own._times[seeded["running_run_number"]]
        assert running[1] is None
        assert running[2] is not None
    finally:
        own.close()


def test_no_select_star_on_configuration_tables():
    """Configuration columns are named, never taken with SELECT *.

    The column names come out of the catalogue, which is what keeps a
    `config_type` read from a data row out of the query text.
    """
    source = (Path(__file__).resolve().parents[1]
              / "pioneer" / "rundb" / "view.py").read_text()
    assert re.search(r"select\s+\*", source, re.IGNORECASE) is None
