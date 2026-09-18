import subprocess
import datetime
from pathlib import Path

import pioneer.rundb.config as config

class db_admin_tool:
    def __init__(self):
        self.admin_user = input("DB admin user: ")
        self.admin_pwd  = input("DB admin password: ")

    def connect(self, db_name = config.DB_NAME):
        """
        Simplified connection to the DB as admin user
        """
        return config.connect(user = self.admin_user, password = self.admin_pwd, db_name = db_name)

    def backup_db(self) -> bool:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"backup_{ts}.dump"

        subprocess.run([
            "pg_dump",
            "-h", config.DB_HOST,
            "-p", str(config.DB_PORT),
            "-U", "readonly",
            "-d", config.DB_NAME,
            "-Fc",
            "-f", filename
        ], check=True)

        print(f"Backup written to {filename}")

    def destroy_DB_completely(self) -> bool:
        # As the name suggests, this function is going to wipe the old DB out of existence.
        # If you want to use this mid-run, you are wrong.

        print("Received request to eliminate database ", config.DB_NAME)
        print("This resets the DB and roles and completely erases\nany data collected so far.")
        input("press enter to continue.")

        admin_connection = self.connect(db_name = "postgres")
        admin_connection.autocommit = True
        with admin_connection.cursor() as admin_cursor:
            # Step 1: Terminate all conections other than this one
            admin_cursor.execute(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{config.DB_NAME}' AND pid <> pg_backend_pid();"
            )

            # Step 2: Drop database.
            admin_cursor.execute(
                f"DROP DATABASE IF EXISTS {config.DB_NAME}"
            )

            # Step 3: Remove all existing roles other than admin and system
            admin_cursor.execute(f"""
                    DO $$
                    DECLARE r RECORD;
                    BEGIN
                        FOR r IN
                            SELECT rolname
                            FROM pg_roles
                            WHERE rolname NOT LIKE 'pg_%'
                            AND rolname <> '{self.admin_user}' AND rolname <> 'postgres'
                        LOOP
                            EXECUTE format('DROP OWNED BY %I', r.rolname);
                            EXECUTE format('DROP ROLE IF EXISTS %I', r.rolname);
                        END LOOP;
                    END $$;
                """)
        admin_connection.close()
        return True

    def create_DB(self) -> bool:
        print("Creating database", config.DB_NAME)
        input("Press enter to continue")

        admin_connection = self.connect(db_name = "postgres")
        admin_connection.autocommit = True
        with admin_connection.cursor() as admin_cursor:
            admin_cursor.execute(
                f"SELECT EXISTS ( SELECT 1 FROM pg_database WHERE datname = '{config.DB_NAME}' )"
            )
            exists = admin_cursor.fetchone()[0]
        if (exists):
            print(f"Database {config.DB_NAME} already exists")
            return False

        with admin_connection.cursor() as admin_cursor:
            admin_cursor.execute(
                f"CREATE DATABASE {config.DB_NAME}"
            )
        admin_connection.close()

        return self._create_tables_inside_DB()

    def _create_tables_inside_DB(self) -> bool:
        print("Building tables in database", config.DB_NAME)
        input("Press enter to continue")

        with open(Path(__file__).resolve().parent / "db_config.sql", "r") as f:
            sql = f.read()

        theConnection = self.connect()
        with theConnection.cursor() as aCursor:
            aCursor.execute(sql)
            aCursor.execute(f"ALTER DATABASE {config.DB_NAME} SET search_path TO config, state, logs, utils;")
        theConnection.commit()
        theConnection.close()

        return True

    def insert_dummy_test_data(self) -> bool:
        with open("db_dummy_data.sql", "r") as f:
            sql = f.read()

        print("Filling DB tables with dummy data")
        input("Press enter to continue")

        theConnection = self.connect()
        with theConnection.cursor() as aCursor:
            aCursor.execute(sql)
        theConnection.commit()
        theConnection.close()
        return True

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        prog = "pioneer_db_admin",
        description="an admin tool to recreate the pioneer run database")
    parser.add_argument("--create", action = 'store_true')
    parser.add_argument("--hard-reset", action='store_true')
    parser.add_argument("--fill-dummy", action='store_true')
    parser.add_argument("--backup", action = 'store_true')
    parser.add_argument("--no-backup", action = 'store_true')
    args = parser.parse_args()

    if (args.backup and args.no_backup):
        print("Schroedingers backup is not a feature!")
        exit(1)

    print("Actions Requested on database ", config.DB_NAME)
    make_backup = args.backup
    if (make_backup):
        print(" - Create a backup of the database")
    if (args.create):
        if not make_backup and not args.no_backup:
            print(" - Create a backup of the database (included in recreation process)")
            make_backup = True
        if (args.hard_reset):
            print(" - Hard reset and subsequent recreation of database ")
            print("   This will completely erase any existing data.")
        else:
            print(" - run table initialisation scripts, resetting permissions")

    if (args.fill_dummy):
        print(" - Fill some dummy data into the tables for testing")

    theTool = db_admin_tool()
    if (make_backup):
        theTool.backup_db()
    if (args.create):
        if (args.hard_reset):
            theTool.destroy_DB_completely()
        theTool.create_DB()
    if (args.fill_dummy):
        theTool.insert_dummy_test_data()


    exit(0)
