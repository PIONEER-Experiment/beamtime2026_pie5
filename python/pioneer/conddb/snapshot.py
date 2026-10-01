#!/usr/bin/env python3
"""Snapshot the conditions database: a pg_dump plus the five JSON containers.

The database is where constants are written first. A snapshot is what the
nearline job reads when the database is down
(``process.py --conditions json:~/bt2026/conddb-snapshots/latest``), and the
dump is what the database is restored from if it is lost.

    python -m pioneer.conddb.snapshot                  # the database's default service
    python -m pioneer.conddb.snapshot --conninfo service=pioneer-conditions-admin
    python -m pioneer.conddb.snapshot --quiet          # cron: silent when nothing changed
    python -m pioneer.conddb.snapshot --force          # a new snapshot even if unchanged

What one run does, under an flock on ROOT/.lock (cron and a writer's hook
never run at the same time; the second waits):

1. Fingerprint the database: per table the highest row_id, the active
   interval count (cond_export.table_fingerprint), the interval and cell
   counts and the newest inserted_at; per base table a sha256 of its rows.
   If it equals the fingerprint of the snapshot ``latest`` points at, and that
   snapshot's MANIFEST still verifies, stop (exit 0, silent with --quiet).
2. Build ROOT/.building-XXXX/: ``conditions.dump`` (``pg_dump -Fc --no-owner``,
   checked with ``pg_restore --list``, whose output is kept as
   ``conditions.dump.list``), the five ``bt2026_*.json`` containers written by
   ``pg2json --check --strict`` with the order taken from the previous
   snapshot (a database table no container names fails the snapshot), its
   output as ``pg2json.log``, ``fingerprint.json`` and a ``MANIFEST``
   (``sha256sum -c MANIFEST`` checks it; the ``#`` lines carry the server,
   pg_dump and tool versions). The fingerprint is taken again at the end; if a
   write landed in between, the build is thrown away and redone (3 tries).
3. Rename it to ROOT/<UTC stamp>/ and repoint ROOT/latest (a relative symlink,
   swapped with a rename, so a reader sees the old snapshot or the new one).
4. If the backup disk is mounted (``os.path.ismount(--backup-mount)``), copy
   every snapshot the backup directory lacks into it and repoint its
   ``latest``. If it is not mounted, warn on stderr: the snapshot itself
   succeeded, so the exit code stays 0.

Exit codes: 0 snapshot written, or nothing changed (backup problems are
warnings); 1 the snapshot failed (database unreachable, pg_dump or the export
failed, lock not obtained), and ``latest`` still points at the previous one;
2 usage.

Standard library only, plus psql / pg_dump / pg_restore on PATH (the same
libpq service files and ~/.pgpass as every other tool here). Nothing is ever
deleted: snapshots are small, and every run prints the total.

Credentials: cond_viewer (``service=pioneer-conditions``, the default) is
enough. pg_dump needs SELECT on the five cond_* tables and USAGE on the
schema, which db/conddb_viewer.sql grants; the admin service works as well
and is what the writers pass after a write.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import errno
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from . import cond_export, pg2json
    from .cond_loader import LoaderError
except ImportError:          # run as a script: python3 snapshot.py ...
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cond_export
    import pg2json
    from cond_loader import LoaderError

DEFAULT_CONNINFO = "service=pioneer-conditions"
DEFAULT_ROOT = "~/bt2026/conddb-snapshots"
DEFAULT_BACKUP_DIR = "/home/pinky/backup/conddb"
#: the mount point the backup directory must be on; unmounted after the 09-27 reboot once
DEFAULT_BACKUP_MOUNT = "/home/pinky/backup"
#: environment overrides of the three defaults above, e.g. for a laptop, where the
#: writers' hook (which passes --conninfo only) must not write into ~/bt2026
ENV_ROOT = "PIONEER_CONDDB_SNAPSHOT_ROOT"
ENV_BACKUP_DIR = "PIONEER_CONDDB_BACKUP_DIR"
ENV_BACKUP_MOUNT = "PIONEER_CONDDB_BACKUP_MOUNT"
DEFAULT_LOCK_TIMEOUT_S = 300.0
CONSISTENCY_TRIES = 3

LATEST = "latest"
LOCK_NAME = ".lock"
BUILD_PREFIX = ".building-"
DUMP = "conditions.dump"
DUMP_LIST = "conditions.dump.list"
EXPORT_LOG = "pg2json.log"
FINGERPRINT = "fingerprint.json"
MANIFEST = "MANIFEST"
CONTAINERS = tuple(pg2json.CONTAINERS)
BASE_TABLES = ("cond_schema", "cond_tables", "cond_tags", "cond_iov", "cond_values")
FINGERPRINT_VERSION = 1

STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_STAMP = re.compile(r"^\d{8}T\d{6}Z(-\d+)?$")


class SnapshotError(Exception):
    """The snapshot could not be taken; the message says why."""


# ---------------------------------------------------------------------------
# Names, files, symlinks (pure: no database)
# ---------------------------------------------------------------------------

def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def utc_stamp(when: _dt.datetime) -> str:
    """20260930T213000Z: sorts in time order, no characters a shell minds."""
    return when.astimezone(_dt.timezone.utc).strftime(STAMP_FORMAT)


def is_snapshot_name(name: str) -> bool:
    return bool(_STAMP.match(name))


def unique_name(root: Path, stamp: str) -> str:
    """``stamp``, or ``stamp-1``, ``stamp-2``, ... if a snapshot of that second exists."""
    name, n = stamp, 0
    while (root / name).exists():
        n += 1
        name = f"{stamp}-{n}"
    return name


def snapshot_dirs(root: Path) -> list[Path]:
    """The snapshot directories in ``root``, oldest first (not ``latest``, not builds)."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if is_snapshot_name(p.name) and p.is_dir() and not p.is_symlink())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_manifest(directory: Path, meta: list[tuple[str, str]]) -> Path:
    """MANIFEST: ``# key: value`` lines, then ``<sha256>  <file>`` per file.

    The hash lines are sha256sum's format, which skips the ``#`` lines, so
    ``cd <snapshot> && sha256sum -c MANIFEST`` checks the snapshot by hand.
    """
    lines = [f"# {k}: {v}" for k, v in meta]
    for p in sorted(directory.iterdir()):
        if p.name != MANIFEST and p.is_file() and not p.is_symlink():
            lines.append(f"{sha256_file(p)}  {p.name}")
    path = directory / MANIFEST
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def read_manifest(directory: Path) -> tuple[dict[str, str], dict[str, str]]:
    """({meta key: value}, {file: sha256}) of a snapshot's MANIFEST."""
    meta, files = {}, {}
    for line in (directory / MANIFEST).read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            k, _, v = line[2:].partition(": ")
            meta[k] = v
        elif line.strip():
            digest, _, name = line.partition("  ")
            files[name] = digest
    return meta, files


def verify_manifest(directory: Path) -> list[str]:
    """What is wrong with a snapshot directory: [] when every file checks."""
    if not (directory / MANIFEST).is_file():
        return [f"{directory}: no {MANIFEST}"]
    try:
        _, files = read_manifest(directory)
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{directory / MANIFEST}: {exc}"]
    problems = []
    for name in (DUMP, FINGERPRINT) + CONTAINERS:
        if name not in files:
            problems.append(f"{directory}: {MANIFEST} does not list {name}")
    for name, digest in files.items():
        p = directory / name
        if not p.is_file():
            problems.append(f"{p}: missing")
        elif sha256_file(p) != digest:
            problems.append(f"{p}: sha256 differs from {MANIFEST}")
    return problems


def read_fingerprint(directory: Path) -> dict | None:
    try:
        return json.loads((directory / FINGERPRINT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def same_content(a: dict | None, b: dict | None) -> bool:
    """Two fingerprints describe the same database content.

    Only ``content`` counts; when and from where the fingerprint was taken
    does not.
    """
    if not a or not b:
        return False
    return (a.get("fingerprint_version") == b.get("fingerprint_version")
            and a.get("content") == b.get("content"))


def resolve_latest(root: Path) -> Path | None:
    """The snapshot directory ``root/latest`` points at, or None."""
    link = root / LATEST
    if not link.is_symlink():
        return None
    target = link.resolve()
    return target if target.is_dir() else None


def swap_symlink(directory: Path, name: str, target: str) -> None:
    """Point ``directory/name`` at ``target`` (relative) in one rename.

    A reader following the link at any moment finds the old target or the
    new one, never nothing.
    """
    link = directory / name
    if link.exists() and not link.is_symlink():
        raise SnapshotError(f"{link} exists and is not a symlink; not replacing it")
    tmp = directory / f".{name}.{os.getpid()}.tmp"
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)


def tree_size(path: Path) -> int:
    """Bytes in the regular files below ``path``, symlinks not followed."""
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                with contextlib.suppress(OSError):
                    total += os.path.getsize(p)
    return total


def human(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000.0
    return f"{n:.1f} GB"


def remove_stale_builds(directory: Path) -> list[str]:
    """Delete half-built directories a killed run left behind. Call under the lock."""
    removed = []
    if directory.is_dir():
        for p in directory.iterdir():
            if p.name.startswith(BUILD_PREFIX) and p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
                removed.append(p.name)
    return removed


class Lock:
    """An exclusive flock on ``path``, waited for up to ``timeout_s``."""

    def __init__(self, path: Path, timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
                 poll_s: float = 0.5):
        self.path, self.timeout_s, self.poll_s = path, timeout_s, poll_s
        self._fh = None

    def __enter__(self):
        self._fh = open(self.path, "a")
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise SnapshotError(
                        f"{self.path} is still locked after {self.timeout_s:.0f} s: another "
                        f"snapshot is running (cron or a writer's hook). Try again later.")
                time.sleep(self.poll_s)

    def __exit__(self, *exc):
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        return False


# ---------------------------------------------------------------------------
# Backup copy
# ---------------------------------------------------------------------------

def backup_ready(backup_dir: Path, mount: str) -> tuple[bool, str]:
    """(usable, why not). ``mount`` empty: no mount check, the directory is made."""
    if mount:
        if not os.path.ismount(mount):
            return False, (f"{mount} is not mounted (the backup disk; it was found "
                           f"unmounted after a reboot once)")
        if not os.path.realpath(backup_dir).startswith(os.path.realpath(mount) + os.sep):
            return False, f"{backup_dir} is not below the mount point {mount}"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"cannot create {backup_dir}: {exc}"
    return True, ""


def copy_snapshot(src: Path, backup_dir: Path) -> bool:
    """Copy one snapshot directory into ``backup_dir`` atomically.

    False when it is already there with the same MANIFEST. A copy there with
    a different MANIFEST is left alone and reported.
    """
    dest = backup_dir / src.name
    if dest.exists():
        if (dest / MANIFEST).is_file() and \
                (dest / MANIFEST).read_bytes() == (src / MANIFEST).read_bytes():
            return False
        raise SnapshotError(f"{dest} exists and differs from {src}; left as it is")
    tmp = Path(tempfile.mkdtemp(prefix=BUILD_PREFIX, dir=backup_dir))
    try:
        for p in src.iterdir():
            if p.is_file() and not p.is_symlink():
                shutil.copy2(p, tmp / p.name)
        problems = verify_manifest(tmp)
        if problems:
            raise SnapshotError("the copy does not verify: " + "; ".join(problems))
        os.chmod(tmp, 0o755)
        os.rename(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return True


def sync_backup(root: Path, backup_dir: Path) -> list[str]:
    """Copy every snapshot of ``root`` the backup lacks; repoint its ``latest``.

    Catches up the snapshots taken while the disk was not mounted.
    """
    remove_stale_builds(backup_dir)
    copied = [s.name for s in snapshot_dirs(root) if copy_snapshot(s, backup_dir)]
    latest = resolve_latest(root)
    if latest is not None and (backup_dir / latest.name).is_dir():
        current = os.readlink(backup_dir / LATEST) if (backup_dir / LATEST).is_symlink() else None
        if current != latest.name:
            swap_symlink(backup_dir, LATEST, latest.name)
    return copied


# ---------------------------------------------------------------------------
# The database side
# ---------------------------------------------------------------------------

def _run(argv: list[str], what: str) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:
        raise SnapshotError(f"{what}: cannot run {argv[0]}: {exc}") from None
    if proc.returncode != 0:
        raise SnapshotError(f"{what} failed (exit {proc.returncode}):\n{proc.stderr.strip()}")
    return proc


def tool_version(program: str) -> str:
    try:
        return subprocess.run([program, "--version"], capture_output=True,
                              text=True).stdout.strip() or "?"
    except OSError:
        return "not found"


def tool_revision() -> str:
    """The git commit of this checkout, with ``-dirty`` when it has local changes."""
    here = Path(__file__).resolve().parent
    try:
        rev = subprocess.run(["git", "-C", str(here), "rev-parse", "--short=12", "HEAD"],
                             capture_output=True, text=True)
        if rev.returncode != 0:
            return "unknown"
        dirty = subprocess.run(["git", "-C", str(here), "status", "--porcelain", "--", "."],
                               capture_output=True, text=True).stdout.strip()
        return rev.stdout.strip() + ("-dirty" if dirty else "")
    except OSError:
        return "unknown"


_STATS_SQL = """
SELECT t.name, t.schema, t.version, t.kind,
       COALESCE(i.n, 0), COALESCE(i.max_ins, 0), COALESCE(v.n, 0)
FROM cond_tables t
LEFT JOIN (SELECT table_name, COUNT(*) AS n, MAX(inserted_at) AS max_ins
           FROM cond_iov GROUP BY table_name) i ON i.table_name = t.name
LEFT JOIN (SELECT table_name, COUNT(*) AS n
           FROM cond_values GROUP BY table_name) v ON v.table_name = t.name
ORDER BY t.name;
"""

#: sha256 of every row of a base table, in text form, sorted
_HASH_SQL = " UNION ALL ".join(
    f"SELECT '{t}', COUNT(*), encode(sha256(convert_to(COALESCE(string_agg(x::text, "
    f"E'\\n' ORDER BY x::text), ''), 'UTF8')), 'hex') FROM {t} x"
    for t in BASE_TABLES) + ";"


class PgSource:
    """The PostgreSQL database a snapshot is taken of, through psql/pg_dump."""

    def __init__(self, conninfo: str):
        if conninfo.startswith("sqlite:"):
            raise SnapshotError("a snapshot is of the PostgreSQL database (pg_dump); "
                                "a SQLite file is its own snapshot")
        self.conninfo = conninfo
        self.ex = cond_export.executor_for(conninfo)
        self.label = self.ex.label
        self.warnings: list[str] = []

    def _query(self, sql: str) -> list[list[str]]:
        try:
            return self.ex.query(sql)
        except LoaderError as exc:
            raise SnapshotError(str(exc)) from None

    def fingerprint(self) -> dict:
        """What changes whenever anything a job or an export reads changes."""
        try:
            version = cond_export.schema_version(self.ex)
            tables = {}
            for name, schema, tversion, kind, n_iov, max_ins, n_cells in \
                    self._query(_STATS_SQL):
                max_row, active = cond_export.table_fingerprint(self.ex, name)
                tables[name] = {"schema": schema, "version": int(tversion), "kind": kind,
                                "max_row_id": max_row, "active_intervals": active,
                                "intervals": int(n_iov), "cells": int(n_cells),
                                "max_inserted_at": int(max_ins or 0)}
        except LoaderError as exc:
            raise SnapshotError(str(exc)) from None
        rows = {r[0]: {"rows": int(r[1]), "sha256": r[2]} for r in self._query(_HASH_SQL)}
        max_ins = max((t["max_inserted_at"] for t in tables.values()), default=0)
        return {"fingerprint_version": FINGERPRINT_VERSION,
                "content": {"schema_version": version, "max_inserted_at": max_ins,
                            "tables": tables, "base_tables": rows}}

    def server_version(self) -> str:
        rows = self._query("SELECT version();")
        return rows[0][0] if rows else "?"

    def dump(self, path: Path) -> str:
        """pg_dump -Fc into ``path``; returns the checked ``pg_restore --list``."""
        _run(["pg_dump", "--format=custom", "--no-owner", f"--file={path}",
              f"--dbname={self.conninfo}"], f"pg_dump of {self.label}")
        listing = _run(["pg_restore", "--list", str(path)], f"pg_restore --list {path}").stdout
        have = set(re.findall(r"TABLE DATA \S+ (cond_\w+)", listing))
        missing = [t for t in BASE_TABLES if t not in have]
        if missing:
            raise SnapshotError(f"{path}: the dump has no data for {', '.join(missing)}")
        return listing

    def export(self, out_dir: Path, order_from: Path | None) -> str:
        """The five containers into ``out_dir`` through ``pg2json --check --strict``.

        --strict makes a table of the database that no container names an
        error (exit 3) instead of a warning, so a snapshot can never be
        silently incomplete: the job reading it would lack that table. Any
        failure raises, with pg2json's stderr in the message, which main()
        prints on stderr even with --quiet. Returns stdout and stderr, the
        snapshot's pg2json.log; stderr lines of a successful export are kept
        in ``self.warnings`` as well.
        """
        argv = [self.conninfo, "--out-dir", str(out_dir), "--check", "--strict",
                "--order-from", str(order_from if order_from is not None else out_dir)]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = pg2json.main(argv)
        except SystemExit as exc:            # argparse: e.g. a pg2json without --strict
            rc = exc.code if isinstance(exc.code, int) else 2
        stderr = err.getvalue().strip()
        if rc != 0:
            raise SnapshotError(f"pg2json --check --strict failed (exit {rc}), nothing "
                                f"published:\n{stderr or out.getvalue().strip()}")
        missing = [f for f in CONTAINERS if not (out_dir / f).is_file()]
        if missing:
            raise SnapshotError(f"pg2json wrote no {', '.join(missing)}")
        self.warnings = [f"pg2json: {line}" for line in stderr.splitlines() if line.strip()]
        return out.getvalue() + (f"--- stderr\n{stderr}\n" if stderr else "")


# ---------------------------------------------------------------------------
# One snapshot
# ---------------------------------------------------------------------------

class Result:
    def __init__(self):
        self.status = ""            # "written" | "unchanged"
        self.name = ""              # the snapshot directory's name
        self.path: Path | None = None
        self.previous: Path | None = None
        self.backup = ""            # what happened to the backup copy, one line
        self.backup_ok = True
        self.warnings: list[str] = []
        self.total_bytes = 0
        self.n_snapshots = 0


def _build(source, root: Path, name: str, previous: Path | None, order_from: Path | None,
           taken: _dt.datetime, log) -> tuple[Path, dict]:
    """Build a complete snapshot in a temp directory in ``root``; (its path, fingerprint)."""
    tmp = Path(tempfile.mkdtemp(prefix=BUILD_PREFIX, dir=root))
    try:
        before = source.fingerprint()
        log(f"pg_dump -Fc of {source.label}")
        listing = source.dump(tmp / DUMP)
        (tmp / DUMP_LIST).write_text(listing, encoding="utf-8")
        hint = order_from if order_from is not None else previous
        log(f"pg2json --check --strict (order from {hint or 'nothing: the database order'})")
        (tmp / EXPORT_LOG).write_text(source.export(tmp, hint), encoding="utf-8")
        after = source.fingerprint()
        if not same_content(before, after):
            raise _Moved()
        fp = dict(after)
        fp.update({"snapshot": name, "taken_utc": taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "source": source.label})
        (tmp / FINGERPRINT).write_text(json.dumps(fp, indent=2, sort_keys=True) + "\n",
                                       encoding="utf-8")
        meta = [("snapshot", name),
                ("taken_utc", fp["taken_utc"]),
                ("source", source.label),
                ("server_version", source.server_version()),
                ("pg_dump", tool_version("pg_dump")),
                ("pg_restore", tool_version("pg_restore")),
                ("psql", tool_version("psql")),
                ("tool", f"pioneer.conddb.snapshot {tool_revision()}"),
                ("previous", previous.name if previous else "none"),
                ("order_from", str(hint) if hint else "none"),
                ("check", "sha256sum -c MANIFEST")]
        write_manifest(tmp, meta)
        problems = verify_manifest(tmp)
        if problems:
            raise SnapshotError("; ".join(problems))
        for p in tmp.iterdir():
            os.chmod(p, 0o644)
        os.chmod(tmp, 0o755)
        return tmp, fp
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


class _Moved(Exception):
    """The database changed while the snapshot was being built."""


def take_snapshot(source, root: Path, *, force: bool = False, order_from: Path | None = None,
                  backup_dir: Path | None = None, backup_mount: str = DEFAULT_BACKUP_MOUNT,
                  lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S, log=lambda msg: None,
                  now=utc_now) -> Result:
    """Everything one run does, under the lock. Raises SnapshotError on failure."""
    res = Result()
    root = root.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    with Lock(root / LOCK_NAME, lock_timeout_s):
        for stale in remove_stale_builds(root):
            res.warnings.append(f"removed {root / stale}, left by an interrupted run")
        previous = resolve_latest(root)
        res.previous = previous
        current = source.fingerprint()
        unchanged = False
        if previous is not None and same_content(current, read_fingerprint(previous)):
            problems = verify_manifest(previous)
            if problems:
                res.warnings.append("the latest snapshot does not verify, taking a new one: "
                                    + "; ".join(problems))
            else:
                unchanged = True
        if unchanged and not force:
            res.status, res.name, res.path = "unchanged", previous.name, previous
        else:
            for attempt in range(1, CONSISTENCY_TRIES + 1):
                taken = now()
                name = unique_name(root, utc_stamp(taken))
                try:
                    tmp, _fp = _build(source, root, name, previous, order_from, taken, log)
                    break
                except _Moved:
                    res.warnings.append(f"the database changed during snapshot attempt "
                                        f"{attempt}; starting again")
            else:
                raise SnapshotError(f"the database kept changing during {CONSISTENCY_TRIES} "
                                    f"attempts; no snapshot written")
            res.warnings += getattr(source, "warnings", [])
            os.rename(tmp, root / name)
            log(f"wrote {root / name}")
            swap_symlink(root, LATEST, name)
            log(f"{root / LATEST} -> {name}")
            res.status, res.name, res.path = "written", name, root / name

        if backup_dir is not None:
            ok, why = backup_ready(backup_dir, backup_mount)
            if not ok:
                res.backup_ok = False
                res.backup = f"backup skipped: {why}"
            else:
                try:
                    copied = sync_backup(root, backup_dir)
                    res.backup = (f"backup: copied {', '.join(copied)} to {backup_dir}"
                                  if copied else f"backup: {backup_dir} is up to date")
                except (OSError, SnapshotError) as exc:
                    res.backup_ok = False
                    res.backup = f"backup to {backup_dir} failed: {exc}"
        snaps = snapshot_dirs(root)
        res.n_snapshots = len(snaps)
        res.total_bytes = sum(tree_size(s) for s in snaps)
    return res


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m pioneer.conddb.snapshot",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See 'Snapshots and backups' in pioneer/conddb/README.md.")
    ap.add_argument("--conninfo", default=DEFAULT_CONNINFO,
                    help=f"libpq conninfo of the database, no password (~/.pgpass) "
                         f"(default: {DEFAULT_CONNINFO}; cond_viewer is enough)")
    env = os.environ
    ap.add_argument("--root", type=Path, default=Path(env.get(ENV_ROOT, DEFAULT_ROOT)),
                    help=f"where snapshots and 'latest' go (default: ${ENV_ROOT}, "
                         f"else {DEFAULT_ROOT})")
    ap.add_argument("--backup-dir", type=Path,
                    default=Path(env.get(ENV_BACKUP_DIR, DEFAULT_BACKUP_DIR)),
                    help=f"second copy of every snapshot (default: ${ENV_BACKUP_DIR}, "
                         f"else {DEFAULT_BACKUP_DIR})")
    ap.add_argument("--backup-mount", default=env.get(ENV_BACKUP_MOUNT, DEFAULT_BACKUP_MOUNT),
                    metavar="DIR",
                    help=f"copy only when DIR is a mount point (default: ${ENV_BACKUP_MOUNT}, "
                         f"else {DEFAULT_BACKUP_MOUNT}); '' checks nothing and creates "
                         f"--backup-dir")
    ap.add_argument("--no-backup", action="store_true", help="do not copy to --backup-dir")
    ap.add_argument("--order-from", type=Path, metavar="DIR",
                    help="take the containers' order from DIR instead of the previous "
                         "snapshot (the first snapshot: the git reco_testbeam/conditions)")
    ap.add_argument("--force", action="store_true",
                    help="write a snapshot even when the database is unchanged")
    ap.add_argument("--quiet", action="store_true",
                    help="cron: no output when nothing changed, one line when a snapshot "
                         "was written; warnings and errors always go to stderr")
    ap.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT_S, metavar="S",
                    help=f"seconds to wait for another snapshot to finish "
                         f"(default: {DEFAULT_LOCK_TIMEOUT_S:.0f})")
    args = ap.parse_args(argv)

    def log(msg: str) -> None:
        if not args.quiet:
            print(f"[snapshot] {msg}", flush=True)

    root = args.root.expanduser()
    try:
        source = PgSource(args.conninfo)
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        res = take_snapshot(source, root, force=args.force, order_from=args.order_from,
                            backup_dir=None if args.no_backup else args.backup_dir.expanduser(),
                            backup_mount=args.backup_mount, lock_timeout_s=args.lock_timeout,
                            log=log)
    except (SnapshotError, OSError) as exc:
        stamp = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"{stamp} conddb snapshot FAILED ({source.label}): {exc}", file=sys.stderr)
        latest = resolve_latest(root)
        print(f"  {root / LATEST} still points at "
              f"{latest.name if latest else 'nothing'}", file=sys.stderr)
        return 1

    for w in res.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if not res.backup_ok:
        print(f"warning: {res.backup}; the snapshot itself is fine", file=sys.stderr)
    total = f"{res.n_snapshots} snapshot(s), {human(res.total_bytes)} in {root}"
    if res.status == "unchanged":
        log(f"unchanged since {res.name}: nothing written ({source.label})")
        if res.backup:
            log(res.backup)
        log(total)
        if args.quiet and res.backup.startswith("backup: copied"):
            print(f"{utc_now().strftime('%Y-%m-%dT%H:%M:%SZ')} conddb snapshot unchanged; "
                  f"{res.backup}")
        return 0
    if args.quiet:
        print(f"{utc_now().strftime('%Y-%m-%dT%H:%M:%SZ')} conddb snapshot {res.name} written "
              f"from {source.label}; latest -> {res.name}; "
              f"{res.backup or 'no backup'}; {total}")
    else:
        if res.backup:
            log(res.backup)
        log(total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
