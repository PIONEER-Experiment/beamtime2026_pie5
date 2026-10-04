"""pioneer.nearline.requeue against a scratch run database.

The requeue has to leave exactly the rows the daemon's MIDAS callbacks leave,
so most tests build one run through the callback path (the interface calls
daemon.py makes) and one through the requeue, and compare. The database
tests need $PIONEER_RUNDB_TEST_DSN (see conftest.py) and skip without it;
the dump-parsing tests at the top need nothing.

Every test uses its own run numbers and a logger channel no other test
file uses, because the scratch database is shared by the whole session and
close_files_in_channel closes the open files of every run on its channel.
"""

import io
import json

import psycopg
import pytest

from pioneer.nearline import requeue
from pioneer.nearline.requeue import MidasState, main, read_run_json

# producer logger_7: no other test opens files on this channel
CHANNEL = 7


# ---------------------------------------------------------------------------
# mlogger dumps
# ---------------------------------------------------------------------------

def odb_entry(tree, name, value, tid = 7):
    """Add `name` to `tree` the way MIDAS writes it, with its "/key" sibling."""
    tree[f"{name}/key"] = {"type": tid, "access_mode": 7, "last_written": 1790000000}
    tree[name] = value


def write_dump(data_dir, number, start = "Sat Oct  3 22:50:19 2026", stop = "Sat Oct  3 22:55:01 2026",
               start_bin = "0x6ac0114a", stop_bin = "0x6ac01265", events = (1000, 234),
               quality = "", description = "a beam run", operator = "shifter"):
    runinfo, channels, info = {}, {}, {}
    odb_entry(runinfo, "State", 3)
    odb_entry(runinfo, "Run number", number)
    odb_entry(runinfo, "Start time", start, 12)
    odb_entry(runinfo, "Start time binary", start_bin, 6)
    odb_entry(runinfo, "Stop time", stop, 12)
    odb_entry(runinfo, "Stop time binary", stop_bin, 6)
    for i, n in enumerate(events):
        stats = {}
        odb_entry(stats, "Events written", n, 10)
        channels[str(i)] = {"Statistics": stats}
    odb_entry(info, "Operator", operator, 12)
    odb_entry(info, "Description", description, 12)
    odb_entry(info, "Quality", quality, 12)
    edit = {}
    # the daemon's links, as a dump writes them: their target path
    odb_entry(edit, "Quality", "/Nearline/Info/Quality", 16)
    tree = {"/MIDAS version": "2.1", "Runinfo": runinfo,
            "Logger": {"Data dir": str(data_dir), "Channels": channels},
            "Nearline": {"Info": info}, "Experiment": {"Edit on Start": edit}}
    path = data_dir / f"run{number:05d}.json"
    path.write_text(json.dumps(tree))
    return path


def write_raw(data_dir, number, subruns):
    names = []
    for s in subruns:
        name = f"run{number:05d}_{s:05d}.mid.lz4"
        (data_dir / name).write_bytes(b"raw")
        (data_dir / f"run{number:05d}_{s:05d}.mid.lz4.crc32c").write_bytes(b"0")
        names.append(name)
    return names


def test_dump_values(tmp_path):
    path = write_dump(tmp_path, 1106, quality = "Debug")
    rj = read_run_json(path)
    assert rj.number == 1106
    assert rj.start == "Sat Oct  3 22:50:19 2026"
    assert rj.stop == "Sat Oct  3 22:55:01 2026"
    assert rj.events == 1234
    assert rj.quality == "Debug"
    assert rj.description == "a beam run"
    assert rj.operator == "shifter"


def test_dump_written_at_the_start_has_no_stop(tmp_path):
    rj = read_run_json(write_dump(tmp_path, 1107, stop = "Sat Oct  3 20:00:00 2026", stop_bin = "0x00000000"))
    assert rj.stop is None


def test_dump_link_is_not_a_quality(tmp_path):
    path = write_dump(tmp_path, 1108)
    tree = json.loads(path.read_text())
    del tree["Nearline"]
    path.write_text(json.dumps(tree))
    rj = read_run_json(path)
    # only the link "/Nearline/Info/Quality" is left, which is not a value
    assert rj.quality is None
    assert rj.description is None


def test_dump_keys_are_case_insensitive(tmp_path):
    path = write_dump(tmp_path, 1109, quality = "NL Test")
    tree = json.loads(path.read_text())
    tree["nearline"] = tree.pop("Nearline")
    tree["nearline"]["info"] = tree["nearline"].pop("Info")
    path.write_text(json.dumps(tree))
    assert read_run_json(path).quality == "NL Test"


def test_missing_dump(tmp_path):
    assert read_run_json(tmp_path / "run01110.json") is None


def test_raw_files_on_disk(tmp_path):
    write_raw(tmp_path, 1111, [1, 0])
    (tmp_path / "run01111.mid.lz4").write_bytes(b"raw")
    (tmp_path / "run11111_00000.mid.lz4").write_bytes(b"raw")
    assert requeue.raw_files_on_disk(tmp_path, 1111) == [
        "run01111.mid.lz4", "run01111_00000.mid.lz4", "run01111_00001.mid.lz4"]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@pytest.fixture
def db(fresh_db):
    """(interface, connection) on the scratch database, with
    pioneer.rundb.config pointed at it for the length of the test."""
    from rundb_seed import _patch_config, _restore_config
    from pioneer.rundb.interface import interface

    params = psycopg.conninfo.conninfo_to_dict(fresh_db)
    saved = _patch_config(fresh_db)
    conn = psycopg.connect(fresh_db, autocommit = True)
    try:
        yield interface(user = params.get("user") or "postgres", password = params.get("password") or ""), conn
    finally:
        conn.close()
        _restore_config(saved)


# MIDAS answering and no run being taken: what --apply needs without --no-midas-check
STOPPED = MidasState(state = 1, run_number = 1, data_dir = "", run_db_pk = 0, dump_file = "")


def run(iface, *argv, midas_state = STOPPED):
    out = io.StringIO()
    rc = main([str(a) for a in argv], iface = iface, midas_state = midas_state, out = out)
    return rc, out.getvalue()


def table_row(text, number):
    """The summary table's row of run `number`, split into its columns:
    run, row, row action, files registered, jobs created, reset, present,
    result."""
    rows = [l.split() for l in text.splitlines() if l.split()[:1] == [str(number)]]
    assert len(rows) == 1, text
    return rows[0]


def run_row(conn, number):
    return conn.execute(
        "SELECT id, status, start_time, stop_time, recorded_events, quality FROM state.midas_run "
        "WHERE midas_run_number = %s ORDER BY id", (number,)).fetchall()


def jobs_of(conn, run_id):
    """The run's jobs, comparable between two runs: (client, job_type,
    filebase without the run number, status, dependencies in the same
    form), sorted."""
    rows = conn.execute(
        """
        SELECT j.id, j.client, j.job_type, substr(f.filebase, 9), j.status
        FROM state.postproc_job j LEFT JOIN state.file_list f ON f.id = j.file_id
        WHERE j.midas_run_id = %s
        """, (run_id,)).fetchall()
    label = {r[0]: (r[1], r[2], r[3]) for r in rows}
    deps = {}
    for job, dep in conn.execute(
            "SELECT pp_job_id, depends_on FROM state.postproc_depends d "
            "JOIN state.postproc_job j ON j.id = d.pp_job_id WHERE j.midas_run_id = %s", (run_id,)):
        deps.setdefault(job, []).append(label[dep])
    return sorted((r[1], r[2], r[3], r[4], tuple(sorted(deps.get(r[0], []), key = str))) for r in rows)


def files_of(conn, run_id):
    return sorted(conn.execute(
        "SELECT substr(filebase, 9), fileext, producer, status FROM state.file_list "
        "WHERE run_id = %s AND fileext = 'mid.lz4'", (run_id,)).fetchall())


def table_state(conn):
    """Row count and largest id of every table a requeue may write."""
    out = {}
    for table in ("state.midas_run", "state.file_list", "state.postproc_job", "logs.run_annotations"):
        out[table] = conn.execute(f"SELECT count(*), max(id) FROM {table}").fetchone()
    out["state.postproc_depends"] = conn.execute("SELECT count(*) FROM state.postproc_depends").fetchone()
    out["statuses"] = conn.execute(
        "SELECT md5(string_agg(id || status, ',' ORDER BY id)) FROM state.postproc_job").fetchone()
    out["runs"] = conn.execute(
        "SELECT md5(string_agg(id || coalesce(status, '') || coalesce(midas_run_number, 0), ',' ORDER BY id)) "
        "FROM state.midas_run").fetchone()
    return out


def callback_run(iface, number, subruns, quality = ""):
    """Run `number` the way the daemon's callbacks write it: start, a file
    row per subrun closed through finish_file's calls, then the stop."""
    run_id = iface.register_run("CLAIMED", author = "test", note = "callback path", quality = quality)
    iface.start_of_midas_run(run_id, number, "Sat Oct  3 22:50:19 2026")
    for s in subruns:
        # filename_change_callback: close the previous file, open the next
        for file_id in iface.close_files_in_channel(CHANNEL):
            iface.schedule_postproc_job_on_file(file_id, task = 'nearline', client = 'nearline')
        iface.open_file(f"logger_{CHANNEL}", run_id, f"run{number:05d}_{s:05d}.mid.lz4")
    # end_of_run_callback: finish_file, then end_of_midas_run
    for file_id in iface.close_files_in_channel(CHANNEL):
        iface.schedule_postproc_job_on_file(file_id, task = 'nearline', client = 'nearline')
    iface.end_of_midas_run(run_id, recorded_events = 1234, stop_time = "Sat Oct  3 22:55:01 2026",
                           schedule_post_processing = (quality.lower() != 'debug'))
    return run_id


def test_gap_fill_gives_the_callback_rows(db, tmp_path):
    iface, conn = db
    callback_id = callback_run(iface, 7001, [0, 1, 2])

    write_raw(tmp_path, 7002, [0, 1, 2])
    write_dump(tmp_path, 7002)
    rc, text = run(iface, 7002, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 0, text
    (requeue_id, status, *_), = run_row(conn, 7002)
    assert status == "DONE"

    assert files_of(conn, requeue_id) == files_of(conn, callback_id)
    expected = jobs_of(conn, callback_id)
    assert jobs_of(conn, requeue_id) == expected
    # what the callbacks make, spelled out: the cleanup waits for every job
    # queued before it, the nearline jobs included
    cleanup = [j for j in expected if j[:2] == ("nearline", "cleanup")][0]
    assert len(cleanup[4]) == 5 and cleanup[3] == "DEPENDING"
    assert sum(1 for j in expected if j[:2] == ("farline", "farline")) == 3
    assert "jobs created" in text


def test_second_call_creates_nothing(db, tmp_path):
    iface, conn = db
    write_raw(tmp_path, 7011, [0, 1])
    write_dump(tmp_path, 7011)
    assert run(iface, 7011, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)[0] == 0
    before = table_state(conn)
    rc, text = run(iface, 7011, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL, "-v")
    assert rc == 0, text
    after = table_state(conn)
    assert before == after
    job_lines = [l for l in text.splitlines() if l.startswith("    ")]
    assert len(job_lines) == 2 + 3 + 2 + 1
    assert all(" present (" in l for l in job_lines)
    assert table_row(text, 7011)[4:7] == ["0", "0", "8"]


def test_dry_run_writes_nothing(db, tmp_path):
    iface, conn = db
    # a run with no row, and one left RUNNING with an open file
    write_raw(tmp_path, 7021, [0, 1])
    write_dump(tmp_path, 7021)
    stale = iface.register_run("CLAIMED", author = "test", note = "stale", quality = "")
    iface.start_of_midas_run(stale, 7022, "Sat Oct  3 23:00:00 2026")
    iface.open_file(f"logger_{CHANNEL}", stale, "run07022_00000.mid.lz4")
    write_raw(tmp_path, 7022, [0, 1])
    write_dump(tmp_path, 7022)

    class NoWrites:
        """The interface with every writing method made to fail."""
        writes = {"register_run", "start_of_midas_run", "end_of_midas_run", "create_finished_run",
                  "register_logger_file", "open_file", "close_files_in_channel", "update_file_status",
                  "update_status", "update_postproc_status", "reset_jobs", "schedule_postproc_job",
                  "schedule_postproc_job_on_file", "schedule_run_post_processing", "annotate_run_id",
                  "annotate_run_number"}

        def __getattr__(self, name):
            if name in self.writes:
                raise AssertionError(f"dry run called {name}")
            return getattr(iface, name)

    before = table_state(conn)
    rc, text = run(NoWrites(), 7021, 7022, "--again", "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 0, text
    assert table_state(conn) == before
    assert "DRY RUN" in text and "nothing was written" in text
    assert "create from run07021.json" in text
    assert f"row {stale}: left open by the daemon; close it" in text


def test_again_resets_only_done_and_failed(db, tmp_path):
    iface, conn = db
    write_raw(tmp_path, 7031, [0, 1, 2, 3])
    write_dump(tmp_path, 7031)
    assert run(iface, 7031, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)[0] == 0
    (run_id, *_), = run_row(conn, 7031)
    nearline = [r[0] for r in conn.execute(
        "SELECT id FROM state.postproc_job WHERE midas_run_id = %s AND job_type = 'nearline' ORDER BY id",
        (run_id,))]
    for job_id, status in zip(nearline, ("DONE", "FAILED", "RUNNING", "CLAIMED")):
        iface.update_postproc_status(job_id, status)
    for job_type in ("backup", "remote", "cleanup"):
        job = iface.find_postproc_job(run_id, "nearline", job_type)
        iface.update_postproc_status(job["id"], "DONE")
    farline_before = sorted(conn.execute(
        "SELECT id, status FROM state.postproc_job WHERE midas_run_id = %s AND client = 'farline'",
        (run_id,)).fetchall())

    argv = (7031, "--again", "--stage", "nearline", "--stage", "transfer", "--data-dir", tmp_path,
            "--channel", CHANNEL)
    rc, dry = run(iface, *argv)
    assert rc == 0, dry
    rc, text = run(iface, *argv, "--apply")
    assert rc == 0, text
    # the dry run said what the apply did
    assert table_row(dry, 7031)[4:7] == table_row(text, 7031)[4:7]
    status = dict(conn.execute("SELECT id, status FROM state.postproc_job WHERE midas_run_id = %s",
                               (run_id,)).fetchall())
    assert [status[j] for j in nearline] == ["PENDING", "PENDING", "RUNNING", "CLAIMED"]
    assert status[iface.find_postproc_job(run_id, "nearline", "backup")["id"]] == "PENDING"
    assert status[iface.find_postproc_job(run_id, "nearline", "remote")["id"]] == "PENDING"
    # the cleanup waits again for the jobs it depends on instead of running now
    assert status[iface.find_postproc_job(run_id, "nearline", "cleanup")["id"]] == "DEPENDING"
    # farline was not asked for and is not reset; the database puts it back
    # to waiting, because the remote copy it depends on runs again
    assert farline_before and {s for _, s in farline_before} == {"PENDING"}
    farline_after = sorted(conn.execute(
        "SELECT id, status FROM state.postproc_job WHERE midas_run_id = %s AND client = 'farline'",
        (run_id,)).fetchall())
    assert [j for j, _ in farline_after] == [j for j, _ in farline_before]
    assert {s for _, s in farline_after} == {"DEPENDING"}
    # created, reset, present: two nearline and three transfer jobs reset
    assert table_row(text, 7031)[4:7] == ["0", "5", "2"]


def test_debug_quality_queues_only_nearline(db, tmp_path):
    iface, conn = db
    callback_id = callback_run(iface, 7041, [0, 1], quality = "Debug")
    write_raw(tmp_path, 7042, [0, 1])
    write_dump(tmp_path, 7042, quality = "debug")
    rc, text = run(iface, 7042, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 0, text
    (run_id, *_), = run_row(conn, 7042)
    assert jobs_of(conn, run_id) == jobs_of(conn, callback_id)
    assert {j[1] for j in jobs_of(conn, run_id)} == {"nearline"}
    assert "quality Debug" in text

    # named explicitly, a stage is queued whatever the quality
    rc, text = run(iface, 7042, "--apply", "--stage", "transfer", "--data-dir", tmp_path,
                   "--channel", CHANNEL)
    assert rc == 0, text
    assert {(j[0], j[1]) for j in jobs_of(conn, run_id)} == {
        ("nearline", "nearline"), ("nearline", "backup"), ("nearline", "remote"), ("nearline", "cleanup")}


def test_row_created_from_the_dump(db, tmp_path):
    iface, conn = db
    write_raw(tmp_path, 7051, [0])
    write_dump(tmp_path, 7051, quality = "NL Test", events = (500, 70, 3), operator = "Ada")
    rc, text = run(iface, 7051, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 0, text
    (run_id, status, start, stop, events, quality), = run_row(conn, 7051)
    assert status == "DONE" and events == 573 and quality == "NL Test"
    # stored as the daemon stores /Runinfo/Start time: the string, read by the server
    same = conn.execute("SELECT %s::timestamptz = %s, %s::timestamptz = %s",
                        ("Sat Oct  3 22:50:19 2026", start, "Sat Oct  3 22:55:01 2026", stop)).fetchone()
    assert same == (True, True)
    author, note = conn.execute("SELECT author, note FROM logs.run_annotations WHERE run_id = %s",
                                (run_id,)).fetchone()
    assert author == "requeue, Ada"
    assert "run07051.json" in note and "a beam run" in note
    assert files_of(conn, run_id) == [("_00000", "mid.lz4", f"logger_{CHANNEL}", "DONE")]


def test_stale_running_row_is_closed(db, tmp_path):
    iface, conn = db
    stale = iface.register_run("CLAIMED", author = "test", note = "stale", quality = "")
    iface.start_of_midas_run(stale, 7061, "Sat Oct  3 23:00:00 2026")
    iface.open_file(f"logger_{CHANNEL}", stale, "run07061_00000.mid.lz4")
    write_raw(tmp_path, 7061, [0, 1])
    write_dump(tmp_path, 7061, start = "Sat Oct  3 23:00:00 2026", stop = "Sat Oct  3 23:10:00 2026",
               events = (42,))

    rc, text = run(iface, 7061, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 0, text
    (run_id, status, _, stop, events, _), = run_row(conn, 7061)
    assert run_id == stale and status == "DONE" and events == 42
    assert conn.execute("SELECT %s::timestamptz = %s", ("Sat Oct  3 23:10:00 2026", stop)).fetchone()[0]
    # the open file closed, the second one registered, everything queued
    assert files_of(conn, stale) == [("_00000", "mid.lz4", f"logger_{CHANNEL}", "DONE"),
                                     ("_00001", "mid.lz4", f"logger_{CHANNEL}", "DONE")]
    assert len(jobs_of(conn, stale)) == 2 + 3 + 2 + 1
    assert conn.execute("SELECT count(*) FROM logs.run_annotations WHERE run_id = %s AND author = 'requeue'",
                        (stale,)).fetchone()[0] == 1


def test_the_run_being_taken_is_refused(db, tmp_path):
    iface, conn = db
    write_raw(tmp_path, 7071, [0])
    write_dump(tmp_path, 7071)
    before = table_state(conn)
    taking = MidasState(state = 3, run_number = 7071, data_dir = str(tmp_path), run_db_pk = 0, dump_file = "")
    rc, text = run(iface, 7071, "--apply", "--channel", CHANNEL, midas_state = taking)
    assert rc == 1
    assert "MIDAS is taking this run now" in text
    assert table_state(conn) == before
    assert run_row(conn, 7071) == []


def test_missing_raw_file(db, tmp_path):
    iface, conn = db
    # the shape of a row whose only raw file is gone: closed, nothing queued
    stale = iface.register_run("CLAIMED", author = "test", note = "stale", quality = "")
    iface.start_of_midas_run(stale, 7081, "Sat Oct  3 23:00:00 2026")
    iface.open_file(f"logger_{CHANNEL}", stale, "run07081.mid.lz4")
    (tmp_path / "run07081.mid.lz4.crc32c").write_bytes(b"0")
    write_dump(tmp_path, 7081)
    rc, text = run(iface, 7081, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 0, text
    assert run_row(conn, 7081)[0][1] == "DONE"
    assert jobs_of(conn, stale) == []
    assert "not in" in text and "run07081.mid.lz4" in text
    # its row, left open, is marked ERROR, so that the live daemon's close of
    # logger channel 7 does not queue a nearline job on a file that is gone
    assert files_of(conn, stale) == [("", "mid.lz4", f"logger_{CHANNEL}", "ERROR")]
    assert "open rows of gone files marked ERROR: 1" in text

    # one file there, one gone: the one there gets its nearline job, and no
    # run-level job is queued, because the raw transfer needs every file
    other = iface.register_run("CLAIMED", author = "test", note = "half", quality = "")
    iface.start_of_midas_run(other, 7082, "Sat Oct  3 23:00:00 2026")
    for name in ("run07082_00000.mid.lz4", "run07082_00001.mid.lz4"):
        iface.open_file(f"logger_{CHANNEL}", other, name)
    write_raw(tmp_path, 7082, [1])
    write_dump(tmp_path, 7082)
    rc, text = run(iface, 7082, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 0, text
    assert [(j[0], j[1], j[2]) for j in jobs_of(conn, other)] == [("nearline", "nearline", "_00001")]
    assert "transfer not queued" in text and "farline not queued" in text


def test_no_row_and_no_dump_is_refused(db, tmp_path):
    iface, conn = db
    write_raw(tmp_path, 7091, [0])
    rc, text = run(iface, 7091, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 1
    assert "REFUSED" in text and run_row(conn, 7091) == []


def test_attach_to_an_existing_row(db, tmp_path):
    iface, conn = db
    pending = iface.register_run("PENDING", author = "test", note = "queued by the sequencer", quality = "")
    write_raw(tmp_path, 7101, [0])
    write_dump(tmp_path, 7101, events = (7,))
    rc, text = run(iface, 7101, "--apply", "--run-id", pending, "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 0, text
    (run_id, status, start, _, events, _), = run_row(conn, 7101)
    assert (run_id, status, events) == (pending, "DONE", 7) and start is not None

    # a row that already has another run number is not attached
    rc, text = run(iface, 7102, "--apply", "--run-id", pending, "--data-dir", tmp_path)
    assert rc == 1 and "is run 7101" in text


def test_stale_primary_key_is_reported(db, tmp_path):
    iface, conn = db
    write_raw(tmp_path, 7111, [0])
    write_dump(tmp_path, 7111)
    stopped = MidasState(state = 1, run_number = 7115, data_dir = str(tmp_path), run_db_pk = 87, dump_file = "")
    rc, text = run(iface, 7111, "--channel", CHANNEL, midas_state = stopped)
    assert rc == 0, text
    assert "Run DB PK is 87" in text
    assert "odbedit -c 'set \"/Nearline/Info/Run DB PK\" 0'" in text


def test_the_stop_transition_still_fails_over_an_existing_job(db):
    """schedule_run_post_processing as end_of_midas_run calls it keeps its
    old answer: a run-level job that is already there is a failure. Only
    the requeue's existing_ok takes it as fine."""
    iface, conn = db
    run_id = callback_run(iface, 7121, [0])
    assert iface.schedule_run_post_processing(run_id) is False
    outcome = []
    assert iface.schedule_run_post_processing(run_id, existing_ok = True, outcome = outcome) is True
    assert [o["created"] for o in outcome] == [False] * 5
    assert [(o["client"], o["job_type"]) for o in outcome] == [
        ("nearline", "backup"), ("nearline", "remote"), ("nearline", "cleanup"),
        ("farline", "farline"), ("farline", "backup")]


def test_again_on_the_remote_copy_reruns_what_waits_for_it(db, tmp_path):
    """Resetting the remote copy makes the database put piana's finished
    jobs back to waiting (state.recompute_job_state), so they run again
    after it. The dry run says so, and the apply reports them as reset."""
    iface, conn = db
    write_raw(tmp_path, 7131, [0, 1])
    write_dump(tmp_path, 7131)
    assert run(iface, 7131, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)[0] == 0
    (run_id, *_), = run_row(conn, 7131)
    # every job finished, in dependency order
    for job_id, in conn.execute("SELECT id FROM state.postproc_job WHERE midas_run_id = %s ORDER BY id",
                                (run_id,)).fetchall():
        iface.update_postproc_status(job_id, "DONE")

    argv = (7131, "--again", "--stage", "transfer", "--data-dir", tmp_path, "--channel", CHANNEL, "-v")
    rc, dry = run(iface, *argv)
    assert rc == 0, dry
    rc, text = run(iface, *argv, "--apply")
    assert rc == 0, text
    # backup, remote, cleanup reset by request; 2 farline + farline backup by the database
    assert table_row(dry, 7131)[4:7] == table_row(text, 7131)[4:7] == ["0", "6", "0"]
    statuses = dict(conn.execute(
        "SELECT client || '/' || job_type, status FROM state.postproc_job "
        "WHERE midas_run_id = %s AND file_id IS NULL", (run_id,)).fetchall())
    assert statuses == {"nearline/backup": "PENDING", "nearline/remote": "PENDING",
                        "nearline/cleanup": "DEPENDING", "farline/backup": "DEPENDING"}
    # the nearline jobs were not touched
    assert {s for s, in conn.execute(
        "SELECT status FROM state.postproc_job WHERE midas_run_id = %s AND job_type = 'nearline'",
        (run_id,)).fetchall()} == {"DONE"}


def test_apply_works_as_the_daemon_role(db, tmp_path):
    """--apply writes as `bot`, which may insert rows but update only what
    db_config.sql grants it (all of midas_run, the status of jobs and
    files). Every write of the requeue has to fit in that."""
    from pioneer.rundb.interface import interface

    iface, conn = db
    bot = interface(user = requeue.kDbUser, password = requeue.kDbPwd)
    stale = iface.register_run("CLAIMED", author = "test", note = "stale", quality = "")
    iface.start_of_midas_run(stale, 7141, "Sat Oct  3 23:00:00 2026")
    iface.open_file(f"logger_{CHANNEL}", stale, "run07141_00000.mid.lz4")
    for number in (7141, 7142):
        write_raw(tmp_path, number, [0, 1])
        write_dump(tmp_path, number)
    rc, text = run(bot, 7141, 7142, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 0, text
    for job_id, in conn.execute(
            "SELECT j.id FROM state.postproc_job j JOIN state.midas_run r ON r.id = j.midas_run_id "
            "WHERE r.midas_run_number IN (7141, 7142) ORDER BY j.id").fetchall():
        iface.update_postproc_status(job_id, "DONE")
    rc, text = run(bot, 7141, 7142, "--apply", "--again", "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 0, text
    assert table_row(text, 7141)[4:7] == table_row(text, 7142)[4:7] == ["0", "8", "0"]



# ---------------------------------------------------------------------------
# Regressions from the review
# ---------------------------------------------------------------------------

def status_map(conn, run_id):
    return {(c, t, f): st for c, t, f, st in conn.execute(
        "SELECT j.client, j.job_type, f.filebase, j.status FROM state.postproc_job j "
        "LEFT JOIN state.file_list f ON f.id = j.file_id WHERE j.midas_run_id = %s", (run_id,))}


def test_a_new_file_in_a_run_with_transfer_jobs_is_refused(db, tmp_path):
    """The daemon restarted mid-run, so subrun 1 never got a row, and the stop
    queued the transfer anyway. Registering subrun 1 now would let the cleanup
    (which deletes every registered raw file) remove it although the backup
    and the remote copy, already DONE, never copied it."""
    iface, conn = db
    run_id = callback_run(iface, 8001, [0, 2])
    write_raw(tmp_path, 8001, [0, 1, 2])
    write_dump(tmp_path, 8001)
    for job_type in ("backup", "remote"):
        iface.update_postproc_status(iface.find_postproc_job(run_id, "nearline", job_type)["id"], "DONE")
    before = table_state(conn)
    statuses = status_map(conn, run_id)

    rc, dry = run(iface, 8001, "--data-dir", tmp_path, "--channel", CHANNEL)
    rc_apply, text = run(iface, 8001, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)
    for out, code in ((dry, rc), (text, rc_apply)):
        assert code == 1, out
        assert "REFUSED" in out and "run08001_00001.mid.lz4" in out
        assert "Expert steps" in out and "remote" in out and "cleanup" in out
        assert table_row(out, 8001)[-1] == "refused"
    assert table_state(conn) == before
    assert status_map(conn, run_id) == statuses


def test_a_running_row_without_a_dump_is_not_closed(db, tmp_path):
    """MIDAS does not answer and the run is still being taken: its row is
    RUNNING, its file open, and mlogger has not written runNNNNN.json yet."""
    iface, conn = db
    run_id = iface.register_run("CLAIMED", author = "test", note = "live", quality = "")
    iface.start_of_midas_run(run_id, 8011, "Sat Oct  3 23:00:00 2026")
    iface.open_file(f"logger_{CHANNEL}", run_id, "run08011_00000.mid.lz4")
    write_raw(tmp_path, 8011, [0])
    before = table_state(conn)

    # without the ODB, --apply is refused outright
    rc, text = run(iface, 8011, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL, midas_state = None)
    assert rc == 2 and "--no-midas-check" in text
    assert table_state(conn) == before

    # the dry run is allowed, and says the run is refused
    rc, dry = run(iface, 8011, "--data-dir", tmp_path, "--channel", CHANNEL, midas_state = None)
    assert rc == 1 and "NOT checked" in dry and "may still be being taken" in dry

    # with --no-midas-check, the missing dump still keeps the row open
    rc, text = run(iface, 8011, "--apply", "--no-midas-check", "--data-dir", tmp_path,
                   "--channel", CHANNEL, midas_state = None)
    assert rc == 1, text
    assert "may still be being taken" in text
    assert table_state(conn) == before
    assert run_row(conn, 8011)[0][1] == "RUNNING"
    assert jobs_of(conn, run_id) == []


def test_apply_without_midas_check_works_on_a_stopped_run(db, tmp_path):
    iface, conn = db
    write_raw(tmp_path, 8021, [0])
    write_dump(tmp_path, 8021)
    rc, text = run(iface, 8021, "--apply", "--no-midas-check", "--data-dir", tmp_path,
                   "--channel", CHANNEL, midas_state = None)
    assert rc == 0, text
    assert run_row(conn, 8021)[0][1] == "DONE"


def test_attach_without_a_dump_is_refused(db, tmp_path):
    iface, conn = db
    pending = iface.register_run("PENDING", author = "test", note = "queued", quality = "")
    write_raw(tmp_path, 8031, [0])
    before = table_state(conn)
    rc, text = run(iface, 8031, "--apply", "--run-id", pending, "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 1 and "may still be being taken" in text
    assert table_state(conn) == before
    # out of the queue the run-database page tests read
    iface.update_status("midas_run", pending, "CANCELLED")


def test_again_is_refused_while_a_dependent_runs(db, tmp_path):
    """Resetting the remote copy would make the database put piana's farline
    job, which piana is running, back to DEPENDING."""
    iface, conn = db
    write_raw(tmp_path, 8041, [0, 1])
    write_dump(tmp_path, 8041)
    assert run(iface, 8041, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)[0] == 0
    (run_id, *_), = run_row(conn, 8041)
    for job_type in ("backup", "remote"):
        iface.update_postproc_status(iface.find_postproc_job(run_id, "nearline", job_type)["id"], "DONE")
    farline = conn.execute("SELECT id FROM state.postproc_job WHERE midas_run_id = %s AND "
                           "client = 'farline' AND job_type = 'farline' ORDER BY id", (run_id,)).fetchall()
    iface.update_postproc_status(farline[0][0], "RUNNING")
    before = table_state(conn)

    argv = (8041, "--again", "--stage", "transfer", "--data-dir", tmp_path, "--channel", CHANNEL)
    rc, dry = run(iface, *argv)
    rc_apply, text = run(iface, *argv, "--apply")
    for out, code in ((dry, rc), (text, rc_apply)):
        assert code == 1, out
        assert f"farline/farline {farline[0][0]} (run08041_00000.mid.lz4) RUNNING" in out
    assert table_state(conn) == before


def test_run_id_of_the_next_start_is_refused(db, tmp_path):
    iface, conn = db
    pending = iface.register_run("PENDING", author = "test", note = "next start", quality = "")
    write_raw(tmp_path, 8051, [0])
    write_dump(tmp_path, 8051)
    nxt = MidasState(state = 1, run_number = 8050, data_dir = "", run_db_pk = pending, dump_file = "")
    rc, text = run(iface, 8051, "--apply", "--run-id", pending, "--data-dir", tmp_path,
                   "--channel", CHANNEL, midas_state = nxt)
    assert rc == 1 and "Run DB PK" in text and "next start" in text
    assert run_row(conn, 8051) == []
    iface.update_status("midas_run", pending, "CANCELLED")


def test_a_run_whose_row_is_error_is_refused(db, tmp_path):
    """The daemon's auto-recovery: the run's row is ERROR and its later files
    went to a new row without a run number."""
    iface, conn = db
    run_id = iface.register_run("CLAIMED", author = "test", note = "recovered", quality = "")
    iface.start_of_midas_run(run_id, 8061, "Sat Oct  3 23:00:00 2026")
    iface.open_file(f"logger_{CHANNEL}", run_id, "run08061_00000.mid.lz4")
    iface.update_status("midas_run", run_id, "ERROR")
    recovery = iface.register_run("RUNNING", author = "AutoRecovery", note = "", quality = "check")
    iface.open_file(f"logger_{CHANNEL}", recovery, "run08061_00001.mid.lz4")
    write_raw(tmp_path, 8061, [0, 1])
    write_dump(tmp_path, 8061)
    before = table_state(conn)
    rc, text = run(iface, 8061, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 1 and "auto-recovery" in text and "AutoRecovery" in text
    assert table_state(conn) == before


def test_dry_run_lists_the_files_the_apply_queues(db, tmp_path):
    """A registered file that sorts after unregistered ones: the dry run's
    file span is the apply's."""
    iface, conn = db
    stale = iface.register_run("CLAIMED", author = "test", note = "stale", quality = "")
    iface.start_of_midas_run(stale, 8071, "Sat Oct  3 23:00:00 2026")
    iface.open_file(f"logger_{CHANNEL}", stale, "run08071_00002.mid.lz4")
    write_raw(tmp_path, 8071, [0, 1, 2])
    write_dump(tmp_path, 8071)
    rc, dry = run(iface, 8071, "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 0, dry
    rc, text = run(iface, 8071, "--apply", "--data-dir", tmp_path, "--channel", CHANNEL)
    assert rc == 0, text
    def job_lines(out):
        return [l for l in out.splitlines() if l.startswith("    ")]
    assert job_lines(dry) == job_lines(text)
    assert "run08071_00000.mid.lz4 .. run08071_00002.mid.lz4" in job_lines(text)[0]
    assert "open file rows to close: 1" in dry and "open file rows closed: 1" in text
