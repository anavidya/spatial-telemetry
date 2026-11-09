"""Database connection helpers."""
from contextlib import contextmanager

import psycopg2

from helpers.settings import DATABASE_URL


@contextmanager
def db_cursor(commit=False):
    """Open a connection, yield a cursor, optionally commit, always close."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cursor:
            yield cursor
        if commit:
            conn.commit()
    finally:
        conn.close()


def fetch_dicts(cursor):
    """Rows of the last query as a list of {column: value} dicts."""
    cols = [c.name for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]
