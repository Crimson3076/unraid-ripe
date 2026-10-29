"""Shared persistent records. No Stage 1 model contains an update/exec action."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

Tier = Literal["infrastructure", "internet", "general"]

class Profile(BaseModel):
    tier: Tier = "general"
    exposure: Literal["lan", "internet", "vpn", "internal", "unknown"] = "unknown"
    own_login: bool | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    notes: str = ""
    # Per field: "auto" (derived by Ripe, refreshed every scan) or "user" (edited, never overwritten).
    origin: dict[str, str] = Field(default_factory=dict)

class AppRecord(BaseModel):
    id: str
    name: str
    source: Literal["docker", "home_assistant", "unraid", "demo"]
    installed_version: str | None = None
    latest_version: str | None = None
    image: str | None = None
    installed_digest: str | None = None
    latest_digest: str | None = None
    image_created_at: datetime | None = None
    image_changed_at: datetime | None = None
    latest_only: bool = False
    update_available: bool | None = None
    upstream_repo: str | None = None
    icon: str | None = None
    web_url: str | None = None
    profile: Profile = Field(default_factory=Profile)
    detected: dict[str, Any] = Field(default_factory=dict)
    last_seen: datetime = Field(default_factory=utcnow)
    error: str | None = None
    # Docker state ("running", "exited", ...); None for sources that have no such notion.
    state: str | None = None
    # Home Assistant grouping on the Overview: "firmware" or "cards" (HACS cards and integrations).
    section: Literal["firmware", "cards"] | None = None
    # A container that only supports another one (database, cache) points at its parent's id.
    parent_id: str | None = None
    # A Home Assistant entity folded into another one that tracks the same repository.
    merged_into: str | None = None
    ignored: bool = False
    # Latest version (or digest) the user chose to hide; shown again once something newer exists.
    ignored_version: str | None = None

class Release(BaseModel):
    id: str
    app_id: str
    version: str
    published_at: datetime | None = None
    prerelease: bool = False
    notes: str = ""
    source_url: str = ""
    classification: dict[str, Any] | None = None
    applicability: list[dict[str, Any]] = Field(default_factory=list)
    model_disagreement: bool = False
    classification_key: str | None = None

class Settings(BaseModel):
    timezone: str = "UTC"
    soak_days: dict[str, int] = Field(default_factory=lambda: {"infrastructure":21,"internet":14,"general":14})
    patch_soak_days: int = Field(default=3, ge=0, le=365)
    patch_exception: bool = True
    quiet_start: str = "22:00"
    quiet_end: str = "09:00"
    poll_hours: int = Field(default=12, ge=1, le=168)
    analysis_cap: int = Field(default=15, ge=1, le=500)
    digest_weekday: int = Field(default=0, ge=0, le=6)
    digest_time: str = "09:00"
    notification_script: str = ""
    notification_channel: str = ""
    notification_people: list[str] = Field(default_factory=list)
    notifications_enabled: bool = False
    dashboard_url: str = "http://localhost:8087"
    ollama_url: str = ""
    local_model: str = "qwen3:8b"
    gemini_model: str = "gemini-2.5-flash"
    classification_provider: Literal["local", "gemini"] = "local"
    second_opinion: bool = False
    demo_mode: bool = False
    include_stopped: bool = False
    # Optional read-only internet-exposure sources; an empty path leaves them off.
    npm_database_path: str = ""
    cloudflared_config_path: str = ""

class AppView(BaseModel):
    app: AppRecord
    status: str
    status_label: str
    recommended_version: str | None = None
    latest_version: str | None = None
    days_remaining: int | None = None
    releases_behind: int = 0
    tags: list[str] = Field(default_factory=list)
    hard_signals: list[str] = Field(default_factory=list)
    pending: list[Release] = Field(default_factory=list)
    history: list[Release] = Field(default_factory=list)
    available_notes: list[Release] = Field(default_factory=list)
    no_notes: list[str] = Field(default_factory=list)
    rollup: dict[str, Any] | None = None
    reason: str = ""
    next_ripe_at: datetime | None = None
    sidecars: list["AppView"] = Field(default_factory=list)


def stopped(app: AppRecord) -> bool:
    return app.state is not None and app.state.lower() in ("exited", "created", "dead")


def hidden(app: AppRecord, settings: Settings, apps: dict[str, AppRecord]) -> bool:
    """Not polled, not analysed and not listed: ignored, merged, stopped (unless included), or a sidecar of such an app."""
    if app.ignored or app.merged_into or (stopped(app) and not settings.include_stopped):
        return True
    parent = apps.get(app.parent_id) if app.parent_id else None
    return bool(parent and (parent.ignored or parent.merged_into or (stopped(parent) and not settings.include_stopped)))
