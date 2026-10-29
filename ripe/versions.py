"""Conservative version comparison, including common homelab release tags.

Unknown schemes never get a guessed lexical order. LinuxServer's packaging
suffix is stripped because upstream release notes refer to the app version.
"""
from __future__ import annotations

import re
from packaging.version import InvalidVersion, Version

_PREFIX = re.compile(r"^(?:version[-_]|release[-_]|v(?=\d))", re.I)
_LINUXSERVER = re.compile(r"(?:[-_](?:ls|linuxserver)\d+)$", re.I)
# Monorepo tags such as "n8n@1.2.3" or "@scope/pkg@1.2.3" carry the package name before the version.
_PACKAGE = re.compile(r"^(?:@?[\w.-]+(?:/[\w.-]+)?)@(?=v?\d)")
# Segments exclude '-' and '_' so each separator can only match one way (no catastrophic backtracking).
# Image build flavours that follow the app version: -python3.13-alpine3.23, -r0-ls237, -bookworm-slim.
_BUILD = re.compile(r"(?:[-_](?:py(?:thon)?\d[\d.]*|alpine[\d.]*|debian[\d.]*|ubuntu[\d.]*|bookworm|bullseye|trixie|jammy|noble|slim|r\d+|(?:ls|linuxserver)\d+))+$", re.I)
_MAX_TAG = 128
_DATE = re.compile(r"^(?:RELEASE[.-])?(\d{4})[-.](\d{2})[-.](\d{2})(?:[Tt](\d{2})[-:](\d{2})[-:](\d{2})[Zz]?)?$")


def normalize_version(value: str | None) -> str | None:
    if not value:
        return None
    text = _PACKAGE.sub("", value.strip())
    text = _PREFIX.sub("", text)
    if len(text) <= _MAX_TAG:  # real tags are short; this bounds the unanchored scan
        text = _BUILD.sub("", text)
    text = _LINUXSERVER.sub("", text)
    date = _DATE.fullmatch(text)
    if date:
        text = ".".join(str(int(part)) for part in date.groups() if part is not None)
    if not re.match(r"^\d", text):
        return None
    try:
        return str(Version(text))
    except InvalidVersion:
        return None


def parse_version(value: str | None) -> Version | None:
    normalized = normalize_version(value)
    return Version(normalized) if normalized is not None else None


def compare_versions(a: str | None, b: str | None) -> int | None:
    """Return -1/0/1, or None when either version cannot be interpreted."""
    left, right = parse_version(a), parse_version(b)
    if left is None or right is None:
        return None
    return (left > right) - (left < right)


def version_parts(value: str | None) -> tuple[int, ...] | None:
    version = parse_version(value)
    return version.release if version else None


def semver_major_bump(installed: str | None, candidate: str | None) -> bool:
    old, new = version_parts(installed), version_parts(candidate)
    # Calendar release schemes (e.g. HA 2026.10.3) do not promise semver.
    if not old or not new or old[0] >= 1900 or new[0] >= 1900:
        return False
    return new[0] > old[0]
