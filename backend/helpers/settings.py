"""All runtime settings, read once from the environment (values come from .env via docker-compose.yml)."""
import os

DATABASE_URL = os.environ["DATABASE_URL"]

# Encounter rule: what counts as "sampling the same water"
MATCH_MAX_DIST_M = float(os.getenv("MATCH_MAX_DIST_M", "50000"))
MATCH_MAX_DEPTH_DIFF_M = float(os.getenv("MATCH_MAX_DEPTH_DIFF_M", "100"))
MATCH_MAX_TIME_DIFF_S = float(os.getenv("MATCH_MAX_TIME_DIFF_DAYS", "10")) * 86400

# How long a fetched box counts as fresh before it is fetched again
CACHE_TTL_HOURS = float(os.getenv("CACHE_TTL_HOURS", "12"))

# Real-data fetch
FETCH_LOOKBACK_DAYS = int(os.getenv("FETCH_LOOKBACK_DAYS", os.getenv("ARGO_LOOKBACK_DAYS", "30")))
ARGO_SOURCE = os.getenv("ARGO_SOURCE", "erddap")
GLIDER_ERDDAP_URL = os.getenv("GLIDER_ERDDAP_URL", "https://gliders.ioos.us/erddap").rstrip("/")
# Calibration finder: every glider ERDDAP to catalogue (comma separated)
GLIDER_ERDDAP_URLS = [u.strip().rstrip("/") for u in os.getenv(
    "GLIDER_ERDDAP_URLS",
    "https://gliders.ioos.us/erddap,https://cnodc-cndoc.azure.cloud-nuage.dfo-mpo.gc.ca/erddap",
).split(",") if u.strip()]
# Trajectory-layout datasets (no dive numbers): sample every N, group into pseudo-profiles of H hours
GLIDER_TRAJ_SAMPLE = os.getenv("GLIDER_TRAJ_SAMPLE", "5minutes")
GLIDER_TRAJ_BIN_HOURS = float(os.getenv("GLIDER_TRAJ_BIN_HOURS", "6"))
# A deployment whose box spans more than this (degrees of lon or lat) is treated as unreliable
# (junk positions or a multi-glider dataset): listed, but not drawn as a box
GLIDER_MAX_EXTENT_DEG = float(os.getenv("GLIDER_MAX_EXTENT_DEG", "20"))
GLIDER_MAX_DATASETS = int(os.getenv("GLIDER_MAX_DATASETS", "20"))
GLIDER_PARALLEL = int(os.getenv("GLIDER_PARALLEL", "4"))                  # deployments fetched at once
GLIDER_DATASET_TIMEOUT_S = float(os.getenv("GLIDER_DATASET_TIMEOUT_S", "180"))  # per deployment request
GLIDER_CATALOGUE_TTL_MIN = float(os.getenv("GLIDER_CATALOGUE_TTL_MIN", "60"))  # refresh of the deployment list
GLIDER_SEARCH_TIMEOUT_S = float(os.getenv("GLIDER_SEARCH_TIMEOUT_S", "120"))     # catalogue / info / dive positions
GLIDER_PER_DIVE_MAX_PROFILES = int(os.getenv("GLIDER_PER_DIVE_MAX_PROFILES", "3000"))  # above this, depth from metadata

# Reference-profile rule (calibration finder)
REF_MAX_DIST_M = float(os.getenv("REF_MAX_DIST_M", "50000"))
REF_MAX_TIME_S = float(os.getenv("REF_MAX_TIME_DAYS", "10")) * 86400
REF_LAYER_TOP_M = float(os.getenv("REF_LAYER_TOP_M", "150"))
REF_MIN_OVERLAP_M = float(os.getenv("REF_MIN_OVERLAP_M", "100"))
REF_TOP_N = int(os.getenv("REF_TOP_N", "5"))

# Argo fetch cache: reuse an earlier fetch that covers the needed area and period for this many days
# (old profiles rarely change, apart from real-time -> delayed-mode upgrades)
ARGO_FETCH_TTL_DAYS = float(os.getenv("ARGO_FETCH_TTL_DAYS", "7"))

# Retention: drop profile rows not used for this many days (glider not reopened; Argo not relied on)
PROFILES_RETENTION_DAYS = float(os.getenv("PROFILES_RETENTION_DAYS", "30"))
# Gliders that never reach the comparison layer can't have references: keep only a thinned
# track for the map, one row per this many hours (depth range = min/max over the window)
GLIDER_SHALLOW_TRACK_HOURS = float(os.getenv("GLIDER_SHALLOW_TRACK_HOURS", "3"))

# Sensor check (compare): salinity offset thresholds at equal potential temperature (glider - Argo)
CMP_SAL_OK = float(os.getenv("CMP_SAL_OK", "0.01"))       # within this: agrees
CMP_SAL_WARN = float(os.getenv("CMP_SAL_WARN", "0.03"))   # up to this: watch; beyond: possible drift
CMP_MIN_LEVELS = int(os.getenv("CMP_MIN_LEVELS", "5"))    # fewer shared theta levels: no verdict
MAX_FETCH_AREA_DEG2 = float(os.getenv("MAX_FETCH_AREA_DEG2", "100"))
MOCK_GLIDER = os.getenv("MOCK_GLIDER", "false").lower() == "true"


def matchup_rule():
    return {
        "max_dist_m": MATCH_MAX_DIST_M,
        "max_depth_diff_m": MATCH_MAX_DEPTH_DIFF_M,
        "max_time_diff_days": MATCH_MAX_TIME_DIFF_S / 86400,
    }
