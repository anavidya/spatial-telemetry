"""
FastAPI entry point: creates the app and registers the routes.

    routes/   one module per API area (deployments = calibration finder; overlaps,
              platforms, cache = original box search; config)
    helpers/  settings, database, schema, glider catalogue, calibration matching,
              ERDDAP/Argo ingest, cache and encounter SQL
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from helpers import catalogue, retention, settings
from helpers.schema import apply_schema
from routes import cache, compare, config, deployments, overlaps, platforms

logging.basicConfig(level=logging.INFO)

@asynccontextmanager
async def lifespan(app):
    apply_schema()   # create the calibration-finder tables if they don't exist
    # Keep the glider deployment catalogue in PostGIS fresh (now, then every TTL minutes)
    catalogue.start_refresh_loop(settings.GLIDER_ERDDAP_URLS, settings.FETCH_LOOKBACK_DAYS,
                                 settings.GLIDER_CATALOGUE_TTL_MIN)
    retention.start_loop()   # expire profile rows nobody has used for PROFILES_RETENTION_DAYS
    yield


app = FastAPI(title="5D Ocean Spatiotemporal Encounter Registry", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (deployments, compare, overlaps, platforms, cache, config):
    app.include_router(module.router)
