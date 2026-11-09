"""
Calibration finder endpoints:
    GET /api/deployments                         active glider deployments (GeoJSON extents)
    GET /api/deployments/{dataset_id}/references Argo reference profiles for one deployment
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query

from helpers import catalogue, settings
from helpers.calibration import find_reference_profiles
from helpers.db import db_cursor

router = APIRouter()


@router.get("/api/deployments")
def list_deployments(
    bbox: str | None = Query(None, description="optional min_lon,min_lat,max_lon,max_lat"),
    days: int | None = Query(None, description="active within the last N days (default FETCH_LOOKBACK_DAYS)"),
):
    end = datetime.now(timezone.utc) + timedelta(days=1)
    start = end - timedelta(days=(days or settings.FETCH_LOOKBACK_DAYS) + 1)
    box = tuple(map(float, bbox.split(","))) if bbox else None
    with db_cursor() as cursor:
        rows = catalogue.deployments_in(cursor, start, end, bbox=box)
    # Prefer the quality-checked "-delayed" copy when both exist
    ids = {r["dataset_id"] for r in rows}
    rows = [r for r in rows if not (f"{r['dataset_id']}-delayed" in ids)]
    for r in rows:
        r["unreliable_extent"] = (r["span_deg"] or 0) > settings.GLIDER_MAX_EXTENT_DEG
    return {
        "type": "FeatureCollection",
        "catalogue": catalogue.status(),
        "features": [{"type": "Feature", "geometry": r.pop("geometry"), "properties": r} for r in rows],
    }


@router.get("/api/deployments/{dataset_id}/references")
def deployment_references(
    dataset_id: str,
    param: str = Query("temperature", description="sensor both platforms must carry"),
):
    try:
        return find_reference_profiles(dataset_id, param)
    except Exception as exc:
        return {"dataset_id": dataset_id, "error": f"{type(exc).__name__}: {exc}"}
