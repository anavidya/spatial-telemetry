"""
Retention for the `profiles` table, so it holds roughly what was used recently
instead of growing forever. Runs at start-up and then hourly in a daemon thread.

  * Glider rows: deleted when that glider has not been (re)opened for
    PROFILES_RETENTION_DAYS. Reopening it fetches its dives again.
  * Argo rows: deleted when older than PROFILES_RETENTION_DAYS and no fresh
    entry in argo_fetch_windows still relies on them (otherwise the fetch cache
    would say "covered" while the profiles were gone).

glider_deployments keeps max_depth_m and data_extent: they are tiny and still
useful for colouring the map.
"""
import logging
import threading
import time

from helpers import settings
from helpers.db import db_cursor

log = logging.getLogger(__name__)
_status = {"last_run": None, "deleted_glider_rows": 0, "deleted_argo_rows": 0, "error": None}

DELETE_GLIDERS = """
    DELETE FROM profiles
    WHERE platform_type = 'GLIDER'
      AND platform_id IN (SELECT platform_id FROM profiles WHERE platform_type = 'GLIDER'
                          GROUP BY platform_id
                          HAVING max(fetched_at) < now() - make_interval(days => %(days)s));
"""
DELETE_ARGO = """
    DELETE FROM profiles a
    WHERE a.platform_type = 'ARGO'
      AND a.fetched_at < now() - make_interval(days => %(days)s)
      AND NOT EXISTS (SELECT 1 FROM argo_fetch_windows w
                      WHERE w.fetched_at > now() - make_interval(secs => %(ttl)s)
                        AND ST_Covers(w.area, a.geom) AND w.period @> a.t);
"""


def run_once():
    params = {"days": int(settings.PROFILES_RETENTION_DAYS), "ttl": settings.ARGO_FETCH_TTL_DAYS * 86400}
    with db_cursor(commit=True) as cursor:
        cursor.execute(DELETE_GLIDERS, params)
        gliders = cursor.rowcount
        cursor.execute(DELETE_ARGO, params)
        argo = cursor.rowcount
    _status.update(last_run=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   deleted_glider_rows=gliders, deleted_argo_rows=argo, error=None)
    if gliders or argo:
        log.info("retention: deleted %s glider rows and %s Argo rows older than %s days",
                 gliders, argo, params["days"])
    return gliders, argo


def start_loop(every_minutes=60):
    def loop():
        while True:
            try:
                run_once()
            except Exception as exc:
                _status["error"] = f"{type(exc).__name__}: {exc}"
                log.warning("retention run failed: %s", exc)
            time.sleep(every_minutes * 60)
    threading.Thread(target=loop, name="profiles-retention", daemon=True).start()


def status():
    return dict(_status)
