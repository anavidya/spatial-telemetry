"""
Calibration finder: for one glider deployment, find the Argo profiles that can
serve as references for checking its sensors.

    1. Glider dives  - positions/times from ERDDAP's cached subset table (fast),
                       depth range per dive via orderByMinMax when affordable,
                       otherwise the deployment's geospatial_vertical_min/max.
    2. Argo profiles - argopy (Ifremer ERDDAP) around the glider track,
                       buffered by the distance limit, dates +/- the time limit.
    3. Matching      - one SQL query: ST_DWithin on geography, time window,
                       numrange intersection below the stable-layer top,
                       best glider dive per Argo profile, top N by score.
Nothing is written to disk; profiles are stored in the PostGIS table `profiles`.
"""
import logging
import math
import time
from functools import lru_cache

import argopy
import pandas as pd
from argopy.errors import NoData
from psycopg2.extras import execute_values

from helpers import settings
from helpers.db import db_cursor, fetch_dicts
from helpers.erddap import ErddapNoData, get_csv, tabledap_url

log = logging.getLogger(__name__)

SENSOR_KEYWORDS = {"temperature": "temperature", "salinity": "salinity", "pressure": "pressure",
                   "oxygen": "oxygen", "doxy": "oxygen", "nitrate": "nitrate"}
# EGO / OceanGliders style names (DFO, European glider servers)
EGO_NAMES = {"TEMP": "temperature", "PSAL": "salinity", "CNDC": "salinity", "PRES": "pressure"}
SENSOR_ORDER = ["temperature", "salinity", "pressure", "oxygen", "nitrate"]
ARGO_PARAMS = {"TEMP": "temperature", "PSAL": "salinity", "PRES": "pressure",
               "DOXY": "oxygen", "NITRATE": "nitrate"}


# ---------------------------------------------------------------- glider side
@lru_cache(maxsize=512)
def dataset_info(server, dataset_id):
    """(variable names, global attributes) of a deployment; cached, they don't change."""
    info = get_csv(f"{server}/info/{dataset_id}/index.csv", timeout=settings.GLIDER_SEARCH_TIMEOUT_S)
    variables = frozenset(info.loc[info["Row Type"] == "variable", "Variable Name"])
    glob = info[(info["Row Type"] == "attribute") & (info["Variable Name"] == "NC_GLOBAL")]
    attrs = dict(zip(glob["Attribute Name"], glob["Value"]))
    return variables, attrs


def glider_sensors(variables) -> list:
    found = set()
    for var in variables:
        low = var.lower()
        if low.startswith("qartod_") or low.endswith("_qc") or low.endswith("_uncertainty"):
            continue
        if var in EGO_NAMES:
            found.add(EGO_NAMES[var])
        for keyword, name in SENSOR_KEYWORDS.items():
            if keyword in low:
                found.add(name)
    return [s for s in SENSOR_ORDER if s in found]


def _float(value):
    try:
        f = float(value)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def fetch_glider_dives(server, dataset_id, deep_needed_m=None):
    """
    One row per dive/climb: profile_id, t, lon, lat, dmin, dmax.
    Returns (dives, report) where report says where the depth ranges came from.
    If the metadata says the glider never reaches deep_needed_m, per-dive depths are
    skipped (the slow part): it cannot have references anyway.
    """
    variables, attrs = dataset_info(server, dataset_id)
    if "profile_id" not in variables:
        return fetch_trajectory_dives(server, dataset_id, variables, attrs, deep_needed_m)
    report = {"sensors": glider_sensors(variables), "title": attrs.get("title"), "layout": "profiles (IOOS DAC)",
              "platform": attrs.get("platform_type") or attrs.get("platform")}

    # Positions and times: profile-level variables are in ERDDAP's cached subset table,
    # so this does not scan the measurements.
    tic = time.perf_counter()
    dives = get_csv(tabledap_url(server, dataset_id, ["profile_id", "time", "latitude", "longitude"],
                                 ["distinct()"]), timeout=settings.GLIDER_SEARCH_TIMEOUT_S)
    dives = dives.dropna().drop_duplicates("profile_id")
    dives["t"] = pd.to_datetime(dives["time"], utc=True)
    dives = dives.rename(columns={"latitude": "lat", "longitude": "lon"})[["profile_id", "t", "lon", "lat"]]
    report["n_dives"] = len(dives)
    report["seconds_positions"] = round(time.perf_counter() - tic, 1)

    # Depth range per dive when the deployment is small enough, else from metadata
    vmin, vmax = _float(attrs.get("geospatial_vertical_min")), _float(attrs.get("geospatial_vertical_max"))
    depth_source = None
    too_shallow = deep_needed_m is not None and vmax is not None and vmax < deep_needed_m
    if too_shallow:
        report["skipped_per_dive_depths"] = "metadata shows the glider never reaches the comparison layer"
    elif len(dives) <= settings.GLIDER_PER_DIVE_MAX_PROFILES:
        tic = time.perf_counter()
        try:
            mm = get_csv(tabledap_url(server, dataset_id, ["profile_id", "depth"],
                                      ["depth!=NaN", 'orderByMinMax("profile_id,depth")']),
                         timeout=settings.GLIDER_DATASET_TIMEOUT_S)
            rng = mm.groupby("profile_id")["depth"].agg(["min", "max"]).rename(columns={"min": "dmin", "max": "dmax"})
            dives = dives.merge(rng, left_on="profile_id", right_index=True, how="inner")
            depth_source = "per dive"
        except Exception as exc:
            log.warning("per-dive depths for %s failed (%s); using deployment metadata", dataset_id, exc)
        report["seconds_depths"] = round(time.perf_counter() - tic, 1)
    if depth_source is None:
        if vmax is None:
            raise ValueError("no per-dive depths and no geospatial_vertical_max in the metadata")
        dives["dmin"], dives["dmax"] = max(vmin or 0.0, 0.0), vmax
        depth_source = "deployment metadata"
    report["depth_source"] = depth_source
    report["max_depth_m"] = float(dives["dmax"].max()) if len(dives) else vmax
    return dives, report


def _clean_positions(df):
    """Drop impossible or far-off positions (real-time glider data has occasional junk fixes)."""
    df = df[df["lon"].between(-180, 180) & df["lat"].between(-90, 90)
            & ~((df["lon"] == 0) & (df["lat"] == 0))]          # 0,0 = a failed GPS fix
    if len(df) >= 3:
        lon_med, lat_med = df["lon"].median(), df["lat"].median()
        df = df[((df["lon"] - lon_med).abs() < 10) & ((df["lat"] - lat_med).abs() < 10)]
    return df


def fetch_trajectory_dives(server, dataset_id, variables, attrs, deep_needed_m=None):
    """
    Trajectory-layout datasets (EGO / OceanGliders style, e.g. DFO): no dive numbers, one
    continuous series. ERDDAP thins it to one row every GLIDER_TRAJ_SAMPLE (orderByClosest),
    then rows are grouped into pseudo-profiles of GLIDER_TRAJ_BIN_HOURS, each with its
    depth range, mean position and mid time.
    """
    report = {"sensors": glider_sensors(variables), "title": attrs.get("title"),
              "layout": f"trajectory, {settings.GLIDER_TRAJ_BIN_HOURS:g} h pseudo-profiles",
              "platform": attrs.get("platform") or attrs.get("platform_type")}
    vmin, vmax = _float(attrs.get("geospatial_vertical_min")), _float(attrs.get("geospatial_vertical_max"))
    depth_var = "depth" if "depth" in variables else "PRES" if "PRES" in variables else None
    if depth_var is None:
        raise ValueError("dataset has neither depth nor PRES")
    if deep_needed_m is not None and vmax is not None and vmax < deep_needed_m:
        report["skipped_per_dive_depths"] = "metadata shows the glider never reaches the comparison layer"

    tic = time.perf_counter()
    rows = get_csv(tabledap_url(server, dataset_id, ["time", "latitude", "longitude", depth_var],
                                [f"{depth_var}!=NaN", f'orderByClosest("time/{settings.GLIDER_TRAJ_SAMPLE}")']),
                   timeout=settings.GLIDER_DATASET_TIMEOUT_S)
    rows = rows.rename(columns={"latitude": "lat", "longitude": "lon", depth_var: "depth"})
    rows["t"] = pd.to_datetime(rows["time"], utc=True, errors="coerce")
    rows = rows.dropna(subset=["t", "depth"]).sort_values("t")
    # Some real-time Slocum feeds (e.g. NOC on linkedsystems.uk) only carry a position at the
    # surface GPS fixes; the underwater rows have no latitude/longitude. Clean the fixes, then
    # fill the rows in between by interpolating in time, so the dives are not all dropped.
    fixes = _clean_positions(rows.dropna(subset=["lat", "lon"]))
    report["position_fixes"] = len(fixes)
    rows = rows.drop(columns=["lat", "lon"]).join(fixes[["lat", "lon"]])
    filled = rows.set_index("t")[["lat", "lon"]].interpolate(method="time", limit_area="inside")
    rows[["lat", "lon"]] = filled.to_numpy()
    rows = rows.dropna(subset=["lat", "lon"])
    rows["bin"] = rows["t"].dt.floor(f"{settings.GLIDER_TRAJ_BIN_HOURS:g}h")
    dives = rows.groupby("bin").agg(t=("t", "mean"), lon=("lon", "mean"), lat=("lat", "mean"),
                                    dmin=("depth", "min"), dmax=("depth", "max"), n=("depth", "size")).reset_index()
    dives = dives[dives["n"] >= 3]
    dives["dmin"] = dives["dmin"].clip(lower=0)
    dives["profile_id"] = dives["bin"].dt.strftime("%Y%m%dT%H%M")
    dives = dives[["profile_id", "t", "lon", "lat", "dmin", "dmax"]]
    report.update(n_dives=len(dives), rows_transferred=len(rows),
                  seconds_positions=round(time.perf_counter() - tic, 1),
                  depth_source=f"{settings.GLIDER_TRAJ_SAMPLE} samples grouped in {settings.GLIDER_TRAJ_BIN_HOURS:g} h",
                  max_depth_m=float(dives["dmax"].max()) if len(dives) else vmax)
    return dives, report


def thin_track(dives, hours):
    """
    One row per `hours` window: mean position, mid time, depth range = min..max over the window.
    Used for gliders that can't have references, so the map still gets their track without
    storing every dive.
    """
    if dives.empty:
        return dives
    bins = dives["t"].dt.floor(f"{hours:g}h")
    out = dives.groupby(bins).agg(t=("t", "mean"), lon=("lon", "mean"), lat=("lat", "mean"),
                                  dmin=("dmin", "min"), dmax=("dmax", "max")).reset_index(drop=True)
    out["profile_id"] = out["t"].dt.strftime("track_%Y%m%dT%H%M")
    return out[["profile_id", "t", "lon", "lat", "dmin", "dmax"]]


# ------------------------------------------------------------------ Argo side
def _epoch_like(ts):
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def fetch_argo_profiles(min_lon, min_lat, max_lon, max_lat, start, end, src="erddap"):
    """One row per Argo profile in the box and window: wmo, cycle, t, lon, lat, pmin, pmax, sensors, data_mode."""
    region = [min_lon, max_lon, min_lat, max_lat, 0, 6000,
              pd.Timestamp(start).strftime("%Y-%m-%d"), pd.Timestamp(end).strftime("%Y-%m-%d")]
    frames = []
    for ds in (["phy", "bgc"] if src == "erddap" else ["phy"]):
        try:
            frames.append(argopy.DataFetcher(src=src, ds=ds, mode="standard", cache=False)
                          .region(region).to_dataframe())
        except (NoData, FileNotFoundError):
            # argopy reports ERDDAP's 404 "no matching results" (an empty box) as
            # FileNotFoundError in some versions: treat it as "no profiles here", not a failure
            pass
    if not frames:
        return pd.DataFrame(), 0
    df = pd.concat(frames, ignore_index=True).dropna(subset=["PRES", "LONGITUDE", "LATITUDE", "TIME"])
    n_measurements = len(df)
    present = [c for c in ARGO_PARAMS if c in df.columns]
    rows = []
    for (wmo, cycle), prof in df.groupby(["PLATFORM_NUMBER", "CYCLE_NUMBER"]):
        rows.append({
            "wmo": int(wmo), "cycle": int(cycle),
            "t": _epoch_like(prof["TIME"].max()),
            "lon": float(prof["LONGITUDE"].iloc[0]), "lat": float(prof["LATITUDE"].iloc[0]),
            "pmin": float(prof["PRES"].min()), "pmax": float(prof["PRES"].max()),
            "sensors": [ARGO_PARAMS[c] for c in present if prof[c].notna().any()],
            "data_mode": str(prof["DATA_MODE"].dropna().iloc[0]) if "DATA_MODE" in prof and prof["DATA_MODE"].notna().any() else None,
        })
    return pd.DataFrame(rows), n_measurements


# ------------------------------------------------------------------- storage
INSERT_SQL = """
    INSERT INTO profiles (platform_id, platform_type, profile_id, geom, t, depth_range,
                          sensors, data_mode, source_url, fetched_at)
    VALUES %s
    ON CONFLICT (platform_id, profile_id) DO UPDATE SET
        geom = EXCLUDED.geom, t = EXCLUDED.t, depth_range = EXCLUDED.depth_range,
        sensors = EXCLUDED.sensors, data_mode = EXCLUDED.data_mode,
        source_url = EXCLUDED.source_url, fetched_at = now();
"""
ROW_TEMPLATE = ("(%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, "
                "numrange(%s::numeric, %s::numeric, '[]'), %s::jsonb, %s, %s, now())")


def store_glider_dives(cursor, platform_id, dives, sensors, url):
    import json
    rows = [(platform_id, "GLIDER", str(d.profile_id), float(d.lon), float(d.lat), d.t.to_pydatetime(),
             round(float(d.dmin), 1), round(float(d.dmax), 1), json.dumps(sensors), None, url)
            for d in dives.itertuples()]
    cursor.execute("DELETE FROM profiles WHERE platform_id = %s", (platform_id,))
    if rows:
        execute_values(cursor, INSERT_SQL, rows, template=ROW_TEMPLATE)


def store_argo_profiles(cursor, argo):
    import json
    rows = [(f"ARGO_{a.wmo}", "ARGO", f"{a.wmo}_{a.cycle:03d}", a.lon, a.lat, a.t.to_pydatetime(),
             round(a.pmin, 1), round(a.pmax, 1), json.dumps(a.sensors), a.data_mode,
             f"https://fleetmonitoring.euro-argo.eu/float/{a.wmo}")
            for a in argo.itertuples()]
    if rows:
        execute_values(cursor, INSERT_SQL, rows, template=ROW_TEMPLATE)


# ------------------------------------------------------------ Argo fetch cache
COVERED_SQL = """
    SELECT id, fetched_at, profiles FROM argo_fetch_windows
    WHERE source = %(src)s
      AND ST_Covers(area, ST_MakeEnvelope(%(w)s, %(s)s, %(e)s, %(n)s, 4326))   -- area contains the box
      AND period @> tstzrange(%(t0)s, %(t1)s, '[]')                            -- period contains the dates
      AND fetched_at > now() - make_interval(secs => %(ttl)s)
    ORDER BY fetched_at DESC
    LIMIT 1;
"""


def argo_window_covered(cursor, box, t0, t1, src):
    """The most recent fresh fetch window covering this box and period, or None."""
    cursor.execute(COVERED_SQL, {"src": src, "w": box[0], "s": box[1], "e": box[2], "n": box[3],
                                 "t0": t0, "t1": t1, "ttl": settings.ARGO_FETCH_TTL_DAYS * 86400})
    row = cursor.fetchone()
    return {"window_id": row[0], "fetched_at": row[1], "profiles": row[2]} if row else None


def record_argo_window(cursor, box, t0, t1, src, n_profiles):
    cursor.execute("""
        INSERT INTO argo_fetch_windows (area, period, source, profiles)
        VALUES (ST_MakeEnvelope(%s, %s, %s, %s, 4326), tstzrange(%s, %s, '[]'), %s, %s);
    """, (*box, t0, t1, src, n_profiles))
    # Tidy up windows long past their refresh age
    cursor.execute("DELETE FROM argo_fetch_windows WHERE fetched_at < now() - make_interval(secs => %s)",
                   (settings.ARGO_FETCH_TTL_DAYS * 86400 * 4,))


# ------------------------------------------------------------------ matching
REFERENCE_SQL = """
WITH g AS (                                      -- the glider's dives
    SELECT profile_id, geom, t, depth_range FROM profiles WHERE platform_id = %(glider)s
),
pairs AS (                                       -- Argo profiles near each dive (indexed)
    SELECT a.platform_id AS argo_id, a.profile_id AS argo_profile, a.data_mode,
           a.sensors AS argo_sensors, a.source_url,
           g.profile_id AS glider_profile,
           ST_Distance(g.geom::geography, a.geom::geography)  AS dist_m,
           abs(extract(epoch FROM a.t - g.t))                 AS dt_s,
           g.depth_range * a.depth_range
               * numrange(%(layer_top)s::numeric, NULL)       AS shared,   -- range intersection
           a.depth_range AS argo_range, g.depth_range AS glider_range,
           a.geom AS a_geom, g.geom AS g_geom, a.t AS argo_time, g.t AS glider_time
    FROM g
    JOIN profiles a
      ON a.platform_type = 'ARGO'
     AND a.t BETWEEN g.t - make_interval(secs => %(max_dt)s) AND g.t + make_interval(secs => %(max_dt)s)
     AND ST_DWithin(g.geom::geography, a.geom::geography, %(max_dist)s)
    WHERE a.sensors ? %(param)s
),
scored AS (
    SELECT *, upper(shared) - lower(shared)                   AS overlap_m,
           dist_m / %(max_dist)s + dt_s / %(max_dt)s          AS score  -- 0 best, 2 worst
    FROM pairs
    WHERE NOT isempty(shared)
),
best AS (                                        -- best glider dive for each Argo profile
    SELECT DISTINCT ON (argo_id, argo_profile) *,
           count(*) OVER (PARTITION BY argo_id, argo_profile) AS n_glider_dives
    FROM scored
    WHERE overlap_m >= %(min_overlap)s
    ORDER BY argo_id, argo_profile, score
)
SELECT argo_id, argo_profile, data_mode, argo_sensors, source_url, glider_profile,
       round(dist_m::numeric) AS dist_m, round(dt_s::numeric) AS dt_s, round(score::numeric, 3) AS score,
       lower(shared)::float AS shared_top_m, upper(shared)::float AS shared_bottom_m, overlap_m::float,
       lower(argo_range)::float AS argo_pmin, upper(argo_range)::float AS argo_pmax,
       lower(glider_range)::float AS glider_dmin, upper(glider_range)::float AS glider_dmax,
       argo_time, glider_time, n_glider_dives,
       ST_X(a_geom) AS argo_lon, ST_Y(a_geom) AS argo_lat, ST_X(g_geom) AS glider_lon, ST_Y(g_geom) AS glider_lat
FROM best
ORDER BY score
LIMIT %(top_n)s;
"""


def reference_rule():
    return {
        "max_dist_m": settings.REF_MAX_DIST_M,
        "max_time_days": settings.REF_MAX_TIME_S / 86400,
        "layer_top_m": settings.REF_LAYER_TOP_M,
        "min_overlap_m": settings.REF_MIN_OVERLAP_M,
        "top_n": settings.REF_TOP_N,
    }


def find_references(cursor, glider_platform_id, param):
    cursor.execute(REFERENCE_SQL, {
        "glider": glider_platform_id, "param": param,
        "max_dist": settings.REF_MAX_DIST_M, "max_dt": settings.REF_MAX_TIME_S,
        "layer_top": settings.REF_LAYER_TOP_M, "min_overlap": settings.REF_MIN_OVERLAP_M,
        "top_n": settings.REF_TOP_N,
    })
    return fetch_dicts(cursor)


def glider_track(cursor, platform_id):
    """The glider's dives as a GeoJSON line (for the map) plus summary numbers."""
    cursor.execute("""
        SELECT ST_AsGeoJSON(ST_MakeLine(geom ORDER BY t))::jsonb AS line,
               count(*) AS n_dives, min(t) AS start_time, max(t) AS end_time,
               max(upper(depth_range))::float AS max_depth_m, max(fetched_at) AS fetched_at
        FROM profiles WHERE platform_id = %s;
    """, (platform_id,))
    return fetch_dicts(cursor)[0]


def argo_candidates(cursor, glider_platform_id):
    """Argo profiles fetched around this glider (all, not only matches), for the map."""
    cursor.execute("""
        WITH g AS (SELECT ST_Collect(geom) AS geoms, min(t) AS t0, max(t) AS t1
                   FROM profiles WHERE platform_id = %(glider)s)
        SELECT a.platform_id, a.profile_id, a.t, upper(a.depth_range)::float AS pmax, a.data_mode,
               ST_X(a.geom) AS lon, ST_Y(a.geom) AS lat
        FROM profiles a, g
        WHERE a.platform_type = 'ARGO'
          AND a.t BETWEEN g.t0 - make_interval(secs => %(max_dt)s) AND g.t1 + make_interval(secs => %(max_dt)s)
          AND ST_DWithin(a.geom::geography, g.geoms::geography, %(max_dist)s * 2)
        ORDER BY a.t;
    """, {"glider": glider_platform_id, "max_dt": settings.REF_MAX_TIME_S, "max_dist": settings.REF_MAX_DIST_M})
    return fetch_dicts(cursor)


# --------------------------------------------------------------- orchestration
def buffered_box(dives, dist_m):
    """Bounding box of the dives grown by dist_m (degrees; longitude widened by latitude)."""
    lat_pad = dist_m / 111_320
    max_abs_lat = min(float(dives["lat"].abs().max()), 85.0)
    lon_pad = dist_m / (111_320 * math.cos(math.radians(max_abs_lat)))
    return (float(dives["lon"].min()) - lon_pad, float(dives["lat"].min()) - lat_pad,
            float(dives["lon"].max()) + lon_pad, float(dives["lat"].max()) + lat_pad)


def find_reference_profiles(dataset_id, param):
    """Full workflow for one deployment. Returns the API response body."""
    platform_id = f"GLIDER_{dataset_id}"
    with db_cursor() as cursor:
        cursor.execute("SELECT server FROM glider_deployments WHERE dataset_id = %s", (dataset_id,))
        row = cursor.fetchone()
    server = row[0] if row else settings.GLIDER_ERDDAP_URL
    rule = reference_rule()
    report = {"seconds": {}}
    tic_all = time.perf_counter()

    # 1. Glider dives (re-used if fetched within the cache TTL)
    with db_cursor() as cursor:
        track = glider_track(cursor, platform_id)
    fresh = track["fetched_at"] is not None and \
        (pd.Timestamp.now(tz="UTC") - pd.Timestamp(track["fetched_at"])).total_seconds() < settings.CACHE_TTL_HOURS * 3600
    variables, attrs = dataset_info(server, dataset_id)
    sensors = glider_sensors(variables)
    if fresh:
        report["glider"] = {"cached": True}
    else:
        tic = time.perf_counter()
        deep_needed = rule["layer_top_m"] + rule["min_overlap_m"]
        dives, glider_report = fetch_glider_dives(server, dataset_id, deep_needed_m=deep_needed)
        # Can't match (never reaches the comparison layer): keep a thinned track for the map only
        if len(dives) and float(dives["dmax"].max()) < deep_needed:
            n_before = len(dives)
            dives = thin_track(dives, settings.GLIDER_SHALLOW_TRACK_HOURS)
            glider_report["thinned"] = (f"too shallow for references: {n_before} dives stored as "
                                        f"{len(dives)} track points ({settings.GLIDER_SHALLOW_TRACK_HOURS:g} h)")
        report["glider"] = {"cached": False, **glider_report}
        report["seconds"]["glider"] = round(time.perf_counter() - tic, 1)
        with db_cursor(commit=True) as cursor:
            store_glider_dives(cursor, platform_id, dives, sensors, f"{server}/tabledap/{dataset_id}.html")
            # Remember max depth and the extent measured from the cleaned dive positions
            cursor.execute("""
                UPDATE glider_deployments SET max_depth_m = %(depth)s,
                       data_extent = (SELECT ST_Envelope(ST_Buffer(ST_Envelope(ST_Collect(geom)), 0.01))
                                      FROM profiles WHERE platform_id = %(pid)s)
                WHERE dataset_id = %(id)s""",
                           {"depth": glider_report["max_depth_m"], "pid": platform_id, "id": dataset_id})
        with db_cursor() as cursor:
            track = glider_track(cursor, platform_id)

    response = {"dataset_id": dataset_id, "title": attrs.get("title"), "institution": attrs.get("institution"),
                "server": server,
                "param": param, "rule": rule, "glider_sensors": sensors,
                "glider": {k: track[k] for k in ("n_dives", "start_time", "end_time", "max_depth_m")},
                "glider_track": track["line"], "fetch": report, "references": [], "argo_candidates": []}

    # 2. Can this glider match at all?
    deep_needed = rule["layer_top_m"] + rule["min_overlap_m"]
    if not track["n_dives"]:
        response["verdict"] = "no dives found for this deployment"
        return response
    if (track["max_depth_m"] or 0) < deep_needed:
        response["verdict"] = (f"no deep overlap possible: this glider reaches {track['max_depth_m']:.0f} m, "
                               f"but references need data from {rule['layer_top_m']:.0f} to at least {deep_needed:.0f} m")
        report["seconds"]["total"] = round(time.perf_counter() - tic_all, 1)
        return response
    if param not in sensors:
        response["verdict"] = f"this glider has no {param} sensor (it has: {', '.join(sensors) or 'none listed'})"
        return response

    # 3. Argo profiles around the track, dates +/- the time limit
    with db_cursor() as cursor:
        cursor.execute("SELECT lon, lat FROM (SELECT ST_X(geom) lon, ST_Y(geom) lat FROM profiles WHERE platform_id=%s) d",
                       (platform_id,))
        pos = pd.DataFrame(cursor.fetchall(), columns=["lon", "lat"])
    box = buffered_box(pos, rule["max_dist_m"])
    area = (box[2] - box[0]) * (box[3] - box[1])
    t0 = pd.Timestamp(track["start_time"]) - pd.Timedelta(seconds=settings.REF_MAX_TIME_S)
    t1 = pd.Timestamp(track["end_time"]) + pd.Timedelta(seconds=settings.REF_MAX_TIME_S) + pd.Timedelta(days=1)
    report["argo_box"] = [round(v, 3) for v in box]
    if area > settings.MAX_FETCH_AREA_DEG2:
        response["verdict"] = (f"glider track box is {area:.0f} square degrees (limit {settings.MAX_FETCH_AREA_DEG2:.0f}); "
                               "raise MAX_FETCH_AREA_DEG2 to search it")
        return response
    tic = time.perf_counter()
    t0_utc, t1_utc = t0.to_pydatetime(), t1.to_pydatetime()
    with db_cursor() as cursor:
        cached = argo_window_covered(cursor, box, t0_utc, t1_utc, settings.ARGO_SOURCE)
    if cached:
        report["argo"] = {"cached": True, **cached}
    else:
        try:
            argo, n_meas = fetch_argo_profiles(*box, t0, t1, src=settings.ARGO_SOURCE)
            report["argo"] = {"cached": False, "profiles": len(argo),
                              "floats": int(argo["wmo"].nunique()) if len(argo) else 0, "measurements": n_meas}
            with db_cursor(commit=True) as cursor:
                store_argo_profiles(cursor, argo)
                record_argo_window(cursor, box, t0_utc, t1_utc, settings.ARGO_SOURCE, len(argo))
        except Exception as exc:   # not recorded as fetched, so the next open retries
            log.exception("Argo fetch failed")
            report["argo_error"] = f"{type(exc).__name__}: {exc}"
    report["seconds"]["argo"] = round(time.perf_counter() - tic, 1)

    # 4. Matching in PostGIS
    tic = time.perf_counter()
    with db_cursor() as cursor:
        response["references"] = find_references(cursor, platform_id, param)
        response["argo_candidates"] = argo_candidates(cursor, platform_id)
    report["seconds"]["match"] = round(time.perf_counter() - tic, 2)
    report["seconds"]["total"] = round(time.perf_counter() - tic_all, 1)
    n = len(response["references"])
    response["verdict"] = (f"{n} reference profile(s) found" if n else
                           "no Argo profile within the rule; try another sensor or a longer time limit")
    return response
