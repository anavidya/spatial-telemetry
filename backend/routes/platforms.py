"""GET /api/platforms: every track in the index as GeoJSON."""
from fastapi import APIRouter

from helpers.db import db_cursor, fetch_dicts

router = APIRouter()


@router.get("/api/platforms")
def list_platforms():
    """Every platform track in the index, as a GeoJSON FeatureCollection."""
    with db_cursor() as cursor:
        cursor.execute("""
            SELECT platform_id, platform_type, available_parameters,
                   ST_NPoints(trajectory_4d)                        AS n_points,
                   to_timestamp(ST_M(ST_StartPoint(trajectory_4d))) AS start_time,
                   to_timestamp(ST_M(ST_EndPoint(trajectory_4d)))   AS end_time,
                   ST_ZMin(trajectory_4d::box3d)                    AS deepest_m,
                   ST_AsGeoJSON(trajectory_4d)::jsonb               AS geometry
            FROM remote_ocean_fleet_index
            ORDER BY platform_id;
        """)
        rows = fetch_dicts(cursor)
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": r.pop("geometry"), "properties": r} for r in rows],
    }
