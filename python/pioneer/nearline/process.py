"""Process one MIDAS file by hand: the daemon's flow without the daemon.

    python -m pioneer.nearline.process /workdir/scratch/online/run00175.mid.lz4 --out-dir out

renders nearline_job.py for that file and runs gaudirun.py on the result, so a
shifter gets exactly the artefacts the daemon would have produced -- the same
<filebase>.py, .root and _hists.root, from the same renderer -- without a
database, a scheduler or a hand-written NL_MIDAS line.

--light renders the light job, as a daemon started with --light does (pinky):
histograms only, so the artefacts are the <filebase>.py and the _hists.root, and
no RNTuple.

--conditions picks where the constants come from:

    --conditions db                     the service pioneer-conditions (the default)
    --conditions db:NAME                another libpq service
    --conditions db:"host=H dbname=D user=U"   a server named directly
    --conditions json:DIR               the JSON containers in DIR, e.g. the latest
                                        snapshot, ~/bt2026/conddb-snapshots/latest
    --conditions json                   the JSON containers in the job's CONDITIONS_DIR

The last two are the manual path while the conditions database is down (README,
"Conditions DB down"). Without the flag, NL_CONDITIONS in this shell, else the
job's own CONDITIONS setting, the database.

Because the rendered job ignores NL_*, the .py left in the output directory
re-runs the same processing later whatever the environment then says; and
because it is rendered HERE, the conditions source (a service expanded into the
explicit host/port/dbname/user it names, a snapshot link resolved to the
directory it points at) and NL_CONDITIONS_DIR in this shell are baked into it,
exactly as the daemon bakes in its own.

The standard library and render only. No pioneer.rundb, so this runs in the
testbeam-midas container (no psycopg) and on pinky alike.
"""

import argparse
import re
import shutil
import subprocess
from pathlib import Path

from pioneer.conddb.pgservice import ServiceNotFound
from pioneer.nearline.render import render_job

# What to source before gaudirun.py exists. Printed rather than guessed at: the
# job needs the Gaudi environment, ROOT, and the install tree's libraries and
# genConf, and getting one of the three wrong is the usual reason a hand run
# fails at import time. On pinky and piana it is this repository's
# software/env.sh; inside the testbeam-midas container it is the three lines
# after it.
_ENV_SH = Path(__file__).resolve().parents[3] / "software" / "env.sh"
ENV_HINT = (f"source {_ENV_SH}",
            "or, inside the testbeam-midas container:",
            "source /software/setup_container_env.sh",
            "pushd /software/root/install && source bin/thisroot.sh && popd",
            "source /simulation/docker/setenv.sh")


def file_base(midas_file) -> str:
    """run00175.mid.lz4 -> run00175: the name up to the FIRST dot.

    The same rule the run database uses in rundb.interface.open_file, so a
    file processed by hand and the same file processed by the daemon land on
    the same output names instead of one of them growing a ".mid" in it.
    """
    return Path(midas_file).name.split(".")[0]


def run_id_of(filebase: str) -> str:
    """The digits in run00175 -> "175"; "" when the name carries no number.

    Only provenance -- it goes into the banner of the rendered file. A file
    named after something other than its run number simply says nothing,
    which is better than saying something wrong.
    """
    digits = re.findall(r"\d+", filebase)
    return str(int(digits[0])) if digits else ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pioneer.nearline.process",
        description="Render and run the nearline job on one MIDAS file.")
    parser.add_argument("midas_file", help="the MIDAS file to process (.mid or .mid.lz4)")
    parser.add_argument("--out-dir", default=".",
                        help="directory for the .py, .root and _hists.root (default: here; "
                             "no .root with --light)")
    parser.add_argument("--evt-max", type=int, default=-1,
                        help="events to process; -1 (default) is the whole file")
    parser.add_argument("--light", action="store_true",
                        help="the light job, as the daemon with --light runs it: histograms "
                             "only, no RNTuple, no timewalk histograms, no wide SMA dt plot, "
                             "no SMA raw-word diagnostics")
    parser.add_argument("--conditions", default=None, metavar="SOURCE",
                        help="db[:SERVICE|CONNINFO] (the conditions database; default "
                             "service pioneer-conditions) or json[:DIR] (JSON containers, "
                             "e.g. json:~/bt2026/conddb-snapshots/latest when the database "
                             "is down). Default: $NL_CONDITIONS, else the job's CONDITIONS")
    parser.add_argument("--render-only", action="store_true",
                        help="write the rendered job and stop, without running it")
    parser.add_argument("--job", default=None,
                        help="the job file to render (default: nearline_job.py next to this module)")
    args = parser.parse_args(argv)

    midas_file = Path(args.midas_file)
    filebase = file_base(midas_file)
    out_dir = Path(args.out_dir)
    # Made now, not at write time: the render step and the output stream both
    # want it, and a missing output directory is the one failure a shifter
    # should never have to read a Gaudi backtrace for.
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{filebase}.root"
    try:
        rendered = render_job(midas_file, out_file, evt_max=args.evt_max,
                              job_id="manual", run_id=run_id_of(filebase),
                              light=args.light, conditions=args.conditions,
                              job_source=args.job)
    except ServiceNotFound as exc:
        print(f"[process] {exc}")
        print("[process] Define the service in ~/.pg_service.conf (or set PGSERVICEFILE), or "
              "process from a JSON snapshot instead:")
        print(f"[process]   python -m pioneer.nearline.process {args.midas_file} --out-dir "
              f"{args.out_dir} --conditions json:~/bt2026/conddb-snapshots/latest")
        print("[process] (README.md, \"Conditions DB down\")")
        return 2
    except ValueError as exc:
        print(f"[process] {exc}")
        return 2

    print(f"[process] job        {rendered}")
    # The out_file is still the name the histogram file is derived from, but the
    # light job never writes it.
    print(f"[process] rntuple    {'no RNTuple (light)' if args.light else out_file}")
    print(f"[process] histograms {out_dir / f'{filebase}_hists.root'}")

    if args.render_only:
        print(f"[process] render only; run it with: gaudirun.py {rendered}")
        return 0

    # Checked before launching rather than after: subprocess would raise
    # FileNotFoundError, which says nothing about the three lines missing.
    if shutil.which("gaudirun.py") is None:
        print("[process] gaudirun.py is not on PATH. Source the environment first:")
        for line in ENV_HINT:
            print(f"[process]   {line}")
        return 2

    # The rendered file is the whole configuration, so nothing is passed to
    # gaudirun.py besides it and nothing is put into its environment: this
    # process' own NL_* variables are already baked in or deliberately absent.
    return subprocess.run(["gaudirun.py", str(rendered)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
