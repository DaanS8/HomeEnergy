# Architecture

Home Energy stores FusionSolar usage and Belgian ENTSO-E prices in PostgreSQL,
then produces consumption, EV charging, and day-ahead reports through FastAPI.
n8n schedules requests, retries failures, and delivers reports to Discord.

## Repository map

```text
src/home_energy/
|-- api/                    HTTP boundary
|   |-- app.py              Service wiring, startup schema initialization, health
|   |-- routes.py           Invoke reports; serialize results and base64 PNGs
|   |-- schemas.py          Request validation and response contracts
|   `-- errors.py           Translate failures into safe HTTP responses
|-- usage/                  Consumption and EV domain
|   |-- fusionsolar.py      Provider requests and configuration
|   |-- auth.py             Provider authentication and sessions
|   |-- energy_balance.py   Parse provider payloads into snapshots
|   |-- consumption.py      Consumption completeness and boundary context
|   |-- daily.py            Daily consumption and EV analysis
|   |-- ev.py               Shared charging detection and calibrated model
|   `-- constants.py        Plant identity and provider defaults
|-- prices/                 Day-ahead price domain
|   |-- entsoe.py           Provider requests and price normalization
|   `-- analysis.py         Price analysis and threshold ranges
|-- database/               Shared PostgreSQL infrastructure
|   |-- connection.py       Configuration and connections
|   |-- schema.py           Schema setup and transactional repository base
|   `-- schema.sql          Tables and constraints
|-- charts/                 Shared Matplotlib figures, styling, rendering, exports
|-- investigations/ev/      Offline hypotheses, calibration, and report exports
|-- filling.py              Shared date-range loop, progress, skips, failures
`-- cli.py                  Database setup, ingestion, charts, investigations

tests/                      Analysis, services, API, SQL, CLI, chart, workflow checks
examples/                   Python ingestion and analysis usage
docs/                       Architecture, EV methodology, chart conventions
deploy/                     Release build/publish scripts, smoke tests, Portainer
`-- n8n/                    Scheduling, retries, and Discord delivery workflows
Dockerfile                  Application image
compose.yaml                Local application and PostgreSQL stack
pyproject.toml + uv.lock    Package, CLI entry point, dependencies, locked versions
```

Both `usage/` and `prices/` also contain these modules:

| Module | Responsibility |
| --- | --- |
| `models.py` | Domain snapshot and result types |
| `ingestion.py` | Date policy, lazy provider access, validation before storage |
| `repository.py` | SQL persistence and reconstruction of stored snapshots |
| `report.py` | Orchestrate ingestion, reload, analysis, and PNG rendering |
| `chart.py` | Build domain figures using shared `charts/` primitives |

## Report flow

```text
n8n -> api/routes.py -> domain/report.py
                         |-- ingestion.py -> provider -> repository.py -> PostgreSQL
                         |-- repository.py -> committed snapshots -> analysis
                         `-- chart.py + charts/ -> PNG bytes
     <- JSON + base64 PNG <- completed report
```

Prices load one day; EV reports load the requested day and both adjacent days
for boundary context. The CLI and Python callers reuse domain services directly.
Offline investigations reuse usage snapshots, detection, and optional ingestion
to export figures, CSV, PDF, and HTML.

## Component boundaries

- Provider adapters handle external protocols; repositories own SQL.
- Ingestion skips stored dates and opens a provider client only when needed.
  Validated snapshots are immutable; each day commits atomically and independently.
- Reports analyse committed snapshots, including concurrent insert winners.
  Rendering failures leave stored days available for retries.
- Analysis operates on domain data; HTTP encoding belongs in `api/` and figure
  rendering belongs in domain `chart.py` and shared `charts/`.
- `api/app.py` wires dependencies; services accept injected repositories,
  ingestion services, provider factories, or clocks at their relevant boundaries.

Dates use **Europe/Brussels**, power **kW**, energy **kWh**, and prices **EUR/MWh**.
API decimal values are strings; timestamps carry timezone offsets.

See [setup and API usage](../README.md), [EV methodology](ev-estimation.md),
[chart conventions](chart-style-guide.md), and [test guidance](../tests/README.md).
