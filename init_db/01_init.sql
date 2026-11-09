-- Enable PostGIS Extensions
CREATE EXTENSION IF NOT EXISTS postgis;

-- 1. Main Table for Global Active Fleet Index
CREATE TABLE IF NOT EXISTS remote_ocean_fleet_index (
    platform_id VARCHAR(50) PRIMARY KEY,
    platform_type VARCHAR(20) CHECK (platform_type IN ('ARGO', 'GLIDER')),
    last_surfaced_at TIMESTAMP WITH TIME ZONE NOT NULL,
    trajectory_4d GEOMETRY(LineStringZM, 4326), -- Longitude, Latitude, Depth(Z), EpochTime(M)
    remote_source_url TEXT NOT NULL,
    available_parameters JSONB NOT NULL
);

-- Index for multi-dimensional spatial joins
CREATE INDEX IF NOT EXISTS idx_fleet_spatial_4d ON remote_ocean_fleet_index USING GIST(trajectory_4d);
-- GIN index for lightning-fast sub-document searching inside sensor lists
CREATE INDEX IF NOT EXISTS idx_fleet_parameters ON remote_ocean_fleet_index USING GIN (available_parameters);

-- 2. Spatiotemporal Cache Tracking Table
CREATE TABLE IF NOT EXISTS spatial_cache_registry (
    id SERIAL PRIMARY KEY,
    cache_window GEOMETRY(Polygon, 4326),
    last_fetched_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cache_window_spatial ON spatial_cache_registry USING GIST(cache_window);
