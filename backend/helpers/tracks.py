"""Writing 4D tracks (LineStringZM) into remote_ocean_fleet_index."""
import json
from datetime import datetime, timezone

from helpers.ingest_argo import Track

UPSERT_SQL = """
    INSERT INTO remote_ocean_fleet_index (
        platform_id, platform_type, last_surfaced_at,
        trajectory_4d, remote_source_url, available_parameters)
    VALUES (%(platform_id)s, %(platform_type)s, to_timestamp(%(last_epoch)s),
            ST_GeomFromText(%(wkt)s, 4326), %(url)s, %(params)s)
    ON CONFLICT (platform_id) DO UPDATE SET
        platform_type        = EXCLUDED.platform_type,
        last_surfaced_at     = EXCLUDED.last_surfaced_at,
        trajectory_4d        = EXCLUDED.trajectory_4d,
        remote_source_url    = EXCLUDED.remote_source_url,
        available_parameters = EXCLUDED.available_parameters;
"""


def track_to_wkt(points):
    """[(lon, lat, z, m), ...] -> 'LINESTRING ZM (lon lat z m, ...)'"""
    return "LINESTRING ZM (" + ", ".join(f"{x:.6f} {y:.6f} {z:.1f} {m:.0f}" for x, y, z, m in points) + ")"


def upsert_track(cursor, track: Track):
    cursor.execute(UPSERT_SQL, {
        "platform_id": track.platform_id,
        "platform_type": track.platform_type,
        "last_epoch": track.points[-1][3],
        "wkt": track_to_wkt(track.points),
        "url": track.source_url,
        "params": json.dumps(track.params),
    })


def mock_glider_track(min_lon, min_lat):
    """Synthetic glider (no real glider ingest yet): one dive just inside the box corner, starting now."""
    t0 = datetime.now(timezone.utc).timestamp()
    return Track(
        platform_id="ALR_BODC_01",
        platform_type="GLIDER",
        points=[(min_lon + 0.1, min_lat + 0.1, 0, t0),
                (min_lon + 0.2, min_lat + 0.2, -500, t0 + 3600),
                (min_lon + 0.3, min_lat + 0.3, 0, t0 + 7200)],
        params=["temperature", "salinity", "pressure", "oxygen", "nitrate"],
        source_url="mock://glider",
    )
