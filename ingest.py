import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
from sqlalchemy import create_engine

# 1. Connect to PostGIS
engine = create_engine('postgresql+psycopg2://postgres:postgrespassword@localhost:5433/spatial_db')

# 2. Dummy telemetry data (Latitude, Longitude)
data = {
    'platform_id': ['GLIDER_01', 'GLIDER_01', 'GLIDER_02'],
    'recorded_at': pd.to_datetime(['2026-10-25 08:00', '2026-10-25 12:00', '2026-10-25 10:00']),
    'lat': [50.1, 50.2, 51.5],
    'lon': [-4.2, -4.1, -1.2]
}
df = pd.DataFrame(data)

# 3. Convert DataFrame to GeoDataFrame
geometry = [Point(xy) for xy in zip(df.lon, df.lat)] # Note: Point order is (Longitude, Latitude)
gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
gdf = gdf.rename_geometry('location')

# 4. Push to PostGIS
gdf[['platform_id', 'recorded_at', 'location']].to_postgis(
    'platform_telemetry', 
    engine, 
    if_exists='append', 
    index=False,
    schema='public'
)

print("Spatial ingestion successful!")