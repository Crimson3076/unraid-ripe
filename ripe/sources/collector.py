from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin, urlparse
from xml.etree import ElementTree

import httpx
import websockets

from ripe.models import AppRecord, Profile, Release, Settings, utcnow
from ripe.versions import compare_versions, parse_version, version_parts

JSON = dict[str, Any]
INFRA = re.compile(r"unraid|home.?assistant|adguard|nginx.?proxy.?manager|cloudflar|wireguard|mariadb|mysql|postgres|redis|mongo|pihole|pi.hole|traefik|caddy", re.I)
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LINUXSERVER_UPSTREAM = {
    "sonarr":"Sonarr/Sonarr", "radarr":"Radarr/Radarr", "lidarr":"Lidarr/Lidarr",
    "readarr":"Readarr/Readarr", "prowlarr":"Prowlarr/Prowlarr", "bazarr":"morpheus65535/bazarr",
    "jellyfin":"jellyfin/jellyfin", "qbittorrent":"qbittorrent/qBittorrent",
    "sabnzbd":"sabnzbd/sabnzbd", "transmission":"transmission/transmission",
    "overseerr":"sct/overseerr", "tautulli":"Tautulli/Tautulli",
    "heimdall":"linuxserver/Heimdall", "bookstack":"BookStackApp/BookStack",
    "duplicati":"duplicati/duplicati", "code-server":"coder/code-server",
    "nextcloud":"nextcloud/server", "syncthing":"syncthing/syncthing",
}
ACCEPT_MANIFEST = ", ".join(("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"))

BASE_OS_LABEL = re.compile(r"\d{2}\.\d{2}|\$\{?\w+\}?|(?:ubuntu|debian|alpine|bookworm|bullseye|buster|trixie|focal|jammy|noble|oracular|plucky|centos|fedora|rockylinux|almalinux|busybox|scratch)(?:[-: ]?[\d.]+)?", re.I)


def usable_version_label(value: Any) -> str | None:
    """Return the OCI version label unless it is unset, a placeholder or a base-image release."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 100 or value.lower() in ("latest", "unknown", "develop", "master", "main"):
        return None
    return None if BASE_OS_LABEL.fullmatch(value) else value


def date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def github_repo(value: Any) -> str | None:
    """Accept only an explicit GitHub repository identity, never a fetch URL."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if REPO.fullmatch(value) and not value.startswith((".", "-")) and not any(p in (".", "..") for p in value.split("/")):
        return value.removesuffix(".git")
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return None
    candidate = "/".join(parts[:2]).removesuffix(".git")
    return candidate if REPO.fullmatch(candidate) and not any(p in (".", "..") for p in candidate.split("/")) else None


def safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        p = urlparse(value)
    except ValueError:
        return None
    if p.scheme in ("https", "http") and p.hostname and not p.username and not p.password and not p.query and not p.fragment:
        return value
    return None


def inferred_profile(name: str) -> Profile:
    return Profile(tier="infrastructure" if INFRA.search(name) else "general", exposure="unknown")


def read_templates(directory: str) -> dict[str, JSON]:
    """Read only metadata nodes; Config/Variable values are deliberately ignored."""
    base = Path(directory)
    found = {}
    if not base.is_dir():
        return found
    root = base.resolve()
    for path in sorted(base.glob("*.xml"))[:500]:
        try:
            if not path.resolve().is_relative_to(root) or path.stat().st_size > 1024 * 1024:
                continue
            content = path.read_bytes()
            if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                continue
            tree = ElementTree.fromstring(content)
            name = (tree.findtext("Name") or "").strip()
            image = (tree.findtext("Repository") or "").strip()
            project = github_repo(tree.findtext("Project")) or github_repo(tree.findtext("Support"))
            webui = (tree.findtext("WebUI") or "").strip()
            if "[IP]" in webui:
                probe = re.sub(r"\[PORT:(\d+)\]", r"\1", webui.replace("[IP]", "127.0.0.1"))
                webui = webui if safe_url(probe) else None
            else:
                webui = safe_url(webui)
            metadata = {"repo": project, "icon": safe_url(tree.findtext("Icon")), "web_url": webui, "image": image}
            if name:
                found[name.lower()] = metadata
        except (OSError, ElementTree.ParseError, ValueError):
            continue
    return found


# pydantic's ValidationError is a ValueError.
PARSE_ERRORS = (AttributeError, TypeError, ValueError, KeyError)


MAPPING_RETRY_SECONDS = 6 * 3600
MAPPING_ERROR = "GitHub: mapping needs attention (repository not found)"


class SourceError(Exception):
    """Intentionally safe to persist: only fixed source labels and status codes."""


class SourceNotFound(SourceError):
    """HTTP 404: the mapped repository is private or does not exist."""


class CachedHTTP:
    def __init__(self, client: httpx.AsyncClient, cache_get=None, cache_set=None):
        self.client = client
        self.cache_get = cache_get or (lambda _: None)
        self.cache_set = cache_set or (lambda *_: None)

    async def get(self, url: str, *, headers: JSON | None = None, ttl: int = 21600, source: str = "Upstream") -> Any:
        key = "http:" + hashlib.sha256(url.encode()).hexdigest()
        cached = self.cache_get(key) or {}
        if cached.get("fetched", 0) + ttl > time.time() and "body" in cached:
            return cached["body"]
        hdr = dict(headers or {})
        if cached.get("etag"):
            hdr["If-None-Match"] = cached["etag"]
        try:
            response = await self.client.get(url, headers=hdr)
        except httpx.HTTPError:
            raise SourceError(f"{source}: connection failed") from None
        if response.status_code == 304 and "body" in cached:
            cached["fetched"] = time.time()
            self.cache_set(key, cached)
            return cached["body"]
        if response.status_code == 404:
            raise SourceNotFound(f"{source}: HTTP 404")
        if response.status_code >= 400:
            raise SourceError(f"{source}: HTTP {response.status_code}")
        if len(response.content) > 8 * 1024 * 1024:
            raise SourceError(f"{source}: response too large")
        try:
            body = response.json()
        except ValueError:
            raise SourceError(f"{source}: invalid JSON") from None
        self.cache_set(key, {"body": body, "etag": response.headers.get("etag"), "fetched": time.time()})
        return body


class DockerReader:
    """A second barrier beyond the read-only socket mount: a GET route allowlist.

A :ro socket mount alone does not make the Docker daemon's API read-only.
    """
    ALLOWED = re.compile(r"^/(?:containers/json|images/(?:sha256%3A|sha256:)?[a-fA-F0-9]{12,64}/json)$")

    def __init__(self, client: httpx.AsyncClient, host: str = "localhost"):
        self.client = client
        self.host = host
        self.skipped: list[str] = []

    async def read(self, path: str, params=None) -> Any:
        if not self.ALLOWED.fullmatch(path):
            raise SourceError("Docker: endpoint not permitted")
        try:
            response = await self.client.get(path, params=params)
            if response.status_code >= 400:
                raise SourceError(f"Docker: HTTP {response.status_code}")
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise SourceError("Docker: connection or JSON error") from None

    async def apps(self, templates: dict[str, JSON], existing: dict[str, AppRecord]) -> list[AppRecord]:
        containers = await self.read("/containers/json", {"all": "1"})
        if not isinstance(containers, list):
            raise SourceError("Docker: invalid container list")
        apps = []
        image_cache = {}
        self.skipped = []
        for item in containers[:1000]:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("Id", ""))
            if not re.fullmatch(r"[a-fA-F0-9]{12,64}", ident):
                continue
            try:
                apps.append(await self._app(item, ident, templates, existing, image_cache))
            except (AttributeError, TypeError, ValueError, KeyError):
                # One malformed record must not hide every other container.
                self.skipped.append("docker:" + ident[:12])
        return apps

    async def _app(self, item: JSON, ident: str, templates: dict[str, JSON], existing: dict[str, AppRecord], image_cache: dict) -> AppRecord:
        name = next(iter(item.get("Names") or []), ident[:12]).strip("/")
        key = "docker:" + name
        previous = existing.get(key)
        tpl = templates.get(name.lower(), {})
        image_id = item.get("ImageID", "")
        info = {}
        if re.fullmatch(r"sha256:[a-fA-F0-9]{64}", image_id):
            if image_id not in image_cache:
                image_cache[image_id] = await self.read("/images/" + quote(image_id, safe="") + "/json")
            info = image_cache[image_id]
        if not isinstance(info, dict):
            info = {}
        labels = dict((info.get("Config") or {}).get("Labels") or {})
        labels.update(item.get("Labels") or {})
        image = item.get("Image", "")
        # Only OCI metadata is retained. Never persist Config.Env or inspect blobs.
        installed = usable_version_label(labels.get("org.opencontainers.image.version"))
        host, repo, tag = parse_image(image)
        if not installed and tag not in ("latest", "stable", "main", "master", "edge", "develop") and not tag.startswith("sha256:"):
            installed = tag
        digests = info.get("RepoDigests") or []
        digest = next((d.rsplit("@", 1)[-1] for d in digests if "@sha256:" in d), None)
        upstream = previous.upstream_repo if previous and previous.upstream_repo else github_repo(labels.get("org.opencontainers.image.source")) or tpl.get("repo")
        if not (previous and previous.upstream_repo) and repo.startswith("linuxserver/"):
            upstream = LINUXSERVER_UPSTREAM.get(repo.split("/", 1)[1], upstream)
        profile = previous.profile if previous else inferred_profile(name + " " + repo)
        ports = [{"private":p.get("PrivatePort"), "public":p.get("PublicPort")} for p in item.get("Ports") or [] if isinstance(p, dict)]
        web_url = tpl.get("web_url")
        if web_url:
            port_map = {p["private"]:p["public"] for p in ports if p.get("public")}
            web_url = re.sub(r"\[PORT:(\d+)\]", lambda match:str(port_map.get(int(match.group(1)), match.group(1))), web_url.replace("[IP]", self.host))
            web_url = safe_url(web_url)
        return (AppRecord(id=key, name=name, source="docker", installed_version=installed, image=image, installed_digest=digest, image_created_at=date(info.get("Created")), upstream_repo=upstream, icon=tpl.get("icon"), web_url=web_url, profile=profile, detected={"ports":ports, "revision": str(labels.get("org.opencontainers.image.revision", ""))[:80], "registry": host, "tag": tag, **({"image_source": source} if (source := github_repo(labels.get("org.opencontainers.image.source"))) else {})}))


def parse_image(image: str) -> tuple[str, str, str]:
    if "@" in image:
        base, tag = image.rsplit("@", 1)
    elif ":" in image.rsplit("/", 1)[-1]:
        base, tag = image.rsplit(":", 1)
    else:
        base, tag = image, "latest"
    segments = base.split("/")
    if "." in segments[0] or ":" in segments[0] or segments[0] == "localhost":
        host, repo = segments[0].lower(), "/".join(segments[1:])
    else:
        host, repo = "registry-1.docker.io", base if "/" in base else "library/" + base
    if host in ("docker.io", "index.docker.io"):
        host = "registry-1.docker.io"
    return host, repo, tag


class RateLimited(SourceError):
    """A registry answered 429. Not a failure: the caller keeps its last known data."""

    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


LATEST_TAGS = ("latest", "stable", "main", "master", "edge", "develop")
MAX_TAG_PAGES = 10
MAX_RETRY_WAIT = 30
MAX_ATTEMPTS = 3
LOCAL_HOSTS = ("localhost", "127.0.0.1")


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+", value, re.ASCII):
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def is_local_image(image: str, digest: str | None, previously_tracked: bool = False) -> bool:
    """Never pulled from a registry: a bare image ID, or an unnamespaced name with no RepoDigests.

    previously_tracked: registry data was found for this app before, so a missing digest
    (e.g. a retagged pulled image) must not turn an official image into a local build."""
    image = (image or "").strip()
    if not image or re.fullmatch(r"(?:sha256:)?[a-fA-F0-9]{12,64}", image):
        return True
    if digest:
        return False
    name = image.split("@", 1)[0]
    first = name.split("/", 1)[0]
    if "/" in name and (first in LOCAL_HOSTS or first.startswith("localhost:")):
        return True
    return "/" not in name and not previously_tracked


class RegistryReader:
    HOSTS = {"registry-1.docker.io", "ghcr.io", "lscr.io"}
    TOKEN_HOSTS = {"auth.docker.io", "ghcr.io", "lscr.io"}
    KEPT_HEADERS = ("docker-content-digest", "link")

    def __init__(self, client: httpx.AsyncClient, cache_get=None, cache_set=None, min_interval: float = 0.25, sleep=asyncio.sleep):
        self.client = client
        self.tokens: dict[tuple[str, str], str] = {}
        self.cache_get = cache_get or (lambda _: None)
        self.cache_set = cache_set or (lambda *_: None)
        self.min_interval = min_interval
        self.sleep = sleep
        self.last_request: dict[str, float] = {}
        self.cooldown: dict[str, tuple[float, float]] = {}

    async def _throttle(self, host: str) -> None:
        """Spread requests to one registry instead of bursting."""
        wait = self.last_request.get(host, 0) + self.min_interval - time.monotonic()
        if wait > 0:
            await self.sleep(wait)
        self.last_request[host] = time.monotonic()

    async def _get(self, host: str, url: str, **kwargs) -> httpx.Response:
        """GET with Retry-After and exponential backoff on 429."""
        blocked = self.cooldown.get(host)
        if blocked and blocked[0] > time.monotonic():
            raise RateLimited("Registry: rate limited", blocked[1])
        for attempt in range(MAX_ATTEMPTS):
            await self._throttle(host)
            response = await self.client.get(url, **kwargs)
            if response.status_code != 429:
                return response
            wait = retry_after_seconds(response.headers.get("retry-after"))
            wait = 2.0 ** (attempt + 1) if wait is None else wait
            if wait > MAX_RETRY_WAIT or attempt == MAX_ATTEMPTS - 1:
                wait = min(wait, 3600)
                self.cooldown[host] = (time.monotonic() + wait, wait)
                raise RateLimited("Registry: rate limited", wait)
            await self.sleep(wait)
        raise RateLimited("Registry: rate limited")

    async def request(self, host: str, repo: str, suffix: str, extra_headers: JSON | None = None) -> httpx.Response:
        if host not in self.HOSTS or not re.fullmatch(r"[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*", repo) or any(p in (".", "..") for p in repo.split("/")):
            raise SourceError("Registry: unsupported image registry or name")
        url = f"https://{host}/v2/{repo}/{suffix}"
        headers = {"Accept": ACCEPT_MANIFEST, **(extra_headers or {})}
        if (host, repo) in self.tokens:
            headers["Authorization"] = "Bearer " + self.tokens[(host, repo)]
        try:
            response = await self._get(host, url, headers=headers)
            if response.status_code == 401:
                challenge = response.headers.get("www-authenticate", "")
                fields = dict(re.findall(r'(realm|service|scope)="([^"]+)"', challenge))
                realm = fields.get("realm", "")
                parsed = urlparse(realm)
                if parsed.scheme != "https" or parsed.hostname not in self.TOKEN_HOSTS or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise SourceError("Registry: unsupported authentication destination")
                auth = await self._get(host, realm, params={"service":fields.get("service", host), "scope":f"repository:{repo}:pull"})
                if auth.status_code >= 400:
                    raise SourceError(f"Registry: authentication HTTP {auth.status_code}")
                token = auth.json().get("token") or auth.json().get("access_token")
                if not isinstance(token, str):
                    raise SourceError("Registry: missing read token")
                self.tokens[(host, repo)] = token
                response = await self._get(host, url, headers={**headers, "Authorization":"Bearer " + token})
            if response.status_code == 304:
                return response
            if response.status_code >= 400:
                raise SourceError(f"Registry: HTTP {response.status_code}")
            if len(response.content) > 8 * 1024 * 1024:
                raise SourceError("Registry: response too large")
            if not isinstance(response.json(), dict):
                raise SourceError("Registry: invalid JSON object")
            return response
        except (httpx.HTTPError, ValueError):
            raise SourceError("Registry: connection or JSON error") from None

    async def fetch(self, host: str, repo: str, suffix: str, ttl: int = 0) -> JSON:
        """Cached request: revalidated with If-None-Match once older than ttl."""
        key = "http:" + hashlib.sha256(f"{host}/{repo}/{suffix}".encode()).hexdigest()
        cached = self.cache_get(key) or {}
        have = isinstance(cached.get("body"), dict)
        if have and cached.get("fetched", 0) + ttl > time.time():
            return cached
        response = await self.request(host, repo, suffix, {"If-None-Match": cached["etag"]} if have and cached.get("etag") else None)
        if response.status_code == 304 and have:
            entry = {**cached, "fetched": time.time()}
        else:
            entry = {"body": response.json(), "headers": {k: response.headers.get(k) for k in self.KEPT_HEADERS if response.headers.get(k)},
                     "etag": response.headers.get("etag"), "fetched": time.time()}
        self.cache_set(key, entry)
        return entry

    async def tag_list(self, host: str, repo: str) -> tuple[list[str], bool]:
        """Follow Link rel=next up to MAX_TAG_PAGES. The bool is True when the list is still incomplete."""
        tags: list[str] = []
        suffix = "tags/list?n=1000"
        for _ in range(MAX_TAG_PAGES):
            page = await self.fetch(host, repo, suffix)
            tags.extend(t for t in page["body"].get("tags") or [] if isinstance(t, str))
            match = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', page.get("headers", {}).get("link") or "")
            if not match:
                return tags, False
            target = urlparse(urljoin(f"https://{host}/v2/{repo}/tags/list", match.group(1)))
            # Pagination may only continue on the same registry and repository.
            if target.hostname != host or target.path != f"/v2/{repo}/tags/list" or not target.query:
                return tags, True
            suffix = "tags/list?" + target.query
        return tags, True

    async def metadata(self, image: str, installed_digest: str | None, match_version: bool = True) -> JSON:
        host, repo, tag = parse_image(image)
        cache_key = "registry:" + hashlib.sha256((image + "|" + str(installed_digest) + "|" + str(match_version)).encode()).hexdigest()
        cached = self.cache_get(cache_key)
        if cached and cached.get("fetched", 0) + 21600 > time.time():
            return dict(cached["body"])
        manifest = await self.fetch(host, repo, "manifests/" + quote(tag, safe=":"))
        data = manifest["body"]
        digest = manifest.get("headers", {}).get("docker-content-digest")
        matching_digests = {digest} if digest else set()
        # The local RepoDigest can be a manifest list or a platform-specific digest.
        matching_digests.update(m.get("digest") for m in data.get("manifests", []) if isinstance(m, dict))
        tags, truncated = await self.tag_list(host, repo)
        version_tags = [t for t in tags if re.match(r"^(?:v|version-)?\d+\.\d+", t)]
        # A moving tag is judged by its digest, so an incomplete tag list does not matter.
        truncated = truncated and tag not in LATEST_TAGS
        info: JSON = {"latest_digest": digest, "latest_only": not version_tags, "installed_version": None, "image_changed_at": None, "update_available": bool(installed_digest not in matching_digests) if installed_digest and digest else None, "truncated_tags": truncated}
        # Match local digest against version tags, with a strict per-image budget.
        # Architecture variant digests are handled above; exact immutable matches
        # are required rather than assuming the newest tag is what is installed.
        for version_tag in sorted(version_tags, key=version_key, reverse=True)[:12] if installed_digest and match_version else []:
            candidate = await self.fetch(host, repo, "manifests/" + quote(version_tag, safe=""), ttl=21600)
            candidate_digests = {candidate.get("headers", {}).get("docker-content-digest")}
            candidate_digests.update(m.get("digest") for m in candidate["body"].get("manifests", []) if isinstance(m, dict))
            if installed_digest in candidate_digests:
                info["installed_version"] = version_tag
                break
        # Registries do not provide a trustworthy tag-change timestamp. The
        # collector records first observation of a changed digest instead.
        self.cache_set(cache_key, {"body":info, "fetched":time.time()})
        return info


def version_key(value: str) -> tuple:
    """Loose ordering for registry tag candidates only; releases use ripe.versions."""
    return tuple(int(v) for v in re.findall(r"\d+", value)[:6])


def mapping_suggestion(app: AppRecord) -> str | None:
    """Best guess at a working repository: OCI source label, else GHCR owner/package."""
    current = (app.upstream_repo or "").lower()
    if (source := github_repo(app.detected.get("image_source"))) and source.lower() != current:
        return source
    if app.image:
        host, repo, _ = parse_image(app.image)
        if host == "ghcr.io" and repo.count("/") == 1 and repo.lower() != current:
            return repo
    return None


def latest_stable(releases: list[Release]) -> Release | None:
    """Newest final release; unparseable tags and pre-releases never qualify."""
    final = [r for r in releases if not r.prerelease and (v := parse_version(r.version)) is not None and not v.is_prerelease]
    return max(final, key=lambda r: parse_version(r.version)) if final else None


async def github_releases(http: CachedHTTP, repo: str, app_id: str, token: str | None = None, installed_version: str | None = None) -> list[Release]:
    repo = github_repo(repo)
    if not repo:
        raise SourceError("GitHub: invalid repository mapping")
    headers = {"Accept":"application/vnd.github+json", "X-GitHub-Api-Version":"2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    releases = []
    for page in range(1, 11):
        data = await http.get(f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}", headers=headers, source="GitHub")
        if not isinstance(data, list):
            raise SourceError("GitHub: invalid release list")
        for item in data:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            version = item.get("tag_name")
            if not isinstance(version, str):
                continue
            releases.append(Release(id=f"github:{repo}:{item.get('id', version)}", app_id=app_id, version=version, published_at=date(item.get("published_at")), prerelease=bool(item.get("prerelease")), notes=str(item.get("body") or ""), source_url=f"https://github.com/{repo}/releases/tag/{quote(version, safe='')}"))
        if len(data) < 100:
            break
        # We intentionally retain historical base releases for the patch rule.
        if installed_version:
            target = version_parts(installed_version)
            known = [k for r in releases if (k := version_parts(r.version))]
            if target and known and min(known) < target and any(len(k)>=3 and k[:2]==target[:2] and k[2]==0 for k in known):
                break
            # GitHub orders by creation date, so a late backport can sit above newer
            # minors. Only stop once a whole page is older than the installed minor.
            page_keys = [k for item in data if isinstance(item, dict) and isinstance(item.get("tag_name"), str) and (k := version_parts(item["tag_name"]))]
            if target and page_keys and all(k[:2] < target[:2] for k in page_keys):
                break
    # Intentional: a history longer than 10 pages is truncated silently, not an error.
    # Only releases near the installed version matter; older ones cannot be pending.
    return releases


async def unraid_releases(http: CachedHTTP, installed: str | None, token: str | None = None) -> list[Release]:
    """Read official versioned docs instead of inventing an OS GitHub repo.

    Their Markdown heading contains the actual version/date; a 7.4.0.md file
    can contain 7.4.0-rc.2 and must not be presented as a stable release.
    """
    headers = {"Accept":"application/vnd.github+json", "X-GitHub-Api-Version":"2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    prefix = "https://api.github.com/repos/unraid/docs/contents/docs/unraid-os/release-notes"
    listing = await http.get(prefix + "?ref=main", headers=headers, source="Unraid release notes")
    if not isinstance(listing, list):
        raise SourceError("Unraid release notes: invalid directory listing")
    candidates = [r["name"] for r in listing if isinstance(r, dict) and re.fullmatch(r"\d+\.\d+\.\d+\.md", str(r.get("name", "")))]
    target = version_parts(installed) or ()
    # Include the installed minor's base release for the patch exception. Fetch
    # only the documented selected span rather than all previous Unraid eras.
    if len(target) >= 2:
        candidates = [n for n in candidates if version_parts(n[:-3])[:2] >= target[:2]]
    else:
        candidates = sorted(candidates, key=lambda n: version_parts(n[:-3]))[-10:]
    releases = []
    if len(candidates) > 100:
        raise SourceError("Unraid release notes: selected history exceeds 100 records")
    for filename in sorted(candidates, key=lambda n: version_parts(n[:-3])):
        data = await http.get(prefix + "/" + filename + "?ref=main", headers=headers, source="Unraid release notes")
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise SourceError("Unraid release notes: unsupported document encoding")
        try:
            notes = base64.b64decode(data.get("content", ""), validate=False).decode("utf-8")
        except (ValueError, UnicodeError):
            raise SourceError("Unraid release notes: invalid document") from None
        heading = re.search(r"^#\s+Version\s+(\S+)\s+(\d{4}-\d{2}-\d{2})", notes, re.M)
        if not heading:
            # Missing date is safer than an invented clock, but undocumented
            # version formats are not guessed from filenames.
            continue
        version, published = heading.groups()
        releases.append(Release(id="unraid:" + version, app_id="unraid:os", version=version, published_at=date(published), prerelease=bool(re.search(r"[a-zA-Z]", version)), notes=notes, source_url="https://docs.unraid.net/unraid-os/release-notes/" + filename.removesuffix(".md") + "/"))
    return releases


async def ha_states(client: httpx.AsyncClient, ha_url: str, token: str) -> list[JSON]:
    parsed = urlparse(ha_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SourceError("Home Assistant: invalid base URL")
    try:
        response = await client.get(ha_url.rstrip("/") + "/api/states", headers={"Authorization":"Bearer " + token})
        if response.status_code >= 400:
            raise SourceError(f"Home Assistant: HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, list):
            raise ValueError
        # Copy only documented public update metadata; entity attributes can
        # contain arbitrary integration state which must not be persisted.
        keys = ("installed_version", "latest_version", "release_url", "release_summary", "title", "friendly_name", "entity_picture")
        return [{"entity_id":e["entity_id"], "state":e.get("state"), "attributes":{k:(e.get("attributes") if isinstance(e.get("attributes"), dict) else {}).get(k) for k in keys}} for e in data if isinstance(e, dict) and str(e.get("entity_id", "")).startswith("update.")]
    except (httpx.HTTPError, ValueError, KeyError):
        raise SourceError("Home Assistant: connection or JSON error") from None


async def ha_release_notes(ha_url: str, token: str, entity_ids: list[str]) -> dict[str, str]:
    """Only auth and update/release_notes are sent; no service calls occur here."""
    parsed = urlparse(ha_url)
    uri = parsed._replace(scheme="wss" if parsed.scheme == "https" else "ws", path=parsed.path.rstrip("/") + "/api/websocket", query="", fragment="").geturl()
    result = {}
    try:
        async with websockets.connect(uri, open_timeout=5, close_timeout=2, max_size=4 * 1024 * 1024, proxy=None) as ws:
            if json.loads(await asyncio.wait_for(ws.recv(), 5)).get("type") != "auth_required":
                return result
            await ws.send(json.dumps({"type":"auth", "access_token":token}))
            if json.loads(await asyncio.wait_for(ws.recv(), 5)).get("type") != "auth_ok":
                return result
            for num, entity_id in enumerate(entity_ids[:200], 1):
                await ws.send(json.dumps({"id":num, "type":"update/release_notes", "entity_id":entity_id}))
                response = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if response.get("id") == num and response.get("success") and isinstance(response.get("result"), str):
                    result[entity_id] = response["result"]
    except Exception:
        # Older HA / integration versions may not support release notes.
        return result
    return result


def ha_records(states: list[JSON], notes: dict[str, str], existing: dict[str, AppRecord]) -> tuple[list[AppRecord], list[Release]]:
    apps, releases = [], []
    for item in states:
        attr = item["attributes"]
        entity_id = item["entity_id"]
        key = "ha:" + entity_id
        title = str(attr.get("title") or attr.get("friendly_name") or entity_id)
        installed = str(attr["installed_version"]) if attr.get("installed_version") is not None else None
        latest = str(attr["latest_version"]) if attr.get("latest_version") is not None else None
        url = safe_url(attr.get("release_url"))
        repo = existing[key].upstream_repo if key in existing and existing[key].upstream_repo else github_repo(url)
        if not repo and re.search(r"home assistant core", title, re.I):
            repo = "home-assistant/core"
        if not repo and re.search(r"home assistant (?:operating system|os)", title, re.I):
            repo = "home-assistant/operating-system"
        profile = existing[key].profile if key in existing else inferred_profile(title)
        apps.append(AppRecord(id=key, name=title, source="home_assistant", installed_version=installed, latest_version=latest, update_available=item.get("state")=="on" if item.get("state") in ("on", "off") else None, upstream_repo=repo, icon=safe_url(attr.get("entity_picture")), profile=profile, detected={"entity_id":entity_id}, error="Entity unavailable" if item.get("state") in ("unavailable", "unknown") else None))
        text = notes.get(entity_id) or attr.get("release_summary") or ""
        if latest and text:
            # HA update states do not include publication date. Do not start an
            # invented soak clock from state.last_changed or poll time.
            releases.append(Release(id=f"ha:{entity_id}:{latest}", app_id=key, version=latest, notes=str(text), source_url=url or ""))
    return apps, releases


def installed_unraid(path: str) -> str | None:
    try:
        content = Path(path).read_text()[:2048]
        match = re.search(r'(?:version\s*=\s*)?["\']?(\d+\.\d+(?:\.\d+)?(?:[-.][a-zA-Z0-9]+)*)', content)
        return match.group(1) if match else None
    except OSError:
        return None


async def collect(settings: Settings, *, github_token: str | None = None, ha_token: str | None = None, ha_url: str = "", docker_socket: str = "/var/run/docker.sock", templates_dir: str = "/host/templates", unraid_version_file: str = "/host/unraid-version", cache_get: Callable | None = None, cache_set: Callable | None = None, existing_apps: list[AppRecord] | None = None) -> tuple[list[AppRecord], list[Release], list[JSON]]:
    existing = {a.id:a for a in existing_apps or []}
    apps: list[AppRecord] = []
    releases: list[Release] = []
    errors: list[JSON] = []
    def error(source: str, message: str):
        errors.append({"source":source, "message":message})
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=True, headers={"User-Agent":"Ripe/0.1 homelab update tracker"}) as public, httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as local:
        cached = CachedHTTP(public, cache_get, cache_set)
        registry = RegistryReader(public, cache_get, cache_set)
        if Path(docker_socket).exists():
            try:
                transport = httpx.AsyncHTTPTransport(uds=docker_socket)
                async with httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=15, trust_env=False) as docker:
                    reader = DockerReader(docker, host=urlparse(settings.dashboard_url).hostname or "localhost")
                    apps.extend(await reader.apps(read_templates(templates_dir), existing))
                    for skipped in reader.skipped:
                        error(skipped, "Docker: container record parse error")
            except SourceError as exc:
                error("docker", str(exc))
            except PARSE_ERRORS:
                error("docker", "Docker: parse error")
        else:
            error("docker", "Docker socket not mounted; mount read-only to enable discovery")
        if ha_token and ha_url:
            try:
                states = await ha_states(local, ha_url, ha_token)
                notes = await ha_release_notes(ha_url, ha_token, [e["entity_id"] for e in states if e.get("state")=="on"])
                for state in states:
                    try:
                        ha_apps, ha_notes = ha_records([state], notes, existing)
                    except PARSE_ERRORS:
                        error("ha:" + str(state.get("entity_id")), "Home Assistant: entity parse error")
                        continue
                    apps.extend(ha_apps)
                    releases.extend(ha_notes)
            except SourceError as exc:
                error("home_assistant", str(exc))
            except PARSE_ERRORS:
                error("home_assistant", "Home Assistant: parse error")
        else:
            error("home_assistant", "Home Assistant connection is not configured; open Ripe Settings to enable discovery")
        unraid = AppRecord(id="unraid:os", name="Unraid OS", source="unraid", installed_version=installed_unraid(unraid_version_file), upstream_repo=None, profile=existing["unraid:os"].profile if "unraid:os" in existing else Profile(tier="infrastructure", exposure="lan"))
        if not unraid.installed_version:
            unraid.error = "Unraid version file not mounted or version unrecognised"
            error("unraid", unraid.error)
        # Official Unraid-maintained documentation supplies versioned notes.
        try:
            os_releases = await unraid_releases(cached, unraid.installed_version, github_token)
            releases.extend(os_releases)
            if latest := latest_stable(os_releases):
                unraid.latest_version = latest.version
        except SourceError as exc:
            unraid.error = str(exc)
            error("unraid", str(exc))
        except PARSE_ERRORS:
            unraid.error = "Unraid release notes: parse error"
            error("unraid", unraid.error)
        apps.append(unraid)
        # Pagination can stop at an installed version's historical base. A
        # repository-only cache would silently give older installations the
        # shorter span fetched for a newer installation of the same app.
        upstream_cache: dict[tuple[str, str | None], list[Release] | str] = {}
        for app in apps:
            if app.source == "docker" and app.image and is_local_image(app.image, app.installed_digest, bool(existing.get(app.id) and existing[app.id].latest_digest)):
                # Never pulled from a registry: nothing to look up, and not an error.
                app.detected["local_build"] = "local build (not tracked)"
            elif app.source == "docker" and app.image:
                try:
                    metadata = await registry.metadata(app.image, app.installed_digest, match_version=not bool(app.installed_version))
                    app.latest_digest = metadata.get("latest_digest")
                    app.latest_only = bool(metadata.get("latest_only")) and not metadata.get("truncated_tags")
                    app.update_available = metadata.get("update_available")
                    if not app.installed_version:
                        app.installed_version = metadata.get("installed_version")
                    if app.installed_version and app.image and any(v in app.image for v in ("lscr.io/linuxserver/", "linuxserver/")):
                        match = re.fullmatch(r"(?:version-)?(v?\d+\.\d+(?:\.\d+)?(?:[.\d]*))(?:(?:-ls|build)\d+)?", app.installed_version)
                        if match:
                            app.installed_version = match.group(1)
                            app.detected["version_mapping"] = "LinuxServer upstream version tag"
                    previous = existing.get(app.id)
                    if app.latest_digest and previous and previous.latest_digest == app.latest_digest:
                        app.image_changed_at = previous.image_changed_at
                    elif app.latest_digest and app.update_available:
                        app.image_changed_at = utcnow()
                        app.detected["image_change_date_kind"] = "first_observed"
                    if metadata.get("truncated_tags"):
                        error(app.id, "Registry tag list is incomplete; latest-only status is unknown")
                except RateLimited:
                    # Keep the last known registry data and retry on the next poll.
                    previous = existing.get(app.id)
                    # Only reuse it when this is still the same installation.
                    if previous and previous.image == app.image and previous.installed_digest == app.installed_digest:
                        app.latest_digest, app.latest_only = previous.latest_digest, previous.latest_only
                        app.update_available, app.image_changed_at = previous.update_available, previous.image_changed_at
                        app.installed_version = app.installed_version or previous.installed_version
                    app.detected["registry_status"] = "Registry rate limited; last known data kept, retrying next poll"
                except SourceError as exc:
                    error(app.id, str(exc))
                except PARSE_ERRORS:
                    error(app.id, "Registry: parse error")
            flagged = (existing[app.id].detected.get("invalid_mapping") if app.id in existing else None) or {}
            if app.upstream_repo and str(flagged.get("repo", "")).lower() == app.upstream_repo.lower() \
                    and flagged.get("token") == bool(github_token) and time.time() - float(flagged.get("at") or 0) < MAPPING_RETRY_SECONDS:
                # Known to 404: keep the flag until the mapping or token changes, or the retry window passes.
                app.detected["invalid_mapping"] = flagged
                app.error = MAPPING_ERROR
            elif app.upstream_repo:
                cache_key = (app.upstream_repo, app.installed_version)
                try:
                    if cache_key not in upstream_cache:
                        upstream_cache[cache_key] = await github_releases(cached, app.upstream_repo, app.id, github_token, app.installed_version)
                    upstream = upstream_cache[cache_key]
                    if isinstance(upstream, SourceError):
                        raise upstream
                    if isinstance(upstream, str):
                        raise SourceError(upstream)
                    app_releases = [r.model_copy(update={"app_id":app.id}) for r in upstream]
                    # GitHub's list is ordered by published_at, not semantic version.
                    # For pinned/LTS/CalVer images, the highest final release is the
                    # best public upstream candidate; channel differences remain visible.
                    if latest := latest_stable(app_releases):
                        if app.source != "home_assistant" or not app.latest_version:
                            app.latest_version = latest.version
                    # Prefer GitHub's dated full release over HA's undated summary.
                    releases[:] = [r for r in releases if not (r.app_id==app.id and any(r.version==g.version or compare_versions(r.version, g.version)==0 for g in app_releases))]
                    releases.extend(app_releases)
                except SourceNotFound as exc:
                    upstream_cache[cache_key] = exc
                    app.detected["invalid_mapping"] = {"repo": app.upstream_repo, "status": 404, "at": time.time(), "token": bool(github_token), "suggestion": mapping_suggestion(app)}
                    app.error = MAPPING_ERROR
                    error(app.id, f"GitHub: {app.upstream_repo} not found (HTTP 404); mapping needs attention")
                except SourceError as exc:
                    upstream_cache[cache_key] = str(exc)
                    app.error = str(exc)
                    error(app.id, str(exc))
                except PARSE_ERRORS:
                    upstream_cache[cache_key] = "GitHub: parse error"
                    app.error = "GitHub: parse error"
                    error(app.id, "GitHub: parse error")
            elif app.source == "docker":
                # A missing mapping is a hint, not a source failure: the digest
                # logic still gives a valid status for unmapped :latest images.
                app.detected["mapping_hint"] = "No upstream repository mapping; choose one in the app profile"
            if not app.installed_version and app.image_created_at:
                app.detected["version_fallback"] = "unknown (image date " + app.image_created_at.date().isoformat() + ")"
    return apps, releases, errors
