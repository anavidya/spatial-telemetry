"""
Glider deployment catalogue, kept in PostGIS (table glider_deployments).

A background thread reloads it from the ERDDAP server's allDatasets table every
GLIDER_CATALOGUE_TTL_MIN minutes. Searches then use one indexed query
(extent && box, active && time range) instead of a ~1 minute ERDDAP search.
"""
import logging
import threading
import time

import pandas as pd

from helpers.db import db_cursor, fetch_dicts
from helpers.erddap import get_csv, tabledap_url

log = logging.getLogger(__name__)

UPSERT_SQL = """
    INSERT INTO glider_deployments (dataset_id, title, institution, extent, active, server, refreshed_at)
    VALUES (%(id)s, %(title)s, %(inst)s,
            ST_MakeEnvelope(%(w)s, %(s)s, %(e)s, %(n)s, 4326),
            tstzrange(%(t0)s, %(t1)s, '[]'), %(server)s, now())
    ON CONFLICT (dataset_id) DO UPDATE SET
        title = EXCLUDED.title, institution = EXCLUDED.institution,
        extent = EXCLUDED.extent, active = EXCLUDED.active,
        server = EXCLUDED.server, refreshed_at = now();
"""

_status = {"last_refresh": None, "last_error": None, "count": 0, "seconds": None, "servers": {}}


def load_from_erddap(server, since, timeout) -> pd.DataFrame:
    """Deployments with data after `since`, from the server's allDatasets table."""
    url = tabledap_url(server, "allDatasets",
                       ["datasetID", "title", "institution", "cdm_data_type",
                        "minLongitude", "maxLongitude", "minLatitude", "maxLatitude", "minTime", "maxTime"],
                       [f"maxTime>={pd.Timestamp(since).strftime('%Y-%m-%dT%H:%M:%SZ')}"])
    df = get_csv(url, timeout=timeout)
    df = df[df["datasetID"] != "allDatasets"].copy()
    # Gliders only: trajectory datasets (IOOS DAC: TrajectoryProfile; EGO-style: Trajectory
    # whose title names a glider). Other datasets on a shared server are skipped.
    # NOC/BODC titles say "real-time slocum data" or "real-time seaglider data", so "slocum"
    # must count too; Autosub Long Range (AUV) missions are trajectories but not gliders.
    kind = df["cdm_data_type"].fillna("").str.lower()
    title = df["title"].fillna("").str.lower()
    names_glider = title.str.contains(r"glider|planeur|slocum|seaexplorer|spray", regex=True)
    df = df[kind.eq("trajectoryprofile") | (kind.eq("trajectory") & names_glider & ~title.str.contains("autosub"))]
    # Clamp impossible extents (seen in real-time data: longitude +15.6 in a NW Atlantic mission)
    df = df[df["minLongitude"].between(-180, 180) & df["maxLongitude"].between(-180, 180)
            & df["minLatitude"].between(-90, 90) & df["maxLatitude"].between(-90, 90)]
    df["minTime"] = pd.to_datetime(df["minTime"], utc=True, errors="coerce")
    df["maxTime"] = pd.to_datetime(df["maxTime"], utc=True, errors="coerce")
    # A last-data time in the future is broken metadata (seen: a 2024 Seaglider "ending" in 2027)
    df = df[df["maxTime"] <= pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=1)]
    return df.dropna(subset=["minLongitude", "maxLongitude", "minLatitude", "maxLatitude", "minTime", "maxTime"])


def refresh(server, lookback_days, timeout=120):
    """Reload the catalogue from ERDDAP into PostGIS. Returns the number of deployments."""
    tic = time.perf_counter()
    since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=lookback_days + 2)
    try:
        df = load_from_erddap(server, since, timeout)
        rows = [{
            "id": r.datasetID, "title": r.title, "inst": r.institution,
            "w": float(r.minLongitude), "s": float(r.minLatitude),
            "e": float(r.maxLongitude), "n": float(r.maxLatitude),
            "t0": r.minTime.to_pydatetime(), "t1": r.maxTime.to_pydatetime(), "server": server,
        } for r in df.itertuples()]
        with db_cursor(commit=True) as cursor:
            for row in rows:
                cursor.execute(UPSERT_SQL, row)
        with db_cursor() as cursor:
            cursor.execute("SELECT count(*) FROM glider_deployments WHERE upper(active) >= %s", (since,))
            total = cursor.fetchone()[0]
        _status.update(last_refresh=pd.Timestamp.now(tz="UTC").isoformat(), last_error=None,
                       count=total, seconds=round(time.perf_counter() - tic, 1))
        log.info("glider catalogue: %s deployments active since %s (%.1f s)",
                 len(rows), since.date(), time.perf_counter() - tic)
        return len(rows)
    except Exception as exc:
        _status.update(last_error=f"{type(exc).__name__}: {exc}")
        raise


def start_refresh_loop(servers, lookback_days, ttl_minutes):
    """Refresh every server now, then every ttl_minutes, one daemon thread per server.
    A failing server is retried after 5 minutes without affecting the others."""
    if isinstance(servers, str):
        servers = [servers]

    def loop(server):
        while True:
            try:
                n = refresh(server, lookback_days)
                _status["servers"][server] = {"count": n, "last_refresh": pd.Timestamp.now(tz="UTC").isoformat(), "error": None}
                wait = ttl_minutes * 60
            except Exception as exc:
                log.warning("glider catalogue refresh failed for %s, retrying in 5 min: %s", server, exc)
                _status["servers"].setdefault(server, {})["error"] = f"{type(exc).__name__}: {exc}"
                wait = 300
            time.sleep(wait)
    for server in servers:
        threading.Thread(target=loop, args=(server,), name=f"glider-catalogue-{server}", daemon=True).start()


def status():
    return dict(_status)


def deployments_in(cursor, start, end, bbox=None):
    """Deployments active in [start, end], optionally overlapping bbox (w, s, e, n)."""
    sql = """
        SELECT dataset_id, title, institution, max_depth_m, server,
               lower(active) AS start_time, upper(active) AS end_time,
               data_extent IS NOT NULL AS extent_from_data,
               greatest(ST_XMax(coalesce(data_extent, extent)) - ST_XMin(coalesce(data_extent, extent)),
                        ST_YMax(coalesce(data_extent, extent)) - ST_YMin(coalesce(data_extent, extent))) AS span_deg,
               ST_AsGeoJSON(coalesce(data_extent, extent))::jsonb AS geometry
        FROM glider_deployments
        WHERE active && tstzrange(%(start)s, %(end)s, '[]')
    """
    params = {"start": start, "end": end}
    if bbox:
        sql += " AND coalesce(data_extent, extent) && ST_MakeEnvelope(%(w)s, %(s)s, %(e)s, %(n)s, 4326)"
        params.update(dict(zip("wsen", bbox)))
    sql += " ORDER BY ST_Area(coalesce(data_extent, extent)) DESC;"   # big first, small drawn on top
    cursor.execute(sql, params)
    return fetch_dicts(cursor)
