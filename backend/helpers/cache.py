"""spatial_cache_registry: which boxes have been fetched, and when."""
from helpers.settings import CACHE_TTL_HOURS

TTL_SECONDS = CACHE_TTL_HOURS * 3600


def is_cached(cursor, min_lon, min_lat, max_lon, max_lat) -> bool:
    """True if a box fetched within the TTL fully contains this one."""
    cursor.execute("""
        SELECT EXISTS (
            SELECT 1 FROM spatial_cache_registry
            WHERE ST_Contains(cache_window, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
              AND last_fetched_at >= NOW() - make_interval(secs => %s)
        );
    """, (min_lon, min_lat, max_lon, max_lat, TTL_SECONDS))
    return cursor.fetchone()[0]


def record_window(cursor, min_lon, min_lat, max_lon, max_lat):
    cursor.execute(
        "INSERT INTO spatial_cache_registry (cache_window) VALUES (ST_MakeEnvelope(%s, %s, %s, %s, 4326));",
        (min_lon, min_lat, max_lon, max_lat))


def list_windows(cursor):
    """Every cached box, newest first, with expiry time and a fresh flag."""
    cursor.execute("""
        SELECT id, last_fetched_at,
               last_fetched_at + make_interval(secs => %(ttl)s) AS expires_at,
               last_fetched_at >= NOW() - make_interval(secs => %(ttl)s) AS fresh,
               ST_XMin(cache_window) AS min_lon, ST_YMin(cache_window) AS min_lat,
               ST_XMax(cache_window) AS max_lon, ST_YMax(cache_window) AS max_lat
        FROM spatial_cache_registry
        ORDER BY last_fetched_at DESC;
    """, {"ttl": TTL_SECONDS})
