# Stage 1 validation · 2026-10-27

The Stage 1 implementation is prepared for review and live acceptance on NODE2. Stage 2 has not been started.

Verified in the cloud workspace:

- `python -m pytest -q`: **157 passed**, including version forms, quiet-soak resets, Home Assistant's patch exception, 1.02 as the security target, pending/history transitions, per-release cache reuse across containers, profile-change invalidation, source failures, API protection and secret redaction.
- The RomM fixture reproduces the `roms_folder` removal as applies and CORS as does not apply with changelog/profile citations. LLM responses in these regression tests are mocked; this is not evidence of real model quality or your actual configuration.
- Chromium: dashboard, detail, settings and logs work; light/dark and 390px phone layout checked; no JavaScript page errors. The running cloud UI uses a clearly labelled, separate demo database with six fixtures.
- Container image `ripe:stage1` builds through the optional verified-wheel offline helper. It starts as UID99/GID100 with a read-only root filesystem, reports healthy, serves the HTTP pages and fixture API, preserves timezone settings, and blocks notifications in demo mode. The smoke container had no homelab mounts or credentials. An initial source-file ownership failure was corrected and the image rebuilt successfully.
- Unraid XML parses, Compose configuration validates, and `pip check` passes. The saved installation script was rerun successfully. The test runner emits one upstream Starlette/httpx deprecation warning; no tests are skipped or failing.

Still unverified on the real LAN:

1. NODE2's installed versions, image mappings/digests, Unraid mounts and HA update inventory.
2. NODE2's Ollama port, installed model and real classification/applicability quality. No live LLM calls were made.
3. Household timezone, NPM/Cloudflared exposure, authentication facts, relevant non-secret config keys and app dependencies. These remain editable; arbitrary configuration files are not mounted.
4. HA token permissions, delivery through `script.notify_household`, and receipt of the scheduled weekly digest. Mocked request-routing/quiet-hour tests pass; no phone notification has been sent from this cloud build.
5. The normal online Docker build on NODE2. In the cloud, the builder could fetch the official base image but its internal DNS could not reach package/proxy hosts; the offline helper used host-downloaded, TLS-verified wheels without disabling validation.

Public API calls were also restricted by the cloud's current allowlist. Required custom domains and reusable install/start instructions are saved in the environment draft. Saving the draft does not apply or publish it. Review/save the changes in environment settings, then publish when desired.

Use the README's NODE2 acceptance checklist before treating Stage 1 as live-accepted. Ripe has not modified your homelab or run any update.
