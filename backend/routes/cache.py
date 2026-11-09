"""GET /api/cache-windows: boxes that have triggered a fetch."""
from fastapi import APIRouter

from helpers.cache import list_windows
from helpers.db import db_cursor, fetch_dicts

router = APIRouter()


@router.get("/api/cache-windows")
def list_cache_windows():
    """Every box that has triggered a fetch, newest first, flagged fresh/expired."""
    with db_cursor() as cursor:
        list_windows(cursor)
        return fetch_dicts(cursor)
