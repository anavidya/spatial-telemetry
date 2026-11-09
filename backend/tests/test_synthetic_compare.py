"""
Synthetic test of the sensor check: can it find a salinity error we hid on purpose?

Think of it as testing a weighing scale with a known weight:

  * a made-up ocean, defined by two smooth curves (temperature and salinity vs depth)
  * a fake Argo profile that measures it perfectly
  * a fake glider profile of the same ocean, with
      - a salinity sensor that reads too high/low by a known amount (`offset`), and
      - the whole water column pushed up by `lift` metres, as an eddy or wave does
  * the app's real code (teos10 + compare_profiles) is asked for the offset

Comparing at equal potential temperature should return the hidden offset;
comparing at equal depth (and at equal density) should not.

Run:
    cd backend && poetry run pytest tests/test_synthetic_compare.py -v
Print the comparison table used in the blog:
    cd backend && poetry run python tests/test_synthetic_compare.py
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://unused@localhost/unused")  # no database is touched
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from helpers.compare import compare_profiles, teos10

LON, LAT = -40.0, 45.0          # somewhere in the open North Atlantic
LAYER = (150, 950)              # the stable layer compared, as in the app
SCENARIOS = [                   # (hidden salinity offset, water lifted by m)
    (0.015, 25),
    (0.000, 25),
    (0.000, 0),
    (0.040, 60),
    (-0.008, 10),
]


def water(p):
    """The made-up ocean: temperature (°C) and salinity at depth p (m).
    Warm and salty at the surface (18 °C, 35.85), cold and steady at depth (4 °C, 34.95)."""
    p = np.asarray(p, dtype=float)
    temperature = 4 + 14 * np.exp(-p / 250)
    salinity = 34.95 + 0.9 * np.exp(-p / 300)
    return temperature, salinity


def make_profiles(offset, lift, seed=1):
    rng = np.random.default_rng(seed)

    # Argo: 200 levels to ~2000 m, the truth with no errors
    p_argo = np.linspace(5, 1995, 200)
    t, s = water(p_argo)
    argo = pd.DataFrame({"pres": p_argo, "temp": t, "psal": s})

    # Glider: 900 levels to 950 m, the same ocean but
    #   - at depth p it sees the water from p + lift (the column was pushed up)
    #   - its salinity reads `offset` too high (the broken sensor); temperature is fine
    #   - a little random noise, as real sensors have
    p_gl = np.linspace(1, 950, 900)
    t, s = water(p_gl + lift)
    glider = pd.DataFrame({"pres": p_gl,
                           "temp": t + rng.normal(0, 0.002, p_gl.size),
                           "psal": s + offset + rng.normal(0, 0.002, p_gl.size)})
    return teos10(glider, LON, LAT), teos10(argo, LON, LAT)


def offset_at_equal_density(g, a, width=0.005):
    """The density (sigma0) method, for comparison only: the app does not use it."""
    g = g[g.pres.between(*LAYER)]
    a = a[a.pres.between(*LAYER)]
    gs = g.groupby((g.sigma0 / width).round()).psal.mean()
    as_ = a.groupby((a.sigma0 / width).round()).psal.mean()
    both = pd.concat([gs, as_], axis=1, join="inner")
    return float(np.median(both.iloc[:, 0] - both.iloc[:, 1]))


def run(offset, lift):
    g, a = make_profiles(offset, lift)
    result = compare_profiles(g, a, *LAYER)
    return {"theta": result["theta_levels"]["salinity"]["median"],
            "depth": result["depth"]["salinity"]["median"],
            "density": offset_at_equal_density(g, a)}


@pytest.mark.parametrize("offset,lift", SCENARIOS)
def test_equal_potential_temperature_recovers_hidden_offset(offset, lift):
    assert run(offset, lift)["theta"] == pytest.approx(offset, abs=0.001)


def test_equal_depth_is_fooled_by_lifted_water():
    # no sensor error at all, yet equal depth reports one
    assert abs(run(0.0, 25)["depth"]) > 0.005


def test_equal_density_is_biased_by_the_sensor_error():
    # a salinity error also shifts the glider's density, so it compares different water
    assert abs(run(0.040, 60)["density"] - 0.040) > 0.01


if __name__ == "__main__":
    print(f"{'hidden':>7} {'lift':>5} {'equal θ':>9} {'equal depth':>12} {'equal density':>14}")
    for offset, lift in SCENARIOS:
        r = run(offset, lift)
        print(f"{offset:+7.3f} {lift:4d}m {r['theta']:+9.4f} {r['depth']:+12.4f} {r['density']:+14.4f}")
