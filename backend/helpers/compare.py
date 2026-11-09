"""
Sensor check for one glider dive and one Argo reference profile.

Fetches both full profiles (in memory only), converts to TEOS-10 quantities
(potential temperature, potential density) and compares them in the shared
deep layer:

  * MAIN: salinity at the same potential temperature (theta levels), the
    approach used in Argo delayed-mode salinity QC. In a stable deep layer the
    theta-S relation is conserved when eddies or internal waves lift the water,
    and a conductivity error changes S but not theta, so the offset at equal
    theta is the sensor offset. (Comparing at equal density mixes the two:
    a salinity error also shifts the computed density.)
  * Cross-checks on depth bins (heave-sensitive) for salinity and temperature.

Offsets are glider minus Argo. A steady salinity offset on theta levels in the
stable layer points to a glider conductivity offset or drift.
"""
import logging
import time

import argopy
import gsw
import numpy as np
import pandas as pd

from helpers import settings
from helpers.calibration import dataset_info
from helpers.db import db_cursor
from helpers.erddap import get_csv, tabledap_url

log = logging.getLogger(__name__)

THETA_BIN = 0.05     # degC width of potential-temperature levels
DEPTH_BIN = 20.0     # m width of depth bins
MAX_PLOT_POINTS = 800


# ------------------------------------------------------------------ fetching
def fetch_argo_profile(wmo: int, cycle: int) -> tuple[pd.DataFrame, dict]:
    """One Argo profile: pres, temp, psal (adjusted values where available, via argopy 'standard' mode)."""
    df = argopy.DataFetcher(src=settings.ARGO_SOURCE, ds="phy", mode="standard", cache=False) \
        .profile(wmo, cycle).to_dataframe()
    df = df.rename(columns={"PRES": "pres", "TEMP": "temp", "PSAL": "psal",
                            "LONGITUDE": "lon", "LATITUDE": "lat"})
    meta = {"data_mode": str(df["DATA_MODE"].dropna().iloc[0]) if "DATA_MODE" in df and df["DATA_MODE"].notna().any() else None,
            "lon": float(df["lon"].iloc[0]), "lat": float(df["lat"].iloc[0]),
            "time": pd.Timestamp(df["TIME"].iloc[0]).isoformat() if "TIME" in df else None}
    return df[["pres", "temp", "psal"]], meta


def fetch_glider_dive(server, dataset_id, profile_id) -> pd.DataFrame:
    """
    One glider dive: pres, temp, psal.
    IOOS DAC layout: rows with this profile_id. Trajectory layout (pseudo-profiles
    'YYYYmmddTHHMM'): every row in that GLIDER_TRAJ_BIN_HOURS window.
    """
    variables, _ = dataset_info(server, dataset_id)
    if "profile_id" in variables:
        cols = {"pressure": "pres", "temperature": "temp", "salinity": "psal", "depth": "depth"}
        cols = {k: v for k, v in cols.items() if k in variables}
        url = tabledap_url(server, dataset_id, list(cols), [f"profile_id={int(profile_id)}"])
    else:
        cols = {"PRES": "pres", "TEMP": "temp", "PSAL": "psal", "depth": "depth"}
        cols = {k: v for k, v in cols.items() if k in variables}
        t0 = pd.Timestamp(profile_id, tz="UTC")
        t1 = t0 + pd.Timedelta(hours=settings.GLIDER_TRAJ_BIN_HOURS)
        url = tabledap_url(server, dataset_id, list(cols),
                           [f"time>={t0.strftime('%Y-%m-%dT%H:%M:%SZ')}", f"time<{t1.strftime('%Y-%m-%dT%H:%M:%SZ')}"])
    df = get_csv(url, timeout=settings.GLIDER_DATASET_TIMEOUT_S).rename(columns=cols)
    if "pres" not in df and "depth" in df:
        df["pres"] = df["depth"]   # ~1% difference in the upper 2000 m; noted in the response
    return df[["pres", "temp", "psal"]]


# ------------------------------------------------------------------ physics
def teos10(df: pd.DataFrame, lon: float, lat: float) -> pd.DataFrame:
    """Add SA, CT, potential temperature and sigma0; drop implausible values."""
    df = df.apply(pd.to_numeric, errors="coerce").dropna()
    df = df[df["psal"].between(2, 42) & df["temp"].between(-2.5, 40) & (df["pres"] >= 0)].copy()
    df["SA"] = gsw.SA_from_SP(df["psal"], df["pres"], lon, lat)
    df["CT"] = gsw.CT_from_t(df["SA"], df["temp"], df["pres"])
    df["theta"] = gsw.pt0_from_t(df["SA"], df["temp"], df["pres"])
    df["sigma0"] = gsw.sigma0(df["SA"], df["CT"])
    return df


def _binned(df, key, width):
    b = (df[key] / width).round().astype(int)
    return df.groupby(b).agg(psal=("psal", "mean"), theta=("theta", "mean"), n=("psal", "size"))


def _stats(diff: pd.Series) -> dict:
    if diff.empty:
        return None
    q1, med, q3 = np.percentile(diff, [25, 50, 75])
    return {"median": round(float(med), 4), "iqr": [round(float(q1), 4), round(float(q3), 4)],
            "mean": round(float(diff.mean()), 4), "n_levels": int(diff.size)}


def compare_profiles(g: pd.DataFrame, a: pd.DataFrame, top: float, bottom: float) -> dict:
    """Offsets (glider - Argo) within [top, bottom] dbar, on density and on depth bins."""
    gl = g[g["pres"].between(top, bottom)]
    ar = a[a["pres"].between(top, bottom)]
    out = {"layer": [top, bottom], "n_glider": int(len(gl)), "n_argo": int(len(ar))}

    gt, at = _binned(gl, "theta", THETA_BIN), _binned(ar, "theta", THETA_BIN)
    both = gt.join(at, lsuffix="_g", rsuffix="_a", how="inner")
    out["theta_levels"] = {"salinity": _stats(both["psal_g"] - both["psal_a"]),
                           "theta_range": [round(float(both.index.min() * THETA_BIN), 2),
                                           round(float(both.index.max() * THETA_BIN), 2)] if len(both) else None}
    gd, ad = _binned(gl, "pres", DEPTH_BIN), _binned(ar, "pres", DEPTH_BIN)
    bd = gd.join(ad, lsuffix="_g", rsuffix="_a", how="inner")
    out["depth"] = {"salinity": _stats(bd["psal_g"] - bd["psal_a"]),
                    "temperature": _stats(bd["theta_g"] - bd["theta_a"])}
    return out


def verdict(result: dict) -> dict:
    """Plain-language verdict from the isopycnal salinity offset."""
    s = (result.get("theta_levels") or {}).get("salinity")
    if not s or s["n_levels"] < settings.CMP_MIN_LEVELS:
        return {"level": "unknown",
                "text": "Not enough shared temperature levels to judge salinity (water masses may differ, or the layer is thin)."}
    off, spread = abs(s["median"]), s["iqr"][1] - s["iqr"][0]
    if off <= settings.CMP_SAL_OK:
        level, text = "ok", f"Salinity agrees within {settings.CMP_SAL_OK:g} (median offset {s['median']:+.4f})."
    elif off <= settings.CMP_SAL_WARN:
        level, text = "watch", f"Small salinity offset {s['median']:+.4f}: worth watching across more references."
    else:
        level, text = "drift", f"Salinity offset {s['median']:+.4f}: possible glider conductivity offset or drift."
    if spread > 2 * settings.CMP_SAL_OK:
        text += f" The spread is wide (IQR {spread:.3f}), so the two may not be sampling the same water."
    return {"level": level, "text": text}


def _thin(df):
    if len(df) <= MAX_PLOT_POINTS:
        return df
    return df.iloc[np.linspace(0, len(df) - 1, MAX_PLOT_POINTS).astype(int)]


# ------------------------------------------------------------------ workflow
def run_comparison(dataset_id: str, glider_profile: str, argo_profile: str) -> dict:
    tic = time.perf_counter()
    platform_id = f"GLIDER_{dataset_id}"
    with db_cursor() as cursor:
        cursor.execute("""
            SELECT gd.server, ST_X(g.geom), ST_Y(g.geom), g.t,
                   lower(g.depth_range * a.depth_range * numrange(%(top)s::numeric, NULL))::float,
                   upper(g.depth_range * a.depth_range * numrange(%(top)s::numeric, NULL))::float,
                   a.data_mode, a.source_url
            FROM profiles g
            JOIN profiles a ON a.platform_type = 'ARGO' AND a.profile_id = %(aprof)s
            LEFT JOIN glider_deployments gd ON gd.dataset_id = %(id)s
            WHERE g.platform_id = %(pid)s AND g.profile_id = %(gprof)s;
        """, {"top": settings.REF_LAYER_TOP_M, "aprof": argo_profile, "id": dataset_id,
              "pid": platform_id, "gprof": glider_profile})
        row = cursor.fetchone()
    if not row:
        return {"error": "pair not found: open the glider's references first"}
    server, g_lon, g_lat, g_time, top, bottom, a_mode, a_url = row
    server = server or settings.GLIDER_ERDDAP_URL
    if top is None:
        return {"error": "the two profiles share no depth below the comparison layer top"}

    wmo, cycle = (int(x) for x in argo_profile.split("_"))
    timings = {}
    t = time.perf_counter()
    glider = fetch_glider_dive(server, dataset_id, glider_profile)
    timings["glider"] = round(time.perf_counter() - t, 1)
    t = time.perf_counter()
    argo, a_meta = fetch_argo_profile(wmo, cycle)
    timings["argo"] = round(time.perf_counter() - t, 1)

    g = teos10(glider, g_lon, g_lat)
    a = teos10(argo, a_meta["lon"], a_meta["lat"])
    result = compare_profiles(g, a, top, bottom)
    timings["total"] = round(time.perf_counter() - tic, 1)

    plot_cols = ["pres", "psal", "theta", "sigma0"]
    return {
        "dataset_id": dataset_id, "glider_profile": glider_profile, "argo_profile": argo_profile,
        "argo_data_mode": a_meta["data_mode"] or a_mode, "argo_url": a_url,
        "glider_time": pd.Timestamp(g_time).isoformat(), "argo_time": a_meta["time"],
        "result": result, "verdict": verdict(result),
        "thresholds": {"ok": settings.CMP_SAL_OK, "watch": settings.CMP_SAL_WARN},
        "notes": ["Offsets are glider minus Argo, in the shared layer only.",
                  "Pressure (dbar) is used as depth (~1% difference).",
                  "Real-time data on either side is less certain than delayed-mode."],
        "plot": {"glider": _thin(g[plot_cols]).round(4).to_dict("list"),
                 "argo": _thin(a[plot_cols]).round(4).to_dict("list")},
        "seconds": timings,
    }
