"""
Argo ingest: fetch float profiles for a box and time window with argopy
(Ifremer ERDDAP, which serves the Coriolis GDAC data), reduce each profile to
its turning points and return 4D tracks.

Everything stays in memory: no NetCDF or cache files are written.
"""
import logging
from dataclasses import dataclass

import argopy
import pandas as pd
from argopy.errors import NoData

log = logging.getLogger(__name__)

# Argo floats rise at roughly 10 cm/s. The profile timestamp is (close to) the
# surfacing time, so the time at the deepest point is estimated from this.
ASCENT_SPEED_M_S = 0.1

# Argo variable name -> name used in available_parameters
PARAM_NAMES = {
    "TEMP": "temperature",
    "PSAL": "salinity",
    "PRES": "pressure",
    "DOXY": "oxygen",
    "NITRATE": "nitrate",
}


@dataclass
class Track:
    platform_id: str
    platform_type: str
    points: list            # [(lon, lat, z, epoch_seconds)], epoch strictly increasing
    params: list            # e.g. ["temperature", "salinity", "pressure", "oxygen"]
    source_url: str


@dataclass
class FetchStats:
    raw_points: int = 0         # measurement rows returned by ERDDAP
    profiles: int = 0           # distinct float cycles
    stored_points: int = 0      # points kept in the LineStringZM tracks
    floats: int = 0


def _fetch(src: str, ds: str, region: list) -> pd.DataFrame:
    """One argopy region request; an empty frame if the server has no data."""
    try:
        fetcher = argopy.DataFetcher(src=src, ds=ds, mode="standard", cache=False)
        return fetcher.region(region).to_dataframe()
    except NoData:
        return pd.DataFrame()


def _epoch_seconds(ts) -> float:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return (ts - pd.Timestamp("1970-01-01")) / pd.Timedelta(seconds=1)


def profiles_to_tracks(df: pd.DataFrame) -> tuple[list[Track], FetchStats]:
    """
    Turn argopy measurement rows (one row per level) into one 4D track per float.

    Each profile is reduced to two turning points:
      deepest level  (lon, lat, -max_pres, surfacing_time - max_pres / ascent_speed)
      surface level  (lon, lat, -min_pres, surfacing_time)
    Pressure in dbar is used as depth in metres (within ~1% in the upper 2000 m).
    """
    stats = FetchStats(raw_points=len(df))
    if df.empty:
        return [], stats

    present = [c for c in PARAM_NAMES if c in df.columns]
    tracks = []
    for wmo, fdf in df.groupby("PLATFORM_NUMBER"):
        params = [PARAM_NAMES[c] for c in present if fdf[c].notna().any()]
        points = []
        for _, prof in fdf.groupby("CYCLE_NUMBER"):
            prof = prof.dropna(subset=["PRES", "LONGITUDE", "LATITUDE", "TIME"])
            if prof.empty:
                continue
            stats.profiles += 1
            lon = float(prof["LONGITUDE"].iloc[0])
            lat = float(prof["LATITUDE"].iloc[0])
            t_surface = _epoch_seconds(prof["TIME"].max())
            p_min, p_max = float(prof["PRES"].min()), float(prof["PRES"].max())
            if p_max - p_min >= 1:
                points.append((lon, lat, -p_max, t_surface - p_max / ASCENT_SPEED_M_S))
            points.append((lon, lat, -p_min, t_surface))

        # A trajectory needs time strictly increasing (ST_IsValidTrajectory)
        points.sort(key=lambda p: p[3])
        clean = []
        for p in points:
            if not clean or p[3] > clean[-1][3]:
                clean.append(p)
        if len(clean) < 2:
            continue

        stats.stored_points += len(clean)
        tracks.append(Track(
            platform_id=f"ARGO_{int(wmo)}",
            platform_type="ARGO",
            points=clean,
            params=params,
            source_url=f"https://fleetmonitoring.euro-argo.eu/float/{int(wmo)}",
        ))
    stats.floats = len(tracks)
    return tracks, stats


def fetch_argo_tracks(min_lon, min_lat, max_lon, max_lat, start, end, src="erddap"):
    """
    Fetch core (T/S/P) and BGC (oxygen, nitrate...) Argo data for a box and
    time window, and return (tracks, stats).

    Note argopy's region order: [lon_min, lon_max, lat_min, lat_max, pres_min, pres_max, t_min, t_max]
    (not the min_lon, min_lat, max_lon, max_lat order of a bbox). The end date is exclusive.
    """
    region = [min_lon, max_lon, min_lat, max_lat, 0, 6000,
              pd.Timestamp(start).strftime("%Y-%m-%d"), pd.Timestamp(end).strftime("%Y-%m-%d")]
    log.info("argopy %s region %s", src, region)

    core = _fetch(src, "phy", region)
    bgc = _fetch(src, "bgc", region) if src == "erddap" else pd.DataFrame()  # BGC only on erddap
    frames = [f for f in (core, bgc) if not f.empty]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    tracks, stats = profiles_to_tracks(df)
    log.info("argo: %s rows -> %s profiles -> %s floats, %s stored points",
             stats.raw_points, stats.profiles, stats.floats, stats.stored_points)
    return tracks, stats
