"""Evidence-driven update recommendations. This module never performs updates.

A full soak requires a quiet window: a release superseded before its deadline
never becomes ripe just by growing old. A release that already completed its
quiet window stays a viable older target. Every release retains its own clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re
from typing import Any

from ripe.models import AppRecord, AppView, Release, Settings, utcnow
from ripe.versions import compare_versions, parse_version, semver_major_bump, version_parts

_TAG_ORDER = {"security": 0, "breaking": 1, "migration": 2, "deprecation": 3,
              "big_feature": 4, "bugfix": 5, "performance": 6, "docs_only": 7}
_STATUS_ORDER = {"recommended_now": 0, "read_first": 1, "unknown": 2,
                 "waiting": 3, "ripe": 4, "up_to_date": 5}
_TIER_ORDER = {"infrastructure": 0, "internet": 1, "general": 2}
_DATABASE = re.compile(r"(?:^|[\W_])(?:mariadb|mysql|postgres(?:ql)?|redis|mongo(?:db)?)(?:$|[\W_])", re.I)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _classification(release: Release) -> dict[str, Any]:
    result = release.classification or {}
    return {} if result.get("status") == "unknown" else result


def _applicable(release: Release, kind: str, index: int) -> bool:
    """Only explicit locally evidenced exclusions suppress an item."""
    for item in release.applicability:
        if item.get("kind") == kind and item.get("index") == index:
            return not (item.get("status") == "does_not_apply"
                        and item.get("evidence") and item.get("profile_evidence"))
    return True


def _has_applicable(release: Release, kind: str) -> bool:
    return any(item.get("evidence") and _applicable(release, kind, i)
               for i, item in enumerate(_classification(release).get(kind, [])))


def _needs_review(releases: list[Release]) -> bool:
    return any(r.model_disagreement or (r.classification or {}).get("keyword_warnings")
               for r in releases)


_REVIEW_REASON = "Review incomplete keyword coverage/model disagreement before deciding."


def soak_deadline(release: Release, releases: list[Release], settings: Settings,
                  tier: str) -> datetime | None:
    """Calculate an individual clock without guessing missing publication dates."""
    if release.published_at is None:
        return None
    published = _aware(release.published_at)
    days = settings.soak_days.get(tier, 14)
    version = parse_version(release.version)
    if settings.patch_exception and version and not version.is_prerelease:
        parts = version.release
        if len(parts) >= 3 and parts[2] > 0:
            # A trustworthy first release is required; an earlier patch is not
            # evidence of the start of this major/minor line.
            bases = [r for r in releases if (p := parse_version(r.version))
                     and not p.is_prerelease and len(p.release) >= 3
                     and p.release[:2] == parts[:2] and p.release[2] == 0
                     and r.published_at is not None]
            if bases:
                base_date = min(_aware(r.published_at) for r in bases)
                base_mature = base_date + timedelta(days=days)
                # Patch exception only unlocks once the original line is old
                # enough, and the patch itself must complete its short soak.
                return min(published + timedelta(days=days),
                           max(base_mature, published + timedelta(days=settings.patch_soak_days)))
    return published + timedelta(days=days)


def _sort_releases(releases: list[Release]) -> list[Release]:
    return sorted(releases, key=lambda r: parse_version(r.version))


def _is_ripe(release: Release, pending: list[Release], all_releases: list[Release],
             settings: Settings, tier: str, now: datetime) -> bool:
    if release.prerelease or (v := parse_version(release.version)) is None or v.is_prerelease:
        return False
    deadline = soak_deadline(release, all_releases, settings, tier)
    if deadline is None or deadline > now:
        return False
    newer = [r for r in pending if compare_versions(r.version, release.version) == 1]
    # An unknown release date cannot establish a quiet interval.
    if any(r.published_at is None for r in newer):
        return False
    return not any(_aware(r.published_at) < deadline for r in newer)


def build_view(app: AppRecord, releases: list[Release], settings: Settings,
               now: datetime | None = None) -> AppView:
    view = _build_view(app, releases, settings, now)
    if not app.error:
        return view
    # Keep proven risk cautions visible, but stale inventory cannot establish
    # that an app is current or that its newest update has completed a soak.
    if view.status not in ("recommended_now", "read_first"):
        view.status, view.status_label = "unknown", "Unknown"
        view.recommended_version = None
        view.reason = "Source unavailable; last known data shown. Freshness and the current soak cannot be verified."
    else:
        view.reason = "Source unavailable; last known data shown. " + view.reason
    view.days_remaining = None
    view.next_ripe_at = None
    return view


def _build_view(app: AppRecord, releases: list[Release], settings: Settings,
                now: datetime | None = None) -> AppView:
    now = _aware(now or utcnow())
    valid = [r for r in releases if parse_version(r.version) is not None]
    ordered = _sort_releases(valid)
    latest = app.latest_version
    # HA's reported latest (and an explicitly selected release channel) is
    # authoritative. A GitHub beta above that ceiling is not a pending update.
    # Without a declared latest, the default channel is stable only.
    if not latest:
        stable = [r for r in ordered if not r.prerelease and not parse_version(r.version).is_prerelease]
        latest = stable[-1].version if stable else None
    selected_latest = parse_version(latest)
    prerelease_channel = bool(app.latest_version and selected_latest and selected_latest.is_prerelease)
    pending = [r for r in ordered
               if compare_versions(r.version, app.installed_version) == 1
               and (ceiling := compare_versions(r.version, latest)) is not None and ceiling <= 0
               and (prerelease_channel or not (r.prerelease or parse_version(r.version).is_prerelease))]
    history = [r for r in ordered if (c := compare_versions(r.version, app.installed_version)) is not None and c <= 0]
    available_notes = [r for r in releases
                       if compare_versions(r.version, app.installed_version) is None
                       and (compare_versions(r.version, latest) in (None, -1, 0))]
    # A card-level tag must hold for the whole pending span, not for one release in it.
    tag_sets = [set(_classification(r).get("tags", [])) for r in pending]
    tags = sorted(set.intersection(*tag_sets) if tag_sets else set(),
                  key=lambda tag: (_TAG_ORDER.get(tag, 99), tag))
    signals: list[str] = []
    if app.latest_only:
        signals.append("Only :latest is available; choose latest or wait")
    if any(r.prerelease or (parse_version(r.version) and parse_version(r.version).is_prerelease) for r in pending):
        signals.append("Pending span includes a pre-release")
    if semver_major_bump(app.installed_version, latest):
        signals.append("Semver major version change")
        if _DATABASE.search(f"{app.name} {app.image or ''}"):
            signals.append("Database image major version change")
    view = AppView(app=app, status="unknown", status_label="Unknown", latest_version=latest,
                   pending=pending, history=history, available_notes=available_notes,
                   no_notes=[r.version for r in pending if not r.notes.strip()], releases_behind=len(pending), tags=tags,
                   hard_signals=signals, reason="Could not determine an installed version or release notes.")
    if app.detected.get("local_build"):
        view.status_label, view.reason = "Local build (not tracked)", "Built locally, never pulled from a registry. Map it to a GitHub repository to track its releases."
    if app.latest_only:
        return _latest_only_view(view, settings, now)
    if parse_version(app.installed_version) is None:
        return view
    comparison = compare_versions(latest, app.installed_version)
    if comparison is not None and comparison <= 0:
        return view.model_copy(update={"status": "up_to_date", "status_label": "Up to date",
                                       "reason": "No newer version is known."})
    if comparison is None and not pending:
        return view
    if not pending:
        return view.model_copy(update={"reason": "A newer version is reported, but its release notes or publication date are unavailable."})
    newest = pending[-1]
    latest_missing = compare_versions(latest, newest.version) == 1
    deadline = None if latest_missing else soak_deadline(newest, releases, settings, app.profile.tier)
    days_left = max(0, math.ceil((deadline - now).total_seconds() / 86400)) if deadline else None
    ripe = [] if latest_missing else [r for r in pending if _is_ripe(r, pending, releases, settings, app.profile.tier, now)]
    target = ripe[-1] if ripe else None
    security = [r for r in pending if _has_applicable(r, "security")]
    # The earliest version containing all known fixes is the last fixing
    # release. Recommending the first would knowingly omit later security fixes.
    if security and (target is None or compare_versions(target.version, security[-1].version) == -1):
        target = security[-1]
    view.recommended_version = target.version if target else None
    view.next_ripe_at = deadline
    view.days_remaining = days_left
    applicable_break = any(_has_applicable(r, kind) for r in pending for kind in ("breaking", "migration"))
    attention = _needs_review(pending)
    unknown = latest_missing or any(not r.notes.strip() or not _classification(r) for r in pending)
    if security:
        view.status, view.status_label = "recommended_now", "Recommended now"
        view.reason = f"Applicable security fix: {target.version if target else security[-1].version} is the earliest target containing all known applicable fixes. Your approval is still required."
        if applicable_break:
            view.reason += " Read the applicable breaking or migration steps first."
    elif applicable_break or attention or any(s in signals for s in ("Semver major version change", "Database image major version change", "Pending span includes a pre-release")):
        view.status, view.status_label = "read_first", "Read first"
        view.reason = "Review the applicable breaking/migration items and hard signals before deciding." if applicable_break or signals else ""
    elif unknown and not latest_missing and any(r.classification is None for r in pending) \
            and all(r.notes.strip() for r in pending):
        view.status_label = "Summary pending"
        view.reason = "Release summaries are still being generated; no safety conclusion can be made yet."
    elif unknown:
        view.reason = ("Latest version has no release record or date; no safety conclusion can be made." if latest_missing else
                       "Release classification or changelog is unavailable; no safety conclusion can be made.")
    elif target:
        view.status, view.status_label = "ripe", "Ripe"
        view.reason = f"{target.version} completed its soak without an intervening release."
        if target.version != newest.version and days_left is not None:
            view.reason += f" Newest {newest.version} still needs {days_left} days."
    elif deadline is not None:
        view.status, view.status_label = "waiting", "Waiting"
        view.reason = f"Newest {newest.version} needs {days_left} more days."
        if any((d := soak_deadline(r, releases, settings, app.profile.tier)) and d <= now for r in pending[:-1]):
            view.reason += " Earlier releases were superseded before completing their soak; the clock restarted."
    else:
        view.reason = "Publication date is unknown, so a soak countdown cannot be calculated."
    if attention:
        view.reason = (view.reason + " " + _REVIEW_REASON).strip()
    return view


def _latest_only_view(view: AppView, settings: Settings, now: datetime) -> AppView:
    app = view.app
    security = any(_has_applicable(r, "security") for r in view.pending)
    breaking = any(_has_applicable(r, kind) for r in view.pending for kind in ("breaking", "migration"))
    attention = _needs_review(view.pending)
    digest_matches = bool(app.installed_digest and app.latest_digest and app.installed_digest == app.latest_digest)
    if digest_matches or app.update_available is False:
        if security or breaking or attention:
            view.status, view.status_label = "read_first", "Read first"
            view.reason = "Latest image digest is already installed, but pending upstream notes need review. Verify whether this image contains the applicable changes."
            if attention:
                view.reason += " " + _REVIEW_REASON
            return view
        return view.model_copy(update={"status": "up_to_date", "status_label": "Up to date", "reason": "Installed digest matches latest." if digest_matches else "Registry reports no image change."})
    changed = app.update_available is True or bool(app.installed_digest and app.latest_digest and app.installed_digest != app.latest_digest)
    if not changed:
        view.reason = "Only :latest is available and the remote digest could not be compared."
    elif app.image_changed_at is None:
        view.reason = "Image differs, but the date of the remote change is unknown; wait or inspect latest."
    else:
        deadline = _aware(app.image_changed_at) + timedelta(days=settings.soak_days.get(app.profile.tier, 14))
        view.days_remaining = max(0, math.ceil((deadline - now).total_seconds() / 86400))
        view.next_ripe_at = deadline
        view.status, view.status_label = ("ripe", "Ripe") if deadline <= now else ("waiting", "Waiting")
        view.recommended_version = "latest" if deadline <= now else None
        view.reason = f"Image changed on {_aware(app.image_changed_at).date().isoformat()}; only latest or wait is possible."
    if security:
        view.status, view.status_label = "recommended_now", "Recommended now"
        view.recommended_version = "latest"
        view.reason += " Applicable security fix in the pending changelog: review latest now; a version-specific image target is unavailable. Your approval is still required."
        if breaking:
            view.reason += " Read the applicable breaking or migration steps first."
    elif breaking or attention:
        view.status, view.status_label = "read_first", "Read first"
        if breaking:
            view.reason += " Review the applicable breaking/migration items before deciding."
    if attention:
        view.reason += " " + _REVIEW_REASON
    return view


def dashboard_sort_key(view: AppView) -> tuple[int, int, str]:
    return (_TIER_ORDER.get(view.app.profile.tier, 3), _STATUS_ORDER.get(view.status, 99), view.app.name.casefold())
