# Home Energy

Track FusionSolar electricity usage and Belgian day-ahead prices from ENTSO-E. Data is stored in PostgreSQL; charts and daily reports are available through the CLI and a small API used by n8n.

## Setup

Requires Python 3.10+, uv, and Docker.

Copy `.env.example` to `.env` if you don't already have one. Set the PostgreSQL settings, `ENTSOE_API_KEY` for prices, and FusionSolar credentials for usage.

```powershell
uv sync --locked
docker compose up -d --build --wait
```

The API runs on port 8000 and initializes the database at startup. Browse [API docs](http://localhost:8000/docs) for requests and responses. Database data survives container restarts and rebuilds.

## CLI

```powershell
uv run home-energy fill usage 2026-10-10
uv run home-energy fill prices 2026-10-10
uv run home-energy fill prices 2026-10-08 --end-date 2026-10-10
uv run home-energy chart prices 2026-10-10 --output outputs/prices.png
```

Dates use Europe/Brussels. Usage accepts past days; prices also accept today and tomorrow, once published. Repeated fills skip stored days. Charts read stored data without fetching.

To work locally without the API container, start only the database with `docker compose up -d --wait db`, then run `uv run home-energy init-db` once.

## Reports for n8n

Send `POST /api/v1/reports/day-ahead` with:

```json
{"date": "2026-10-12", "threshold_eur_per_mwh": "300"}
```

The response includes a base64 PNG in `image`, a `success` or `warning` status, and warning text. Unpublished prices return a retryable error. n8n handles scheduling and notifications. The API has no authentication and is intended for a trusted LAN.

## Development

For the first TrueNAS deployment with Portainer, an existing PostgreSQL server, and n8n, follow the [deployment guide](docs/deployment.md). Build a release with `powershell -ExecutionPolicy Bypass -File deploy/build-release.ps1`, verify it, then publish to GHCR with `powershell -ExecutionPolicy Bypass -File deploy/publish-image.ps1`. Portainer uses `ghcr.io/daans8/home-energy:0.1.0`; the importable n8n workflow is in `deploy/n8n-day-ahead.json`.

See the [usage](examples/daily_consumption.py) and [price](examples/day_ahead_prices.py) examples, or the [chart style guide](docs/chart-style-guide.md).

```powershell
uv run --locked python -m unittest discover -s tests
```

Set `HOMEENERGY_TEST_DATABASE=1` to include PostgreSQL integration tests against the running database.
