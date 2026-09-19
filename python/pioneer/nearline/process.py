"""Process one MIDAS file by hand: the daemon's flow without the daemon.

    python -m pioneer.nearline.process /workdir/scratch/online/run00175.mid.lz4 --out-dir out

renders nearline_job.py for that file and runs gaudirun.py on the result, so a
shifter gets exactly the artefacts the daemon would have produced -- the same
<filebase>.py, .root and _hists.root, from the same renderer -- without a
database, a scheduler or a hand-written NL_MIDAS line.

Because the rendered job ignores NL_*, the .py left in the output directory
re-runs the same processing later whatever the environment then says; and
because it is rendered HERE, NL_CONDITIONS_DIR and NL_PG in this shell are
baked into it, exactly as the daemon bakes in its own.

The standard library and render only. No pioneer.rundb, so this runs in the
testbeam-midas container (no psycopg) and on pinky alike.
"""

import argparse
import re
import shutil
import subprocess
from pathlib import Path

from pioneer.nearline.render import render_job

# What to source before gaudirun.py exists, inside the testbeam-midas
# container. Printed rather than guessed at: the job needs the Gaudi
# environment, ROOT, and the install tree's libraries and genConf, and getting
# one of the three wrong is the usual reason a hand run fails at import time.
ENV_HINT = ("source /software/setup_container_env.sh",
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
                        help="directory for the .py, .root and _hists.root (default: here)")
    parser.add_argument("--evt-max", type=int, default=-1,
                        help="events to process; -1 (default) is the whole file")
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
    rendered = render_job(midas_file, out_file, evt_max=args.evt_max,
                          job_id="manual", run_id=run_id_of(filebase),
                          job_source=args.job)

    print(f"[process] job        {rendered}")
    print(f"[process] rntuple    {out_file}")
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
