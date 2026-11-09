"""GET /api/config: settings the frontend shows in its legend."""
from fastapi import APIRouter

from helpers import catalogue, retention, settings
from helpers.calibration import reference_rule

router = APIRouter()


@router.get("/api/config")
def get_config():
    return {
        "reference_rule": reference_rule(),
        "catalogue": catalogue.status(),
        "retention": {"days": settings.PROFILES_RETENTION_DAYS, **retention.status()},
        "rule": settings.matchup_rule(),
        "cache_ttl_hours": settings.CACHE_TTL_HOURS,
        "lookback_days": settings.FETCH_LOOKBACK_DAYS,
        "argo_source": settings.ARGO_SOURCE,
        "argo_fetch_ttl_days": settings.ARGO_FETCH_TTL_DAYS,
        "glider_server": settings.GLIDER_ERDDAP_URL,
        "glider_servers": settings.GLIDER_ERDDAP_URLS,
        "mock_glider": settings.MOCK_GLIDER,
        "max_fetch_area_deg2": settings.MAX_FETCH_AREA_DEG2,
    }
