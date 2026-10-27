# Ripe · Stage 1

Ripe is a self-hosted update tracker for Unraid, Docker containers and Home Assistant. It collects release notes, explains changes with quoted evidence, and recommends when an update has had enough time to settle. **It does not install updates.** Guided updates, backups, rollback, chat and automatic updates are later stages.

Ripe stores its SQLite database and cache in `/config`. Docker access is limited to container/image inspection. Home Assistant discovery reads `update.*` entities. Notifications are optional, disabled by default, and delivered through a Home Assistant script that you configure.

## Build and run

The image is not yet published to a registry. Build it from this checkout:

```sh
docker build -t ripe:stage1 .
```

The normal Docker build needs PyPI access. If the builder has no package-network access, [deploy/build-offline.sh](deploy/build-offline.sh) can download exact locked wheels on the host and install them offline in a temporary build context. It preserves TLS/checksum verification and currently requires Linux x86_64 with Python 3.12.

### Docker Compose

```sh
cp .env.example .env
# Configure integrations and optional LAN access in this local, ignored file.
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

Compose binds the WebUI to loopback by default. For LAN access, set `RIPE_BIND_IP` to a local interface address and add the hostname/IP used to open the UI to `RIPE_ALLOWED_HOSTS`. Configure `RIPE_APPDATA` and `DOCKER_GID` for the host. Inspect the resulting configuration before starting it.

### Unraid Docker tab

Copy [deploy/my-ripe.xml](deploy/my-ripe.xml) to `/boot/config/plugins/dockerMan/templates-user/my-ripe.xml`, then use **Docker → Add Container → ripe**. The template uses the locally built `ripe:stage1` image. Review the appdata path, host bind, allowed hosts, socket group and integration settings before starting.

The template defaults to a loopback WebUI binding. For LAN access, change its publish address and WebUI URL, and include the chosen hostname/IP in the allowed-hosts setting. The container runs as UID 99/GID 100, the usual Unraid `nobody:users` identity. Give that user access to Ripe's appdata directory without changing ownership of unrelated or existing application data.

| Host data | Container path | Access |
|---|---|---|
| Ripe appdata directory | `/config` | Read/write, Ripe data only |
| `/var/run/docker.sock` | `/var/run/docker.sock` | Read-only mount; API allowlist in code |
| `/boot/config/plugins/dockerMan/templates-user` | `/host/templates` | Read-only, Unraid only |
| `/etc/unraid-version` | `/host/unraid-version` | Read-only, Unraid only |

A read-only socket mount does not make Docker's API read-only. Ripe separately allows only the container/image GET operations it needs. There are no pull, exec, restart, recreate or update routes. Check `stat -c '%g' /var/run/docker.sock` and use that supplementary group instead of enabling privileged mode.

The v1 WebUI has no login. Keep it on a trusted network; do not publish it directly through an internet-facing reverse proxy or tunnel. The container listens internally on all interfaces and relies on the configured host bind and firewall.

## Configure integrations

Configuration belongs in local environment variables, the Unraid template, or Ripe's private Settings database. Do not commit deployment-specific addresses, recipients, tokens or app profiles.

| Variable | Purpose | Default |
|---|---|---|
| `HA_URL` | Home Assistant base URL | Unset |
| `HA_TOKEN` | Home Assistant long-lived token | Unset |
| `OLLAMA_URL` | Local Ollama endpoint for private profile analysis | Unset |
| `LOCAL_MODEL` | Already installed Ollama model | `qwen3:8b` |
| `GITHUB_TOKEN` | Optional public-release API rate limit increase | Unset |
| `GEMINI_API_KEY` | Optional public changelog classification/second opinion | Unset |
| `GEMINI_MODEL` | Gemini model identifier | `gemini-2.5-flash` |
| `DASHBOARD_URL` | Dashboard URL included in notifications | `http://localhost:8087` |
| `TZ` | Initial scheduling timezone | `UTC` in Docker |
| `RIPE_CONFIG_DIR` | Private Ripe data directory | `/config` |
| `RIPE_ALLOWED_HOSTS` | Comma-separated UI hostnames/IPs without ports | Loopback names/addresses |

Enter secrets in masked template fields or a local `.env`. Ripe never displays their values. A GitHub token needs no write permissions. Gemini is optional and falls back to local classification on errors or rate limits.

Use an Ollama endpoint that you control on the local network. Ripe does not pull models. Select an already installed model, including a larger model if suitable for your hardware. A successful `/api/tags` request confirms model availability, not classification quality.

Set an IANA timezone and confirm it in Settings. Container timezone is not inherited automatically from the host. Saved Settings take precedence over initial environment defaults. Quiet hours start at **22:00–09:00**, and the weekly digest defaults to **Monday at 09:00**, in the configured timezone.

## Use Ripe

Open the WebUI, choose **Check for updates**, and review source errors. Missing versions, notes or analysis remain **Unknown**. A clearly labelled demo with isolated synthetic data is available for exploring the interface without integrations.

Each app has an editable tier, exposure, login status, dependencies, relevant non-secret config keys and upstream repository mapping. Provide only facts needed to judge changes. Entire configuration files are not required or mounted. Sensitive-looking keys and values are stripped before storage and again before local inference.

Pending release records show classifications, evidence quotes and source links. After an external update changes an app's detected installed version, releases at or below that version move into history. History records observed versions; it does not imply that Ripe performed an update.

### Waiting rules

- Infrastructure starts with a 21-day soak; other tiers start with 14 days. Each tier is configurable.
- A newer release resets the quiet soak. An older release that already completed its soak can remain a target; one superseded before finishing does not silently become ripe later.
- The optional patch exception uses a 3-day soak when the first release in the major/minor series is known to have passed the full soak. Missing lineage dates are not guessed.
- An applicable security fix can recommend the earliest pending version containing the fix before the newest version ripens. With multiple fixes, the target must contain all known applicable fixes. Installation remains the user's decision.
- Major version changes, database major changes, prereleases and latest-only images are shown independently of LLM classifications. A latest-only image offers only “latest” or “wait”.

Unknown applicability is treated conservatively. A “does not apply” conclusion requires verified changelog and profile quotes. Keywords check completeness; they never assign classification tags. Unaddressed passages and model disagreements are flagged for review.

### Model privacy and evidence

Versioned prompts live in [ripe/prompts](ripe/prompts). Public-source release classifications can use Ollama, Gemini or a second opinion. Private “does this affect me?” analysis always uses local Ollama. Gemini receives only public release text, public app identity and version; profiles, config contents, paths, addresses and secrets are excluded.

Every displayed model claim needs a matching source quote after whitespace/case normalization. Unsupported items and summaries are removed and logged. Long notes are chunked and merged. Logs include sanitized model output, prompt version, model/provider, input size, duration, retries and evidence results. A matching quote establishes that source text exists, not that the model interpreted it correctly; inspect the linked notes before deciding.

### Detection limits

Ripe uses OCI source/version labels, then Unraid template project/support links, with manual mappings available. Public Docker Hub, GHCR and LinuxServer registry lookups are supported. Digest-to-version matching is bounded to 12 candidate tags; private registry authentication is not implemented. Incomplete tag listings do not establish latest-only status. Image change dates record when Ripe first observed a changed digest.

Exposure through reverse proxies/tunnels, application authentication, relevant config keys and dependencies remain editable profile facts. Ripe does not inspect arbitrary application files or proxy databases. Home Assistant full notes use its read-only WebSocket request where supported; some entities provide only summaries or omit dates. Missing dates prevent invented soak deadlines. Unraid notes come from the official `unraid/docs` repository. Unraid plugins are out of scope.

### Home Assistant notifications

Notifications start disabled. In Settings, configure a Home Assistant script entity, recipients, channel, timezone, quiet hours and a dashboard URL reachable by the recipients before enabling them. Ripe calls only the configured script, never `notify.mobile_app_*` directly.

The script must accept the fields `people` (list), `title`, `message`, `home_only` (false), `tag` and `channel`. For example, a locally configured `script.ripe_notifications` can translate this payload into the notification system used by the installation. Script names, recipients and channel names are user configuration, not built-in defaults.

Weekly digests summarize ripe, security and read-first counts plus the next ripening. New applicable security findings are queued immediately; quiet hours defer delivery. Test notifications honor quiet hours too. Mocked request success does not establish phone delivery; verify the configured script in the actual deployment.

## Develop and test

Python 3.12 or newer is required. Use the existing checkout; an isolated cloud task does not need a Git worktree.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps -r requirements-dev.lock
.venv/bin/python -m pytest
RIPE_CONFIG_DIR=/tmp/ripe-dev .venv/bin/python -m ripe
```

The development listener defaults to `127.0.0.1:8087`. A local request to `/health` checks HTTP readiness. Exercise the dashboard and representative detail pages as well; a healthy process does not prove integration connectivity.

Tests cover version ordering, patch ripeness, soak resets, security targets, read-only collectors, evidence, privacy and notifications. The RomM regression is a synthetic example: an obsolete `roms_folder` setting applies when present, while a CORS change does not apply when the profile explicitly excludes cross-origin clients. It does not contain a real installation's profile.

## Deployment acceptance checklist

1. Review mounts, file ownership, socket group, host binding and allowed hosts. Only Ripe appdata should be writable.
2. Poll once and compare representative Docker, Home Assistant and Unraid versions with their own interfaces. Resolve errors or retain explicit unknowns.
3. Check a completed local model call and its citations. If Gemini is enabled, verify provider logs and the local-only profile-analysis split.
4. Map unresolved upstream repositories and enter relevant non-secret profile facts.
5. Configure timezone and notifications, then confirm a test and the weekly digest outside quiet hours.
6. Confirm polling did not pull/tag images, create/restart containers, execute commands, install updates or modify host configuration. Ripe's database/cache and explicitly enabled notifications are expected writes.

Stage 1 contains no guided-update, backup, rollback, chat or auto-update machinery.
