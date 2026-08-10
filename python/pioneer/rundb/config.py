
import os

import psycopg2

# Overridable so a test deployment can point at a private cluster (e.g. the
# conda-local one on port 5433) without touching this file. Defaults preserve
# the production values.
DB_NAME = os.environ.get("PIONEER_DB_NAME", "pioneer")
DB_PORT = int(os.environ.get("PIONEER_DB_PORT", "5432"))
DB_HOST = os.environ.get("PIONEER_DB_HOST", "localhost")


def connect(user = "readonly", password = "readonly", db_name = DB_NAME):
    return psycopg2.connect(
        dbname=db_name,
        user=user,
        password=password,
        host=DB_HOST,
        port=DB_PORT,
    )

