# Stage 1 validation · 2026-10-27

This records the initial Stage 1 validation baseline. Live integration acceptance must be performed for each deployment; no private installation configuration belongs in the repository.

Verified during initial development:

- `python -m pytest -q`: **168 passed**, covering version forms, quiet-soak resets, patch exceptions, security targets, pending/history transitions, shared release caches, profile-change invalidation, source failures, API protection and secret redaction.
- Synthetic RomM regression: obsolete `roms_folder` removal applies when configured; a CORS change does not apply when cross-origin clients are explicitly absent. Model responses are mocked, so these checks do not establish real model quality.
- Chromium: dashboard, detail, settings and logs; light/dark themes; 390px phone layout; no JavaScript page errors. Demo data is isolated and cannot send notifications.
- Container image built using the optional verified-wheel offline helper and started as UID99/GID100 with a read-only root filesystem. Health, HTTP pages, fixture APIs, timezone persistence and demo notification blocking passed. The smoke container had no deployment mounts or credentials.
- Unraid XML parsing, Compose configuration validation and `pip check` passed. The test runner emitted one upstream Starlette/httpx deprecation warning.

Per-deployment checks still required:

1. Installed versions, repository mappings, registry digests, host mounts and Home Assistant inventory.
2. Local Ollama connectivity, model availability and real classification/applicability quality.
3. Timezone, exposure, authentication facts, relevant non-secret profile keys and dependency relationships.
4. Home Assistant token permissions, configured notification script delivery and receipt of the weekly digest.
5. Normal online Docker build availability. The offline helper was used because the development builder could not resolve package/proxy hosts; TLS and checksum verification stayed enabled.

Follow the README's deployment acceptance checklist before treating an installation as validated. Stage 1 performs no system updates.

## In-app HA connection and fruit icon · 2026-10-27

- `python -m pytest -q`: **178 passed**, including 10 new tests for credential
  persistence/permissions, legacy environment fallback, disconnect across
  restarts, token rotation, redirect refusal, bounded responses, secret
  redaction, API origin protection, demo isolation and icon delivery. The same
  upstream Starlette/httpx deprecation warning remains.
- `node --check ripe/static/app.js` and the optional jsdom settings interaction
  suite passed. The HA form is separate from general settings, tokens clear
  after saving, tests use saved credentials, and demo cannot submit live data.
- Built the app with the existing verified-wheel offline helper. Started a
  disposable container as UID99/GID100 with a read-only root, dropped
  capabilities, no-new-privileges, and `--network none`. A local synthetic HA
  server verified the API probe and bearer authorization. Connection save,
  private file permissions, API/log redaction and the PNG endpoint passed.
  After removing the connection and restarting with legacy environment values,
  it stayed disconnected. Only a temporary named volume was used and removed.
- The container advertises the Ripe PNG through `net.unraid.docker.icon`.
  Template XML, Compose config, dependency checks and patch checks passed.
  The original generated fruit is an RGBA PNG with alpha transparency, shared
  by Ripe's own UI, favicon and Unraid template. Existing templates need their
  Icon URL changed once; the Settings page provides a copy button.
- HA credentials live only in `/config/secrets/home-assistant.json`, outside
  general settings and profiles. Back up that private file with appdata.
  Discovery and notifications use the saved connection immediately, preserving
  Ripe's configured household script, recipients, scheduling and quiet hours.

No live Unraid installation, real HA token, household script or phone delivery
was accessed in this cloud task. The registry publishing workflow is unchanged:
this repository still uses a locally built image, so deployments must rebuild
after pulling the updated source.
