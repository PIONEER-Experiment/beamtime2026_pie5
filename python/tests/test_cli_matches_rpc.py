"""The command line and the custom page get the same answer.

The manual path only helps during a beamtime if it tells the truth about what
the page is showing, which means one code path and not two.  Here the CLI is
run as a separate process, exactly as a shifter would run it, and its `--json`
output is compared with what `commands.dispatch` produces in this process.

Three fields are allowed to differ: the time the reply was made, how long the
query took, and how long the client has been up -- none of which could be equal
across two processes, and all of which are the same data read twice.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pioneer.rundb import commands
from pioneer.rundb.view import RunDbView

PACKAGE_DIR = Path(__file__).resolve().parents[1]


def run_cli(dsn, *arguments):
    """Run the module's own command line and give back (exit code, stdout)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(PACKAGE_DIR), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, "-m", "pioneer.rundb.view", *arguments, "--dsn", dsn],
        capture_output=True, text=True, env=env, timeout=120,
    )
    return result


def normalise(envelope):
    """Remove what two processes cannot agree on."""
    envelope = json.loads(json.dumps(envelope))
    envelope.pop("generated", None)
    envelope.pop("query_ms", None)
    client = (envelope.get("data") or {}).get("client")
    if client is not None:
        client.pop("uptime_s", None)
    return envelope


@pytest.mark.parametrize("command, arguments, payload", [
    ("status", [], {}),
    ("runlog", ["--limit", "4"], {"limit": 4}),
    ("queue", [], {}),
    ("sequences", [], {}),
])
def test_cli_json_matches_dispatch(fresh_db, seeded, command, arguments, payload):
    result = run_cli(fresh_db, command, "--json", *arguments)
    assert result.returncode == 0, result.stderr

    view = RunDbView(dsn=fresh_db)
    try:
        expected = json.loads(commands.dispatch(view, None, command, json.dumps(payload)))
    finally:
        view.close()

    assert normalise(json.loads(result.stdout)) == normalise(expected)


def test_cli_run_and_config(fresh_db, seeded):
    run_id = seeded["sequence_run_ids"][0]
    result = run_cli(fresh_db, "run", str(run_id), "--json")
    assert result.returncode == 0, result.stderr

    view = RunDbView(dsn=fresh_db)
    try:
        expected = json.loads(
            commands.dispatch(view, None, "run", json.dumps({"id": run_id})))
        config_id = seeded["config_ids"]["degrader"]
        expected_config = json.loads(
            commands.dispatch(view, None, "config", json.dumps({"id": config_id})))
    finally:
        view.close()

    assert normalise(json.loads(result.stdout)) == normalise(expected)

    result = run_cli(fresh_db, "config", str(config_id), "--json")
    assert result.returncode == 0, result.stderr
    assert normalise(json.loads(result.stdout)) == normalise(expected_config)


def test_cli_tables_are_printed_without_json(fresh_db, seeded):
    """The default output is for reading, and says the things the page says."""
    result = run_cli(fresh_db, "runlog", "--limit", "10")
    assert result.returncode == 0, result.stderr
    assert "status" in result.stdout
    assert "no configuration recorded for this run" in result.stdout

    result = run_cli(fresh_db, "queue")
    assert result.returncode == 0, result.stderr
    assert "next up:" in result.stdout


def test_cli_reports_a_bad_id(fresh_db, seeded):
    result = run_cli(fresh_db, "run", "10000000")
    assert result.returncode == 1
    assert result.stderr.startswith("error (usage)")
    assert "no run with database id" in result.stderr


def test_cli_reports_an_unreachable_database():
    """With no database the command line still explains itself and exits 1."""
    result = run_cli("host=127.0.0.1 port=1 dbname=pioneer_rundb_test user=nobody",
                     "status")
    assert result.returncode == 0          # status is the one view that survives
    assert "not answering" in result.stdout

    result = run_cli("host=127.0.0.1 port=1 dbname=pioneer_rundb_test user=nobody",
                     "runlog")
    assert result.returncode == 1
    assert result.stderr.startswith("error (db)")
