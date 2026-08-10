#!/bin/bash
# Stop the private bench rundb cluster.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=wdscalers-db-env.sh
source "$SCRIPT_DIR/wdscalers-db-env.sh"
if ! "$PGBIN/pg_ctl" -D "$PGDATA" status > /dev/null 2>&1; then
    echo "not running ($PGDATA)"
    exit 0
fi
exec "$PGBIN/pg_ctl" -D "$PGDATA" -m fast stop
