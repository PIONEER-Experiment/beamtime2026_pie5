
import psycopg

DB_NAME = "pioneer"
DB_PORT = 5432
DB_HOST = "localhost"


def connect(user = "readonly", password = "readonly", db_name = DB_NAME):
    return psycopg.connect(
        dbname=db_name,
        user=user,
        password=password,
        host=DB_HOST,
        port=DB_PORT,
    )

