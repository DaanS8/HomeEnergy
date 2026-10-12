# Tests

Run the offline Python suite with `uv run --locked python -m unittest discover -s tests`
and workflow checks with `node tests/test_n8n_profiles.mjs`.

Set `HOMEENERGY_TEST_DATABASE=1` with PostgreSQL running to include repository
integration tests. Each test creates and drops its own disposable schema.

Tests should describe a regression a user would notice:

- Analysis: exact energy, strict thresholds, missing-data handling, midnight
  attribution, elapsed time through DST, and calibration uncertainty.
- Ingestion: complete immutable snapshots, progress, stopped ranges, resumable
  failures, and avoiding provider requests for stored dates. Shared scenarios
  exercise both real services in `test_ingestion.py`.
- Reports and HTTP: real analysis and PNG rendering, accurate warnings and
  totals, authoritative committed snapshots, and safe error responses.
- CLI: stored dates, useful progress/errors, exit codes, and readable files at
  the requested path.
- Charts: faithful interval power, visible gaps, unambiguous time labels,
  decodable nonblank exports, and recovery after rendering failures.
- PostgreSQL: actual round trips, immutable writes, rollback, and concurrent
  transactions. A mocked SQL call cannot establish these guarantees.
- n8n: selected dates and destinations, delivery routes, bounded retry routes,
  error alerts, and attachments.

`service_helpers.py` supplies immutable in-memory storage and a recording
provider at external boundaries. These let tests run production orchestration
without credentials or network access; they do not validate SQL behavior.
Shared fixtures live in helper modules, so tests never import another test file.

Combine cases that protect the same rule using named subtests. Keep assertions
about outputs and observable effects. Mock calls are useful when the call itself
is a contract (provider protocol, forbidden network access, resource lifetime);
avoid requiring a particular internal helper, call order, or array layout.

For statistical checks, use known synthetic loads and an independent analytical
expectation rather than copying the production estimator or RNG sequence. For
images, decode the whole export rather than accepting a PNG signature alone.
