"""Render nearline_job.py into the complete, standalone job for one MIDAS file.

nearline_job.py is both the file to edit and the template: it carries twelve
${name} placeholders inside string literals, so it is valid Python unrendered
and takes its input from the environment then. Filling the placeholders turns
it into a job that names its own input, output, event limit, light mode,
conditions source and conditions directory, and therefore ignores every NL_*
variable.

The conditions source is resolved HERE, not in the job: a database named by a
libpq service (the job's default, "db:service=pioneer-conditions") is expanded
through ~/.pg_service.conf into the explicit, password-free
"host= port= dbname= user=" string, and a JSON directory into an absolute path
with its symlinks resolved (so "json:~/bt2026/conddb-snapshots/latest" records
the snapshot it read, not the link that moves on). The rendered file therefore
says which server or which snapshot served it, whatever the service file or
the link says later.
That rendered copy is written next to the outputs as <filebase>.py and is the
record of what processed the run: `gaudirun.py run00175.py` reproduces it.

Both callers come through here -- pioneer.nearline.jobs.GaudiJob for the
daemon, pioneer.nearline.process for a shifter -- so the artefacts differ only
in rendered_at, rendered_by and job_id.

The standard library only. pioneer.nearline.jobs needs psycopg, which is not
installed in the testbeam-midas container; this module must import there.
"""

import argparse
import ast
import getpass
import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from pioneer.conddb.pgservice import resolve_conninfo

# The names nearline_job.py's _RENDERED dict carries. Every one must be gone
# from the rendered text: a survivor would reach gaudirun.py as a path that
# does not exist or an int() over a placeholder, several minutes into a run in
# the worst case, so it is caught here instead.
PLACEHOLDERS = ("in_file", "out_file", "evt_max", "conditions_dir", "conditions",
                "rendered_at", "rendered_by", "job_source", "job_git",
                "job_id", "run_id", "light")


# What the job appends to its output's stem for the histogram file:
# run00175.root -> run00175_hists.root (hist_file() in nearline_job.py).
HISTS_SUFFIX = "_hists"


def registered_file_name(filebase: str, light: bool = False) -> str:
    """The output a nearline job registers in the run database for one input.

    The full job registers its RNTuple, <filebase>.root. The light job writes
    no RNTuple, so it registers the file it does write, <filebase>_hists.root.
    The run database splits a name on its FIRST dot, so both rows have fileext
    "root" and differ in filebase: runNNNNN_SSSSS against runNNNNN_SSSSS_hists.
    """
    return f"{filebase}{HISTS_SUFFIX}.root" if light else f"{filebase}.root"


def hists_file_name(filebase: str) -> str:
    """The histogram file behind one nearline "root" row of the run database.

    Takes the row's filebase of either kind (see registered_file_name):
    runNNNNN_SSSSS and runNNNNN_SSSSS_hists both give runNNNNN_SSSSS_hists.root.
    Everything that reads histograms back from the database rows (the merge job,
    the tuning loop) goes through here, so it is right for a full and a light
    host alike.
    """
    if filebase.endswith(HISTS_SUFFIX):
        return f"{filebase}.root"
    return f"{filebase}{HISTS_SUFFIX}.root"


# The libpq service a bare "db" means; nearline_job.py has the same constant.
DEFAULT_SERVICE = "pioneer-conditions"


def split_conditions(spec) -> tuple[str, str]:
    """A conditions source -> ("db", conninfo) or ("json", dir or "").

    The grammar of the job's CONDITIONS setting, of NL_CONDITIONS and of
    process.py --conditions:

        db                    the service pioneer-conditions
        db:NAME               the service NAME (no "=" in it); "db:" alone
                              is an error, as is "json:" alone
        db:k=v k=v ...        a libpq conninfo, e.g. "service=NAME" or
                              "host=H port=P dbname=D user=U"
        json                  the job's CONDITIONS_DIR
        json:DIR              the JSON containers in DIR

    ValueError for anything else. nearline_job.py carries the same function
    (_split_conditions), because a rendered job must run without this module.
    """
    kind, colon, arg = str(spec).strip().partition(":")
    kind, arg = kind.strip().lower(), arg.strip()
    if colon and not arg:
        # "db:" is not "db": an empty service or directory is a mistake, not a default.
        raise ValueError(f"conditions source '{kind}:' has nothing after the colon: write "
                         f"'{kind}' for the default, or name the service or directory")
    if kind == "db":
        if not arg:
            arg = "service=" + DEFAULT_SERVICE
        elif "=" not in arg:
            arg = "service=" + arg
        return "db", arg
    if kind == "json":
        return "json", arg
    # The kind only: the rest of the string could hold a password.
    raise ValueError(f"conditions source starting with {kind!r} is not one of 'db', "
                     "'db:SERVICE', 'db:<libpq conninfo>', 'json', 'json:DIR'")


def resolve_conditions(spec) -> str:
    """The conditions source as a rendered job carries it.

    db   -> "db:" + the explicit conninfo (service expanded, password dropped);
            pioneer.conddb.pgservice.ServiceNotFound when the service is not
            defined on this host.
    json -> "json" unchanged (the job's CONDITIONS_DIR), or "json:" + DIR made
            absolute with ~ expanded and symlinks resolved.
    """
    kind, arg = split_conditions(spec)
    if kind == "db":
        return "db:" + resolve_conninfo(arg)
    if not arg:
        return "json"
    return "json:" + str(Path(arg).expanduser().resolve())


_CONDITIONS_SETTING = re.compile(r"^CONDITIONS = (.+)$", re.MULTILINE)


def job_conditions(source_text: str) -> str:
    """The CONDITIONS setting the job file commits to, read from its text.

    Read, not imported: the job file needs Gaudi to execute. The setting must be
    one string literal on one line, which is what the settings block holds.
    """
    found = _CONDITIONS_SETTING.findall(source_text)
    if len(found) != 1:
        raise RuntimeError("the job file must assign CONDITIONS exactly once at the start "
                           f"of a line; found {len(found)} assignments")
    value = None
    # With and without a trailing comment; a "#" inside the string survives the first.
    for text in (found[0].strip(), found[0].split("#", 1)[0].strip()):
        try:
            value = ast.literal_eval(text)
            break
        except (ValueError, SyntaxError):
            continue
    if value is None:
        raise RuntimeError(f"the job file's CONDITIONS is not a string literal: {found[0]}")
    if not isinstance(value, str):
        raise RuntimeError(f"the job file's CONDITIONS is not a string: {value!r}")
    return value


def _placeholder(name: str) -> str:
    """The template spelling of one name, as it appears in the job file."""
    return "${" + name + "}"


def _job_git(job_source: Path) -> str:
    """The job file's commit, as `git describe` sees it, or "unknown".

    Provenance, not a check: the rendered file should say which version of the
    job it came from, including whether the tree was dirty at the time (it
    usually is during a beamtime). Nothing here may fail the render -- no git,
    no repository, a timeout, a detached worktree all mean the same thing and
    all give "unknown".
    """
    try:
        out = subprocess.run(
            # safe.directory=*: the daemon or a shifter often runs as a different
            # user than the checkout's owner (root in the container against a
            # bind-mounted repo), and git then refuses to look at the tree at all.
            ["git", "-c", "safe.directory=*", "-C", str(job_source.parent),
             "describe", "--always", "--dirty", "--abbrev=12"],
            capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        return "unknown"
    described = out.stdout.strip()
    return described if out.returncode == 0 and described else "unknown"


def render_job(in_file, out_file, *, evt_max=-1, job_id="", run_id="",
               light=False, conditions=None, job_source=None, target=None) -> Path:
    """Write the rendered job for one file and return the path it was written to.

    in_file and out_file are resolved to absolute paths, because the rendered
    file is run from wherever the caller happens to be and a relative path in
    it would point somewhere else the second time.

    light renders the job's LIGHT setting on ("1") or off ("0"): the light job
    writes histograms only (see LIGHT in nearline_job.py). It is an argument, not
    an environment variable, because it is the caller's choice per job -- the
    daemon's --light, process.py's --light -- and a stray NL_LIGHT in the
    daemon's environment must not change what it produces.

    conditions is the source (grammar: split_conditions). None takes
    NL_CONDITIONS from the CALLER's environment, else the job file's own
    CONDITIONS setting, the database. It is resolved now (resolve_conditions),
    so a service this host does not define raises ServiceNotFound here, before
    any file is written. conditions_dir is baked in from NL_CONDITIONS_DIR in
    the caller's environment, which is where a host-local conditions tree
    belongs (pinky has no /simulation; the ODB specs are read from it in both
    modes). Empty means the job's own default, the normal case in the container.
    """
    job_source = Path(job_source) if job_source else Path(__file__).with_name("nearline_job.py")
    job_source = job_source.resolve()
    in_path = Path(in_file).resolve()
    out_path = Path(out_file).resolve()
    # run00175.root -> run00175.py, in the output directory: the rendered job,
    # the RNTuple and the histogram file sit together under one name.
    target = Path(target) if target else out_path.parent / (out_path.stem + ".py")

    source = job_source.read_text()
    if conditions is None:
        conditions = os.environ.get("NL_CONDITIONS") or job_conditions(source)

    mapping = {
        "in_file": str(in_path),
        "out_file": str(out_path),
        "evt_max": str(int(evt_max)),
        "conditions_dir": os.environ.get("NL_CONDITIONS_DIR", ""),
        # Escaped for the double-quoted literal it lands in: a conninfo may quote a
        # value, and libpq's quoting uses backslashes.
        "conditions": resolve_conditions(conditions).replace("\\", "\\\\").replace('"', '\\"'),
        # Seconds resolution and UTC: a beamtime spans time zones and the
        # field is read by people comparing it to a run's start time.
        "rendered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rendered_by": f"{getpass.getuser()}@{socket.gethostname()}",
        "job_source": str(job_source),
        "job_git": _job_git(job_source),
        "job_id": str(job_id),
        "run_id": str(run_id),
        "light": "1" if light else "0",
    }

    # The job file and this module have to agree on all twelve names, in both
    # directions. A name missing from the file means the job's _RENDERED dict
    # has lost a key and the job will die on the KeyError; a name still there
    # after substitution means the file asks for something this module does not
    # fill, and the job would run with a placeholder for a path. Both are edits
    # to one of these two files that forgot the other, so both are reported by
    # name and neither reaches gaudirun.py.
    missing = [name for name in PLACEHOLDERS if _placeholder(name) not in source]
    if missing:
        raise RuntimeError(
            f"{job_source} has no placeholder for: " + ", ".join(missing)
            + ". The job file's rendered block and pioneer.nearline.render disagree; "
              "put the placeholders back or drop the names from PLACEHOLDERS.")

    # safe_substitute, not substitute: a dollar sign someone puts in a comment
    # in the job file is then kept verbatim instead of raising, so an edit to
    # nearline_job.py can never stop the daemon from processing runs. The
    # twelve names that DO matter were just checked, and are checked again now
    # that they should all be gone.
    rendered = Template(source).safe_substitute(mapping)

    survivors = [name for name in PLACEHOLDERS if _placeholder(name) in rendered]
    if survivors:
        raise RuntimeError(
            f"{job_source}: placeholders still unfilled after substitution: "
            + ", ".join(survivors)
            + ". The job file and this renderer disagree about the rendered block; "
              "fix one of them rather than running the result.")

    # A syntax error in the rendered text is a syntax error in the job file,
    # and it should surface here -- where the person who edited it is looking
    # -- and not when gaudirun.py imports Gaudi and then chokes.
    compile(rendered, str(target), "exec")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered)
    return target


def main(argv=None) -> int:
    """Render only; print the path written. The daemon and process.py call
    render_job directly, so this is for the harness, the tests and a hand
    render of a job to edit before running it."""
    parser = argparse.ArgumentParser(
        prog="python -m pioneer.nearline.render",
        description="Render nearline_job.py into a standalone job for one MIDAS file.")
    parser.add_argument("in_file", help="the MIDAS file the rendered job reads")
    parser.add_argument("out_file", help="the RNTuple the rendered job writes (x.root)")
    parser.add_argument("--evt-max", type=int, default=-1,
                        help="events to process; -1 (default) is the whole file")
    parser.add_argument("--light", action="store_true",
                        help="render the light job: histograms only, no RNTuple, "
                             "no timewalk histograms, no wide SMA dt plot, no SMA "
                             "raw-word diagnostics")
    parser.add_argument("--conditions", default=None,
                        help="conditions source: db[:SERVICE|CONNINFO] or json[:DIR] "
                             "(default: $NL_CONDITIONS, else the job's CONDITIONS, the database)")
    parser.add_argument("--target", default=None,
                        help="where to write the rendered job "
                             "(default: <out_file directory>/<out_file stem>.py)")
    parser.add_argument("--job", default=None,
                        help="the job file to render (default: nearline_job.py next to this module)")
    args = parser.parse_args(argv)

    written = render_job(args.in_file, args.out_file, evt_max=args.evt_max,
                         light=args.light, conditions=args.conditions,
                         job_source=args.job, target=args.target)
    print(written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
