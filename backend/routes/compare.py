"""GET /api/compare: check one glider dive against one Argo reference profile."""
from fastapi import APIRouter, Query

from helpers.compare import run_comparison

router = APIRouter()


@router.get("/api/compare")
def compare(
    glider: str = Query(..., description="glider dataset_id"),
    glider_profile: str = Query(..., description="glider profile_id (from the reference)"),
    argo_profile: str = Query(..., description="Argo profile id <wmo>_<cycle>, e.g. 6990687_051"),
):
    try:
        return run_comparison(glider, glider_profile, argo_profile)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
