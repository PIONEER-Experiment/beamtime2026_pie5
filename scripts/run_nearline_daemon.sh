#!/bin/bash
# Run the NearlineDaemon against WDSCALERS and the private bench rundb.
#
#     tmux new -s wds-nearline scripts/run_nearline_daemon.sh
#
# --midas-expt is passed explicitly. That matters beyond convenience: the
# daemon's fallback parses $MIDAS_EXPTAB when the experiment is unset, and on
# this machine the login environment's exptab names an unrelated experiment
# (LYSO) -- the fallback would attach to it silently. Sourcing wdscalers-env.sh
# repoints both, and the explicit flag makes it impossible to get wrong.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"

source /home/pioneer/josh/wavedream-scalar-readout/scripts/wdscalers-env.sh
# shellcheck source=wdscalers-db-env.sh
source "$SCRIPT_DIR/wdscalers-db-env.sh"

export PYTHONPATH="$REPO/python${PYTHONPATH:+:$PYTHONPATH}"

# Where nearline output (scalars.json) lands; seeds /Nearline/config on the
# daemon's first start. Backup/remote stay dry-run (NEARLINE_REAL_JOBS), so
# their destinations are placeholders that never receive data.
export NEARLINE_DIR="${NEARLINE_DIR:-$WDS_RUNDB_ROOT/nearline}"
export NEARLINE_BACKUP_DIR="${NEARLINE_BACKUP_DIR:-/tmp/wds-nl-backup}"
export NEARLINE_REMOTE="${NEARLINE_REMOTE:-/tmp/wds-nl-remote}"

# Only the scalar-extraction job really executes; see jobs.REAL_JOB_TYPES.
export NEARLINE_REAL_JOBS="${NEARLINE_REAL_JOBS:-nearline}"
export NEARLINE_SCALAR_SCRIPT="${NEARLINE_SCALAR_SCRIPT:-/home/pioneer/josh/wavedream-scalar-readout/analysis/extract_scalars.py}"

# Runs per scan point: 1 = centre-only single run, 2 = the 5-point pattern.
export NEARLINE_TARGET_SEQ="${NEARLINE_TARGET_SEQ:-1}"

# The tuning service.
export BEAMTUNE_URL="${BEAMTUNE_URL:-http://127.0.0.1:8420}"

# --midas-host "" = attach to the local shared memory rather than the mserver
# RPC path that the "localhost" default would select (no mserver runs here).
exec "$WDS_PYTHON" "$REPO/python/pioneer/nearline/daemon.py" \
    --midas-expt "$WDS_EXPT_NAME" --midas-host "" -j 1
