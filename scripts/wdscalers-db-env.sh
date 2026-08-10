#!/bin/bash
# Environment for the private WDSCALERS-bench run database.
#
# Source this (don't execute it) before any script or client that talks to the
# rundb. It points the pioneer.rundb code (via the PIONEER_DB_* overrides in
# rundb/config.py) and the psql convenience variables at the conda-local
# Postgres cluster, which is deliberately NOT the system Postgres:
#
#   * server binaries : conda env wdscalers-pg
#   * data directory  : /home/pioneer/josh/wdscalers-rundb/pgdata
#   * port            : 5433 on 127.0.0.1 only
#   * auth            : trust (loopback-only, single-user bench cluster)
#
# Removal of the whole thing:
#   scripts/db_down.sh
#   rm -rf /home/pioneer/josh/wdscalers-rundb
#   conda env remove -n wdscalers-pg

export WDS_RUNDB_ROOT="${WDS_RUNDB_ROOT:-/home/pioneer/josh/wdscalers-rundb}"
export PGBIN="${PGBIN:-/home/pioneer/josh/miniconda/install/envs/wdscalers-pg/bin}"
export PGDATA="$WDS_RUNDB_ROOT/pgdata"

# What pioneer.rundb.config reads.
export PIONEER_DB_HOST="${PIONEER_DB_HOST:-127.0.0.1}"
export PIONEER_DB_PORT="${PIONEER_DB_PORT:-5433}"
export PIONEER_DB_NAME="${PIONEER_DB_NAME:-pioneer}"

# What psql/pg_dump read, so `psql -d pioneer` just works in this shell.
export PGHOST="$PIONEER_DB_HOST"
export PGPORT="$PIONEER_DB_PORT"

export PATH="$PGBIN:$PATH"
