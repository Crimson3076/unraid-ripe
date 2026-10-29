"""Which releases are worth a model call, and in what order."""
from __future__ import annotations
import re
from datetime import datetime, timezone
from .models import AppRecord, Release
from .versions import compare_versions, parse_version

MIN_NOTE_CHARS = 40
_LINK = re.compile(r"https?://\S+|\[[^\]]*\]\([^)]*\)")
_SECURITY = re.compile(r"\bsecurity\b|\bCVE-[\w-]+|\bvulnerab\w*|\bexploit\w*", re.I)
_BOILERPLATE = re.compile(r"full\s+changelog|compare|changes|what'?s\s+changed", re.I)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def is_pending(release: Release, app: AppRecord) -> bool:
    """Newer than the installed version; when that is unknown, only releases since the image was built."""
    if parse_version(app.installed_version) is not None:
        return compare_versions(release.version, app.installed_version) == 1
    if app.image_created_at is None or release.published_at is None:
        return False
    return _aware(release.published_at) > _aware(app.image_created_at)


def trivial_notes(notes: str) -> bool:
    """Only a 'Full Changelog' link, or too little text to classify."""
    text = _BOILERPLATE.sub("", _LINK.sub("", notes or ""))
    text = re.sub(r"[\W_]+", " ", text).strip()
    return len(text) < MIN_NOTE_CHARS


def newest_first(releases: list[Release]) -> list[Release]:
    known = [r for r in releases if parse_version(r.version) is not None]
    unknown = [r for r in releases if parse_version(r.version) is None]
    known.sort(key=lambda r: (parse_version(r.version), r.published_at and _aware(r.published_at).timestamp() or 0), reverse=True)
    unknown.sort(key=lambda r: r.published_at and _aware(r.published_at).timestamp() or 0, reverse=True)
    return known + unknown


def priority(app: AppRecord, release: Release, rank: int) -> tuple[int, int, int]:
    """Infrastructure/internet apps first, then security wording, then newest (rank 0 = newest of its app)."""
    exposed = app.profile.tier in ('infrastructure', 'internet') or app.profile.exposure == 'internet'
    return (0 if exposed else 1, 0 if _SECURITY.search(release.notes or "") else 1, rank)
