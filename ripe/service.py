"""Inventory and analysis orchestration; deliberately no update executor."""
from __future__ import annotations
import asyncio
import hashlib
import json
import time
from collections import Counter
from datetime import datetime
from typing import Any
import httpx
from .config import Runtime
from .models import AppRecord, AppView, Profile, Release, Settings, hidden, stopped, utcnow
from .autoprofile import AUTO_FIELDS, merge_profile
from .store import Store
from .progress import PollProgress
from .sources import collect
from .intelligence import (classify_release, analyze_applicability, sanitize_profile, classification_key,
                           prompt_digest, build_rollup, rollup_key, ROLLBACK_PROMPT)
from .intelligence.evidence import conservative_applicability, keyword_warnings
from .ripeness import build_view, dashboard_sort_key, _STATUS_ORDER
from .versions import compare_versions, parse_version
from .analysis_queue import is_pending, newest_first, priority, trivial_notes
from .notifications import Notifier
from .demo import seed_demo


# Releases read for an app whose installed version can't be ordered against them.
UNKNOWN_SPAN_CONTEXT = 3


def rollup_latest(app: AppRecord, pending: list[Release]) -> str | None:
    return pending[-1].version if pending else app.latest_version


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class Coordinator:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        from .connections import HomeAssistantConnection
        self.ha_connection = HomeAssistantConnection(runtime.config_dir, runtime.ha_url, runtime.ha_token)
        runtime.ha_url, runtime.ha_token = self.ha_connection.credentials()
        from .connections import ApiCredential
        self.api_credentials = {
            'github': ApiCredential(runtime.config_dir, 'github', 'token', runtime.github_token),
            'gemini': ApiCredential(runtime.config_dir, 'gemini', 'api_key', runtime.gemini_key)}
        self.apply_api_credentials()
        self.real_store = Store(runtime.config_dir / 'ripe.sqlite3', runtime.secret_values())
        if self.real_store.get('settings') is None:
            self.real_store.save_settings(runtime.initial_settings())
        self.demo_store: Store | None = None
        self.demo = False
        self.store = self.real_store
        self.polling = False
        self.progress = PollProgress()
        self.closing = False
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.poll_task: asyncio.Task | None = None
        if runtime.demo:
            self.enter_demo()

    def close(self):
        if self.demo_store:
            self.demo_store.close()
        self.real_store.close()

    def save_ha_connection(self, data: dict | None = None, *, disconnect=False) -> dict:
        if self.demo:
            raise ValueError('Leave demo mode before changing the live Home Assistant connection')
        public = self.ha_connection.disconnect() if disconnect else self.ha_connection.save(data)
        self.runtime.ha_url, self.runtime.ha_token = self.ha_connection.credentials()
        for store in (self.real_store, self.demo_store):
            if store:
                store.secrets = self.runtime.secret_values()
        if self.polling:
            self.real_store.set('poll_again', True)
        else:
            self.request_poll()
        return public

    def apply_api_credentials(self):
        self.runtime.github_token = self.api_credentials['github'].effective()
        self.runtime.gemini_key = self.api_credentials['gemini'].effective()

    def change_api_credential(self, name: str, data: dict | None = None, *, clear=False) -> dict:
        if self.demo:
            raise ValueError('Leave demo mode before changing live credentials')
        credential = self.api_credentials[name]
        public = credential.clear() if clear else credential.save(data)
        self.apply_api_credentials()
        for store in (self.real_store, self.demo_store):
            if store:
                store.secrets = self.runtime.secret_values()
        if self.polling:
            self.real_store.set('poll_again', True)
        else:
            self.request_poll()
        return public

    def enter_demo(self):
        if self.real_store.apps():
            raise ValueError('Demo requires an empty live inventory; existing records are preserved')
        if self.polling:
            raise ValueError('Wait until collection finishes before entering demo')
        if self.demo_store is None:
            self.demo_store = Store(self.runtime.config_dir / 'demo.sqlite3', self.runtime.secret_values())
        self.demo = True
        self.store = self.demo_store
        if self.store.get('settings') is None:
            self.store.save_settings(self.runtime.initial_settings())
        seed_demo(self.store)

    def leave_demo(self):
        self.demo = False
        self.store = self.real_store

    def settings(self) -> Settings:
        settings = self.store.settings(self.runtime.initial_settings())
        return settings.model_copy(update={'demo_mode': self.demo})

    def with_rollup(self, view: AppView) -> AppView:
        # A stored rollup is shown only while it still describes this installed -> latest span.
        stored = (self.store.get('rollup:' + view.app.id) or {}).get('result')
        if stored and stored.get('installed') == (view.app.installed_version or view.app.installed_digest) and stored.get('latest') == rollup_latest(view.app, view.pending):
            view.rollup = stored
        back = (self.store.get('rollback:' + view.app.id) or {}).get('result')
        if back and back.get('installed') == view.app.installed_version and back.get('releases') == [r.version for r in view.rollback]:
            view.rollback_rollup = back
        return view

    @staticmethod
    def ignore_target(view: AppView) -> str | None:
        """What an ignore pins: the image digest for latest-only images (rebuilds keep the same upstream version), else the version."""
        app = view.app
        if app.latest_only and app.latest_digest:
            return app.latest_digest
        return view.latest_version or app.latest_digest

    @staticmethod
    def version_ignored(view: AppView) -> bool:
        """The user hid this update; it stays hidden until something newer than the hidden version exists."""
        app = view.app
        if not app.ignored_version:
            return False
        latest = Coordinator.ignore_target(view)
        if not latest or view.status == 'up_to_date':
            return False
        comparison = compare_versions(latest, app.ignored_version)
        return latest == app.ignored_version or (comparison is not None and comparison <= 0)

    def inventory(self, now: datetime | None = None, only: str | None = None) -> dict:
        """Cards for the Overview: hidden apps removed, sidecars folded into their parent, ignored items listed apart."""
        settings = self.settings()
        records = self.store.apps()
        by_id = {a.id: a for a in records}
        # `only` builds a single app and its sidecars instead of the whole inventory.
        visible = [a for a in records if not hidden(a, settings, by_id) and (only is None or only in (a.id, a.parent_id))]
        built = {a.id: self.with_rollup(build_view(a, self.store.releases(a.id), settings, now)) for a in visible}
        ignored = [{'id': a.id, 'name': a.name, 'scope': 'app', 'version': None} for a in records if a.ignored and not a.merged_into]
        cards: list[AppView] = []
        quiet = set()
        for view in built.values():
            if view.app.ignored_version and self.version_ignored(view):
                quiet.add(view.app.id)
                ignored.append({'id': view.app.id, 'name': view.app.name, 'scope': 'version', 'version': view.app.ignored_version})
            elif view.app.parent_id not in built:
                cards.append(view)
        attention = ('recommended_now', 'read_first', 'unknown', 'waiting', 'ripe')
        for view in built.values():
            parent = built.get(view.app.parent_id) if view.app.parent_id else None
            if parent is None or view.app.id in quiet:
                continue
            if parent.app.id in quiet:
                # Only the parent's version was ignored: a sidecar that needs attention brings the card back.
                if view.status not in attention:
                    continue
                if parent not in cards:
                    parent.status, parent.status_label, parent.reason = 'up_to_date', 'Up to date', ''
                    cards.append(parent)
            parent.sidecars.append(view)
            # A sidecar's update is the parent's update: its status can only raise the card's.
            if view.status in attention and _STATUS_ORDER[view.status] < _STATUS_ORDER.get(parent.status, 99):
                parent.status, parent.status_label = view.status, view.status_label
                parent.reason = f'{view.app.name}: {view.reason}'
                parent.recommended_version = view.recommended_version
                parent.days_remaining, parent.next_ripe_at = view.days_remaining, view.next_ripe_at
                parent.tags = sorted({*parent.tags, *view.tags})
        hidden_stopped = sum(1 for a in records if stopped(a) and not settings.include_stopped and not a.ignored and not a.merged_into)
        return {'views': sorted(cards, key=dashboard_sort_key), 'ignored': sorted(ignored, key=lambda i: i['name'].casefold()), 'stopped_hidden': hidden_stopped}

    def views(self, now: datetime | None = None) -> list[AppView]:
        return self.inventory(now)['views']

    def set_ignore(self, app_id: str, scope: str, on: bool) -> AppRecord:
        """Ignore or restore an app, or hide/unhide its current latest version. Undoing an app ignore re-polls it."""
        app = self.store.app(app_id)
        if app is None:
            raise KeyError(app_id)
        if scope == 'app':
            app.ignored = on
        elif on:
            view = self.view(app_id)
            latest = self.ignore_target(view) if view else None
            if not latest:
                raise ValueError('No newer version is known')
            app.ignored_version = latest
        else:
            app.ignored_version = None
        self.mark_changed(app_id)
        self.store.save_app(app)
        if scope == 'app' and not on:
            self.request_poll()
        return app

    def view(self, app_id: str) -> AppView | None:
        app = self.store.app(app_id)
        if app is None:
            return None
        # The card carries the sidecars and the status they raise; hidden or ignored apps fall back to their own view.
        card = next((v for v in self.inventory(only=app_id)['views'] if v.app.id == app_id), None)
        return card or self.with_rollup(build_view(app, self.store.releases(app_id), self.settings()))

    async def refresh_rollup(self, app: AppRecord, settings: Settings, local_ready) -> None:
        """Regenerate the app's rollup only when installed, latest, their notes or the model setup changed."""
        pending = build_view(app, self.store.releases(app.id), settings).pending
        omitted = max(0, len(pending) - settings.analysis_cap)
        total = len(pending)
        pending = pending[omitted:]
        image_diff, manifest = app.detected.get('image_diff'), app.detected.get('manifest_diff')
        # No version known (latest-only image): the installed digest identifies the installation.
        installed = app.installed_version or app.installed_digest
        if app.section == 'firmware' and not any(r.notes.strip() for r in pending):
            # Firmware without release notes has nothing to summarise; never send it to a model.
            self.store.set('rollup:' + app.id, None)
            return
        if not installed or not (pending or image_diff):
            self.store.set('rollup:' + app.id, None)
            return
        latest = rollup_latest(app, pending)
        key = rollup_key(app, installed, latest, pending, image_diff, manifest, settings)
        stored = self.store.get('rollup:' + app.id) or {}
        if stored.get('key') == key and stored['result'].get('status') == 'ok':
            return
        unavailable = None
        if (any(r.notes.strip() for r in pending) or manifest) and settings.classification_provider == 'local' and not await local_ready():
            unavailable = 'Local model is unavailable; no summary was inferred.'
        result = await build_rollup(app, installed, latest, pending, settings, image_diff=image_diff,
                                    manifest_diff=manifest, gemini_key=self.runtime.gemini_key,
                                    model_unavailable=unavailable, log_call=self.store.log_llm)
        if omitted:
            result['span_note'] = f'Covers the newest {len(pending)} of {total} releases; {omitted} older not analysed.'
        self.store.set('rollup:' + app.id, {'key': key if result['status'] == 'ok' else None, 'result': result})

    async def refresh_rollback(self, app: AppRecord, settings: Settings, local_ready) -> None:
        """Summarise what the few releases up to the installed one gave you; nothing older is ever sent to the model."""
        window = build_view(app, self.store.releases(app.id), settings).rollback
        if not app.installed_version or not window:
            self.store.set('rollback:' + app.id, None)
            return
        key = rollup_key(app, app.installed_version, app.installed_version, window, None, None, settings, prompt=ROLLBACK_PROMPT)
        stored = self.store.get('rollback:' + app.id) or {}
        if stored.get('key') == key and stored['result'].get('status') == 'ok':
            return
        unavailable = None
        if any(r.notes.strip() for r in window) and settings.classification_provider == 'local' and not await local_ready():
            unavailable = 'Local model is unavailable; no summary was inferred.'
        result = await build_rollup(app, app.installed_version, app.installed_version, window, settings, gemini_key=self.runtime.gemini_key,
                                    model_unavailable=unavailable, log_call=self.store.log_llm, prompt=ROLLBACK_PROMPT)
        self.store.set('rollback:' + app.id, {'key': key if result['status'] == 'ok' else None, 'result': result})

    @staticmethod
    def card(view: AppView) -> dict:
        """Overview fields only; notes, history and classifications load from /api/apps/{id}."""
        def norm(text) -> str:
            return ' '.join(str(text or '').lower().split())
        quotes: dict[str, str] = {}
        for release in view.pending:
            c = release.classification or {}
            for tag in c.get('tags', []):
                evidence = (c.get('tag_evidence') or {}).get(tag)
                if tag not in quotes and norm(evidence) and norm(evidence) in norm(release.notes):
                    quotes[tag] = f'{release.version}: {evidence}'
        data = view.model_dump(mode='json', exclude={'pending', 'history', 'available_notes', 'no_notes', 'rollup', 'rollback', 'rollback_rollup', 'sidecars'})
        data['sidecars'] = [Coordinator.card(side) for side in view.sidecars]
        data['app']['detected'] = {k: v for k, v in data['app']['detected'].items() if k == 'mapping_hint'}
        data['tag_quotes'] = quotes
        data['rollup'] = {'headline': view.rollup['headline']} if view.rollup and view.rollup.get('headline') else None
        return data

    def dashboard(self) -> dict:
        inventory = self.inventory()
        views = inventory['views']
        counts = Counter(v.status for v in views)
        return {'apps': [self.card(v) for v in views], 'demo': self.demo,
                'ignored': inventory['ignored'], 'stopped_hidden': inventory['stopped_hidden'],
                'include_stopped': self.settings().include_stopped,
                'summary': {s:counts[s] for s in ('ripe','waiting','recommended_now','read_first','up_to_date','unknown')},
                'last_poll':self.store.get('last_poll'), 'polling': self.polling,
                'integration_status': self.integration_status(), 'timezone':self.settings().timezone,
                'auto_profiles': self.auto_profiles(), 'analysis': self.store.get('analysis_progress'), 'poll': self.progress.snapshot()}

    def auto_profiles(self) -> list[dict]:
        """Apps Ripe filled in by itself that the user has not yet acknowledged."""
        reviewed = set(self.store.get('autoprofile:reviewed', []))
        found = []
        settings = self.settings()
        records = self.store.apps()
        by_id = {a.id: a for a in records}
        for app in records:
            if hidden(app, settings, by_id):
                continue
            evidence = app.detected.get('profile_evidence') or {}
            reasons = {f: evidence[f] for f in (*AUTO_FIELDS, 'upstream_repo') if evidence.get(f) and app.profile.origin.get(f, 'auto') != 'user'}
            if reasons and app.id not in reviewed:
                found.append({'id': app.id, 'name': app.name, 'reasons': reasons})
        return found

    def review_auto_profiles(self):
        self.store.set('autoprofile:reviewed', sorted({*self.store.get('autoprofile:reviewed', []), *(a['id'] for a in self.auto_profiles())}))

    def integration_status(self) -> dict:
        return {'docker_socket': 'present' if __import__('os').path.exists(self.runtime.docker_socket) else 'not mounted',
                'home_assistant': 'URL and token configured' if self.runtime.ha_token and self.runtime.ha_url else 'URL or token not configured',
                'unraid': 'version file mounted' if __import__('os').path.isfile(self.runtime.unraid_version_file) else 'version file not mounted',
                'gemini': 'key configured' if self.runtime.gemini_key else 'optional key not configured',
                'github': 'token configured' if self.runtime.github_token else 'public API (no optional token)',
                'ollama': self.store.get('ollama_status', 'not checked'),
                'notifications': 'disabled in demo' if self.demo else ('enabled' if self.settings().notifications_enabled else 'off until enabled')}

    async def local_ready(self, settings: Settings) -> bool:
        try:
            from .intelligence import local_model_url
            url = local_model_url(settings.ollama_url)
            async with httpx.AsyncClient(timeout=4, trust_env=False, follow_redirects=False) as client:
                response = await client.get(url + '/api/tags')
                response.raise_for_status()
                names = {item.get('name') for item in response.json().get('models', [])}
                ready = settings.local_model in names or settings.local_model + ':latest' in names
                self.store.set('ollama_status', 'model available' if ready else 'configured model not installed')
                return ready
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            self.store.set('ollama_status','unreachable; verify the configured local Ollama endpoint')
            return False

    def request_poll(self) -> bool:
        if self.demo or self.polling or (self.poll_task and not self.poll_task.done()):
            return False
        self.polling = True
        self.progress.start()
        self.poll_task = asyncio.create_task(self.poll())
        return True

    def poll_status(self) -> dict:
        return self.progress.snapshot()

    def mark_changed(self, app_id: str):
        self.store.set('generation:' + app_id, self.store.get('generation:' + app_id, 0) + 1)
        if self.polling:
            self.store.set('poll_again', True)

    async def poll(self):
        if self.demo or self.lock.locked():
            return
        async with self.lock:
            self.polling = True
            if not self.progress.running:
                self.progress.start()
            failure = None
            try:
                settings = self.settings()
                prior_apps = self.store.apps()
                generations = {a.id: self.store.get('generation:' + a.id, 0) for a in prior_apps}
                apps, releases, errors = await collect(settings, github_token=self.runtime.github_token,
                    ha_token=self.runtime.ha_token, ha_url=self.runtime.ha_url,
                    docker_socket=self.runtime.docker_socket, templates_dir=self.runtime.templates_dir,
                    unraid_version_file=self.runtime.unraid_version_file,
                    cache_get=self.store.get, cache_set=self.store.set, existing_apps=self.store.apps(),
                    progress=self.progress)
                self.progress.end_collecting()
                self.progress.add_errors(errors)
                for error in errors:
                    self.store.log_error(error)
                by_app: dict[str,list[Release]] = {}
                for release in releases:
                    by_app.setdefault(release.app_id, []).append(release)
                by_id = {a.id: a for a in apps}
                local_ready = None
                queue: list = []
                skipped = 0
                refresh: list = []
                for app in apps:
                    generation = generations.get(app.id, 0)
                    if self.store.get('generation:' + app.id, 0) != generation:
                        continue
                    preference = self.store.get('profile:' + app.id)
                    # Fields the user edited are never overwritten; the rest follow what was read this scan.
                    app.profile, evidence = merge_profile(app.profile, dict(app.detected.get('profile_evidence') or {}),
                                                          Profile.model_validate(preference) if preference is not None else None)
                    app.profile = sanitize_profile(app.profile)
                    mapping = self.store.get('mapping:' + app.id)
                    if mapping is not None:
                        app.upstream_repo = mapping.get('upstream_repo')
                        evidence.pop('upstream_repo', None)
                    app.detected['profile_evidence'] = evidence
                    self.store.save_app(app)
                    if hidden(app, settings, by_id):
                        continue  # ignored, merged or stopped: no analysis, no model calls
                    # Same channel and latest-version ceiling as the dashboard span, so betas
                    # or releases above an authoritative latest don't use up the cap.
                    shown = {r.id for r in build_view(app, by_app.get(app.id, []), settings).pending}
                    unknown_installed = parse_version(app.installed_version) is None
                    pending = newest_first([r for r in by_app.get(app.id, []) if is_pending(r, app) and (unknown_installed or r.id in shown)])
                    # An installed version that can't be ordered leaves the whole history of unknown span:
                    # only the newest few are read, for context, until the version is resolved.
                    limit = min(UNKNOWN_SPAN_CONTEXT, settings.analysis_cap) if unknown_installed else settings.analysis_cap
                    analysed = {r.id: rank for rank, r in enumerate(pending[:limit])}
                    for release in by_app.get(app.id, []):
                        if self.store.get('generation:' + app.id, 0) != generation:
                            break
                        previous = {r.id:r for r in self.store.releases(app.id)}.get(release.id)
                        if previous and previous.notes == release.notes:
                            release.classification = previous.classification
                            release.applicability = previous.applicability
                            release.classification_key = previous.classification_key
                            release.model_disagreement = previous.model_disagreement
                        # Older records supply history and base-line dates without
                        # spending LLM work on releases that aren't pending.
                        if not is_pending(release, app):
                            self.store.save_release(release)
                            continue
                        if trivial_notes(release.notes):
                            release.classification = {'status':'unknown','tags':[],'skipped':'no notes','keyword_warnings':keyword_warnings(release.notes, [])}
                            release.classification_key = None
                            release.applicability = conservative_applicability(release.classification)
                            self.store.save_release(release)
                            skipped += 1
                            continue
                        if release.id not in analysed:
                            if not release.classification or release.classification.get('status') != 'ok':
                                release.classification = {'status':'unknown','tags':[],'skipped':f'not analysed (older than the newest {limit})' if not unknown_installed else 'not analysed (installed version unknown; only the newest are read for context)','keyword_warnings':keyword_warnings(release.notes, [])}
                                release.classification_key = None
                                release.applicability = conservative_applicability(release.classification)
                            else:
                                # Keep the classification, but don't let exclusions made under an
                                # older profile keep suppressing the item without a model call.
                                profile_key = fingerprint([app.profile.model_dump(exclude={'origin'}),release.classification_key,settings.local_model,settings.ollama_url,prompt_digest()])
                                current = self.store.get('applicability:' + profile_key)
                                if current is not None and all(item.get('verified') for item in current):
                                    release.applicability = current
                                else:
                                    release.applicability = conservative_applicability(release.classification)
                            self.store.save_release(release)
                            skipped += 1
                            continue
                        queue.append((priority(app, release, analysed[release.id]), len(queue), app, release, generation))
                    refresh.append((app, generation))
                queue.sort(key=lambda item: item[:2])
                self.store.set('analysis_progress', {'done': 0, 'total': len(queue)})
                self.progress.begin_analysis(len(queue), skipped)
                for done, (_, _, app, release, generation) in enumerate(queue):
                    if self.store.get('generation:' + app.id, 0) != generation:
                        self.progress.release_done(app.name)
                        continue
                    self.progress.working_on(app.name)
                    started, called = time.monotonic(), False
                    key = classification_key(release, app, settings)
                    cached = self.store.get('classification:' + key)
                    if cached and (cached.get('classification') or {}).get('status') == 'ok':
                        release = release.model_copy(update=cached)
                    elif not release.classification or release.classification_key != key or release.classification.get('status') != 'ok':
                        if local_ready is None:
                            local_ready = await self.local_ready(settings)
                        if settings.classification_provider == 'local' and not local_ready:
                            release.classification = {'status':'unknown','tags':[], 'keyword_warnings':keyword_warnings(release.notes, []), 'error':'Local model is unavailable; no classification was inferred.'}
                            release.classification_key = key
                        else:
                            release.classification = None
                            called = True
                            release = await classify_release(release, app, settings, gemini_key=self.runtime.gemini_key,log_call=self.store.log_llm)
                        self.store.set('classification:' + key, {'classification':release.classification,'classification_key':release.classification_key, 'model_disagreement':release.model_disagreement})
                    profile_key = fingerprint([app.profile.model_dump(exclude={'origin'}),release.classification_key,settings.local_model,settings.ollama_url,prompt_digest()])
                    analysis = self.store.get('applicability:' + profile_key)
                    if analysis is not None and all(item.get('verified') for item in analysis):
                        release.applicability = analysis
                    else:
                        if local_ready is None:
                            local_ready = await self.local_ready(settings)
                        if local_ready:
                            called = True
                            release = await analyze_applicability(release,app,settings,log_call=self.store.log_llm)
                        else:
                            release.applicability = conservative_applicability(release.classification)
                        self.store.set('applicability:' + profile_key,release.applicability)
                    self.progress.release_done(app.name, time.monotonic() - started if called else None)
                    if self.store.get('generation:' + app.id, 0) != generation:
                        continue
                    self.store.save_release(release)
                    self.store.set('analysis_progress', {'done': done + 1, 'total': len(queue)})
                self.store.set('analysis_progress', None)
                self.progress.begin_summaries()
                for app, generation in refresh:
                    if self.store.get('generation:' + app.id, 0) == generation:
                        async def ready():
                            nonlocal local_ready
                            if local_ready is None:
                                local_ready = await self.local_ready(settings)
                            return local_ready
                        await self.refresh_rollup(app, settings, ready)
                        await self.refresh_rollback(app, settings, ready)
                # Mark a source failure as stale rather than silently deleting its
                # previous records; these visible errors prevent a fresh-data claim.
                current_ids = {a.id for a in apps}
                for prior in self.store.apps():
                    if prior.id not in current_ids:
                        prior.error = 'Not seen in the latest collection; source may be unavailable. Last known data shown.'
                        self.store.save_app(prior)
                self.store.set('last_poll',utcnow().isoformat())
                self.store.set('poll_errors',errors)
                failing = {str(e.get('source')) for e in errors}
                if local_ready is False:
                    failing.add('ollama')
                    self.store.log_error({'source':'ollama','message':'Configured model unavailable on the local Ollama endpoint. Pending classifications remain unknown.'})
                # A source that didn't fail this time has recovered; hide its old errors.
                self.store.resolve_errors(failing)
                self.store.prune_errors()
                await self.notify()
            except asyncio.CancelledError:
                failure = 'Check was interrupted.'
                raise
            except Exception as exc:
                # External response bodies/exception strings can contain secrets.
                failure = f'Collection failed ({type(exc).__name__}); last known records preserved.'
                self.store.log_error({'source':'collector','message':failure})
            finally:
                self.progress.finish(failure)
                self.polling = False
                if not self.closing and self.store.get('poll_again'):
                    self.store.set('poll_again',False)
                    self.progress.start()
                    self.poll_task = asyncio.create_task(self.poll())

    async def notify(self):
        if not self.store.get('last_poll'):
            return
        notifier = Notifier(self.store,self.runtime.ha_url,self.runtime.ha_token)
        await notifier.tick(self.settings(),self.views(),demo=self.demo)

    async def cycle(self):
        if not self.demo:
            last = self.store.get('last_poll')
            elapsed = (utcnow()-datetime.fromisoformat(last)).total_seconds() if last else float('inf')
            if elapsed >= self.settings().poll_hours*3600:
                self.request_poll()
            if not self.polling:
                await self.notify()

    async def run(self):
        while True:
            delay = 30
            try:
                await self.cycle()
            except Exception:
                # Keep the scheduler alive; the fixed message avoids leaking secrets from exception text.
                self.store.log_error({'source':'scheduler','message':'Scheduler cycle failed; will retry shortly.'})
                delay = 60
            await asyncio.sleep(delay)
