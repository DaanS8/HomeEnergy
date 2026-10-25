# Update Home Energy on the home server

Target image: `ghcr.io/daans8/home-energy:0.3.0` (Linux amd64).
Previous release: `ghcr.io/daans8/home-energy:0.2.0`.
The normal API address is `http://192.168.0.10:18000`.

## Portainer (existing installation)

1. Open your Docker environment, select **Stacks**, then the existing Home Energy
   stack. Record the currently deployed image tag for rollback.
2. Open **Editor** and change only the `app` image to
   `ghcr.io/daans8/home-energy:0.3.0`. The full reference configuration is
   [compose.portainer.yaml](compose.portainer.yaml).
3. Keep your existing environment variables (`PGHOST`, `PGPORT`, `PGUSER`,
   `PGPASSWORD`, `ENTSOE_API_KEY`, FusionSolar credentials and any API bind/port
   overrides), stack name and volume definitions.
4. Click **Update the stack**. Enable **Re-pull image and redeploy** if offered.
   Wait for the app container to be healthy; stack submission alone does not
   establish readiness. Check the container's logs if it fails.
5. Run the verification commands below from a machine that can reach the API.

For a Git-managed stack, update `deploy/compose.portainer.yaml` in the tracked
repository and push that change first, then use **Pull and redeploy** with
**Re-pull image and redeploy**. The editor is unavailable for Git-managed stacks.

Public GHCR images can be pulled anonymously. Publishing the image from your
workstation does not change the running home-server container.

## Docker Compose alternative (SSH)

Use this only if you manage this installation with Compose directly. For an
existing Portainer stack, use Portainer above to keep its configuration in sync.
In the existing deployment directory, update the `app` image in
`compose.portainer.yaml` to `ghcr.io/daans8/home-energy:0.3.0`. Use the same
Compose project name and environment file as the existing deployment; the shell
commands below assume these already match it. Portainer's stored environment
variables are not automatically available in an SSH shell.

```sh
docker compose -f compose.portainer.yaml pull app
docker compose -f compose.portainer.yaml up -d --no-deps --wait --wait-timeout 120 app
docker compose -f compose.portainer.yaml ps app
docker compose -f compose.portainer.yaml logs --tail=100 app
```

Pull downloads the selected tag; `up` recreates the app with that image while
preserving mounted volumes. The repository-root `compose.yaml` is for local
development and builds `home-energy:dev`; use the deployment file on the server.
Do not delete the stack or run `down --volumes` for an image update.

## Verify

```sh
curl --fail --show-error http://192.168.0.10:18000/health/ready
curl --fail --show-error --silent http://192.168.0.10:18000/openapi.json | python3 -c 'import json,sys; v=json.load(sys.stdin)["info"]["version"]; print(v); assert v == "0.3.0"'
curl --fail --show-error -X POST http://192.168.0.10:18000/api/v1/reports/ev/daily -H 'Content-Type: application/json' -d '{"date":"2026-10-10"}' -o /tmp/home-energy-ev-check.json
curl --fail --show-error -X POST http://192.168.0.10:18000/api/v1/reports/day-ahead -H 'Content-Type: application/json' -d '{"date":"2026-10-10"}' -o /tmp/home-energy-prices-check.json
```

Expect readiness `{"status":"ok"}`, API version `0.3.0`, and HTTP 200 for both
reports. Substitute a known stored date if appropriate. A date missing from the
database needs working provider credentials; the EV report also needs both
adjacent source days. Requests save reports locally and do not send Discord
messages. Leave n8n schedules enabled only after the API checks succeed.

## Roll back

Change the app image back to the tag you recorded (normally `0.2.0`) and repeat
the Portainer update or the Compose `pull` and `up` commands. Verify readiness
and that OpenAPI reports the selected older version. This release changes no
database schema, so reverting its image requires no data migration.

## References

- [Portainer stack editing and redeployment](https://docs.portainer.io/user/docker/stacks/edit)
- [Docker Compose pull](https://docs.docker.com/reference/cli/docker/compose/pull/)
- [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)
- [GHCR authentication and public images](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
