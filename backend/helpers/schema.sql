-- Calibration-finder schema. Applied automatically when the backend starts
-- (every statement is idempotent), and safe to run by hand:
--   psql -h localhost -p 5433 -U postgres -d spatial_db -f backend/helpers/schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;

-- Catalogue of glider deployments on the glider ERDDAP server.
-- Refreshed in the background (searching the server live takes ~1 minute).
CREATE TABLE IF NOT EXISTS glider_deployments (
    dataset_id    text PRIMARY KEY,
    title         text,
    institution   text,
    extent        geometry(Polygon, 4326) NOT NULL,   -- bounding box of the whole deployment
    active        tstzrange NOT NULL,                 -- [first, last] observation time
    max_depth_m   real,                               -- filled in when the deployment is opened
    server        text NOT NULL,
    refreshed_at  timestamptz NOT NULL DEFAULT now()
);
-- Extent measured from the dives once a deployment has been opened (more reliable than the
-- server's metadata, which can contain junk positions). Kept across catalogue refreshes.
ALTER TABLE glider_deployments ADD COLUMN IF NOT EXISTS data_extent geometry(Polygon, 4326);
CREATE INDEX IF NOT EXISTS idx_deployments_extent ON glider_deployments USING GIST (extent);
CREATE INDEX IF NOT EXISTS idx_deployments_active ON glider_deployments USING GIST (active);

-- One row per profile, i.e. one vertical column of measurements:
-- a glider dive/climb or an Argo float cycle.
CREATE TABLE IF NOT EXISTS profiles (
    platform_id    text NOT NULL,                      -- GLIDER_<dataset_id> or ARGO_<wmo>
    platform_type  text NOT NULL CHECK (platform_type IN ('GLIDER', 'ARGO')),
    profile_id     text NOT NULL,                      -- glider profile_id, or <wmo>_<cycle>
    geom           geometry(Point, 4326) NOT NULL,
    t              timestamptz NOT NULL,
    depth_range    numrange NOT NULL,                  -- metres (Argo: dbar, ~1% difference)
    sensors        jsonb NOT NULL DEFAULT '[]',        -- e.g. ["temperature","salinity","oxygen"]
    data_mode      text,                               -- Argo: R real-time, A adjusted, D delayed-mode
    source_url     text,
    fetched_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform_id, profile_id)
);
-- Expression index so ST_DWithin(geom::geography, ...) in metres can use it
CREATE INDEX IF NOT EXISTS idx_profiles_geog ON profiles USING GIST ((geom::geography));
CREATE INDEX IF NOT EXISTS idx_profiles_type_t ON profiles (platform_type, t);
CREATE INDEX IF NOT EXISTS idx_profiles_sensors ON profiles USING GIN (sensors);

-- Areas and periods already fetched from Argo. Before fetching around a glider, the backend
-- checks whether one stored window covers the needed box (ST_Covers) and period (@>);
-- if so, the Argo profiles are already in `profiles` and the fetch is skipped.
CREATE TABLE IF NOT EXISTS argo_fetch_windows (
    id          serial PRIMARY KEY,
    area        geometry(Polygon, 4326) NOT NULL,
    period      tstzrange NOT NULL,
    source      text NOT NULL,                 -- argopy source, e.g. erddap
    profiles    integer,                       -- Argo profiles found in this window
    fetched_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_argo_windows_area ON argo_fetch_windows USING GIST (area);
CREATE INDEX IF NOT EXISTS idx_argo_windows_period ON argo_fetch_windows USING GIST (period);
