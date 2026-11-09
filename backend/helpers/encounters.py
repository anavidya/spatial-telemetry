"""The 4D encounter query: glider x Argo point pairs within the matchup rule."""
from helpers import settings
from helpers.db import fetch_dicts

# Explode both tracks into 4D points and keep the closest glider/float point
# pair that is within the distance, depth and time limits.
ENCOUNTER_SQL = """
WITH assets AS (                                   -- 1. GiST index prune + JSONB sensor filter
    SELECT platform_id, platform_type, trajectory_4d, available_parameters
    FROM remote_ocean_fleet_index
    WHERE trajectory_4d && ST_MakeEnvelope(%(min_lon)s, %(min_lat)s, %(max_lon)s, %(max_lat)s, 4326)
      AND available_parameters ? %(param)s
),
pts AS (                                           -- 2. explode each track into its 4D points
    SELECT a.platform_id, a.platform_type, d.path[1] AS n, d.geom
    FROM assets a, LATERAL ST_DumpPoints(a.trajectory_4d) AS d
),
matches AS (                                       -- 3. glider point x float point within all limits
    SELECT g.platform_id AS glider_id, f.platform_id AS argo_id,
           ST_Distance(g.geom::geography, f.geom::geography) AS dist_m,
           abs(ST_Z(g.geom) - ST_Z(f.geom))                 AS dz_m,
           abs(ST_M(g.geom) - ST_M(f.geom))                 AS dt_s,
           g.geom AS g_geom, f.geom AS f_geom
    FROM pts g
    JOIN pts f ON g.platform_type = 'GLIDER' AND f.platform_type = 'ARGO'
    WHERE ST_DWithin(g.geom::geography, f.geom::geography, %(max_dist_m)s)
      AND abs(ST_Z(g.geom) - ST_Z(f.geom)) < %(max_dz_m)s
      AND abs(ST_M(g.geom) - ST_M(f.geom)) < %(max_dt_s)s
),
best AS (                                          -- 4. closest match per pair
    SELECT DISTINCT ON (glider_id, argo_id) *,
           count(*) OVER (PARTITION BY glider_id, argo_id) AS n_matching_points
    FROM matches
    ORDER BY glider_id, argo_id, dist_m
)
SELECT b.glider_id, b.argo_id,
       (SELECT jsonb_agg(e) FROM jsonb_array_elements_text(g.available_parameters) e
         WHERE f.available_parameters ? e)                  AS shared_sensors,
       b.dist_m, b.dz_m, b.dt_s, b.n_matching_points,
       ST_Z(b.g_geom) AS glider_depth_m, ST_Z(b.f_geom) AS argo_depth_m,
       to_timestamp(ST_M(b.g_geom)) AS glider_time, to_timestamp(ST_M(b.f_geom)) AS argo_time,
       ST_AsGeoJSON(b.g_geom)::jsonb AS glider_point,
       ST_AsGeoJSON(b.f_geom)::jsonb AS argo_point,
       ST_AsGeoJSON(g.trajectory_4d)::jsonb AS glider_track,
       ST_AsGeoJSON(f.trajectory_4d)::jsonb AS float_track
FROM best b
JOIN assets g ON g.platform_id = b.glider_id
JOIN assets f ON f.platform_id = b.argo_id
ORDER BY b.dist_m;
"""


def find_encounters(cursor, min_lon, min_lat, max_lon, max_lat, target_param):
    cursor.execute(ENCOUNTER_SQL, {
        "min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat,
        "param": target_param,
        "max_dist_m": settings.MATCH_MAX_DIST_M,
        "max_dz_m": settings.MATCH_MAX_DEPTH_DIFF_M,
        "max_dt_s": settings.MATCH_MAX_TIME_DIFF_S,
    })
    encounters = fetch_dicts(cursor)
    for e in encounters:
        e["intersection_point"] = e["glider_point"]  # kept for older clients
    return encounters
