#!/bin/bash
# Run the ODB -> rundb slow-control logger against WDSCALERS.
#
#     tmux new -s wds-pioneer-logger scripts/run_pioneer_logger.sh
#
# --midas-expt is passed explicitly: the logger's built-in default experiment
# is a hardcoded "test", and nothing else overrides it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"

source /home/pioneer/josh/wavedream-scalar-readout/scripts/wdscalers-env.sh
# shellcheck source=wdscalers-db-env.sh
source "$SCRIPT_DIR/wdscalers-db-env.sh"

export PYTHONPATH="$REPO/python${PYTHONPATH:+:$PYTHONPATH}"

exec "$WDS_PYTHON" -m pioneer.rundb.logger --midas-expt "$WDS_EXPT_NAME"
