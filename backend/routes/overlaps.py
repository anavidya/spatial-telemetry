"""GET /api/overlaps: cache check, on-demand fetch, then the 4D encounter query."""
import logging

from fastapi import APIRouter, Query

from helpers import settings
from helpers.cache import is_cached
from helpers.db import db_cursor
from helpers.encounters import find_encounters
from helpers.etl import run_on_demand_etl

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/overlaps")
def get_parametric_overlaps(
    bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat"),
    target_param: str = Query("oxygen", description="Sensor both platforms must carry"),
):
    try:
        lon_min, lat_min, lon_max, lat_max = map(float, bbox.split(","))
    except ValueError:
        return {"error": "bbox must be min_lon,min_lat,max_lon,max_lat"}

    # Step 1: cache check
    with db_cursor() as cursor:
        cache_hit = is_cached(cursor, lon_min, lat_min, lon_max, lat_max)

    # Step 2: fetch real data for a new area
    fetch_report = None
    if not cache_hit:
        area = (lon_max - lon_min) * (lat_max - lat_min)
        if area > settings.MAX_FETCH_AREA_DEG2:
            return {"error": f"Box is {area:.0f} square degrees; the fetch limit is "
                             f"{settings.MAX_FETCH_AREA_DEG2:.0f}. Draw a smaller box "
                             "(or raise MAX_FETCH_AREA_DEG2 in .env)."}
        log.info("Cache miss: fetching %s", bbox)
        fetch_report = run_on_demand_etl(lon_min, lat_min, lon_max, lat_max)

    # Step 3: 4D encounter query
    with db_cursor() as cursor:
        encounters = find_encounters(cursor, lon_min, lat_min, lon_max, lat_max, target_param)

    return {
        "cache_status": "HIT" if cache_hit else "MISS",
        "rule": settings.matchup_rule(),
        "fetch": fetch_report,
        "encounter_count": len(encounters),
        "encounters": encounters,
    }
