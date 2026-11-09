# spatial_telemetry: implementation notes

What has been built, how it works, and **why** each choice was made. Kept in the repo as
`docs/IMPLEMENTATION.md` and in the Claude Project as `claude/implementation-notes.md`.
Last updated 2026-11-01.

Related docs (Claude Project): `claude/blog-roadmap.md` (decisions, claim tracker, blog plan),
`claude/postgis-reference.md` (every PostGIS/SQL feature used), `claude/data-flow.md` (original design).

---

## 1. What the app does

**Glider Reference Finder.** For any glider deployment, find the trusted Argo profiles taken
close enough in place, time and depth to check the glider's sensors, then check them.

1. Pick a glider deployment on the map (US IOOS, DFO Canada, linkedsystems.uk servers).
2. The app fetches its dive positions and depth ranges, fetches Argo profiles around its track,
   and runs one SQL query to find the 5 best reference profiles.
3. "Check sensors" on a reference fetches both full profiles and reports the glider's salinity
   offset (glider − Argo) at equal potential temperature, with a verdict and a θ–S plot.

**Why this use case.** The first idea (find any glider/float "encounters" in a map box) mixed
shelf gliders (0–100 m) with Argo floats (0–2000 m): apples and mangoes. Glider conductivity
and oxygen sensors drift; Argo profiles, especially delayed-mode, are the most available
independent reference. Finding references is usually done by hand per deployment; this does
it with a stated, repeatable rule. Users: glider data managers/QC, scientists deciding whether
to trust a glider's deep salinity, and coverage (which deployments can't be checked against Argo).

**Constraint from the start:** never save NetCDF or raw measurements. Everything is read into
memory; only small summaries are kept in PostGIS.

---

## 2. Architecture

```
Browser (Leaflet, dark map)  ──►  nginx :8080  (static frontend + /config.js from .env)
        │
        └──── fetch ────►  FastAPI :8000  (backend/main.py, routes/, helpers/)
                                │
                ┌───────────────┼──────────────────────────────┐
                ▼               ▼                              ▼
   Glider ERDDAP servers   Argo (argopy → Ifremer ERDDAP)   PostGIS (postgis_dev :5433)
   IOOS DAC, DFO Canada,   core + BGC, in memory            glider_deployments, profiles,
   linkedsystems.uk                                         argo_fetch_windows
```

Runs with Podman Compose: `postgis` (PostGIS 16-3.4), `backend` (Python 3.11, Poetry, uvicorn
`--reload`, `./backend` mounted), `frontend` (nginx, `./frontend` mounted). All settings in `.env`.

### Backend layout
| Path | Purpose |
|---|---|
| `backend/main.py` | Creates the app; on start-up applies the schema, starts the catalogue refresh threads and the retention thread; registers routers |
| `routes/deployments.py` | `GET /api/deployments`, `GET /api/deployments/{id}/references` |
| `routes/compare.py` | `GET /api/compare` (sensor check) |
| `routes/config.py` | `GET /api/config` (rules, sources, catalogue and retention status, for the legend) |
| `routes/overlaps.py`, `platforms.py`, `cache.py` | Original box search (legacy) |
| `helpers/schema.sql`, `schema.py` | Tables and indexes, applied idempotently at start-up |
| `helpers/catalogue.py` | Glider deployment catalogue in PostGIS, refreshed per server |
| `helpers/calibration.py` | Glider dives, Argo profiles, storage, reference SQL, Argo fetch cache, shallow-track thinning |
| `helpers/compare.py` | Full-profile fetch, TEOS-10, θ-level salinity offset, verdict |
| `helpers/retention.py` | Hourly clean-up of old `profiles` rows |
| `helpers/erddap.py` | ERDDAP CSV helpers |
| `helpers/settings.py` | Every `.env` value, read once |
| `helpers/db.py` | `db_cursor()` context manager, `fetch_dicts()` |
| `helpers/ingest_argo.py`, `ingest_glider.py`, `etl.py`, `tracks.py`, `cache.py`, `encounters.py` | Original box search (legacy) |

### Frontend layout
| Path | Purpose |
|---|---|
| `frontend/index.html`, `scripts/app.js` | Glider Reference Finder |
| `frontend/scripts/compare.js` | Sensor-check panel and SVG charts |
| `frontend/styles/main.css` | All styles (dark theme) |
| `frontend/area-search.html`, `scripts/area-search.js` | Original box search (legacy) |
| `nginx/default.conf.template` | Serves `/config.js` from `API_BASE_URL`; no-cache headers, `sendfile off` |

---

## 3. Data model (PostGIS)

| Table | Rows | Filled by | Why it exists |
|---|---|---|---|
| `glider_deployments` | One per glider deployment on any server | Catalogue refresh (hourly); `max_depth_m` + `data_extent` when opened | Searching the Glider DAC live takes ~1 min; querying our own table takes ms |
| `profiles` | One per vertical profile (glider dive / pseudo-profile / Argo cycle) | Opening a deployment (glider rows), Argo fetch (Argo rows) | Calibration compares profiles, so the unit is a profile, not a track point |
| `argo_fetch_windows` | One per Argo fetch (area + period) | Successful Argo fetch | Skip re-fetching the same area/dates for 7 days |
| `remote_ocean_fleet_index`, `spatial_cache_registry` | — | Original box search | Legacy; to be retired |

Key types: `geometry(Point/Polygon, 4326)`, `tstzrange` (deployment active span, fetch period),
`numrange` (depth range), `jsonb` (sensors). Key indexes: GiST on extents and ranges, GiST on the
expression `(geom::geography)`, btree `(platform_type, t)`, GIN on `sensors`.
Full details: `claude/postgis-reference.md`.

---

## 4. Features: what and why

### 4.1 Glider deployment catalogue (`helpers/catalogue.py`)
- **What:** one background thread per server loads ERDDAP `allDatasets` (deployments with data in
  the last ~32 days) into `glider_deployments` now and every `GLIDER_CATALOGUE_TTL_MIN` minutes.
  Keeps gliders only (`TrajectoryProfile`, or `Trajectory` with "glider/planeur" in the title) and
  drops impossible extents.
- **Why:** the IOOS Glider DAC took about a minute to answer searches (both `allDatasets` with
  constraints and `search/advanced`), causing timeouts. A local, indexed catalogue makes every
  search instant and isolates one slow or failing server from the others.

### 4.2 Several glider servers, two data layouts
- **IOOS Glider DAC** (`gliders.ioos.us`): dive-numbered (`profile_id`). Dive positions/times via
  `profile_id,time,latitude,longitude&distinct()` (ERDDAP's cached subset table, no measurement
  scan); depth range per dive via `orderByMinMax("profile_id,depth")` when ≤ 3000 dives.
- **DFO Canada CNODC** (EGO-style trajectory, deep Seagliders ~650 m): no dive numbers. ERDDAP
  thins to one row per 5 minutes (`orderByClosest("time/5minutes")`), grouped into 6-hour
  pseudo-profiles. Junk positions (> 10° from the median) dropped.
- **linkedsystems.uk**: added at the user's request; unverified from the dev environment.
- **Why:** October's US Northeast IOOS gliders were all shelf gliders (≲ 100 m), so no references
  were possible. DFO's deep Seagliders make the use case work. Supporting the EGO layout also
  means European EGO-style ERDDAPs can be added by URL.
- **Checked and rejected:** NCEI ERDDAP (no gliders), Ifremer `OceanGlidersGDACTrajectories`
  (ends Dec 2024), BODC ERDDAP (CTD cruises, no gliders; BODC gliders are in its Deployment
  Catalogue as NRT file downloads, no API found yet).

### 4.3 Unreliable extents
- **What:** deployments whose area spans > `GLIDER_MAX_EXTENT_DEG` (20°) are listed with an
  "area unreliable" tag but not drawn; once opened, the area measured from the cleaned dives
  (`data_extent`) replaces it and survives refreshes. Big boxes drawn first so small ones stay clickable.
- **Why:** a real DFO dataset reported longitude +15.6° in a NW Atlantic mission; its box covered
  the whole map and swallowed clicks.

### 4.4 Reference matching (`REFERENCE_SQL` in `helpers/calibration.py`)
- **Rule** (`REF_*`): within 50 km and 10 days of a glider dive; depth ranges sharing ≥ 100 m below
  150 m; best 5, ranked by distance/50 km + time/10 days; best glider dive per Argo profile.
- **SQL:** `ST_DWithin(geography)` for metres on the ellipsoid; time window with `make_interval`;
  shared layer = `g.depth_range * a.depth_range * numrange(150, NULL)`; `DISTINCT ON`; window `count(*)`.
- **Why these numbers:** 50 km / 10 days is a common matchup scale where the deep ocean changes
  slowly. The layer starts below 150 m because surface water changes within hours. Originally
  200 m overlap below 200 m (glider must reach 400 m); loosened to 100 below 150 (reach 250 m)
  after the first live run found mostly shallow gliders. Trade-off: a less stable layer.
- **Shallow shortcut:** if metadata shows the glider never reaches 250 m, it answers
  "no deep overlap possible" without the slow per-dive request or any Argo fetch.

### 4.5 Argo fetching and its cache
- **What:** argopy `src='erddap'` (Ifremer, Coriolis GDAC data), core + BGC, over the glider
  track's box grown by 50 km and dates ± 10 days; one `profiles` row per profile with pressure
  range, sensors (DOXY → oxygen, NITRATE → nitrate) and data mode. A fetch is skipped when a
  fresh `argo_fetch_windows` row covers the area (`ST_Covers`) and dates (`@>`); refreshed after
  `ARGO_FETCH_TTL_DAYS` (7). Failed fetches are not recorded, so the next open retries.
- **Why `erddap` not `gdac`:** argopy's gdac source has no BGC (no oxygen/nitrate) and ERDDAP
  filters box/depth/time on the server.
- **Why cache:** reopening a glider re-downloaded the same Argo data every time; old profiles
  rarely change except R → A → D upgrades, hence a 7-day refresh.

### 4.6 Sensor check (`helpers/compare.py`, `frontend/scripts/compare.js`)
- **What:** fetches the full glider dive (DAC: `profile_id=`; trajectory layout: the 6-hour
  window) and the Argo profile (argopy `.profile()`, adjusted values), computes TEOS-10
  potential temperature (`gsw`), then the **salinity offset at equal potential temperature**
  (0.05 °C levels) in the shared layer: median, middle 50%, number of levels. Cross-checks at
  equal depth. Verdict (`CMP_*`): agrees ≤ 0.01, watch ≤ 0.03, else possible drift; < 5 levels → none.
- **Why θ levels:** in a stable deep layer the θ–S relation is conserved when eddies or internal
  waves lift the water, and a conductivity error changes S but not θ; this is the Argo
  delayed-mode approach. Synthetic test: true offsets recovered within 0.0005 (e.g. +0.015 with
  25 m heave → +0.0149) while equal-depth comparison gave +0.0035. A first version on density
  (σ0) surfaces gave +0.023 for +0.015, because the salinity error shifts the computed density.
- **Plot colours:** glider `#2f98d8`, Argo `#e8484a` (dashed), checked with a colour-blindness
  and contrast validator against the dark panel.

### 4.7 Keeping `profiles` small
- **Retention** (`helpers/retention.py`, hourly): glider rows deleted when that glider hasn't been
  reopened for `PROFILES_RETENTION_DAYS` (30); Argo rows older than that deleted unless a fresh
  fetch window still relies on them.
- **Shallow gliders thinned:** gliders that can't reach the comparison layer keep one track point
  per `GLIDER_SHALLOW_TRACK_HOURS` (3 h) instead of every dive (e.g. 2,500 dives → 161 points).
- **Why:** 18k rows after a few sessions, mostly shelf Slocum dives (one every 10–20 min) that
  can never have references; nothing was ever deleted. Not a performance problem yet, but
  unbounded growth.

### 4.8 Frontend
- Dark map (OpenStreetMap tiles inverted with CSS; CARTO dark tiles needed an API key).
- Left: sensor, deployment list (filter, "only in map view", server tag, depth tag), results.
  Right: layer toggles (remembered in the browser), recent gliders (scroll list), legend with
  the live rule and sources. URL holds `?glider=&param=` so a check is repeatable and shareable.
- **Why:** map first; nothing floating over it; the earlier "remember the box" problem solved by
  starting from a deployment instead of a drawn box.

---

## 5. Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `REF_MAX_DIST_M`, `REF_MAX_TIME_DAYS` | 50000, 10 | Reference distance and time limits |
| `REF_LAYER_TOP_M`, `REF_MIN_OVERLAP_M` | 150, 100 | Compare below this depth, with at least this overlap |
| `REF_TOP_N` | 5 | References kept per deployment |
| `CMP_SAL_OK`, `CMP_SAL_WARN`, `CMP_MIN_LEVELS` | 0.01, 0.03, 5 | Sensor-check verdict thresholds |
| `GLIDER_ERDDAP_URLS` | IOOS, DFO, linkedsystems | Servers in the catalogue |
| `GLIDER_CATALOGUE_TTL_MIN` | 60 | Catalogue refresh interval |
| `GLIDER_SEARCH_TIMEOUT_S`, `GLIDER_DATASET_TIMEOUT_S` | 120, 180 | ERDDAP request time limits |
| `GLIDER_PER_DIVE_MAX_PROFILES` | 3000 | Above this, depth ranges from metadata |
| `GLIDER_TRAJ_SAMPLE`, `GLIDER_TRAJ_BIN_HOURS` | 5minutes, 6 | Trajectory-layout sampling and pseudo-profile length |
| `GLIDER_MAX_EXTENT_DEG` | 20 | Wider areas listed but not drawn |
| `GLIDER_SHALLOW_TRACK_HOURS` | 3 | Track thinning for gliders that can't match |
| `ARGO_SOURCE`, `ARGO_FETCH_TTL_DAYS` | erddap, 7 | Argo source and fetch-cache lifetime |
| `CACHE_TTL_HOURS` | 12 | Reuse a glider's dives for this long |
| `FETCH_LOOKBACK_DAYS` | 30 | Deployments active within this many days |
| `PROFILES_RETENTION_DAYS` | 30 | Delete unused profile rows after this |
| `MAX_FETCH_AREA_DEG2` | 100 | Largest Argo search box |
| `API_BASE_URL`, ports, `POSTGRES_*` | — | Infrastructure |
| `MATCH_*`, `GLIDER_ERDDAP_URL`, `GLIDER_MAX_DATASETS`, `GLIDER_PARALLEL`, `MOCK_GLIDER` | — | Original box search |

---

## 6. Timeline of changes (and dead ends)

| When | Change | Reason |
|---|---|---|
| 30 Oct | CSS moved to `styles/`; CDN links and tiles fixed (OSM after CARTO needed a key) | Clean-up; page didn't load |
| 30 Oct | `.env` for every setting; compose fixed (indentation, DB host/port/password, init path) | No hard-coding; stack didn't start |
| 30 Oct | Poetry in the image; Dockerfile at repo root (podman ignored `dockerfile:`) | Use the project's own dependency file |
| 31 Oct | 4D encounter query (point-level, distance + depth + time) | `ST_ClosestPoint` is 2D, so the old depth check was always NULL |
| 31 Oct | Real Argo via argopy; real gliders via Glider DAC with `orderByMinMax` | Replace mock data |
| 31 Oct | Backend split into `routes/` and `helpers/` | Maintainability |
| 31 Oct | Glider catalogue cached; parallel fetches; timeouts | Glider DAC searches took ~1 min |
| 31 Oct | **Reframed to calibration** (Glider Reference Finder); `profiles` + catalogue tables | Shelf gliders vs Argo was the wrong question |
| 31 Oct | Looser depth rule; DFO Canada server (EGO layout) | October's IOOS gliders were all shallow |
| 31 Oct | Unreliable-extent handling | A junk +15.6° longitude produced a map-covering box |
| 31 Oct | Argo fetch cache (`argo_fetch_windows`) | Same Argo data re-downloaded on every open |
| 31 Oct | Sensor check on θ levels | Turn "found candidates" into "can I trust this glider?" |
| 1 Nov | Retention + shallow-track thinning | `profiles` reached 18k rows and never shrank |

First live result: Argo 6990687 cycle 51 (adjusted), 20.9 km and 4.7 days from a glider dive,
shared layer 150–958 m.

---

## 7. Known limits and next steps

- 50 km / 10 days is not "same water" near fronts and eddies; the IQR spread warns about it.
- Only deployments active in the last 30 days; delayed-mode QC of recovered gliders needs older
  data (date selection).
- Real-time data (glider and Argo R/A) is less certain than delayed-mode; ranking doesn't yet
  prefer delayed-mode Argo.
- Oxygen isn't compared yet (units and sensor variables differ between sources).
- A fetch window must cover the needed area on its own (no union of windows).
- Next: rank delayed-mode first; per-deployment report (all references, overall verdict, export);
  older deployments; EXPLAIN ANALYZE evidence; retire the box search and its tables.

---

## 8. Running and checking

```
podman compose build backend          # needed after pyproject/poetry.lock changes (e.g. gsw)
podman compose up -d --force-recreate backend
podman logs -f ocean_fastapi_backend  # "glider catalogue: N deployments ..." per server
```
Open http://localhost:8080 (Ctrl+F5 after updates). psql: `psql -h localhost -p 5433 -U postgres -d spatial_db`.
