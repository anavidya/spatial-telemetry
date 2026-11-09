# spatial-telemetry

A local spatial data playground built on PostGIS, with a Python startup script that reuses an existing Postgres instance or spins one up via Docker Compose.

## Project Structure

```
spatial_telemetry/
├── docker-compose.yml   # PostGIS container definition
├── pyproject.toml       # Poetry dependencies
├── startup.py           # Detect Postgres or start container, enable PostGIS
└── CLAUDE.md            # This file
```

## Tech Stack

| Layer | Choice |
| --- | --- |
| Database | PostgreSQL 16 + PostGIS 3.4 (`postgis/postgis:16-3.4-alpine`) |
| Container | Podman Compose, service `postgis_dev` |
| Python | 3.11+, managed via Poetry |
| DB driver | `psycopg2-binary` |

## Database

- Host: `localhost:5432`
- Database: `spatial_db`
- User: `postgres`
- Password: `postgrespassword`
- PostGIS extension enabled on startup

## Requirements

**Functional**
- Detect whether Postgres is already running on port 5432
- If running: connect and enable PostGIS extension on `spatial_db`
- If not running: start `postgis_dev` container, wait until ready, then enable extension
- Idempotent: safe to run multiple times

**Non-functional**
- Python 3.11+
- Credentials stored only in `docker-compose.yml`

## Setup

1. Install dependencies: `poetry install`
2. Ensure Podman is running (`podman machine start` if using a VM)
3. Run: `poetry run python startup.py`
4. Verify: `psql -h localhost -U postgres -d spatial_db -c "SELECT PostGIS_Version();"`

## Status

| Item | Status |
| --- | --- |
| `docker-compose.yml` | Done |
| `pyproject.toml` | Done |
| `startup.py` | Done |
| Load spatial data into `spatial_db` | Next |
| Write first PostGIS queries | Planned |
| Add telemetry/analysis scripts | Planned |
