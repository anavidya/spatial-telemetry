"""On-demand fetch: pull real Argo and glider data for a box and store it as 4D tracks."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from helpers import settings
from helpers.cache import record_window
from helpers.db import db_cursor
from helpers.ingest_argo import fetch_argo_tracks
from helpers.ingest_glider import fetch_glider_tracks
from helpers.tracks import mock_glider_track, upsert_track

log = logging.getLogger(__name__)


def run_on_demand_etl(min_lon, min_lat, max_lon, max_lat):
    """
    Fetch real Argo and glider tracks for the box and store them.
    The box is logged in the cache only if every source succeeded, so a
    server error is retried next time instead of being cached as "no data".
    Returns a report the frontend shows after a cache miss.
    """
    end = datetime.now(timezone.utc) + timedelta(days=1)  # argopy end date is exclusive
    start = end - timedelta(days=settings.FETCH_LOOKBACK_DAYS + 1)
    report = {"window": {"start": start.date().isoformat(), "end": end.date().isoformat()}}

    tracks = []
    fetched_ok = True
    if settings.MOCK_GLIDER:
        tracks.append(mock_glider_track(min_lon, min_lat))

    sources = {
        "argo": lambda: fetch_argo_tracks(min_lon, min_lat, max_lon, max_lat, start, end,
                                          src=settings.ARGO_SOURCE),
        "glider": lambda: fetch_glider_tracks(min_lon, min_lat, max_lon, max_lat, start, end,
                                              server=settings.GLIDER_ERDDAP_URL,
                                              max_datasets=settings.GLIDER_MAX_DATASETS,
                                              parallel=settings.GLIDER_PARALLEL,
                                              dataset_timeout=settings.GLIDER_DATASET_TIMEOUT_S,
                                              lookback_days=settings.FETCH_LOOKBACK_DAYS,
                                              catalogue_ttl_minutes=settings.GLIDER_CATALOGUE_TTL_MIN),
    }

    def timed(fetch):
        tic = time.perf_counter()
        result = fetch()
        return result, round(time.perf_counter() - tic, 1)

    # Argo and gliders come from different servers, so fetch them at the same time
    tic = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        futures = {name: pool.submit(timed, fetch) for name, fetch in sources.items()}
    report["seconds"] = {"total": round(time.perf_counter() - tic, 1)}
    for name, future in futures.items():
        try:
            (source_tracks, stats), seconds = future.result()
            tracks += source_tracks
            report[name] = asdict(stats)
            report["seconds"][name] = seconds
        except Exception as exc:  # network/server problems: report, don't cache
            log.exception("%s fetch failed", name)
            report[f"{name}_error"] = f"{type(exc).__name__}: {exc}"
            fetched_ok = False

    with db_cursor(commit=True) as cursor:
        for track in tracks:
            upsert_track(cursor, track)
        if fetched_ok:
            record_window(cursor, min_lon, min_lat, max_lon, max_lat)
    return report
