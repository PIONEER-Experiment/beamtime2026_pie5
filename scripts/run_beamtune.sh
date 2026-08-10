#!/bin/bash
# Run the beam-tuning service with the wdscalar backend (the bench "minitwin").
#
#     tmux new -s wds-beamtune scripts/run_beamtune.sh
#
# The wdscalar backend is stdlib-only (conda_env: null), so the pion313
# interpreter serves both the HTTP service and the backend worker.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source /home/pioneer/josh/wavedream-scalar-readout/scripts/wdscalers-env.sh
# shellcheck source=wdscalers-db-env.sh
source "$SCRIPT_DIR/wdscalers-db-env.sh"

BEAMTUNE_REPO="${BEAMTUNE_REPO:-/home/pioneer/josh/beam-tuning-client}"
export PYTHONPATH="$BEAMTUNE_REPO${PYTHONPATH:+:$PYTHONPATH}"

cd "$BEAMTUNE_REPO"
exec "$WDS_PYTHON" -m beamtune.serve --backend wdscalar \
    --state-dir "$WDS_RUNDB_ROOT/beamtune-state" \
    --port "${BEAMTUNE_PORT:-8420}"
