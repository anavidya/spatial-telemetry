"""
Glider ingest: find glider deployments (Slocum, Seaglider, ...) on an ERDDAP
server (default: IOOS Glider DAC) that overlap a box and time window, and
return one 4D track per deployment.

Network saving: ERDDAP's orderByMinMax("profile_id,depth") returns only the
shallowest and deepest row of every profile (one dive or one climb), i.e. the
turning points, so the full-resolution data never leaves the server.
Everything stays in memory: no NetCDF or CSV files are written.
"""
import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import quote

import pandas as pd
from erddapy import ERDDAP

from helpers.catalogue import deployments_in
from helpers.db import db_cursor
from helpers.erddap import ErddapNoData, get_csv, tabledap_url

from helpers.ingest_argo import Track

log = logging.getLogger(__name__)

SEARCH_TIMEOUT_S = 120     # deployment search / catalogue / info requests
SENSOR_KEYWORDS = {           # variable-name keyword -> name used in available_parameters
    "temperature": "temperature",
    "salinity": "salinity",
    "pressure": "pressure",
    "oxygen": "oxygen",
    "doxy": "oxygen",
    "nitrate": "nitrate",
}


@dataclass
class GliderStats:
    datasets_found: int = 0     # deployments overlapping box + window
    datasets_used: int = 0      # after the per-search cap
    rows_transferred: int = 0   # rows ERDDAP sent (turning points only)
    profiles: int = 0
    stored_points: int = 0
    gliders: int = 0
    skipped: list = field(default_factory=list)   # deployments that timed out or failed
    found_via: str = ""                           # "catalogue" or "search"
    seconds: dict = field(default_factory=dict)   # time spent per step


# Shared ERDDAP helpers (kept under the old names used below)
_get_csv = get_csv
_tabledap_url = tabledap_url


def _prefer_delayed(ids) -> list[str]:
    """The DAC can hold a real-time and a "-delayed" copy of one deployment: keep the delayed (QC'd) one."""
    by_deployment = {}
    for d in ids:
        if d == "allDatasets":
            continue
        key = d.removesuffix("-delayed")
        if key not in by_deployment or d.endswith("-delayed"):
            by_deployment[key] = d
    return list(by_deployment.values())


def search_datasets(server, min_lon, min_lat, max_lon, max_lat, start, end) -> list[str]:
    """
    Fallback: ERDDAP's advanced search (search/advanced.csv with the box and dates),
    the same call erddapy makes. Can take about a minute on the Glider DAC.
    """
    url = ERDDAP(server=server, protocol="tabledap").get_search_url(
        response="csv", items_per_page=1000,
        min_lon=min_lon, max_lon=max_lon, min_lat=min_lat, max_lat=max_lat,
        min_time=pd.Timestamp(start).strftime("%Y-%m-%dT%H:%M:%SZ"),
        max_time=pd.Timestamp(end).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    try:
        df = _get_csv(url, timeout=SEARCH_TIMEOUT_S, units_row=False)
    except ErddapNoData:
        return []
    return _prefer_delayed(df["Dataset ID"])


def find_datasets(server, min_lon, min_lat, max_lon, max_lat, start, end,
                  lookback_days=30, catalogue_ttl_minutes=60) -> tuple[list[str], str]:
    """Deployments overlapping the box and window: from the PostGIS catalogue, else by ERDDAP search."""
    try:
        with db_cursor() as cursor:
            rows = deployments_in(cursor, start, end, bbox=(min_lon, min_lat, max_lon, max_lat))
        cursor_has_catalogue = rows or _catalogue_loaded()
        if cursor_has_catalogue:
            return _prefer_delayed([r["dataset_id"] for r in rows]), "catalogue"
    except Exception as exc:
        log.warning("glider catalogue query failed (%s); falling back to ERDDAP search", exc)
    return search_datasets(server, min_lon, min_lat, max_lon, max_lat, start, end), "search"


def _catalogue_loaded() -> bool:
    with db_cursor() as cursor:
        cursor.execute("SELECT EXISTS (SELECT 1 FROM glider_deployments)")
        return cursor.fetchone()[0]


@lru_cache(maxsize=512)
def dataset_variables(server, dataset_id) -> frozenset[str]:
    """Variable names of a deployment (cached: they don't change between searches)."""
    info = _get_csv(f"{server}/info/{dataset_id}/index.csv", timeout=SEARCH_TIMEOUT_S)
    return frozenset(info.loc[info["Row Type"] == "variable", "Variable Name"])


def sensors_from_variables(variables: set[str]) -> list[str]:
    found = []
    for var in sorted(variables):
        if var.startswith("qartod_") or var.endswith("_qc"):
            continue
        for keyword, name in SENSOR_KEYWORDS.items():
            if keyword in var.lower() and name not in found:
                found.append(name)
    order = ["temperature", "salinity", "pressure", "oxygen", "nitrate"]
    return [s for s in order if s in found]


def turning_points(df: pd.DataFrame, has_precise: bool) -> list:
    """
    Rows from orderByMinMax (shallowest + deepest row per profile) -> [(lon, lat, z, epoch_s)]
    with time strictly increasing.

    With precise_time/lat/lon each turning point has its own measured time and position.
    Without them (older datasets) both points share the profile's time and position, so
    the shallow point is placed 1 s before the deep one to keep the trajectory valid.
    """
    t_col, lat_col, lon_col = ("precise_time", "precise_lat", "precise_lon") if has_precise \
        else ("time", "latitude", "longitude")
    df = df.dropna(subset=[t_col, lat_col, lon_col, "depth"]).copy()
    # Resolution-safe epoch seconds (pandas 3 may parse to second, not nanosecond, resolution)
    df["epoch"] = (pd.to_datetime(df[t_col], utc=True) - pd.Timestamp("1970-01-01", tz="UTC")) / pd.Timedelta(seconds=1)
    points = []
    for _, prof in df.groupby("profile_id"):
        shallow = prof.loc[prof["depth"].idxmin()]
        deep = prof.loc[prof["depth"].idxmax()]
        if has_precise:
            pts = [shallow, deep]
            points += [(r[lon_col], r[lat_col], -float(r["depth"]), float(r["epoch"])) for r in pts]
        else:
            t = float(deep["epoch"])
            points.append((shallow[lon_col], shallow[lat_col], -float(shallow["depth"]), t - 1))
            points.append((deep[lon_col], deep[lat_col], -float(deep["depth"]), t))
    points.sort(key=lambda p: p[3])
    clean = []
    for p in points:
        if not clean or p[3] > clean[-1][3]:
            clean.append((float(p[0]), float(p[1]), p[2], p[3]))
    return clean


def fetch_one_deployment(server, dataset_id, min_lon, min_lat, max_lon, max_lat, t0, t1, timeout):
    """Turning-point rows for one deployment inside the box and window, plus its variables."""
    variables = dataset_variables(server, dataset_id)
    has_precise = {"precise_time", "precise_lat", "precise_lon"} <= variables
    cols = ["profile_id", "time", "latitude", "longitude", "depth"]
    if has_precise:
        cols += ["precise_time", "precise_lat", "precise_lon"]
    url = _tabledap_url(server, dataset_id, cols, [
        f"time>={t0}", f"time<={t1}",
        f"latitude>={min_lat}", f"latitude<={max_lat}",
        f"longitude>={min_lon}", f"longitude<={max_lon}",
        "depth!=NaN",
        'orderByMinMax("profile_id,depth")',
    ])
    return _get_csv(url, timeout=timeout), variables, has_precise


def fetch_glider_tracks(min_lon, min_lat, max_lon, max_lat, start, end, server,
                        max_datasets=20, parallel=4, dataset_timeout=180,
                        lookback_days=30, catalogue_ttl_minutes=60):
    """
    Return (tracks, stats) for every glider deployment in the box and window.
    Deployments are fetched in parallel; one that is slow or failing is skipped
    (listed in stats.skipped) instead of failing the whole search.
    """
    stats = GliderStats()
    tic = time.perf_counter()
    ids, stats.found_via = find_datasets(server, min_lon, min_lat, max_lon, max_lat, start, end,
                                         lookback_days, catalogue_ttl_minutes)
    stats.seconds["search"] = round(time.perf_counter() - tic, 1)
    stats.datasets_found = len(ids)
    ids = ids[:max_datasets]
    stats.datasets_used = len(ids)
    log.info("gliders: %s deployment(s) overlap the box, fetching %s", stats.datasets_found, len(ids))

    t0 = pd.Timestamp(start).strftime("%Y-%m-%dT%H:%M:%SZ")
    t1 = pd.Timestamp(end).strftime("%Y-%m-%dT%H:%M:%SZ")
    tic = time.perf_counter()
    tracks = []
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = {
            pool.submit(fetch_one_deployment, server, d, min_lon, min_lat, max_lon, max_lat,
                        t0, t1, dataset_timeout): d
            for d in ids
        }
        for future, dataset_id in futures.items():
            try:
                df, variables, has_precise = future.result()
            except ErddapNoData:
                continue
            except Exception as exc:
                log.warning("glider deployment %s skipped: %s", dataset_id, exc)
                stats.skipped.append(f"{dataset_id}: {type(exc).__name__}")
                continue

            stats.rows_transferred += len(df)
            stats.profiles += df["profile_id"].nunique()
            points = turning_points(df, has_precise)
            if len(points) < 2:
                continue
            stats.stored_points += len(points)
            tracks.append(Track(
                platform_id=f"GLIDER_{dataset_id}",
                platform_type="GLIDER",
                points=points,
                params=sensors_from_variables(set(variables)),
                source_url=f"{server}/tabledap/{dataset_id}.html",
            ))
    stats.seconds["data"] = round(time.perf_counter() - tic, 1)
    stats.gliders = len(tracks)
    log.info("gliders: %s rows -> %s profiles -> %s gliders, %s stored points, skipped %s, %s",
             stats.rows_transferred, stats.profiles, stats.gliders, stats.stored_points,
             len(stats.skipped), stats.seconds)
    return tracks, stats
