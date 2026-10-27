"""Runtime inputs. Secrets never enter general Settings; HA also supports private app credentials."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlsplit
from .models import Settings


def system_timezone() -> str:
    choices = [os.getenv('TZ', '')]
    try:
        choices.append(Path('/etc/timezone').read_text().strip())
    except OSError:
        pass
    try:
        zonepath = str(Path('/etc/localtime').resolve())
        if '/zoneinfo/' in zonepath:
            choices.append(zonepath.split('/zoneinfo/', 1)[1])
    except OSError:
        pass
    for value in choices + ['UTC']:
        try:
            ZoneInfo(value)
            return value
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return 'UTC'


def checked_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Use an http(s) URL with a hostname')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('URLs must not contain credentials, query strings or fragments')
    return value.rstrip('/')


def optional_url(value: str) -> str:
    return checked_url(value) if value.strip() else ''


@dataclass(repr=False)
class Runtime:
    config_dir: Path = field(default_factory=lambda: Path(os.getenv('RIPE_CONFIG_DIR', '/config')))
    ha_url: str = field(default_factory=lambda: optional_url(os.getenv('HA_URL', '')))
    ha_token: str | None = field(default_factory=lambda: os.getenv('HA_TOKEN') or None, repr=False)
    github_token: str | None = field(default_factory=lambda: os.getenv('GITHUB_TOKEN') or None, repr=False)
    gemini_key: str | None = field(default_factory=lambda: os.getenv('GEMINI_API_KEY') or None, repr=False)
    docker_socket: str = field(default_factory=lambda: os.getenv('DOCKER_SOCKET', '/var/run/docker.sock'))
    templates_dir: str = field(default_factory=lambda: os.getenv('UNRAID_TEMPLATES_DIR', '/host/templates'))
    unraid_version_file: str = field(default_factory=lambda: os.getenv('UNRAID_VERSION_FILE', '/host/unraid-version'))
    demo: bool = field(default_factory=lambda: os.getenv('RIPE_DEMO_MODE', '0') == '1')
    disable_scheduler: bool = field(default_factory=lambda: os.getenv('RIPE_DISABLE_SCHEDULER', '0') == '1')

    def initial_settings(self) -> Settings:
        return Settings(
            timezone=system_timezone(),
            ollama_url=optional_url(os.getenv('OLLAMA_URL', '')),
            local_model=os.getenv('LOCAL_MODEL', 'qwen3:8b'),
            gemini_model=os.getenv('GEMINI_MODEL', 'gemini-2.5-flash'),
            dashboard_url=checked_url(os.getenv('DASHBOARD_URL', 'http://localhost:8087')),
        )

    def secret_values(self) -> list[str]:
        return [value for value in (self.ha_token, self.github_token, self.gemini_key) if value]
