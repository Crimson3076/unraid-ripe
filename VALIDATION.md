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
