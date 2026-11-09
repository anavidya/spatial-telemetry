# Glider Reference Finder

Find Argo float profiles that can check an ocean glider's sensors, using public data and one PostGIS query.

Pick a glider deployment on a map. Within seconds the app finds the Argo profiles that were close enough to its dives **in place, time and depth** to act as a reference, ranks them, and can compare the glider's salinity with a reference profile. Nothing is downloaded to disk: data is read from public ERDDAP servers into memory, and only small summaries are kept in PostGIS.

> A personal project built to learn PostGIS. It is not an official BODC or NOC product, and its results are a first look, not a calibration.

## What it does

1. **Lists glider deployments** from several public ERDDAP servers (US IOOS Glider DAC, DFO Canada, BODC/NOC via linkedsystems.uk) on a dark Leaflet map.
2. **Finds reference profiles** for the glider you click: Argo profiles within **50 km** and **10 days** of one of its dives that share at least **100 m of water below 150 m**. The best 5 are ranked by `distance / 50 km + time gap / 10 days`.
3. **Checks the sensors**: fetches the glider dive and the Argo profile into memory and reports the glider's salinity offset (glider minus Argo) at equal potential temperature, with a plain verdict: *agrees*, *watch* or *possible drift*.

Shelf gliders that never reach the comparison layer get a clear "no deep overlap possible" instead of an empty list.

## Quick start

You need **Docker** (or **Podman**) with Compose, and an internet connection. Everything else runs in containers.

```bash
git clone <this repo>
cd spatial_telemetry
cp .env.example .env          # then set POSTGRES_PASSWORD in .env
docker compose up -d --build  # or: podman compose up -d --build
```

Open **http://localhost:8080**.

The first start takes a minute: the backend loads each server's list of glider deployments into PostGIS (and refreshes it every hour). Watch it with `docker compose logs -f backend`; look for `glider catalogue: N deployments active since …`.

| Service | Container | Port |
| --- | --- | --- |
| PostGIS 16 + PostGIS 3.4 | `postgis_dev` | 5433 |
| FastAPI backend | `ocean_fastapi_backend` | 8000 |
| Frontend (nginx) | `ocean_nginx_frontend` | 8080 |

## Using the app

1. **Find a glider.** Untick *Only those in map view* to see every deployment, or type a name in the filter box. Boxes are coloured by depth: deep enough, too shallow, or not checked yet.
2. **Click a deployment** (on the map or in the list). The app fetches its dives, searches Argo nearby, and draws the track, the nearby Argo profiles (grey) and the ranked references (numbered).
3. **Click "Check sensors"** on a reference. The panel gives a one-line answer, the verdict, and two plots: potential temperature against salinity, and salinity against pressure with the compared layer shaded. *Show the numbers* opens the full table.

Opening a deployment again is fast: dives and Argo searches are cached.

## How it works

```
ERDDAP servers ──(read in memory)──► FastAPI backend ──(small summaries)──► PostGIS
  gliders: IOOS, DFO, linkedsystems.uk          │                              ├─ glider_deployments  (catalogue)
  Argo: Ifremer ERDDAP via argopy               │                              ├─ profiles            (one row per dive / Argo cycle)
                                                ▼                              └─ argo_fetch_windows  (Argo fetch cache)
                                         Leaflet map (browser)
```

- **One row per profile.** A glider dive or one Argo cycle is a point, a time and a depth range (`numrange`). "Which depths did both cover?" is a range intersection.
- **One SQL query does the matching**: `ST_DWithin` on `geography` (metres, not degrees) for distance, a time window, `depth_range * depth_range * numrange(150, NULL)` for the shared deep layer, and `DISTINCT ON` to keep the best dive per Argo profile. See `backend/helpers/calibration.py` (`REFERENCE_SQL`).
- **The sensor check compares salinity at equal potential temperature**, not equal depth: eddies and internal waves move water up and down, and a drifting conductivity sensor changes salinity but not temperature. This is the principle Argo's own delayed-mode salinity checks use. Potential temperature comes from the TEOS-10 library [`gsw`](https://teos-10.github.io/GSW-Python/).
- **The database stays small**: the glider catalogue is refreshed hourly, Argo fetches are reused for 7 days, unused rows are deleted after 30 days, and shallow gliders keep a thinned track only.

Design notes and the reasons behind each choice are in [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md).

## Configuration

All settings live in `.env` (copy `.env.example`). The ones you are most likely to change:

| Setting | Default | What it does |
| --- | --- | --- |
| `GLIDER_ERDDAP_URLS` | IOOS, DFO, linkedsystems.uk | Comma-separated ERDDAP servers to list gliders from |
| `FETCH_LOOKBACK_DAYS` | 30 | Only deployments with data in this many recent days are listed (e.g. 730 for two years) |
| `REF_MAX_DIST_M` | 50000 | Largest glider–Argo distance, in metres |
| `REF_MAX_TIME_DAYS` | 10 | Largest time gap, in days |
| `REF_LAYER_TOP_M` | 150 | Compare only below this depth (surface water changes too fast) |
| `REF_MIN_OVERLAP_M` | 100 | Least shared depth range below that layer |
| `REF_TOP_N` | 5 | References kept per deployment |
| `CMP_SAL_OK` / `CMP_SAL_WARN` | 0.01 / 0.03 | Salinity offsets for *agrees* and *watch*; above that, *possible drift* |
| `PROFILES_RETENTION_DAYS` | 30 | Delete cached rows not used for this many days |

After changing `.env`, run `docker compose up -d` again.

**Adding a glider server:** append its ERDDAP base URL to `GLIDER_ERDDAP_URLS`. The app keeps datasets whose `cdm_data_type` is `TrajectoryProfile` (IOOS layout, dives numbered), or `Trajectory` with a glider named in the title (glider, slocum, seaglider, SeaExplorer, Spray). It reads `profile_id`, or `PRES`/`depth`, `TEMP`/`temperature`, `PSAL`/`salinity`, `latitude`, `longitude`, `time`.

## API

The frontend uses these endpoints (served on port 8000; interactive docs at `http://localhost:8000/docs`):

| Endpoint | Returns |
| --- | --- |
| `GET /api/deployments` | Glider deployments as GeoJSON |
| `GET /api/deployments/{dataset_id}/references?param=salinity` | The glider's dives and its ranked Argo references |
| `GET /api/compare?glider=…&glider_profile=…&argo_profile=<wmo>_<cycle>` | The sensor check for one pair (open the references first) |
| `GET /api/config` | The current rules and catalogue status |

## Tests

```bash
poetry install
cd backend
poetry run pytest tests/ -v                       # checks
poetry run python tests/test_synthetic_compare.py  # prints the comparison table
```

`test_synthetic_compare.py` builds a made-up ocean, a perfect Argo profile of it and a glider profile with a salinity error hidden on purpose and the water pushed up, then checks that the comparison finds the hidden error. It needs no database or network.

## Project layout

```
backend/
  main.py                FastAPI app: schema, catalogue refresh and retention loops
  routes/                API endpoints
  helpers/
    catalogue.py         glider deployment catalogue (ERDDAP allDatasets -> PostGIS)
    calibration.py       glider dives, Argo search, the matching query
    compare.py           sensor check (TEOS-10, equal potential temperature)
    retention.py         hourly clean-up
    schema.sql           tables and indexes
  tests/
frontend/                index.html, scripts/, styles/  (area-search.html: the earlier box search)
nginx/                   serves the frontend and /config.js (API_BASE_URL)
docs/IMPLEMENTATION.md   what was built and why
```

## Limits

- 50 km and 10 days is not "the same water" near fronts and eddies; a wide spread in the offsets is the warning sign.
- Real-time data on either side has no or little quality control; delayed-mode (D) Argo profiles are the most trustworthy references.
- Oxygen is listed but not compared yet: sources report it in different units and forms.
- Public servers are sometimes slow or down (an expired certificate, a timeout); the app retries and says so in the log.

## Data sources and credits

- **Gliders:** [US IOOS Glider DAC](https://gliders.ioos.us/), [DFO Canada CNODC ERDDAP](https://cnodc-cndoc.azure.cloud-nuage.dfo-mpo.gc.ca/erddap/), and UK gliders from the [BODC glider data assembly centre](https://platforms.bodc.ac.uk/) via [linkedsystems.uk](https://linkedsystems.uk/erddap/) (Open Government Licence; please acknowledge the data originators).
- **Argo:** read with [argopy](https://argopy.readthedocs.io/) from Ifremer's ERDDAP. These data were collected and made freely available by the International Argo Program and the national programs that contribute to it ([argo.ucsd.edu](https://argo.ucsd.edu), [ocean-ops.org](https://www.ocean-ops.org)). The Argo Program is part of the Global Ocean Observing System. Argo (2000). Argo float data and metadata from Global Data Assembly Centre (Argo GDAC). SEANOE. https://doi.org/10.17882/42182
- **Seawater properties:** [TEOS-10 GSW](https://www.teos-10.org/).
- **Method:** Wong, Johnson & Owens (2003), delayed-mode calibration of float salinity by θ–S climatology; the [OWC toolbox](https://github.com/ArgoDMQC/matlab_owc).

## Contributing

Issues and pull requests are welcome, especially new glider servers, better variable-name handling for other formats, and oxygen comparison.

## License

[MIT](LICENSE): free to use, change and share, keeping the copyright notice. The licence covers this code only; the data it reads keeps its own terms (see *Data sources and credits*).
