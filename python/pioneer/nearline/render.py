"""Render nearline_job.py into the complete, standalone job for one MIDAS file.

nearline_job.py is both the file to edit and the template: it carries eleven
${name} placeholders inside string literals, so it is valid Python unrendered
and takes its input from the environment then. Filling the placeholders turns
it into a job that names its own input, output, event limit, conditions
directory and database connections, and therefore ignores every NL_* variable.
That rendered copy is written next to the outputs as <filebase>.py and is the
record of what processed the run: `gaudirun.py run00175.py` reproduces it.

Both callers come through here -- pioneer.nearline.jobs.GaudiJob for the
daemon, pioneer.nearline.process for a shifter -- so the artefacts differ only
in rendered_at, rendered_by and job_id.

The standard library only. pioneer.nearline.jobs needs psycopg, which is not
installed in the testbeam-midas container; this module must import there.
"""

import argparse
import getpass
import os
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from string import Template

# The names nearline_job.py's _RENDERED dict carries. Every one must be gone
# from the rendered text: a survivor would reach gaudirun.py as a path that
# does not exist or an int() over a placeholder, several minutes into a run in
# the worst case, so it is caught here instead.
PLACEHOLDERS = ("in_file", "out_file", "evt_max", "conditions_dir", "pg",
                "rendered_at", "rendered_by", "job_source", "job_git",
                "job_id", "run_id")


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
               job_source=None, target=None) -> Path:
    """Write the rendered job for one file and return the path it was written to.

    in_file and out_file are resolved to absolute paths, because the rendered
    file is run from wherever the caller happens to be and a relative path in
    it would point somewhere else the second time.

    conditions_dir and pg are baked in from NL_CONDITIONS_DIR and NL_PG in the
    CALLER's environment, which is where a host-local conditions tree (pinky
    has no /simulation) and a campaign-database connection belong. Empty means
    "the job's own defaults", which is the normal case in the container.
    """
    job_source = Path(job_source) if job_source else Path(__file__).with_name("nearline_job.py")
    job_source = job_source.resolve()
    in_path = Path(in_file).resolve()
    out_path = Path(out_file).resolve()
    # run00175.root -> run00175.py, in the output directory: the rendered job,
    # the RNTuple and the histogram file sit together under one name.
    target = Path(target) if target else out_path.parent / (out_path.stem + ".py")

    mapping = {
        "in_file": str(in_path),
        "out_file": str(out_path),
        "evt_max": str(int(evt_max)),
        "conditions_dir": os.environ.get("NL_CONDITIONS_DIR", ""),
        "pg": os.environ.get("NL_PG", ""),
        # Seconds resolution and UTC: a beamtime spans time zones and the
        # field is read by people comparing it to a run's start time.
        "rendered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rendered_by": f"{getpass.getuser()}@{socket.gethostname()}",
        "job_source": str(job_source),
        "job_git": _job_git(job_source),
        "job_id": str(job_id),
        "run_id": str(run_id),
    }

    source = job_source.read_text()

    # The job file and this module have to agree on all eleven names, in both
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
    # eleven names that DO matter were just checked, and are checked again now
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
    parser.add_argument("--target", default=None,
                        help="where to write the rendered job "
                             "(default: <out_file directory>/<out_file stem>.py)")
    parser.add_argument("--job", default=None,
                        help="the job file to render (default: nearline_job.py next to this module)")
    args = parser.parse_args(argv)

    written = render_job(args.in_file, args.out_file, evt_max=args.evt_max,
                         job_source=args.job, target=args.target)
    print(written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
