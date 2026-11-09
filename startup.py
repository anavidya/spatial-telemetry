import socket
import subprocess
import time
import psycopg2

DB_HOST = "localhost"
DB_PORT = 5433
DB_NAME = "spatial_db"
DB_USER = "postgres"
DB_PASSWORD = "postgrespassword"


def is_postgres_running():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((DB_HOST, DB_PORT)) == 0


def wait_for_postgres(timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT,
                dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
                connect_timeout=2,
            )
            conn.close()
            return True
        except psycopg2.OperationalError:
            time.sleep(2)
    return False


def ensure_postgis_extension():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    conn.close()


def main():
    if is_postgres_running():
        print("Postgres already running, using existing instance.")
    else:
        print("No Postgres found, starting container...")
        subprocess.run(["podman", "compose", "up", "-d", "postgis"], check=True)
        if not wait_for_postgres():
            raise RuntimeError("Postgres did not start in time.")
        print("Postgres is up.")

    ensure_postgis_extension()
    print(f"PostGIS extension ready on '{DB_NAME}'.")


if __name__ == "__main__":
    main()
