
import psycopg2

DB_NAME = "pioneer"
DB_PORT = 5432
DB_HOST = "localhost"


def connect(user = "readonly", password = "readonly"):
    return psycopg2.connect(
        dbname=DB_NAME,
        user=user,
        password=password,
        host=DB_HOST,
        port=DB_PORT,
    )

# Debugging stuff below

if __name__ == "__main__":
    from interface import interface
    iface = interface("bot", "bot")
    acfg = iface.load_config()
    print("End of debug")
    print(acfg.__repr__())
