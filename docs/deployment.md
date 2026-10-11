# First deployment: TrueNAS + Portainer + existing PostgreSQL

Target: TrueNAS SCALE ElectricEel-24.10.2.4, `192.168.0.10`. n8n runs on the same NAS.
Deploy the app through your existing Portainer Docker Standalone environment. PostgreSQL port 5432 was reachable from this workstation; check its published port in Portainer before deployment.

## 1. Create the application database

Use your existing PostgreSQL account. This is the PostgreSQL username, not the TrueNAS `truenas_admin` username unless you intentionally created a database role with that name.

In Portainer, open **Containers → your PostgreSQL container → Console** and connect using `/bin/sh`. For the official PostgreSQL image, run:

```sh
psql -U "$POSTGRES_USER" -d postgres
```

If your container uses different settings, substitute the existing PostgreSQL admin username. Inside psql, run:

```sql
CREATE DATABASE home_energy;
\q
```

The connected role becomes the owner. Set `PGUSER` in the app to that same role and use its existing password. If `home_energy` already exists, inspect it and reuse it rather than replacing it. The API creates its own `home_energy` schema/tables on startup; it does not import data from the development database.

## 2. Configure GitHub Container Registry in Portainer

The release image is `ghcr.io/daans8/home-energy:0.1.0`. It was published and successfully pulled on 11 October 2026; GitHub confirmed the package is private. The publishing script does not change visibility. The image includes a source label pointing to [TrackingPowerUsage](https://github.com/DaanS8/TrackingPowerUsage).

Published package: [Home Energy on GHCR](https://github.com/users/DaanS8/packages/container/package/home-energy). Verified digest: `sha256:37cefa68e04b9426fecb54ad169f73057db9edf8930ecc8a9cb7d077389e0d44`.

For a private package, create a GitHub **personal access token (classic)** with `read:packages` access to this package. In Portainer, open **Registries → Add registry → Custom registry**, then enter:

| Setting | Value |
| --- | --- |
| Name | `GitHub Container Registry` |
| Registry URL | `ghcr.io` |
| Authentication | Enabled |
| Username | `DaanS8` |
| Password | Your GitHub classic token, not your GitHub account password |

Save the registry and make it available to the TrueNAS Docker environment if Portainer asks. [Portainer custom registry documentation](https://docs.portainer.io/admin/registries/add/custom), [GHCR authentication documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

For local publishing, authenticate with `docker login ghcr.io -u DaanS8` using a classic token with `write:packages`. Keep tokens in Docker/Portainer credential storage; they are not app environment variables. No image archive upload is needed when deploying from GHCR.

### Optional offline package

The release folder is `dist/home-energy-0.1.0/`. It contains:

| File | Purpose |
| --- | --- |
| `images.tar` | Built Linux amd64 app image with local and GHCR tags |
| `compose.portainer.yaml` | App-only Portainer stack |
| `n8n-day-ahead.json` | Importable n8n workflow |
| `images.json` | Image metadata, including its exact image ID |
| `SHA256SUMS` | File integrity checks |
| `publish-image.ps1` | Upload the tested image to GHCR |
| `registry.json` | Published image metadata/digest, written after a successful push/pull |

For offline deployment only, select **TrueNAS Docker environment → Images → Import** in Portainer, select `images.tar`, and click **Upload**. Keep the tag `ghcr.io/daans8/home-energy:0.1.0`. The current archive includes that tag. If using an older archive with only `home-energy:0.1.0`, tag that image with the GHCR name or use the local name in the stack. [Portainer image import documentation](https://docs.portainer.io/user/docker/images/import).

The release contains no `.env`, account credentials, FusionSolar sessions, or database data. There is no need to unpack it into `/mnt/fast_pool/home_energy`. Keep a copy of the release for redeployment.

## 3. Deploy the Portainer stack

Open **Stacks → Add stack**, name it `home-energy`, select **Web editor**, and paste `compose.portainer.yaml`. [Portainer stack documentation](https://docs.portainer.io/user/docker/stacks/add).

Under **Environment variables**, add the following values. Enter credential values directly, without shell quotes:

| Variable | Value |
| --- | --- |
| `PGHOST` | `192.168.0.10` |
| `PGPORT` | `5432`, or your PostgreSQL published host port |
| `PGUSER` | Your existing PostgreSQL account that owns `home_energy` |
| `PGPASSWORD` | That account's existing password |
| `ENTSOE_API_KEY` | Your ENTSO-E key, available in your local project `.env` |
| `API_BIND_IP` | `192.168.0.10` (default) |
| `API_PORT` | `18000` (default; choose another free port if occupied) |
| `FUSIONSOLAR_USERNAME` | Optional for later CLI usage fills |
| `FUSIONSOLAR_PASSWORD` | Optional for later CLI usage fills |

`PGDATABASE=home_energy` is already set in the stack. The stack uses the NAS IP and published PostgreSQL port, so the containers do not need to share a Docker network. Do not use `localhost` for `PGHOST`: inside the app, that means the app container itself.

Select the configured GHCR registry if Portainer asks which registry credentials to use, then click **Deploy the stack**. The app uses `pull_policy: if_not_present`, so Docker downloads the versioned image from GHCR when it is not already present. For offline deployment, leave **Re-pull image/Pull latest image** off.

The image runs as UID/GID 10001, has a readiness health check, restarts automatically, and limits container logs. Its optional cache/output directories use Docker named volumes. API reports are generated in memory and returned in the response. The important persistent data belongs to the existing PostgreSQL deployment, so preserve and back up its storage. No host bind mounts are required for HomeEnergy.

If you choose a different port, update the n8n workflow's `base_url` accordingly.

## 4. Verify the NAS deployment

Wait for the app container to become **healthy**, then open:

- `http://192.168.0.10:18000/health/live`: HTTP process, expected `{"status":"ok"}`.
- `http://192.168.0.10:18000/health/ready`: PostgreSQL/schema access, expected `{"status":"ok"}`.
- `http://192.168.0.10:18000/docs`: interactive API documentation.

From PowerShell, test a completed day first:

```powershell
$body = @{ date = '2026-10-10'; threshold_eur_per_mwh = '300' } | ConvertTo-Json
$report = Invoke-RestMethod -Method Post -Uri 'http://192.168.0.10:18000/api/v1/reports/day-ahead' -ContentType 'application/json' -Body $body
$report | Select-Object date,status,warning,image_filename
[IO.File]::WriteAllBytes((Join-Path $PWD 'day-ahead.png'), [Convert]::FromBase64String($report.image))
```

Both `success` and `warning` return HTTP 200. A warning means actual intervals exceeded the configured EUR/MWh threshold; `300 EUR/MWh` corresponds to `0.30 EUR/kWh` for the market energy component, excluding taxes and other charges. The image is plain base64 PNG, not a PNG HTTP response.

The API has no authentication, matching the trusted LAN setup. Configure **Authentication: None** in n8n. ENTSO-E credentials belong in the app, not in the n8n HTTP request.

## 5. Import and configure n8n

Create a workflow in n8n and choose **Import from File → n8n-day-ahead.json**. It is initially inactive. In **Settings** (the workflow's Edit Fields node), check:

- `base_url`: `http://192.168.0.10:18000`.
- `threshold_eur_per_mwh`: string `300`, or your desired threshold.
- `date`: tomorrow in Brussels, calculated once at the start: `{{ $now.setZone('Europe/Brussels').plus({ days: 1 }).toISODate() }}`.

For your first test, temporarily set `date` to a fixed past date, such as `2026-10-10`, and run manually. Restore the date expression after testing.

The **Prepare report** HTTP Request node is preconfigured:

| Setting | Value |
| --- | --- |
| Method | POST |
| URL | `{{ $json.base_url + '/api/v1/reports/day-ahead' }}` |
| Authentication | None |
| Send Body | On |
| Body Content Type | JSON |
| Specify Body | Using JSON |
| JSON body expression | `{{ { date: $json.date, threshold_eur_per_mwh: $json.threshold_eur_per_mwh } }}` |
| Response Format | JSON |
| Include Response Headers and Status | On |
| Never Error | On |
| Timeout | 180000 ms |

With the full response enabled, the API report is under `body`; the HTTP status is under `statusCode`. **Never Error** allows workflow branching on the API's error envelope. Transport failures go to the retry path. [n8n HTTP Request documentation](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/).

The workflow:

1. Runs manually or daily at **16:00 Europe/Brussels**.
2. Keeps the requested date fixed through retries.
3. Calls the report API. HTTP 200 with `success` or `warning` follows the completed-report path.
4. Retries errors with `retryable: true` after ten minutes, up to six total attempts. Errors that are not retryable stop immediately. The last failed attempt marks the execution as failed.
5. Converts `body.image` into a binary PNG in field **chart**, with `body.image_filename` and MIME type `image/png`.
6. Restores the report metadata in **Report and attachment**, because Convert to File returns binary data without retaining the input JSON.

The daily time is a starting schedule, not a guarantee that prices are published; delayed publication can return `prices_unavailable`. [n8n schedule/timezone documentation](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.scheduletrigger/).

The final node outputs `$json.status`, `$json.warning`, `$json.high_price_ranges`, `$json.date`, and `$binary.chart`. Attach your notification node after it. Select binary field **chart** for the image attachment; use `$json.warning` only when `$json.status === 'warning'`. This workflow does not send notifications until you add and configure that node.

If rebuilding manually, **Convert to File → Move Base64 String to File** uses Base64 Input Field `body.image`, output field `chart`, filename `{{ $json.body.image_filename }}`, MIME type `image/png`. [n8n Convert to File documentation](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.converttofile/).

Save and activate/publish the workflow once the manual run succeeds. Keep its workflow timezone set to **Europe/Brussels**. Use the NAS IP, not `localhost`, in n8n; n8n is a separate container. If your n8n instance blocks private-network HTTP requests, allow the NAS address in that instance's request security settings.

## Available endpoints and failures

| Endpoint | Purpose |
| --- | --- |
| `GET /health/live` | HTTP process health |
| `GET /health/ready` | PostgreSQL and schema readiness |
| `POST /api/v1/reports/day-ahead` | Belgian price analysis and PNG |
| `GET /docs` | Interactive API documentation |
| `GET /openapi.json` | API contract |

Usage fills/charts are currently CLI operations; they do not have HTTP endpoints yet.

| HTTP/code | What to do |
| --- | --- |
| 422 `invalid_request` | Correct date/body/threshold; do not retry unchanged input |
| 503 `prices_unavailable` | Prices not published; retry later |
| 502 `provider_unavailable` | Retry provider access later |
| 502 `invalid_provider_data` | Incomplete/invalid provider data; investigate |
| 503 `database_unavailable` | Check PostgreSQL status, credentials, database ownership, published port, and network access |
| 500 `configuration_error` | Check Portainer environment variables |
| 500 `internal_error` | Inspect app logs |

If PostgreSQL refuses remote access, inspect its `listen_addresses` and `pg_hba.conf` for the application's Docker source network. Add an appropriate password-authenticated rule for `home_energy` and your existing account rather than enabling database `trust` authentication.

## Rebuilds and updates

From the project directory on Windows:

```powershell
uv run --locked python -m unittest discover -s tests
docker build --platform linux/amd64 -t home-energy:0.1.0 .
uv run --locked python deploy/smoke_test.py --live-provider
powershell -ExecutionPolicy Bypass -File deploy/build-release.ps1 -Version 0.1.0
powershell -ExecutionPolicy Bypass -File deploy/publish-image.ps1 -Version 0.1.0
```

The package script refuses to overwrite an existing release. For the next version, build/package with a new tag, for example `-Version 0.1.1`, then run `smoke_test.py --image home-energy:0.1.1`, and publish with `publish-image.ps1 -Version 0.1.1`. Change the stack's app image to `ghcr.io/daans8/home-energy:0.1.1` and redeploy. Keep your Portainer variables and PostgreSQL database. Take a database backup before upgrades that change schema. Keep old images for rollback; a rollback after schema changes may also require a compatible database restore.

The smoke test uses an isolated PostgreSQL container and disposable Docker volume; it does not alter your NAS database or local development tables. It validates real PNGs, both report statuses, restart persistence, database outage/recovery, and, with `--live-provider`, real ENTSO-E prices for yesterday.

## Release verification (11 October 2026)

- All 101 Python tests passed, including the 19 PostgreSQL integration tests.
- The packaged `home-energy:0.1.0` image passed the isolated container smoke test, including an actual ENTSO-E fetch for 10 October 2026 and a cached repeat.
- The workflow imported and executed in a temporary local n8n 2.42.6 container, producing the real API's report metadata and a PNG in binary field `chart`.
- n8n branch checks passed for warning reports, success on the third attempt, immediate failure for non-retryable errors, and stopping after six retryable failures. These branch tests shortened the wait interval; the distributed workflow retains ten-minute waits.
- NAS deployment and verification remain the manual steps above; no changes have been made to your NAS PostgreSQL or n8n instance.
