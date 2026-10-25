# Home Energy 0.3.0

Release date: 2026-10-25. Target: `ghcr.io/daans8/home-energy:0.3.0` (Linux amd64).

- EV reports use the mean of accepted clean-stop drops: 2.159 kW, with two
  continuous hours above that power required. Historical reports are recalculated
  from existing source snapshots with this calibration.
- Day-ahead reports add a configurable hysteresis margin (default 25 EUR/MWh)
  around peaks above the trigger (default 300 EUR/MWh). Chart highlights, warning
  text and returned ranges follow the same rule. Set the margin to zero for
  strict threshold ranges.
- EV n8n manual runs can deliver each day from `start_date` through two days ago;
  scheduled runs still report only two days ago. Re-running a backfill sends
  those reports again.
- API version metadata and release script defaults now follow `pyproject.toml`.
  Release packaging includes these notes and the home-server update guide.

No database schema changes are introduced by this release. Existing credentials,
PostgreSQL data, cache volumes and output volumes are reused.

Follow [HOME_SERVER_UPDATE.md](HOME_SERVER_UPDATE.md) to deploy, verify or roll
back. Import the updated EV workflow only if you want its backfill behavior;
check its Discord destinations and leave `test_date` blank for normal operation.
