# Home Energy

FusionSolar consumption, estimated EV charging and Belgian day-ahead prices from ENTSO-E. PostgreSQL stores source snapshots; FastAPI serves daily results to n8n. Dates use **Europe/Brussels**, power uses **kW**, energy uses **kWh** and prices use **EUR/MWh**.

## Setup

Requires Python 3.10+, uv and Docker. Copy `.env.example` to `.env`; configure PostgreSQL, `ENTSOE_API_KEY` and FusionSolar credentials.

```powershell
uv sync --locked
docker compose up -d --build --wait
```

The API initializes its database schema at startup. Open [API documentation](http://localhost:8000/docs); `/health/live` checks the process and `/health/ready` checks PostgreSQL. The API is intended for a trusted LAN and has no authentication.

For local Python development, start `docker compose up -d --wait db`, then run `uv run home-energy init-db`.

## Daily APIs

| POST endpoint | JSON request | Result |
| --- | --- | --- |
| `/api/v1/usage/daily` | `{"date":"2026-10-10"}` | Consumption intervals, EV blocks, estimated EV kWh and FusionSolar's total daily kWh |
| `/api/v1/reports/day-ahead` | `{"date":"2026-10-12","threshold_eur_per_mwh":"300"}` | Price analysis, `success`/`warning`, warning text and a base64 PNG |

Usage accepts today and past days; `{}` selects today. EV detection requires **two continuous hours above 2.169 kW**, using two hours of boundary context on each side. The EV power is the equal-weight mean of accepted clean-stop drops, rounded to 0.001 kW; see [how we estimate it](docs/ev-estimation.md). No baseload is subtracted. Request yesterday after **02:00 Brussels time** and check `status: "complete"`; today and incomplete context return `provisional`. Past source snapshots are reused; today's evolving snapshot is held in memory.

Usage returns Decimal powers/energies as strings and timestamps with Brussels offsets. Missing consumption and pending EV qualification are null. The daily total comes from FusionSolar's `totalUsePower`; it is separate from integrating the sampled curve. See [EV estimation](docs/ev-estimation.md) for calibration evidence.

Price dates extend through tomorrow once published. Actual prices above the threshold produce a warning; both report statuses return HTTP 200. Errors use `{status,error,code,retryable}`: 422 invalid input, 502 provider/data failure, 503 unavailable prices/database, or 500 configuration/processing failure. n8n controls scheduling and retries.

## CLI and investigation

```powershell
uv run home-energy fill usage 2026-10-10
uv run home-energy fill prices 2026-10-08 --end-date 2026-10-10
uv run home-energy chart prices 2026-10-10 --output outputs/prices.png
uv run --locked home-energy investigate ev 2026-09-01 --end-date 2026-10-10 --fetch --output outputs/ev-investigation-2026-09-01_2026-10-10
```

Usage fills accept past days. Repeated fills skip stored dates; price charts read stored data. The EV investigation exports real-data graphs, CSV tables, a PDF and an HTML index. Omit `--fetch` to reuse stored data and cached boundary context.

## Structure

`usage/` contains provider access, storage, daily ingestion and the shared EV detector. `investigations/ev/` contains calibration experiments and graph exports. `prices/` handles market data and price reports; `api/` defines the HTTP contract. See the [chart style guide](docs/chart-style-guide.md) and [Python examples](examples/).

## Portainer and n8n

Use [deploy/compose.portainer.yaml](deploy/compose.portainer.yaml) with an existing PostgreSQL database named `home_energy`. Set `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `ENTSOE_API_KEY`, `FUSIONSOLAR_USERNAME` and `FUSIONSOLAR_PASSWORD` in Portainer. Set `PGHOST` to the database host reachable from the app container. The app creates its own schema; PostgreSQL holds the persistent data.

The stack uses `ghcr.io/daans8/home-energy:0.1.0` and defaults to `192.168.0.10:18000`. For a release containing current source changes, build and publish a new version, then update the stack image and redeploy:

```powershell
powershell -ExecutionPolicy Bypass -File deploy/build-release.ps1 -Version 0.1.1
uv run --locked python deploy/smoke_test.py --image home-energy:0.1.1 --live-provider
powershell -ExecutionPolicy Bypass -File deploy/publish-image.ps1 -Version 0.1.1
```

The build requires Docker; publishing requires a GHCR login. Release packaging includes this README, both guides and their figures, with checksums.

Import [deploy/n8n-day-ahead.json](deploy/n8n-day-ahead.json), set its `base_url` to the app's reachable address and select the Discord credentials. It runs at 19:00 Brussels time for tomorrow, retries retryable failures every ten minutes up to six attempts, and sends the report PNG and warnings. Manual execution sends messages to its configured channels. Verify `/health/ready` and a fixed past-date report before activating the workflow.

## Verification

```powershell
uv run --locked python -m unittest discover -s tests
```

Set `HOMEENERGY_TEST_DATABASE=1` to include PostgreSQL integration tests in disposable schemas. Deployment checks are available in [deploy/smoke_test.py](deploy/smoke_test.py).
