# Ripe · Stage 1

Ripe is a self-hosted update tracker for Unraid, Docker containers and Home Assistant. It keeps release records, explains changes with quoted evidence, and recommends when an update has had enough time to settle. **It does not install updates.** Guided updates, backups, rollback, chat and automatic updates are later stages and are not included.

Ripe writes its own SQLite cache in `/config`. Its Docker client is restricted to container/image inspection. Home Assistant updates are read from `update.*` entities. The only optional outbound action on your homelab is a notification through `script.notify_household`, enabled explicitly in Settings.

## Run on NODE2

The image is not published to a registry. Build it from this checkout on NODE2:

```sh
docker build -t ripe:stage1 .
mkdir -p /mnt/user/appdata/ripe
chown 99:100 /mnt/user/appdata/ripe
```

The normal Docker build needs PyPI access from the Docker builder. The cloud builder could pull the official Python image but could not resolve package/proxy hosts inside its build network. Container validation therefore used [deploy/build-offline.sh](deploy/build-offline.sh): it downloads the exact locked wheels through the host's verified pip connection, then installs those wheels without network access in a temporary build context. No TLS or checksum checks are disabled. This optional helper currently requires Linux x86_64 and Python 3.12 and changes only the dependency installation transport; it does not establish that the normal online build works on NODE2.

The container runs as UID 99/GID 100, the usual Unraid `nobody:users` identity. Change ownership only for Ripe's new appdata directory. Preserve any existing appdata and its ownership if you already use that path.

### Unraid Docker tab

Copy [deploy/my-ripe.xml](deploy/my-ripe.xml) to `/boot/config/plugins/dockerMan/templates-user/my-ripe.xml`, then use **Docker → Add Container → ripe**. Confirm the generated command uses the locally built `ripe:stage1` image rather than pulling another image.

The template provides the appdata path, read-only mounts, environment variables, an icon and a WebUI link. Its Extra Parameters publish `192.168.0.83:8087:8087`, binding specifically to NODE2's LAN address. There is no additional Port entry, to avoid a duplicate wildcard binding. If NODE2's address or the port differs, edit the publish argument, WebUI URL and allowed-hosts field. The icon URL becomes available after these files are pushed to the repository; the application itself uses its bundled icon.

Do not put this unauthenticated v1 UI behind a publicly accessible NPM proxy or Cloudflared tunnel. A LAN binding still needs your router/firewall to keep WAN clients out. `python -m ripe` binds only to loopback for development; the container listens internally on all interfaces and relies on the explicit host bind.

The Docker socket and host metadata are mounted as:

| Host | Container | Access |
|---|---|---|
| `/mnt/user/appdata/ripe` | `/config` | Read/write, Ripe data only |
| `/var/run/docker.sock` | `/var/run/docker.sock` | Read-only mount; API method allowlist in code |
| `/boot/config/plugins/dockerMan/templates-user` | `/host/templates` | Read-only |
| `/etc/unraid-version` | `/host/unraid-version` | Read-only |

A read-only socket mount **does not** make Docker's API read-only. Ripe separately allows only the specific container/image GET operations it needs. There are no pull, exec, restart, recreate or update routes. The template adds socket group `0`, normal on Unraid. Check `stat -c '%g' /var/run/docker.sock` and change `--group-add` if your socket group differs. Never use privileged mode to fix socket permissions.

### Docker Compose alternative

```sh
cp .env.example .env
# Edit .env locally; do not paste secrets into a shell command or Git.
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

Compose defaults to a loopback host binding. Set `RIPE_BIND_IP=192.168.0.83` in `.env` for LAN access. Set `DOCKER_GID` to the socket's group. Add a changed LAN address/hostname to `RIPE_ALLOWED_HOSTS` as well. Compose-managed containers do not use Unraid's template lifecycle; use the XML option when you want Unraid to manage Ripe alongside your existing apps.

### Configure integrations

| Variable | Purpose | Default |
|---|---|---|
| `HA_URL` | Home Assistant base URL | `http://192.168.0.61:8123` |
| `HA_TOKEN` | HA long-lived token for discovery and optional household notifications | Unset |
| `OLLAMA_URL` | Ollama **on NODE2** | `http://192.168.0.83:11434` |
| `LOCAL_MODEL` | Existing Ollama model | `qwen3:8b` |
| `GITHUB_TOKEN` | Optional public-release API rate limit increase | Unset |
| `GEMINI_API_KEY` | Optional public changelog classification / second opinion | Unset |
| `GEMINI_MODEL` | Optional Gemini model identifier | `gemini-2.5-flash` |
| `TZ` | IANA timezone for initial scheduling settings | `America/New_York` |
| `RIPE_CONFIG_DIR` | Ripe's private data directory | `/config` in Docker |
| `RIPE_ALLOWED_HOSTS` | Comma-separated UI hostnames/IPs without ports | Loopback and NODE2 names/IP |

Enter tokens in the masked Unraid template fields or local `.env`; never put them into app profiles. Ripe does not display secret values. A GitHub token needs no write permissions. Gemini is optional: rate limits or failures fall back to local classification with the provider recorded.

Confirm Ollama's published port from NODE2's Docker tab and update `OLLAMA_URL` if necessary. Ripe does not pull a model or use the gaming PC. Select an already installed 14B model in Settings if desired. A successful local `/api/tags` request from NODE2 confirms the service address, but does not prove that the configured model can complete a classification.

Container timezone is not inherited automatically from the Unraid host. Set `TZ` to NODE2's IANA timezone, then confirm the displayed timezone in Ripe Settings. Settings are persisted in appdata; changing the initial environment default does not overwrite saved settings. Quiet hours default to **22:00–09:00**, and the digest defaults to **Monday at 09:00**, in the configured timezone.

## Use Ripe

Open the WebUI, run **Check for updates**, and review any source errors. Missing information stays **Unknown**. An unavailable model does not turn unknown changes into a clean bill of health. On a machine without the homelab integrations, the optional demo is clearly marked synthetic and is not live evidence.

Each app has an editable tier, exposure, login status, dependency list, relevant config keys and upstream repository mapping. Provide only the configuration facts needed to judge changes. For example, the RomM test profile includes `filesystem.roms_folder` and whether browser clients use another origin. No entire configuration files are required or mounted. Sensitive-looking keys and values are stripped before storage and again before local inference.

Pending release cards link to upstream notes, show evidence quotes, and preserve per-release classifications. Updating an app outside Ripe changes its detected installed version on the next poll; releases at or below that version move into its history. History records observed upstream versions, not an assertion that Ripe performed the update.

### Waiting rules

- Infrastructure starts with a 21-day soak; internet-facing and general apps use 14 days. Edit each tier in Settings.
- A newer release resets the quiet soak. An older release that had already completed its quiet soak can remain a ripe target; one superseded before completing it does not silently become ripe later.
- The optional patch exception uses a shorter 3-day soak for patch releases when the first release in that major/minor line is known to be older than the tier soak. Missing lineage dates are not guessed.
- If a verified security fix applies and the newest version is still soaking, Ripe can recommend the earliest pending version containing that fix. With several fixes, the target must contain all known applicable fixes. It still requires your own update decision.
- Major version changes, database major changes, prereleases and latest-only images are shown independently of LLM classification. A latest-only image can offer only “latest” or “wait”; Ripe does not invent an old pullable tag.

Unknown or failed applicability analysis is treated conservatively. A “does not apply” conclusion requires verified changelog and profile evidence. Keyword matches never assign classification tags: they check whether the model addressed potentially important wording, including negations.

### Model privacy and evidence

Versioned prompts live in [ripe/prompts](ripe/prompts). Public release classifications can use Ollama, Gemini or a second opinion. “Does this affect me?” always uses Ollama. The Gemini payload contains only public changelog information, public app identity and version; app profiles, config contents, paths, addresses and secrets are excluded.

Claims must quote text present in the source release notes after whitespace/case normalization. Unsupported items are removed and logged. Unsupported summaries are suppressed. Conflicting classifications are flagged for review. Long notes are chunked and merged. The Logs page records sanitized model output, prompt version, provider/model, input size, duration, retry and evidence results, plus source failures.

Reviewing evidence is still useful: a matching quote proves the source text exists, not that a model interpreted it correctly. Ripe provides the source link and never takes action based on a classification.

### Detection limits

Ripe reads OCI source/version labels first, then Unraid template project/support links, and accepts manual repository mappings. Public Docker Hub, GHCR and LinuxServer registry lookups are supported. Installed digest-to-version matching is bounded to 12 candidate tags and is skipped when an OCI version is already known; private registries need a future authentication integration. A failed or truncated tag listing does not establish that an image is latest-only. Image change dates are when Ripe first observed the digest change, not a claim about when the publisher pushed it.

NPM/Cloudflared exposure, app login settings, relevant config keys and dependency relationships are editable profile facts. Ripe does not mount proxy databases or arbitrary app configuration to infer them. HA full notes use the read-only WebSocket release-notes request where supported; some entities expose only summaries or no publication date. Missing release dates prevent Ripe from inventing a soak deadline. Unraid releases come from the official `unraid/docs` repository's `docs/unraid-os/release-notes` directory; Unraid plugins are out of scope.

### Household notifications

Notifications start **disabled**. After confirming the HA token, timezone, quiet hours and recipients, enable them in Settings. All notifications call only:

```yaml
script.notify_household:
  people: [dallas]
  title: Ripe
  message: "…"
  home_only: false
  tag: "ripe-…"
  channel: Home Alerts
```

No `notify.mobile_app_*` services are used. Weekly digests summarize ripe/security/read-first counts and the next ripening. Newly detected applicable security fixes are queued immediately; quiet hours defer delivery until allowed. Configure the dashboard URL to a LAN address your phones can reach. A test notification is also subject to quiet hours. Delivery requires the real HA script and token; a mocked HTTP success is not proof that a phone received it.

## Develop and test

Python 3.12 or newer is required. Use the existing checkout; an isolated cloud task does not need a Git worktree.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps -r requirements-dev.lock
.venv/bin/python -m pytest
RIPE_CONFIG_DIR=/tmp/ripe-dev .venv/bin/python -m ripe
```

The default development listener is `127.0.0.1:8087`. In another local terminal, `curl --fail http://127.0.0.1:8087/health` checks HTTP readiness. Use the dashboard for a representative app view and source status. A healthy HTTP process does not establish that homelab integrations are reachable.

Tests cover version ordering, LinuxServer/HA version forms, soak resets, the security-target example, HA patch ripeness, read-only data sources, quoted evidence, privacy boundaries and scheduling. The RomM regression uses a representative release fixture and model responses: `roms_folder` removal applies to a configured folder; a CORS change does not apply when the profile explicitly excludes cross-origin clients. It does not claim to have queried your actual RomM configuration or a live model from the cloud.

## NODE2 acceptance checklist

The cloud development machine cannot inspect NODE2's containers, HA token, Ollama port or phone delivery. Finish these live checks on your LAN:

1. Inspect the template before starting. Confirm no privileged flag, the three host read-only mounts, only Ripe appdata writable, the socket group, and the LAN IP binding.
2. Check HTTP health and open the dashboard. Poll once. Confirm NODE2's containers, HA `update.*` entities and Unraid OS appear with correct versions or an explicit unknown/error. Compare a few known versions against the existing Docker/HA interfaces.
3. Confirm Ollama is NODE2's service, then inspect a completed LLM call and its evidence. If Gemini is enabled, inspect its log/provider information and verify the local-only applicability split.
4. Map unresolved repos manually, identify internet-facing apps and dependencies, and enter only relevant non-secret profile facts. Recheck the RomM case against your actual release notes/configuration before treating it as a live result.
5. Set the household timezone and dashboard URL. Enable notifications deliberately, request a test outside quiet hours, and confirm it arrives through `script.notify_household`. Confirm the weekly digest at its configured time; no phone delivery has been established until you receive it.
6. Compare the host state before/after polling: Ripe must not create/restart containers, pull/tag images, install updates, execute commands or modify host config. Its own database/cache and explicitly enabled household notifications are expected.

Stop after Stage 1 review. Ripe contains no Stage 2 approval/backup/update machinery and no Stage 4 auto-update toggle.
