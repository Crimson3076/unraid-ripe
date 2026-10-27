"""The only Stage 1 external write: explicitly enabled household notifications."""
from __future__ import annotations
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
import hashlib
import httpx
from .models import Settings, AppView, utcnow
from .store import Store


def clock(value: str) -> time:
    return time.fromisoformat(value)


def is_quiet(settings: Settings, now: datetime | None = None) -> bool:
    local = (now or utcnow()).astimezone(ZoneInfo(settings.timezone)).time().replace(tzinfo=None)
    start, end = clock(settings.quiet_start), clock(settings.quiet_end)
    if start == end:
        return True  # Safe interpretation of a full quiet day.
    return start <= local < end if start < end else local >= start or local < end


def digest_text(views: list[AppView]) -> str:
    counts = {name: sum(v.status == name for v in views) for name in ('ripe','recommended_now','read_first','unknown')}
    message = (f"Ripe: {counts['ripe']} ripe, {counts['recommended_now']} recommended now (security), "
               f"{counts['read_first']} read-first, {counts['unknown']} unknown.")
    waiting = sorted((v for v in views if v.status == 'waiting' and v.days_remaining is not None), key=lambda v:v.days_remaining)
    if waiting:
        first = waiting[0]
        message += f' Next ripening: {first.app.name} in {first.days_remaining} days.'
    return message


class Notifier:
    def __init__(self, store: Store, ha_url: str, ha_token: str | None, *, transport=None):
        self.store, self.ha_url, self.ha_token, self.transport = store, ha_url, ha_token, transport

    async def send(self, settings: Settings, message: str, tag: str, *, now: datetime | None = None) -> bool:
        if not settings.notifications_enabled or not self.ha_token or is_quiet(settings, now):
            return False
        payload = {'people': settings.notification_people, 'title': 'Ripe',
                   'message': message + '\n' + settings.dashboard_url, 'home_only': False,
                   'tag': tag, 'channel': 'Home Alerts'}
        try:
            # Local HA traffic uses neither public proxy nor redirects, so tokens
            # cannot follow a redirect to another destination.
            async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False, transport=self.transport) as client:
                response = await client.post(self.ha_url + '/api/services/script/notify_household',
                    headers={'Authorization': 'Bearer ' + self.ha_token}, json=payload)
                response.raise_for_status()
            return True
        except (httpx.HTTPError, ValueError):
            self.store.log_error({'source':'notifications','message':'Home Assistant household script request failed; check connection, token permissions and script fields.'})
            return False

    async def tick(self, settings: Settings, views: list[AppView], *, now: datetime | None = None, demo: bool = False):
        now = now or utcnow()
        if demo or not settings.notifications_enabled or not self.ha_token or is_quiet(settings, now):
            return
        # Stable item/version keys prevent repeat alerts; unsent keys persist
        # across quiet hours and process restarts by remaining absent here.
        for view in views:
            if view.status != 'recommended_now':
                continue
            fixes = [(r.version, (r.classification or {}).get('security', [])) for r in view.pending if (r.classification or {}).get('security')]
            key = 'security-sent:' + hashlib.sha256(repr((view.app.id, fixes)).encode()).hexdigest()
            if self.store.get(key):
                continue
            if await self.send(settings, f'{view.app.name}: applicable security fix available. Review {view.recommended_version or "the release notes"}; no update has been performed.', 'ripe-security-' + hashlib.sha256(view.app.id.encode()).hexdigest()[:12], now=now):
                self.store.set(key, now.isoformat())
        local = now.astimezone(ZoneInfo(settings.timezone))
        # Most recent scheduled occurrence, so a quiet-time schedule is deferred
        # until the next permitted time instead of being lost for a week.
        scheduled = local.replace(hour=clock(settings.digest_time).hour, minute=clock(settings.digest_time).minute, second=0, microsecond=0)
        scheduled -= timedelta(days=(local.weekday() - settings.digest_weekday) % 7)
        if scheduled > local:
            scheduled -= timedelta(days=7)
        due_key = scheduled.date().isoformat()
        previous = self.store.get('digest-sent-for')
        if previous != due_key:
            if await self.send(settings, digest_text(views), 'ripe-weekly', now=now):
                self.store.set('digest-sent-for', due_key)
