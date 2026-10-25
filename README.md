# Home Energy

FusionSolar consumption, estimated EV charging and Belgian day-ahead prices from ENTSO-E. PostgreSQL stores source snapshots; FastAPI serves daily results to n8n. Dates use **Europe/Brussels**, power uses **kW**, energy uses **kWh** and prices use **EUR/MWh**.

## Setup

Requires Python 3.10+, uv and Docker. Copy `.env.example` to `.env`; configure PostgreSQL, `ENTSOE_API_KEY` and FusionSolar credentials.

Plant identity, timezone, portal hosts and request defaults are shared in [usage/constants.py](src/home_energy/usage/constants.py). Update those constants for another installation; credentials alone do not select a different plant. Requests use the selected day's local midnight and a fresh timestamp. The captured request lives in the test fixtures. Huawei's winter interpretation of `timeZone=2.0` and `X-Timezone-Offset=120` is still unverified.

```powershell
uv sync --locked
docker compose up -d --build --wait
```

The API initializes its database schema at startup. Open [API documentation](http://localhost:8000/docs); `/health/live` checks the process and `/health/ready` checks PostgreSQL. The API is intended for a trusted LAN and has no authentication.

For local Python development, start `docker compose up -d --wait db`, then run `uv run home-energy init-db`.

## Daily APIs

| POST endpoint | JSON request | Result |
| --- | --- | --- |
| `/api/v1/reports/ev/daily` | `{"date":"2026-10-10"}` or `{}` | Consumption readings, EV blocks, totals and a base64 PNG with EV blocks overlaid |
| `/api/v1/reports/day-ahead` | `{"date":"2026-10-12","threshold_eur_per_mwh":"300","hysteresis_eur_per_mwh":"25"}` | Price analysis, `success`/`warning`, warning text and a base64 PNG |

Usage accepts dates **at least two days ago**; `{}` selects two days ago in Brussels. EV detection requires **two continuous hours above 2.169 kW**, using two hours of boundary context on each side. The EV power is the equal-weight mean of accepted clean-stop drops, rounded to 0.001 kW; see [how we estimate it](docs/ev-estimation.md). No baseload is subtracted. The requested day and both adjacent days must have finished. Missing days are fetched once and stored only when every consumption interval and the daily total are available; stored days are immutable and reused. Successful reports always return `status: "complete"`. Yesterday and today are rejected with 422; incomplete consumption or required context returns 502.

Usage returns Decimal powers/energies as strings and timestamps with Brussels offsets. The daily total comes from FusionSolar's `totalUsePower`; it is separate from integrating the sampled curve. See [EV estimation](docs/ev-estimation.md) for calibration evidence.

Price dates extend through tomorrow once published. The trigger defaults to **300 EUR/MWh** and the nonnegative hysteresis margin to **25 EUR/MWh**. A warning region is a contiguous run strictly above trigger minus margin that contains at least one price strictly above the trigger. With the defaults, a peak above 300 colours its neighbouring intervals above 275 red in both directions; 275 breaks a region, and 300 alone does not trigger one. Nearby peaks join while intervening prices stay above 275. Regions are clipped to the requested day.

The chart shows actual interval prices as steps. Red steps, `high_price_ranges`, and the Dutch warning text describe the same regions. The response echoes `threshold_eur_per_mwh` and `hysteresis_eur_per_mwh` as exact Decimal strings. Both settings are optional; existing n8n requests adopt the defaults automatically. Set `hysteresis_eur_per_mwh` to `"0"` for strict threshold ranges. Both report statuses return HTTP 200. Errors use `{status,error,code,retryable}`: 422 invalid input, 502 provider/data failure, 503 unavailable prices/database, or 500 configuration/processing failure. n8n controls scheduling and retries.

## CLI and investigation

```powershell
uv run home-energy fill usage 2026-10-10
uv run home-energy fill prices 2026-10-08 --end-date 2026-10-10
uv run home-energy chart prices 2026-10-10 --output outputs/prices.png
uv run home-energy chart prices 2026-10-10 --output outputs/prices-strict.png --threshold 300 --hysteresis 0
uv run --locked home-energy investigate ev 2026-09-01 --end-date 2026-10-10 --fetch --output outputs/ev-investigation-2026-09-01_2026-10-10
```

Usage fills accept past days and reject incomplete consumption or missing daily totals before storage. Repeated fills skip stored dates; price charts read stored data. The EV investigation exports real-data graphs, CSV tables, a PDF and an HTML index. Investigation dates must be at least two days ago; both adjacent context days are stored in the database. Omit `--fetch` to reuse stored data.

Price charts and `examples/day_ahead_prices.py` accept `--threshold` (default 300) and `--hysteresis` (default 25), both in EUR/MWh. Zero hysteresis disables range expansion.

## Structure

Both report domains follow the same responsibilities: **ingestion service → repository → analysis → report service → HTTP serialization**. Report services orchestrate this flow: first ingest missing dates, then reload committed snapshots, analyse them, and render a PNG. HTTP routes serialize completed reports; they do not fetch providers or construct charts.

```text
src/home_energy/
├── cli.py                   # CLI commands and service wiring
├── filling.py               # Shared range loop, progress, and failures
├── api/
│   ├── app.py               # Dependency wiring and application lifecycle
│   ├── routes.py            # HTTP serialization
│   ├── schemas.py           # Request and response contracts
│   └── errors.py            # Safe HTTP error translation
├── prices/
│   ├── entsoe.py            # Provider access and normalization
│   ├── ingestion.py         # Missing-day ingestion and date policy
│   ├── repository.py        # SQL persistence and snapshot queries
│   ├── analysis.py          # Pure price analysis
│   ├── report.py            # Analysis, warnings, and rendered report
│   ├── chart.py             # Price figure construction
│   └── models.py            # Price snapshot types
├── usage/
│   ├── fusionsolar.py       # Provider access
│   ├── auth.py              # Provider authentication
│   ├── energy_balance.py   # Provider payload parsing
│   ├── ingestion.py         # Missing-day ingestion and date policy
│   ├── repository.py        # SQL persistence and snapshot queries
│   ├── consumption.py       # Consumption validation and context helpers
│   ├── daily.py             # Pure daily usage analysis
│   ├── ev.py                # Shared EV detection
│   ├── report.py            # Daily EV report and PNG rendering
│   ├── chart.py             # EV figure construction
│   ├── models.py            # Usage snapshot and analysis types
│   └── constants.py         # Fixed plant and provider settings
├── database/
│   ├── connection.py        # PostgreSQL connections and configuration
│   ├── schema.py            # Shared repository connections and schema setup
│   └── schema.sql           # Tables and constraints
├── charts/
│   ├── lines.py             # Shared figures, PNG rendering, and exports
│   └── style.py             # Shared chart styling
└── investigations/ev/
    ├── analysis.py          # Offline EV hypotheses
    ├── calibration.py       # Calibration experiments
    └── report.py            # Investigation figures, tables, PDF, and HTML

tests/                       # Unit, HTTP, and database integration tests
examples/                    # Python ingestion and analysis examples
docs/                        # Chart conventions and EV methodology
deploy/                      # Deployment scripts and configuration
└── n8n/                     # Report scheduling and delivery workflows
```

Package `__init__.py` files are omitted from the map. Use this responsibility breakdown when extending the project:

| Change | Where it belongs |
| --- | --- |
| Provider requests, authentication, or payload normalization | Domain provider adapters: `prices/entsoe.py`, `usage/fusionsolar.py`, `usage/auth.py`, `usage/energy_balance.py` |
| Fetch missing dates, manage client lifetime, validate data before storage | Domain `ingestion.py` services; reuse the root `filling.py` loop |
| SQL writes, transactions, queries, and snapshot reconstruction | Domain `repository.py`; shared connection/schema support in `database/` |
| Price rules, consumption analysis, or EV detection | `prices/analysis.py`, `usage/daily.py`, `usage/consumption.py`, `usage/ev.py`; these do not fetch providers or render images |
| Compose a completed report and render its PNG | Domain `report.py`, using domain `chart.py` and shared `charts/` |
| HTTP fields, encoding, or error responses | `api/schemas.py`, `api/routes.py`, `api/errors.py`; wire services in `api/app.py` |
| Offline calibration and investigation exports | `investigations/ev/`; optional fetching uses usage ingestion |

**Prices:** `DayAheadPriceIngestionService.fill()` ensures a stored price day; the repository reloads it; `analyse_prices()` calculates actual-price warnings; `DayAheadReportService` returns `PriceReport` with PNG bytes; the route encodes those bytes and formats the response.

**Usage:** `UsageIngestionService.fill()` ensures complete snapshots for the requested day and both adjacent days; the repository reloads that range; `analyse_daily_usage()` calculates consumption and EV blocks; `EVUsageReportService` returns `EVUsageReport(usage, png)`; the route serializes the analysis and image.

Ingestion opens one provider client only when a date is missing and closes it on success or failure. Existing dates require no provider configuration. Each completed day commits independently; a failed range can resume without refetching earlier days. Repositories retain defensive `save_day()` validation and atomic, immutable inserts. Reports always analyse committed snapshots, including when another ingestion wins a concurrent insert. A rendering failure leaves stored days available for retries.

For Python callers, use ingestion services instead of repository `fill()` methods:

```python
from datetime import date
from home_energy.usage import EnergyBalanceRepository, UsageIngestionService

repository = EnergyBalanceRepository()
UsageIngestionService(repository).fill(date(2026, 10, 10))
day = repository.load_day(date(2026, 10, 10))
```

The price equivalent is `DayAheadPriceIngestionService(repository).fill(...)`. Both report services accept a repository, an optional `ingestion_service` using that repository, and an injectable `now` clock. `create_app()` accepts `report_service` for prices and `ev_report_service` for completed EV reports. `DailyUsageService`, `DataFetcher`, and the old `usage/filling.py` module have been replaced by these services.

See the [chart style guide](docs/chart-style-guide.md), [EV methodology](docs/ev-estimation.md), and [Python examples](examples/).

## Portainer and n8n

Use [deploy/compose.portainer.yaml](deploy/compose.portainer.yaml) with an existing PostgreSQL database named `home_energy`. Set `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `ENTSOE_API_KEY`, `FUSIONSOLAR_USERNAME` and `FUSIONSOLAR_PASSWORD` in Portainer. Set `PGHOST` to the database host reachable from the app container. The app creates its own schema; PostgreSQL holds the persistent data.

The stack uses `ghcr.io/daans8/home-energy:0.2.0` and defaults to `192.168.0.10:18000`. Build and test the release locally, then publish it and redeploy the Portainer stack:

```powershell
powershell -ExecutionPolicy Bypass -File deploy/build-release.ps1 -Version 0.2.0
uv run --locked python deploy/smoke_test.py --image home-energy:0.2.0
powershell -ExecutionPolicy Bypass -File deploy/publish-image.ps1 -Version 0.2.0
```

The build requires Docker; publishing requires a GHCR login. Release packaging includes this README, both guides and their figures, all n8n workflows, and checksums in `dist/home-energy-0.2.0/`. Add `--live-provider` to the smoke test to also verify a real ENTSO-E request. After publishing, deploy the packaged `compose.portainer.yaml` in Portainer with your existing environment variables and volumes.

Import [deploy/n8n/n8n-day-ahead.json](deploy/n8n/n8n-day-ahead.json), set its `base_url` to the app's reachable address and select the Discord credentials. It runs at 19:00 Brussels time for tomorrow, retries retryable failures every ten minutes up to six attempts, and sends the report PNG and warnings. Manual execution sends messages to its configured channels. Verify `/health/ready` and a fixed past-date report before activating the workflow.

Two additional profiles publish their images and failure messages to the **admin** channel:

| Import file | Daily schedule (Brussels) | Default report date |
| --- | --- | --- |
| [Day-ahead admin](deploy/n8n/n8n-day-ahead-admin.json) | 19:00 | Tomorrow |
| [EV usage](deploy/n8n/n8n-ev-usage.json) | 08:00 | Two days ago |

Configure `base_url` and `guild_id` in **Settings**, and select the Discord bot credentials. The day-ahead admin profile also exposes `admin_channel_id` in Settings; the EV workflow configures its destination directly in **Send image to admin** and **Send admin alert**.

For EV, leave `test_date` blank and click **Run manually** to publish one report per day from `start_date` (default `2026-09-01`) through two days ago in Brussels, inclusive and in date order. Each report must be delivered before the loop advances. The daily schedule publishes only two days ago. To test a single date, set `test_date` to `YYYY-MM-DD`; EV dates must be at least two days ago. Clear it before enabling the schedule. Re-running a backfill republishes the selected dates; stored source data is reused. Change `start_date` to resume after the last delivered day if a run stops.

Dates stay fixed across retries. Both imports are inactive initially, retry retryable API failures every ten minutes up to six attempts per date, and send no messages to the digest or warning channels. Exhausted report retries send an admin alert and stop the run.

The EV image shows the measured five-minute consumption curve in blue and calibrated EV charging blocks in mint. It is published even when no EV charging is detected. The report reuses immutable source snapshots, so generating another report for an already stored day makes no FusionSolar requests. Its response contains the PNG, consumption readings, EV blocks and totals together. Deploy a build containing the new report endpoint before running the EV workflow.

## Verification

```powershell
uv run --locked python -m unittest discover -s tests
```

Set `HOMEENERGY_TEST_DATABASE=1` to include PostgreSQL integration tests in disposable schemas. Deployment checks are available in [deploy/smoke_test.py](deploy/smoke_test.py).

With Node.js installed, run `node tests/test_n8n_profiles.mjs` to check the two admin profiles' date overrides, retry limits, PNG validation, attachment handling and workflow connections without sending Discord messages.
