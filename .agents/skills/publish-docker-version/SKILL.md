---
name: publish-docker-version
description: Publish a new Home Energy Docker image to GHCR and provide home-server update instructions. Use when the user says "publish a new docker version", "release Home Energy", or asks to build and publish its next container version.
---

# Publish a Home Energy Docker version

Use the HomeEnergy repository in the current workspace. Its Git origin is
`DaanS8/TrackingPowerUsage`; the usual local path is
`C:/Users/daan.seuntjens/Documents/PersonalProjects/HomeEnergy`.
This skill is specific to that project; do not apply its registry or server
defaults to another repository.

Read the repository instructions, `README.md`, `deploy/build-release.ps1`,
`deploy/publish-image.ps1`, `deploy/smoke_test.py`, `deploy/RELEASE_NOTES.md`, and
`deploy/HOME_SERVER_UPDATE.md` to confirm the current release procedure. Inspect
Git status and history and the existing GHCR tags before choosing a new version.
Preserve unrelated local work and do not include secrets from `.env` in output.

Keep deployment documentation to the existing files: release workflow in
`README.md`, current release changes in `deploy/RELEASE_NOTES.md`, and server
update/rollback steps in `deploy/HOME_SERVER_UPDATE.md`. Update them in place;
do not create per-version notes, extra deployment guides or release summaries
in the tracked tree. Versioned snapshots and generated artifacts belong in
Git-ignored `dist/home-energy-X.Y.Z/`.

## Version and preparation

- Publish immutable `MAJOR.MINOR.PATCH` tags at
  `ghcr.io/daans8/home-energy`; the target architecture is `linux/amd64`.
  There is no `latest` alias or automatic Git-tag release pipeline.
- `pyproject.toml` is the version source. The API reads installed package
  metadata; build/publish script defaults read the project version. `uv.lock`
  must agree. `Dockerfile` uses `VERSION=dev` unless the release build supplies
  it; the build script supplies the version for its OCI label.
- Use a user-specified version when provided. Otherwise choose the next unused
  patch for fixes or minor for new behavior, explain the choice, and continue.
  Compare changes since the previous release and inspect `dist/` and registry
  tags; do not overwrite a published tag. A failed registry lookup is not
  evidence that a tag is unused.
- Bump `pyproject.toml`, run `uv lock` without upgrading dependencies, and update
  the pinned tag in `deploy/compose.portainer.yaml`, README release examples,
  release notes and home-server guide. Inspect smoke-test expectations for
  changed domain behavior; keep meaningful exact-energy checks.
- Ensure Docker Desktop's Linux engine is available. It may be started locally
  if stopped. Use existing GHCR credentials. If publishing needs login, tell
  the user to run `docker login ghcr.io -u DaanS8` locally using a classic PAT
  with `write:packages`; never ask for the token in chat.

## Validate and publish

From the repository root, substitute the chosen version:

```powershell
uv run --locked python -m unittest discover -s tests
node tests/test_n8n_profiles.mjs
powershell -ExecutionPolicy Bypass -File deploy/build-release.ps1 -Version X.Y.Z
uv run --locked python deploy/smoke_test.py --image home-energy:X.Y.Z --expected-version X.Y.Z
powershell -ExecutionPolicy Bypass -File deploy/publish-image.ps1 -Version X.Y.Z
```

The build creates `dist/home-energy-X.Y.Z/` with the image tar, deployment YAML,
README, release notes, server guide, docs, n8n workflows, metadata and checksums.
It refuses an existing release directory: preserve prior artifacts and inspect
partial failures before recovering a build. Run publishing only after tests and
the isolated Docker/PostgreSQL smoke test pass. The smoke test seeds synthetic
data and cleans up only its disposable project. `--live-provider` is optional
and uses the real ENTSO-E API; ordinary releases do not need it.

A request to publish authorizes pushing the chosen new image. It does not
authorize changes on the home server, Discord messages, or Git pushes. Do not
introduce a separate approval step for an already requested publication.

Publishing pushes and pulls the built image, compares image IDs, and records
`registry.json` and updated `SHA256SUMS`. Also verify anonymous registry access
for the new tag and record its digest. If auth or registry access blocks the
push, retain the validated local release, report its exact state and the needed
login/retry command; do not claim publication or keep retrying the same failure.
If push succeeds but verification fails, report that distinction and inspect
the existing remote image before retrying any mutation.

## Hand off the server update

Return the published tag and digest, validation results, release directory and
a link to `deploy/HOME_SERVER_UPDATE.md`. Include the immediate Portainer steps:
open the existing stack, change only `app.image` to the new tag, preserve stack
variables and volumes, update with image pulling, then verify readiness and the
OpenAPI version at the configured address (normally `192.168.0.10:18000`).

The guide must also cover direct Compose pull/recreate with the existing project
name and environment, a fixed past-date report, and rollback to the previous
tag. Use `deploy/compose.portainer.yaml` for deployment; root `compose.yaml`
builds a development image. Pulling an image alone does not update a running
container. Keep server deployment manual unless the user requests execution.

Keep this repo skill and any installed copy under `~/.codex/skills/` in sync
when changing the workflow. Implicit invocation should remain enabled.
