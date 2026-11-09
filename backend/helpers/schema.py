"""Create the calibration-finder tables at start-up (idempotent)."""
import logging
from pathlib import Path

from helpers.db import db_cursor

log = logging.getLogger(__name__)
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def apply_schema():
    with db_cursor(commit=True) as cursor:
        cursor.execute(SCHEMA_FILE.read_text())
    log.info("schema applied (%s)", SCHEMA_FILE.name)
