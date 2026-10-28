"""Profile fields derived from what Docker (and optional proxy configs) already say.

Every value comes with the evidence it was read from; nothing is guessed. Env values are
read only for an allowlist of harmless names, never for anything credential-shaped, and
neither they nor probe results are ever part of a public (Gemini) payload.
"""
from __future__ import annotations

import ipaddress
import re
import sqlite3
import json
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

from ripe.models import AppRecord, Profile

AUTO_FIELDS = ("tier", "exposure", "own_login", "dependencies", "config")
INFRA = re.compile(r"unraid|home.?assistant|adguard|nginx.?proxy.?manager|cloudflar|wireguard|mariadb|mysql|postgres|redis|mongo|pihole|pi.hole|traefik|caddy", re.I)
DATASTORE = re.compile(r"mariadb|mysql|postgres|redis|mongo|influx|memcache|valkey", re.I)
ENV_ALLOW = re.compile(r"(?:.+_(?:HOST|URL|PORT|VERSION)|TZ)", re.I)
ENV_DENY = re.compile(r"TOKEN|KEY|PASS|SECRET|AUTH", re.I)
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
LOGIN_PATH = re.compile(r"log[-_]?in|sign[-_]?in|sso|oauth|/auth\b|/account", re.I)
PASSWORD_FIELD = re.compile(rb"<input[^>]+type\s*=\s*[\"']?password", re.I)
DB_MOUNT = re.compile(r"mysql|mariadb|postgres|pgdata|redis|mongo|sqlite|/db\b|\.db$", re.I)
CONFIG_MOUNT = re.compile(r"^/(?:config|etc|conf)(?:/|$)|/config(?:/|$)", re.I)
SHARED_NETWORK_MAX = 4  # a network shared by many containers says nothing about who needs whom
Prober = Callable[[str], Awaitable[tuple[bool | None, str]]]


def split_env(env: Any) -> tuple[list[str], dict[str, str]]:
    """Return every variable name, plus values for allowlisted harmless names only."""
    names: list[str] = []
    values: dict[str, str] = {}
    for entry in env if isinstance(env, list) else []:
        if not isinstance(entry, str) or "=" not in entry:
            continue
        name, value = entry.split("=", 1)
        if not ENV_NAME.fullmatch(name):
            continue
        names.append(name)
        if ENV_ALLOW.fullmatch(name) and not ENV_DENY.search(name):
            values[name] = value
    return names, values


def storable(value: str) -> bool:
    """A value safe to persist: short, single-token and carrying no URL credentials."""
    return len(value) <= 200 and "@" not in value and not re.search(r"\s", value)


def host_of(value: str) -> str:
    value = value.strip()
    try:
        if "://" in value:
            return (urlparse(value).hostname or "").lower()
    except ValueError:
        return ""
    return value.split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0].lower()


def mount_role(destination: str) -> str:
    if DB_MOUNT.search(destination):
        return "database"
    return "config" if CONFIG_MOUNT.search(destination) else "data"


def classify_ip(ip: str) -> str | None:
    ip = (ip or "").strip("[]")
    if ip in ("", "0.0.0.0", "::"):
        return "all"
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.is_loopback:
        return "loopback"
    return "lan" if addr.is_private or addr.is_link_local else "public"


def read_facts(item: dict, info: dict, inspect: dict) -> dict:
    """Plain facts about one container; never retains env values beyond the allowlist."""
    container = inspect.get("Config") if isinstance(inspect.get("Config"), dict) else {}
    image_config = info.get("Config") if isinstance(info.get("Config"), dict) else {}
    host_config = inspect.get("HostConfig") if isinstance(inspect.get("HostConfig"), dict) else {}
    names, values = split_env(container.get("Env"))
    _, image_env = split_env(image_config.get("Env"))
    versions = {k.lower(): v for k, v in {**image_env, **values}.items() if k.upper().endswith("_VERSION") and storable(v)}
    ports = item.get("Ports")
    networks = (item.get("NetworkSettings") or {}).get("Networks") if isinstance(item.get("NetworkSettings"), dict) else None
    networks = networks if isinstance(networks, dict) else {}
    item_host = item.get("HostConfig") if isinstance(item.get("HostConfig"), dict) else {}
    mode = host_config.get("NetworkMode") or item_host.get("NetworkMode") or ""
    bindings, private_ports = [], set()
    for p in ports if isinstance(ports, list) else []:
        if isinstance(p, dict):
            if isinstance(p.get("PrivatePort"), int):
                private_ports.add(p["PrivatePort"])
            if isinstance(p.get("PublicPort"), int):
                bindings.append((str(p.get("IP") or ""), p["PublicPort"]))
    exposed = image_config.get("ExposedPorts")
    links = [str(link).split(":", 1)[0].strip("/") for link in host_config.get("Links") or [] if isinstance(link, str)]
    return {
        "readable": isinstance(ports, list) or bool(mode),
        "bindings": bindings, "private_ports": private_ports,
        "network_mode": mode if isinstance(mode, str) else "",
        "networks": {str(k): str((v or {}).get("IPAddress") or "") for k, v in networks.items() if isinstance(v, dict)},
        "links": [link for link in links if link],
        "env_names": names, "env_values": values, "versions": versions,
        "restart": str(((host_config.get("RestartPolicy") or {}).get("Name")) or ""),
        "healthcheck": bool(container.get("Healthcheck") or image_config.get("Healthcheck")),
        "mounts": sorted({mount_role(str(m.get("Destination", ""))) for m in item.get("Mounts") or [] if isinstance(m, dict)}),
        "exposed_ports": sorted(int(k.split("/")[0]) for k in exposed if isinstance(k, str) and k.split("/")[0].isdigit())[:30] if isinstance(exposed, dict) else [],
    }


def exposure_of(facts: dict) -> tuple[str, str]:
    if facts.get("network_mode") == "host":
        return "lan", "host networking: every port is on the server's LAN address"
    kinds = {classify_ip(ip): (ip, port) for ip, port in facts.get("bindings", [])}
    if "public" in kinds:
        return "internet", f"port {kinds['public'][1]} is published on a public address"
    if "all" in kinds or "lan" in kinds:
        ip, port = kinds.get("lan") or kinds["all"]
        return "lan", f"port {port} is published on " + ("a LAN address" if "lan" in kinds else "all interfaces")
    if "loopback" in kinds:
        return "internal", "ports are published on loopback only"
    if facts.get("readable"):
        return "internal", "no published ports"
    return "unknown", "nothing readable from Docker"


def config_of(facts: dict) -> dict:
    cfg: dict[str, Any] = {}
    env = {k.lower(): v for k, v in facts["env_values"].items() if storable(v) and not k.upper().endswith("_VERSION")}
    if env:
        cfg["env"] = dict(sorted(env.items())[:30])
    if facts["versions"]:
        cfg["versions"] = dict(sorted(facts["versions"].items())[:10])
    if facts["env_names"]:
        cfg["env_names"] = sorted({n.lower() for n in facts["env_names"]})[:60]
    if facts["restart"]:
        cfg["restart_policy"] = facts["restart"]
    cfg["healthcheck"] = facts["healthcheck"]
    if facts["mounts"]:
        cfg["mounts"] = facts["mounts"]
    if facts["exposed_ports"]:
        cfg["exposed_ports"] = facts["exposed_ports"]
    return cfg


def dependencies_of(app: AppRecord, facts: dict, all_facts: dict[str, dict], by_name: dict[str, AppRecord]) -> tuple[list[str], list[str]]:
    deps: dict[str, str] = {}
    for name, value in sorted(facts["env_values"].items()):
        target = by_name.get(host_of(value))
        if target and target.id != app.id:
            deps.setdefault(target.name, f"{name} points to container {target.name}")
    for link in facts["links"]:
        target = by_name.get(link.lower())
        if target and target.id != app.id:
            deps.setdefault(target.name, f"linked to container {target.name}")
    own = {n for n in facts["networks"] if n not in ("bridge", "host", "none")}
    for other in by_name.values():
        other_facts = all_facts.get(other.id)
        if not other_facts or other.id == app.id or other.name in deps or DATASTORE.search(app.name + " " + (app.image or "")):
            continue
        if not DATASTORE.search(other.name + " " + (other.image or "")):
            continue
        for network in own & set(other_facts["networks"]):
            members = [i for i, f in all_facts.items() if network in f["networks"]]
            if len(members) <= SHARED_NETWORK_MAX:
                deps[other.name] = f"shares the small network {network} with the datastore {other.name}"
                break
    return sorted(deps), [deps[k] for k in sorted(deps)]


def tier_of(app: AppRecord) -> tuple[str | None, str]:
    match = INFRA.search(app.name + " " + (app.image or ""))
    return ("infrastructure", f"name or image matches '{match.group(0).lower()}'") if match else (None, "")


async def derive(apps: list[AppRecord], facts: dict[str, dict], proxy_targets: list[dict], prober: Prober | None = None, host: str = "") -> None:
    """Fill each Docker app's profile and record the evidence under detected['profile_evidence']."""
    by_name = {a.name.lower(): a for a in apps if a.id in facts}
    dependents: dict[str, list[str]] = {}
    for app in apps:
        f = facts.get(app.id)
        if not f:
            continue
        evidence = app.detected.setdefault("profile_evidence", {})
        exposure, reason = exposure_of(f)
        hit = proxy_hit(app, f, proxy_targets, host)
        if hit:
            exposure, reason = "internet", hit
        evidence["exposure"] = reason
        deps, dep_reasons = dependencies_of(app, f, facts, by_name)
        for dep in deps:
            dependents.setdefault(dep, []).append(app.name)
        if deps:
            evidence["dependencies"] = "; ".join(dep_reasons)
        tier, tier_reason = tier_of(app)
        if not tier and exposure == "internet":
            tier, tier_reason = "internet", reason
        own_login = None
        if prober and app.web_url:
            own_login, login_reason = await prober(app.web_url)
            if own_login is not None:
                evidence["own_login"] = login_reason
        if tier:
            evidence["tier"] = tier_reason
        app.profile = Profile(tier=tier or "general", exposure=exposure, own_login=own_login, dependencies=deps, config=config_of(f), origin={k: "auto" for k in AUTO_FIELDS})
        evidence["config"] = "read from container settings: allowlisted env values, restart policy, healthcheck, mount roles, exposed ports"
    for app in apps:
        users = sorted(set(dependents.get(app.name, [])))
        if users and app.id in facts and app.profile.tier != "infrastructure":
            app.profile.tier = "infrastructure"
            app.detected["profile_evidence"]["tier"] = "required by " + ", ".join(users[:5])


def proxy_hit(app: AppRecord, facts: dict, targets: list[dict], host: str) -> str | None:
    addresses = {ip for ip in facts["networks"].values() if ip}
    ports = facts["private_ports"] | {port for _, port in facts["bindings"]}
    for target in targets:
        name = str(target.get("host", "")).lower()
        port = target.get("port")
        local = name == app.name.lower() or name in addresses or (bool(host) and name == host.lower())
        if local and (port is None or port in ports):
            return f"{target['via']} forwards {target.get('domain') or 'a public hostname'} to this container"
    return None


def read_npm_targets(path: str) -> list[dict]:
    """Nginx Proxy Manager proxy hosts, read straight from its SQLite file (read-only)."""
    if not path or not Path(path).is_file():
        return []
    for uri in (f"file:{path}?mode=ro", f"file:{path}?mode=ro&immutable=1"):
        try:
            db = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                rows = db.execute("SELECT domain_names, forward_host, forward_port FROM proxy_host WHERE enabled=1 AND is_deleted=0 LIMIT 1000").fetchall()
            finally:
                db.close()
        except sqlite3.Error:
            continue
        targets = []
        for domains, forward_host, forward_port in rows:
            try:
                names = json.loads(domains) if isinstance(domains, str) else []
            except ValueError:
                names = []
            targets.append({"via": "Nginx Proxy Manager", "host": str(forward_host or ""), "port": int(forward_port) if str(forward_port).isdigit() else None,
                            "domain": str(names[0]) if names and isinstance(names[0], str) else ""})
        return targets
    return []


def read_cloudflared_targets(path: str) -> list[dict]:
    """cloudflared ingress rules (hostname + service) from its config file."""
    try:
        if not path or not Path(path).is_file() or Path(path).stat().st_size > 256 * 1024:
            return []
        text = Path(path).read_text(errors="ignore")
    except OSError:
        return []
    targets, hostname = [], ""
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        if m := re.match(r"\s*-?\s*hostname:\s*[\"']?([^\s\"']+)", line):
            hostname = m.group(1)
        if m := re.match(r"\s*-?\s*service:\s*[\"']?(?:https?|tcp|ssh|rdp)://([^\s:/\"']+)(?::(\d+))?", line):
            targets.append({"via": "cloudflared tunnel", "host": m.group(1), "port": int(m.group(2)) if m.group(2) else None, "domain": hostname})
            hostname = ""
    return targets


def is_local_host(host: str | None) -> bool:
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback or addr.is_private
    except ValueError:
        return "." not in host or host.endswith((".local", ".lan"))


async def probe_login(client, url: str) -> tuple[bool | None, str]:
    """One GET of a local web UI. Only a positive login signal is reported; the body is never kept."""
    for _ in range(4):
        parsed = urlparse(url)
        if not is_local_host(parsed.hostname) or parsed.scheme not in ("http", "https"):
            return None, ""
        try:
            async with client.stream("GET", url, headers={"Accept": "text/html"}, timeout=5) as response:
                if response.status_code in (401, 407):
                    return True, f"web UI answers HTTP {response.status_code} (authentication required)"
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location", "")
                    if LOGIN_PATH.search(location):
                        return True, "web UI redirects to a login page"
                    target = urlparse(urljoin(url, location))
                    if not location or target.hostname != parsed.hostname:
                        return None, ""
                    url = target.geturl()
                    continue
                if response.status_code == 200:
                    body = b""
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > 262144:
                            break
                    return (True, "web UI shows a password field") if PASSWORD_FIELD.search(body) else (None, "")
                return None, ""
        except Exception:
            return None, ""
    return None, ""


def merge_profile(auto: Profile, evidence: dict[str, str], stored: Profile | None) -> tuple[Profile, dict[str, str]]:
    """Auto values fill every field the user has not edited; user values always win."""
    fresh = {f: getattr(auto, f) for f in AUTO_FIELDS}
    if stored is None:
        return auto.model_copy(update={"origin": {f: "auto" for f in AUTO_FIELDS}}), evidence
    # A profile saved before origins existed was edited by hand as a whole.
    origin = dict(stored.origin) or {f: "user" for f in AUTO_FIELDS}
    update = {f: fresh[f] for f in AUTO_FIELDS if origin.get(f, "auto") != "user"}
    origin = {f: origin.get(f, "auto") for f in AUTO_FIELDS}
    return stored.model_copy(update={**update, "origin": origin}), {k: v for k, v in evidence.items() if k not in AUTO_FIELDS or origin.get(k) != "user"}


def edited_origin(current: Profile, new: Profile) -> dict[str, str]:
    """Fields the user changed become 'user'; untouched ones keep their origin."""
    origin = dict(current.origin) or {f: "user" for f in AUTO_FIELDS}
    return {f: "user" if getattr(new, f) != getattr(current, f) else origin.get(f, "auto") for f in AUTO_FIELDS}
