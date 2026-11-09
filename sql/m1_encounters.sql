-- M1: 4D glider x Argo matchup (horizontal distance + depth + time)
-- Run:  psql -h localhost -p 5433 -U postgres -d spatial_db -f sql/m1_encounters.sql
-- Edit the params CTE to change the box, sensor or thresholds.
-- Thresholds mirror MATCH_MAX_* in .env (the backend reads those).
WITH params AS (
    SELECT ST_MakeEnvelope(-16, 49, -14, 51, 4326) AS bbox,
           'oxygen'::text  AS target_param,
           50000.0         AS max_dist_m,   -- horizontal, metres
           100.0           AS max_dz_m,     -- vertical, metres
           10 * 86400      AS max_dt_s      -- time, seconds (10 days)
),
assets AS (                                   -- 1. GiST index prune + JSONB sensor filter
    SELECT a.platform_id, a.platform_type, a.trajectory_4d
    FROM remote_ocean_fleet_index a, params p
    WHERE a.trajectory_4d && p.bbox
      AND a.available_parameters ? p.target_param
),
pts AS (                                      -- 2. explode each track into its 4D points
    SELECT a.platform_id, a.platform_type, d.path[1] AS n, d.geom
    FROM assets a, LATERAL ST_DumpPoints(a.trajectory_4d) AS d
),
matches AS (                                  -- 3. every glider point x float point inside all 3 thresholds
    SELECT g.platform_id AS glider_id, f.platform_id AS argo_id,
           g.n AS glider_pt, f.n AS argo_pt,
           ST_Distance(g.geom::geography, f.geom::geography) AS dist_m,
           abs(ST_Z(g.geom) - ST_Z(f.geom))                 AS dz_m,
           abs(ST_M(g.geom) - ST_M(f.geom))                 AS dt_s,
           g.geom AS g_geom, f.geom AS f_geom
    FROM pts g
    JOIN pts f ON g.platform_type = 'GLIDER' AND f.platform_type = 'ARGO'
    CROSS JOIN params p
    WHERE ST_DWithin(g.geom::geography, f.geom::geography, p.max_dist_m)
      AND abs(ST_Z(g.geom) - ST_Z(f.geom)) < p.max_dz_m
      AND abs(ST_M(g.geom) - ST_M(f.geom)) < p.max_dt_s
)
SELECT DISTINCT ON (glider_id, argo_id)       -- 4. keep the closest match per pair
       glider_id, argo_id, glider_pt, argo_pt,
       round(dist_m::numeric)             AS dist_m,
       dz_m,
       round(dt_s / 60.0)                 AS dt_min,
       to_timestamp(ST_M(g_geom))         AS glider_time,
       ST_Z(g_geom)                       AS glider_depth,
       count(*) OVER (PARTITION BY glider_id, argo_id) AS n_matching_points
FROM matches
ORDER BY glider_id, argo_id, dist_m;
