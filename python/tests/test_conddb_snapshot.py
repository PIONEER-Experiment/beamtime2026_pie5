"""The conditions snapshot (pioneer.conddb.snapshot).

Every test but the last runs without a database: a fake source stands in for
PostgreSQL, so the snapshot logic (unchanged detection, atomic build and
rename, the ``latest`` swap, the lock, the backup copy, exit codes) is tested
on its own.

The PostgreSQL test runs when $PIONEER_CONDDB_TEST_DSN names a scratch
database (its name must contain "test" or "scratch"; every cond_* table in it
is dropped first) and pg_dump is on PATH. It loads the real containers of
reco_testbeam/conditions ($PI_TB_CONDITIONS_DIR, else the workspace layout).
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pioneer.conddb import cond_loader, mupix_mask, pg2json, snapshot
from pioneer.conddb.snapshot import SnapshotError


class FakeSource:
    """A database whose whole content is one string."""

    label = "fake (host=nowhere dbname=conditions)"

    def __init__(self, content="v1"):
        self.content = content
        self.exports = []           # the order_from of every export call
        self.fail_dump = False
        self.fail_export = False
        self.moves = 0              # dumps during which the content changes

    def fingerprint(self):
        return {"fingerprint_version": snapshot.FINGERPRINT_VERSION,
                "content": {"c": self.content}}

    def server_version(self):
        return "FakeSQL 1.0"

    def dump(self, path):
        if self.fail_dump:
            raise SnapshotError("pg_dump failed: connection refused")
        path.write_text(f"dump of {self.content}\n")
        if self.moves:
            self.moves -= 1
            self.content += "+"
        return "; TABLE DATA public cond_values\n"

    def export(self, out_dir, order_from):
        self.exports.append(order_from)
        if self.fail_export:
            raise SnapshotError("pg2json --check failed")
        for f in pg2json.CONTAINERS:
            (out_dir / f).write_text(json.dumps({"content": self.content}) + "\n")
        return "check: fine\n"


def take(src, root, **kw):
    kw.setdefault("backup_dir", None)
    kw.setdefault("lock_timeout_s", 2.0)
    return snapshot.take_snapshot(src, root, **kw)


def names(root):
    return [p.name for p in snapshot.snapshot_dirs(root)]


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def test_stamp_and_names(tmp_path):
    import datetime as dt
    when = dt.datetime(2026, 9, 30, 21, 30, 5, tzinfo=dt.timezone.utc)
    stamp = snapshot.utc_stamp(when)
    assert stamp == "20260930T213005Z"
    assert snapshot.is_snapshot_name(stamp) and snapshot.is_snapshot_name(stamp + "-2")
    for other in ("latest", ".lock", ".building-abc", "pre-conditions-20260930T213005Z",
                  "cron.log"):
        assert not snapshot.is_snapshot_name(other)
    assert snapshot.unique_name(tmp_path, stamp) == stamp
    (tmp_path / stamp).mkdir()
    (tmp_path / f"{stamp}-1").mkdir()
    assert snapshot.unique_name(tmp_path, stamp) == f"{stamp}-2"


def test_manifest_round_trip_and_tamper(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    for f in (snapshot.DUMP, snapshot.FINGERPRINT) + snapshot.CONTAINERS:
        (d / f).write_text(f"{f}\n")
    snapshot.write_manifest(d, [("snapshot", "s"), ("server_version", "PG 18.6")])
    meta, files = snapshot.read_manifest(d)
    assert meta == {"snapshot": "s", "server_version": "PG 18.6"}
    assert set(files) == {snapshot.DUMP, snapshot.FINGERPRINT, *snapshot.CONTAINERS}
    assert snapshot.verify_manifest(d) == []
    if shutil.which("sha256sum"):
        assert subprocess.run(["sha256sum", "--quiet", "-c", snapshot.MANIFEST],
                              cwd=d).returncode == 0
    (d / snapshot.CONTAINERS[0]).write_text("edited\n")
    assert any("sha256 differs" in p for p in snapshot.verify_manifest(d))
    (d / snapshot.DUMP).unlink()
    assert any("missing" in p for p in snapshot.verify_manifest(d))


def test_same_content_ignores_when_and_where():
    a = {"fingerprint_version": 1, "content": {"x": 1}, "taken_utc": "t1", "source": "a"}
    b = {"fingerprint_version": 1, "content": {"x": 1}, "taken_utc": "t2", "source": "b"}
    assert snapshot.same_content(a, b)
    assert not snapshot.same_content(a, dict(b, content={"x": 2}))
    assert not snapshot.same_content(a, dict(b, fingerprint_version=2))
    assert not snapshot.same_content(a, None)


def test_symlink_swap_is_relative_and_replaces(tmp_path):
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    snapshot.swap_symlink(tmp_path, "latest", "A")
    assert os.readlink(tmp_path / "latest") == "A"
    assert snapshot.resolve_latest(tmp_path) == (tmp_path / "A").resolve()
    snapshot.swap_symlink(tmp_path, "latest", "B")
    assert snapshot.resolve_latest(tmp_path) == (tmp_path / "B").resolve()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["A", "B", "latest"]
    (tmp_path / "plain").mkdir()
    with pytest.raises(SnapshotError, match="not a symlink"):
        snapshot.swap_symlink(tmp_path, "plain", "A")


def test_lock_excludes_a_second_holder(tmp_path):
    with snapshot.Lock(tmp_path / ".lock", timeout_s=1.0):
        with pytest.raises(SnapshotError, match="still locked"):
            with snapshot.Lock(tmp_path / ".lock", timeout_s=0.3, poll_s=0.05):
                pass
    with snapshot.Lock(tmp_path / ".lock", timeout_s=0.3):
        pass


def test_human_sizes():
    assert snapshot.human(512) == "512 B"
    assert snapshot.human(763_200) == "763.2 kB"
    assert snapshot.human(2_300_000) == "2.3 MB"


def test_the_writers_hook_finds_this_module():
    # mupix_mask / mupix_timewalk run it after a --db --write when it exists
    assert mupix_mask.SNAPSHOT_MODULE == "pioneer.conddb.snapshot"
    assert mupix_mask._snapshot_available()


# ---------------------------------------------------------------------------
# one snapshot, with a fake database
# ---------------------------------------------------------------------------

def test_first_snapshot(tmp_path):
    root, src = tmp_path / "snaps", FakeSource()
    res = take(src, root)
    assert res.status == "written" and names(root) == [res.name]
    assert os.readlink(root / "latest") == res.name          # relative: survives a copy
    d = root / res.name
    for f in (snapshot.DUMP, snapshot.DUMP_LIST, snapshot.EXPORT_LOG, snapshot.FINGERPRINT,
              snapshot.MANIFEST) + snapshot.CONTAINERS:
        assert (d / f).is_file(), f
    assert snapshot.verify_manifest(d) == []
    meta, _ = snapshot.read_manifest(d)
    assert meta["previous"] == "none" and meta["server_version"] == "FakeSQL 1.0"
    fp = json.loads((d / snapshot.FINGERPRINT).read_text())
    assert fp["content"] == {"c": "v1"} and fp["snapshot"] == res.name
    assert src.exports == [None]
    assert not [p for p in root.iterdir() if p.name.startswith(snapshot.BUILD_PREFIX)]
    assert oct(d.stat().st_mode & 0o777) == "0o755"


def test_unchanged_writes_nothing(tmp_path):
    root, src = tmp_path / "snaps", FakeSource()
    first = take(src, root)
    again = take(src, root)
    assert again.status == "unchanged" and again.name == first.name
    assert names(root) == [first.name] and src.exports == [None]


def test_change_and_force_write_new_snapshots(tmp_path):
    root, src = tmp_path / "snaps", FakeSource()
    first = take(src, root)
    src.content = "v2"
    second = take(src, root)
    assert second.status == "written" and second.name != first.name
    assert snapshot.resolve_latest(root).name == second.name
    assert src.exports[-1] == (root / first.name).resolve()     # order from the previous one
    third = take(src, root, force=True)
    assert third.status == "written" and len(names(root)) == 3
    hint = tmp_path / "git-conditions"
    take(src, root, force=True, order_from=hint)
    assert src.exports[-1] == hint


def test_failure_leaves_latest_alone(tmp_path):
    root, src = tmp_path / "snaps", FakeSource()
    first = take(src, root)
    src.content, src.fail_export = "v2", True
    with pytest.raises(SnapshotError, match="pg2json"):
        take(src, root)
    src.fail_export, src.fail_dump = False, True
    with pytest.raises(SnapshotError, match="pg_dump"):
        take(src, root)
    assert names(root) == [first.name]
    assert snapshot.resolve_latest(root).name == first.name
    assert not [p for p in root.iterdir() if p.name.startswith(snapshot.BUILD_PREFIX)]


def test_database_changing_during_the_build(tmp_path):
    root, src = tmp_path / "snaps", FakeSource()
    src.moves = 1
    res = take(src, root)
    assert res.status == "written" and len(names(root)) == 1
    assert any("changed during snapshot attempt 1" in w for w in res.warnings)
    fp = json.loads((root / res.name / snapshot.FINGERPRINT).read_text())
    assert fp["content"] == {"c": "v1+"}     # the state the files were exported from
    src.moves = snapshot.CONSISTENCY_TRIES
    with pytest.raises(SnapshotError, match="kept changing"):
        take(src, root, force=True)
    assert len(names(root)) == 1


def test_a_damaged_latest_is_replaced(tmp_path):
    root, src = tmp_path / "snaps", FakeSource()
    first = take(src, root)
    (root / first.name / snapshot.CONTAINERS[0]).write_text("{}\n")
    res = take(src, root)
    assert res.status == "written" and res.name != first.name
    assert any("does not verify" in w for w in res.warnings)


def test_stale_build_is_cleaned(tmp_path):
    root = tmp_path / "snaps"
    (root / (snapshot.BUILD_PREFIX + "dead")).mkdir(parents=True)
    res = take(FakeSource(), root)
    assert any("interrupted run" in w for w in res.warnings)
    assert not (root / (snapshot.BUILD_PREFIX + "dead")).exists()


def test_backup_copies_and_catches_up(tmp_path):
    root, backup, src = tmp_path / "snaps", tmp_path / "disk" / "conddb", FakeSource()
    first = take(src, root, backup_dir=backup, backup_mount="")
    assert first.backup_ok and names(backup) == [first.name]
    assert snapshot.verify_manifest(backup / first.name) == []
    # the disk "unmounted": the snapshot is taken, the backup is skipped
    src.content = "v2"
    second = take(src, root, backup_dir=backup, backup_mount=str(tmp_path / "no-mount"))
    assert second.status == "written" and not second.backup_ok
    assert "not mounted" in second.backup and names(backup) == [first.name]
    # back again: an unchanged run catches up
    third = take(src, root, backup_dir=backup, backup_mount="")
    assert third.status == "unchanged" and third.backup_ok
    assert names(backup) == [first.name, second.name]
    assert os.readlink(backup / "latest") == second.name


def test_backup_dir_must_be_on_the_mount(tmp_path, monkeypatch):
    disk = tmp_path / "disk"
    disk.mkdir()
    ok, why = snapshot.backup_ready(disk / "conddb", str(disk))
    assert not ok and "not mounted" in why and not (disk / "conddb").exists()
    monkeypatch.setattr(snapshot.os.path, "ismount", lambda p: str(p) == str(disk))
    ok, why = snapshot.backup_ready(tmp_path / "elsewhere", str(disk))
    assert not ok and "not below" in why
    ok, why = snapshot.backup_ready(disk / "conddb", str(disk))
    assert ok and (disk / "conddb").is_dir()


# ---------------------------------------------------------------------------
# PgSource.export: pg2json --check --strict (pg2json itself stubbed)
# ---------------------------------------------------------------------------

def _stub_pg2json(monkeypatch, rc=0, stderr="", exit_code=None):
    calls = []

    def main(argv):
        calls.append(argv)
        if exit_code is not None:
            raise SystemExit(exit_code)
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        if rc == 0:
            for f in pg2json.CONTAINERS:
                (out_dir / f).write_text("{}\n")
        print("exported from somewhere")
        if stderr:
            import sys
            print(stderr, file=sys.stderr)
        return rc
    monkeypatch.setattr(snapshot.pg2json, "main", main)
    return calls


def test_export_is_strict(tmp_path, monkeypatch):
    calls = _stub_pg2json(monkeypatch)
    src = snapshot.PgSource("host=nowhere dbname=conditions")
    log = src.export(tmp_path, None)
    assert "--strict" in calls[0] and "--check" in calls[0]
    assert "exported from somewhere" in log and src.warnings == []


def test_export_unmapped_table_fails_with_its_message(tmp_path, monkeypatch):
    msg = "error: table 'extra_table' is in the database but in no bt2026 container"
    _stub_pg2json(monkeypatch, rc=3, stderr=msg)
    src = snapshot.PgSource("host=nowhere dbname=conditions")
    with pytest.raises(SnapshotError) as err:
        src.export(tmp_path, None)
    assert "exit 3" in str(err.value) and msg in str(err.value)


def test_export_without_strict_support_fails(tmp_path, monkeypatch):
    _stub_pg2json(monkeypatch, exit_code=2)      # argparse: unrecognized --strict
    with pytest.raises(SnapshotError, match="exit 2"):
        snapshot.PgSource("host=nowhere dbname=conditions").export(tmp_path, None)


def test_export_stderr_of_a_success_becomes_warnings(tmp_path, monkeypatch):
    _stub_pg2json(monkeypatch, stderr="warning: something odd")
    src = snapshot.PgSource("host=nowhere dbname=conditions")
    log = src.export(tmp_path, None)
    assert src.warnings == ["pg2json: warning: something odd"]
    assert "--- stderr" in log


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------

@pytest.fixture
def fake(monkeypatch):
    src = FakeSource()
    monkeypatch.setattr(snapshot, "PgSource", lambda conninfo: src)
    return src


def test_main_quiet_is_silent_when_unchanged(tmp_path, fake, capsys):
    argv = ["--root", str(tmp_path / "s"), "--no-backup", "--quiet"]
    assert snapshot.main(argv) == 0
    out = capsys.readouterr()
    assert re.search(r"conddb snapshot \d{8}T\d{6}Z written", out.out) and out.err == ""
    assert snapshot.main(argv) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_main_warns_but_succeeds_without_the_backup_disk(tmp_path, fake, capsys):
    rc = snapshot.main(["--root", str(tmp_path / "s"), "--backup-dir", str(tmp_path / "b"),
                        "--backup-mount", str(tmp_path / "not-a-mount"), "--quiet"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "not mounted" in err and "the snapshot itself is fine" in err


def test_main_exit_1_when_the_snapshot_fails(tmp_path, fake, capsys):
    fake.fail_dump = True
    assert snapshot.main(["--root", str(tmp_path / "s"), "--no-backup"]) == 1
    err = capsys.readouterr().err
    assert "FAILED" in err and "still points at nothing" in err


def test_main_export_failure_reaches_stderr_under_quiet(tmp_path, fake, capsys):
    root = tmp_path / "s"
    assert snapshot.main(["--root", str(root), "--no-backup", "--quiet"]) == 0
    first = snapshot.resolve_latest(root)
    capsys.readouterr()
    fake.content, fake.fail_export = "v2", True
    assert snapshot.main(["--root", str(root), "--no-backup", "--quiet"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and "pg2json --check failed" in out.err
    assert f"still points at {first.name}" in out.err
    assert snapshot.resolve_latest(root) == first and len(snapshot.snapshot_dirs(root)) == 1


def test_main_refuses_sqlite(tmp_path, capsys):
    assert snapshot.main(["--conninfo", "sqlite:/tmp/x.db", "--root", str(tmp_path)]) == 2
    assert "PostgreSQL" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# PostgreSQL (scratch database only)
# ---------------------------------------------------------------------------

PG_DSN = os.environ.get("PIONEER_CONDDB_TEST_DSN", "")


def _real_dir():
    if os.environ.get("PI_TB_CONDITIONS_DIR"):
        return Path(os.environ["PI_TB_CONDITIONS_DIR"])
    for parent in Path(__file__).resolve().parents:
        d = parent / "main" / "reco_testbeam" / "conditions"
        if d.is_dir():
            return d
    return None


REAL = _real_dir()


@pytest.mark.skipif(not PG_DSN, reason="PIONEER_CONDDB_TEST_DSN not set")
@pytest.mark.skipif(not shutil.which("pg_dump"), reason="no pg_dump on PATH")
@pytest.mark.skipif(REAL is None, reason="reco_testbeam/conditions not found")
def test_snapshot_of_a_scratch_postgres(tmp_path, capsys):
    name = re.search(r"dbname\s*=\s*'?([^\s']+)", PG_DSN)
    if not name or not ("test" in name.group(1) or "scratch" in name.group(1)):
        pytest.fail("PIONEER_CONDDB_TEST_DSN must name a database whose name contains "
                    "'test' or 'scratch'; its conditions tables are dropped")
    ex = cond_loader.make_executor(conninfo=PG_DSN)
    ex.script("DROP TABLE IF EXISTS cond_values, cond_iov, cond_tags, cond_tables, "
              "cond_schema CASCADE;")
    cond_loader.load(ex, [REAL / f for f in pg2json.CONTAINERS])

    root = tmp_path / "snaps"
    argv = ["--conninfo", PG_DSN, "--root", str(root), "--no-backup", "--quiet"]
    assert snapshot.main(argv) == 0, capsys.readouterr().err
    first = snapshot.resolve_latest(root)
    assert snapshot.verify_manifest(first) == []
    listing = subprocess.run(["pg_restore", "--list", str(first / snapshot.DUMP)],
                             capture_output=True, text=True, check=True).stdout
    assert all(f"TABLE DATA public {t}" in listing for t in snapshot.BASE_TABLES)
    fp = json.loads((first / snapshot.FINGERPRINT).read_text())
    assert set(fp["content"]["tables"]) >= {t for ts in pg2json.CONTAINERS.values() for t in ts}
    assert "check: the 5 files" in (first / snapshot.EXPORT_LOG).read_text()
    meta, _ = snapshot.read_manifest(first)
    assert "PostgreSQL" in meta["server_version"] and "pg_dump" in meta["pg_dump"]

    capsys.readouterr()
    assert snapshot.main(argv) == 0                          # unchanged: silent, nothing new
    assert capsys.readouterr().out == "" and len(snapshot.snapshot_dirs(root)) == 1

    # A reload retires and re-inserts every interval: new row ids, same content.
    cond_loader.load(ex, [REAL / "bt2026_wavedream_calibration.json"])
    assert snapshot.main(argv) == 0
    second = snapshot.resolve_latest(root)
    assert second != first and len(snapshot.snapshot_dirs(root)) == 2

    def canonical(p):
        return json.dumps(json.loads(p.read_text()), sort_keys=True)

    for f in pg2json.CONTAINERS:
        assert canonical(second / f) == canonical(first / f), f
    # The first snapshot had no order to follow; from the second on, each takes
    # the previous one's, so the files are byte-identical while the content is.
    cond_loader.load(ex, [REAL / "bt2026_psm_readout_map.json"])
    assert snapshot.main(argv) == 0
    third = snapshot.resolve_latest(root)
    assert third != second and len(snapshot.snapshot_dirs(root)) == 3
    for f in pg2json.CONTAINERS:
        assert (third / f).read_bytes() == (second / f).read_bytes(), f

    # A table no container names: the snapshot fails, loudly, latest stays.
    ex.script("INSERT INTO cond_tables (name, schema, version, kind) "
              "VALUES ('snapshot_test_unmapped', 'x', 1, 'parameter_set');")
    capsys.readouterr()
    assert snapshot.main(argv) == 1
    err = capsys.readouterr().err
    assert "snapshot_test_unmapped" in err and "FAILED" in err
    assert snapshot.resolve_latest(root) == third and len(snapshot.snapshot_dirs(root)) == 3
    ex.script("DELETE FROM cond_tables WHERE name = 'snapshot_test_unmapped';")
    ex.close()


def test_environment_replaces_the_pinky_defaults(tmp_path, fake, monkeypatch, capsys):
    # the writers' hook passes --conninfo only
    monkeypatch.setenv(snapshot.ENV_ROOT, str(tmp_path / "s"))
    monkeypatch.setenv(snapshot.ENV_BACKUP_DIR, str(tmp_path / "b"))
    monkeypatch.setenv(snapshot.ENV_BACKUP_MOUNT, "")
    assert snapshot.main(["--conninfo", "service=pioneer-conditions-admin", "--quiet"]) == 0
    assert len(snapshot.snapshot_dirs(tmp_path / "s")) == 1
    assert len(snapshot.snapshot_dirs(tmp_path / "b")) == 1
    assert capsys.readouterr().err == ""
