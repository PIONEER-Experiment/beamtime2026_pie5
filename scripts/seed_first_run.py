"""Seed the very first run sequence of the bench loop.

The loop is self-sustaining once running -- each tuning proposal schedules the
next sequence -- but nothing schedules the zeroth one. This does, with the
same shape check_for_updates() uses, at the wdscalar backend's initial knobs.

Run it with the DB env sourced:

    source scripts/wdscalers-db-env.sh
    $WDS_PYTHON scripts/seed_first_run.py [--seq-id 1] [--p1 5.0] [--p2 5.0]
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from pioneer.nearline.run import bench_sequence, midas_run_sequence  # noqa: E402
from pioneer.rundb.interface import interface  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--seq-id", type=int,
                    default=int(os.environ.get("NEARLINE_TARGET_SEQ", "1")),
                    help="target_position pattern: 1 = single run, 2 = 5-point")
    ap.add_argument("--p1", default="5.0")
    ap.add_argument("--p2", default="5.0")
    args = ap.parse_args()

    iface = interface(user="bot", password="bot")
    mrs = midas_run_sequence(iface)
    mrs.set_config_list("dummy", [{"p1": str(args.p1), "p2": str(args.p2)}])
    mrs.set_subsequence(bench_sequence(iface, seq_id=args.seq_id))
    run_ids = mrs.schedule()
    print(f"seeded sequence with runs {run_ids} "
          f"(seq_id={args.seq_id}, p1={args.p1}, p2={args.p2})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
