#!/bin/bash
# One-shot provisioning of the private bench rundb cluster.
#
#     scripts/db_provision.sh
#
# Creates the cluster, starts it, creates the database, and applies
# python/pioneer/rundb/db_config.sql exactly once. Refuses to run if the data
# directory already exists: db_config.sql is NOT idempotent (its seed rows and
# CREATE TRIGGER statements duplicate/error on a second pass), so the recovery
# path from a broken cluster is deletion, not re-application:
#
#     scripts/db_down.sh; rm -rf "$WDS_RUNDB_ROOT"; scripts/db_provision.sh
#
# admin.py is deliberately bypassed: it prompts interactively for admin
# credentials three times and adds nothing over the two psql calls below, and
# its --hard-reset drops every non-admin role on the server (fine here, fatal
# on a shared one -- better not to normalise using it).
#
# Auth is `trust`: the cluster listens on loopback only, is owned and used by
# a single OS user, and the schema's `readonly` role has no password at all,
# so password auth could never work for it anyway.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
# shellcheck source=wdscalers-db-env.sh
source "$SCRIPT_DIR/wdscalers-db-env.sh"

if [ ! -x "$PGBIN/initdb" ]; then
    echo "ERROR: no postgres binaries at $PGBIN." >&2
    echo "       conda create -y -n wdscalers-pg -c conda-forge postgresql" >&2
    exit 1
fi

if [ -e "$PGDATA" ]; then
    echo "ERROR: $PGDATA already exists; this script provisions from scratch only." >&2
    echo "       To rebuild: scripts/db_down.sh; rm -rf $WDS_RUNDB_ROOT; re-run." >&2
    exit 1
fi

mkdir -p "$WDS_RUNDB_ROOT"/{run,log,nearline,beamtune-state}

"$PGBIN/initdb" -D "$PGDATA" -U pioneer --auth=trust --auth-host=trust \
    > "$WDS_RUNDB_ROOT/log/initdb.log" 2>&1

# Appended last, so these win over the defaults initdb wrote above them.
cat >> "$PGDATA/postgresql.conf" <<EOF

# -- wdscalers bench overrides (db_provision.sh) --
port = $PIONEER_DB_PORT
listen_addresses = '127.0.0.1'
unix_socket_directories = '$WDS_RUNDB_ROOT/run'
EOF

"$PGBIN/pg_ctl" -D "$PGDATA" -l "$WDS_RUNDB_ROOT/log/postgres.log" -w start

"$PGBIN/createdb" -h 127.0.0.1 -p "$PIONEER_DB_PORT" -U pioneer "$PIONEER_DB_NAME"

# -1 = one transaction: either the whole schema lands or none of it does,
# which keeps the "apply exactly once" rule enforceable.
"$PGBIN/psql" -1 -v ON_ERROR_STOP=1 -h 127.0.0.1 -p "$PIONEER_DB_PORT" \
    -U pioneer -d "$PIONEER_DB_NAME" \
    -f "$REPO/python/pioneer/rundb/db_config.sql" \
    > "$WDS_RUNDB_ROOT/log/db_config.log" 2>&1

# What admin.py's create path also does: every connection sees all four schemas.
"$PGBIN/psql" -v ON_ERROR_STOP=1 -h 127.0.0.1 -p "$PIONEER_DB_PORT" \
    -U pioneer -d "$PIONEER_DB_NAME" \
    -c "ALTER DATABASE $PIONEER_DB_NAME SET search_path TO config, state, logs, utils;"

echo
echo "cluster    : $PGDATA  (port $PIONEER_DB_PORT, loopback only)"
echo "database   : $PIONEER_DB_NAME  (schemas config/state/logs/utils applied)"
echo "logs       : $WDS_RUNDB_ROOT/log/"
echo "stop/start : scripts/db_down.sh / scripts/db_up.sh"
