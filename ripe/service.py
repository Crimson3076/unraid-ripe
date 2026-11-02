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
                           prompt_digest, build_rollup, rollup_key)
from .intelligence.evidence import conservative_applicability, keyword_warnings
from .ripeness import build_view, dashboard_sort_key, _STATUS_ORDER
from .versions import compare_versions, parse_version
from .analysis_queue import is_pending, newest_first, priority, trivial_notes
from .notifications import Notifier
from .demo import seed_demo


UNKNOWN_SPAN_CAP = 3
ROLLBACK_PROMPT = 'rollback-v1'


def rollup_latest(app: AppRecord, pending: list[Release]) -> str | None:
    return pending[-1].version if pending else app.latest_version


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


POLL_TIMEOUT_SECONDS = 2 * 3600      # a collection older than this is treated as dead
CLASSIFY_BUDGET_SECONDS = 20 * 60    # per classification run; the queue resumes later
CLASSIFY_COOLDOWN_SECONDS = 10 * 60  # pause between budget-limited runs


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
        self.poll_token: object | None = None
        self.poll_started: float | None = None
        self.poll_timeout = POLL_TIMEOUT_SECONDS
        self.classify_task: asyncio.Task | None = None
        self.classify_ended: float | None = None
        self.classify_budget = CLASSIFY_BUDGET_SECONDS
        self.classify_cooldown = CLASSIFY_COOLDOWN_SECONDS
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
        if back and back.get('installed') == (view.app.installed_version or view.app.installed_digest):
            view.rollback = back
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
        """Summarise only the newest few stable releases at or below the installed version; nothing older is sent."""
        window = [r for r in build_view(app, self.store.releases(app.id), settings).history
                  if not r.prerelease and not parse_version(r.version).is_prerelease][-settings.rollback_window:] if settings.rollback_window else []
        if not window or not app.installed_version or not any(r.notes.strip() for r in window):
            self.store.set('rollback:' + app.id, None)
            return
        key = rollup_key(app, app.installed_version, app.installed_version, window, None, None, settings, ROLLBACK_PROMPT)
        stored = self.store.get('rollback:' + app.id) or {}
        if stored.get('key') == key and stored['result'].get('status') == 'ok':
            return
        unavailable = 'Local model is unavailable; no summary was inferred.' if settings.classification_provider == 'local' and not await local_ready() else None
        result = await build_rollup(app, app.installed_version, app.installed_version, window, settings,
                                    gemini_key=self.runtime.gemini_key, model_unavailable=unavailable,
                                    log_call=self.store.log_llm, prompt=ROLLBACK_PROMPT)
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
        data = view.model_dump(mode='json', exclude={'pending', 'history', 'available_notes', 'no_notes', 'rollup', 'rollback', 'sidecars'})
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
                **self.queue_progress(),
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

    def queue_progress(self) -> dict:
        return {'last_collection': self.store.get('last_collection') or self.store.get('last_poll'),
                'classification_queue_length': self.queue_length(),
                'last_classification': self.store.get('last_classification')}

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

    def poll_stale(self) -> bool:
        return self.polling and self.poll_started is not None and time.monotonic() - self.poll_started > self.poll_timeout

    def abandon_poll(self):
        """Treat a poll older than the timeout as dead so the next one can run."""
        if self.poll_task and not self.poll_task.done():
            self.poll_task.cancel()
        self.poll_task = None  # a cancelling task must not block the next poll
        self.polling = False
        self.poll_token = None
        self.store.log_error({'source':'collector','message':'An earlier update check exceeded its time limit and was abandoned.'})

    def start_poll(self):
        token = object()
        self.poll_token = token
        self.polling = True
        self.progress.start()
        self.poll_started = time.monotonic()
        self.poll_task = asyncio.create_task(self.poll(token))

    def request_poll(self) -> bool:
        if self.demo:
            return False
        if self.polling or (self.poll_task and not self.poll_task.done()):
            if not self.poll_stale():
                return False
            self.abandon_poll()
        self.start_poll()
        return True

    def poll_status(self) -> dict:
        return self.progress.snapshot()

    def mark_changed(self, app_id: str):
        self.store.set('generation:' + app_id, self.store.get('generation:' + app_id, 0) + 1)
        if self.polling:
            self.store.set('poll_again', True)

    async def poll(self, token: object | None = None):
        """Collect installed/latest versions and save them; classification is queued, not awaited."""
        if self.demo:
            return
        token = token or object()
        failure = None
        try:
            async with self.lock:
                self.poll_token = token
                self.polling = True
                self.poll_started = time.monotonic()
                if not self.progress.running:
                    self.progress.start()
                await self.collect_inventory()
        except asyncio.CancelledError:
            failure = 'Check was interrupted.'
            raise
        except Exception as exc:
            # External response bodies/exception strings can contain secrets.
            failure = f'Collection failed ({type(exc).__name__}); last known records preserved.'
            self.store.log_error({'source':'collector','message':failure})
        finally:
            # An abandoned poll no longer owns the flag (a newer poll may).
            if self.poll_token is token:
                self.progress.finish(failure)
                self.polling = False
                self.poll_token = None
                if not self.closing and self.store.get('poll_again'):
                    self.store.set('poll_again',False)
                    self.start_poll()

    async def collect_inventory(self):
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
        work: list = []
        by_id = {a.id: a for a in apps}
        # No awaits below: the stored queue is replaced atomically.
        for app in apps:
            if self.store.get('generation:' + app.id, 0) != generations.get(app.id, 0):
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
            previous = {r.id:r for r in self.store.releases(app.id)}
            for release in by_app.get(app.id, []):
                old = previous.get(release.id)
                if old and old.notes == release.notes:
                    release.classification = old.classification
                    release.applicability = old.applicability
                    release.classification_key = old.classification_key
                    release.model_disagreement = old.model_disagreement
                self.store.save_release(release)
            if hidden(app, settings, by_id):
                continue  # ignored, merged or stopped: no analysis, no model calls
            work.extend(self.pending_work(app, settings))
        # Mark a source failure as stale rather than silently deleting its
        # previous records; these visible errors prevent a fresh-data claim.
        current_ids = {a.id for a in apps}
        for prior in self.store.apps():
            if prior.id not in current_ids:
                prior.error = 'Not seen in the latest collection; source may be unavailable. Last known data shown.'
                self.store.save_app(prior)
        # Exposed apps and security wording first, then newest, so what matters most is summarised first.
        work.sort(key=lambda item: item[0])
        self.store.set('classification_queue', [{'app_id':r.app_id,'release_id':r.id} for _, r in work])
        now = utcnow().isoformat()
        self.store.set('last_poll', now)
        self.store.set('last_collection', now)
        self.store.set('poll_errors', errors)
        # A source that didn't fail this time has recovered; hide its old errors.
        self.store.resolve_errors({str(e.get('source')) for e in errors})
        self.store.prune_errors()
        self.request_classification()
        await self.notify()

    def pending_work(self, app: AppRecord, settings: Settings) -> list[tuple]:
        """Apply cached results to pending releases; return (priority, release) for those still needing the model."""
        work = []
        releases = self.store.releases(app.id)
        # Same channel and latest-version ceiling as the dashboard span, so betas
        # or releases above an authoritative latest don't use up the cap.
        shown = {r.id for r in build_view(app, releases, settings).pending}
        unknown_installed = parse_version(app.installed_version) is None
        pending = newest_first([r for r in releases if is_pending(r, app) and (unknown_installed or r.id in shown)])
        # An installed version that can't be ordered means the span is unknown: classify only the newest few for context.
        cap = min(settings.analysis_cap, UNKNOWN_SPAN_CAP) if unknown_installed else settings.analysis_cap
        for rank, release in enumerate(pending):
            if trivial_notes(release.notes):
                release = release.model_copy(update={'classification':{'status':'unknown','tags':[],'skipped':'no notes','keyword_warnings':keyword_warnings(release.notes, [])},'classification_key':None})
                self.store.save_release(release.model_copy(update={'applicability':conservative_applicability(release.classification)}))
                continue
            if rank >= cap:
                if not release.classification or release.classification.get('status') != 'ok':
                    release = release.model_copy(update={'classification':{'status':'unknown','tags':[],'skipped':f'not analysed (older than the newest {cap})' if not unknown_installed else f'not analysed (installed version unknown; only the newest {cap} are read)','keyword_warnings':keyword_warnings(release.notes, [])},'classification_key':None})
                    release = release.model_copy(update={'applicability':conservative_applicability(release.classification)})
                else:
                    # Keep the classification, but don't let exclusions made under an
                    # older profile keep suppressing the item without a model call.
                    current = self.store.get('applicability:' + self.profile_key(app, release, settings))
                    verified = current is not None and all(item.get('verified') for item in current)
                    release = release.model_copy(update={'applicability': current if verified else conservative_applicability(release.classification)})
                self.store.save_release(release)
                continue
            key = classification_key(release, app, settings)
            cached = self.store.get('classification:' + key)
            changed = False
            if cached and (cached.get('classification') or {}).get('status') == 'ok' and (
                    release.classification_key != key or release.classification != cached['classification']):
                release = release.model_copy(update=cached)
                changed = True
            classified = (release.classification or {}).get('status') == 'ok' and release.classification_key == key
            done = False
            if classified:
                analysis = self.store.get('applicability:' + self.profile_key(app, release, settings))
                if analysis is not None and all(item.get('verified') for item in analysis):
                    done = True
                    if release.applicability != analysis:
                        release, changed = release.model_copy(update={'applicability': analysis}), True
                elif changed and not release.applicability:
                    release = release.model_copy(update={'applicability': conservative_applicability(release.classification)})
            if changed:
                self.store.save_release(release)
            if not done:
                work.append((priority(app, release, rank), release))
        return work

    @staticmethod
    def profile_key(app: AppRecord, release: Release, settings: Settings) -> str:
        return fingerprint([app.profile.model_dump(exclude={'origin'}),release.classification_key,settings.local_model,settings.ollama_url,prompt_digest()])

    def queue_length(self) -> int:
        return len(self.store.get('classification_queue', []))

    def rollups_due(self) -> bool:
        settings = self.settings()
        records = self.store.apps()
        by_id = {a.id: a for a in records}
        for app in records:
            if hidden(app, settings, by_id):
                continue
            pending = build_view(app, self.store.releases(app.id), settings).pending
            installed = app.installed_version or app.installed_digest
            if installed and (pending or app.detected.get('image_diff')):
                key = rollup_key(app, installed, rollup_latest(app, pending), pending, app.detected.get('image_diff'), app.detected.get('manifest_diff'), settings)
                stored = self.store.get('rollup:' + app.id) or {}
                if stored.get('key') != key or stored['result'].get('status') != 'ok':
                    return True
            elif self.store.get('rollup:' + app.id):
                return True
            if self.rollback_due(app, settings):
                return True
        return False

    def rollback_due(self, app: AppRecord, settings: Settings) -> bool:
        window = [r for r in build_view(app, self.store.releases(app.id), settings).history
                  if not r.prerelease and not parse_version(r.version).is_prerelease][-settings.rollback_window:] if settings.rollback_window else []
        if not window or not app.installed_version or not any(r.notes.strip() for r in window):
            return bool(self.store.get('rollback:' + app.id))
        key = rollup_key(app, app.installed_version, app.installed_version, window, None, None, settings, ROLLBACK_PROMPT)
        stored = self.store.get('rollback:' + app.id) or {}
        return stored.get('key') != key or stored['result'].get('status') != 'ok'

    def request_classification(self) -> bool:
        if self.demo or self.closing or (self.classify_task and not self.classify_task.done()) or not (self.queue_length() or self.rollups_due()):
            return False
        self.classify_task = asyncio.create_task(self.classify_queue())
        return True

    def dequeue(self, item: dict):
        self.store.set('classification_queue', [i for i in self.store.get('classification_queue', []) if i != item])

    async def classify_queue(self):
        """Work the persisted queue one release at a time until empty or out of time budget."""
        started = time.monotonic()
        ready: list = [None]
        try:
            while not self.closing and not self.demo and time.monotonic() - started < self.classify_budget:
                queue = self.store.get('classification_queue', [])
                if not queue:
                    break
                item = queue[0]
                try:
                    if not await self.classify_item(item, ready):
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.store.log_error({'source':'classifier','message':f'Classification failed ({type(exc).__name__}); will retry on the next check.'})
                    self.dequeue(item)
            if not self.store.get('classification_queue', []):
                await self.refresh_rollups(ready)
        finally:
            self.classify_ended = time.monotonic()

    async def refresh_rollups(self, ready: list):
        """Rollups use the model too, so they run after collection with the queue, never inside it."""
        settings = self.settings()
        async def local_ready():
            if ready[0] is None:
                ready[0] = await self.local_ready(settings)
            return ready[0]
        records = self.store.apps()
        by_id = {a.id: a for a in records}
        for app in records:
            if self.closing or self.demo:
                break
            if hidden(app, settings, by_id):
                continue
            try:
                await self.refresh_rollup(app, settings, local_ready)
                await self.refresh_rollback(app, settings, local_ready)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.log_error({'source':'classifier','message':f'Summary failed ({type(exc).__name__}); will retry on the next check.'})

    async def classify_item(self, item: dict, ready: list) -> bool:
        """Process one queued release; False means stop the run and keep the queue."""
        app = self.store.app(item['app_id'])
        release = next((r for r in self.store.releases(item['app_id']) if r.id == item['release_id']), None)
        if not app or not release or not is_pending(release, app) or hidden(app, self.settings(), {a.id: a for a in self.store.apps()}):
            self.dequeue(item)
            return True
        settings = self.settings()
        generation = self.store.get('generation:' + app.id, 0)
        key = classification_key(release, app, settings)
        cached = self.store.get('classification:' + key)
        if cached and (cached.get('classification') or {}).get('status') == 'ok':
            release = release.model_copy(update=cached)
        elif not release.classification or release.classification_key != key or release.classification.get('status') != 'ok':
            if ready[0] is None:
                ready[0] = await self.local_ready(settings)
            if settings.classification_provider == 'local' and not ready[0]:
                self.store.log_error({'source':'ollama','message':'Configured model unavailable on the local Ollama endpoint. Pending classifications stay queued.'})
                return False
            release = await classify_release(release.model_copy(update={'classification':None}), app, settings,
                                             gemini_key=self.runtime.gemini_key, log_call=self.store.log_llm)
            self.store.set('classification:' + key, {'classification':release.classification,'classification_key':release.classification_key, 'model_disagreement':release.model_disagreement})
        profile_key = self.profile_key(app, release, settings)
        analysis = self.store.get('applicability:' + profile_key)
        if analysis is not None and all(entry.get('verified') for entry in analysis):
            release.applicability = analysis
        else:
            if ready[0] is None:
                ready[0] = await self.local_ready(settings)
            if ready[0]:
                release = await analyze_applicability(release, app, settings, log_call=self.store.log_llm)
            else:
                release.applicability = conservative_applicability(release.classification)
            self.store.set('applicability:' + profile_key, release.applicability)
        if self.store.get('generation:' + app.id, 0) != generation:
            return True  # Edited mid-inference: discard the result and redo with the new profile.
        current = next((r for r in self.store.releases(app.id) if r.id == release.id), None)
        if current and current.notes != release.notes:
            return True  # Notes were revised mid-inference: keep the re-queued item and redo it.
        if current:
            # Only the inference outputs: a collection may have refreshed other fields (e.g. published_at).
            self.store.save_release(current.model_copy(update={f: getattr(release, f) for f in
                ('classification', 'classification_key', 'applicability', 'model_disagreement')}))
        self.dequeue(item)
        self.store.set('last_classification', utcnow().isoformat())
        return True

    async def notify(self):
        if not self.store.get('last_poll'):
            return
        notifier = Notifier(self.store,self.runtime.ha_url,self.runtime.ha_token)
        await notifier.tick(self.settings(),self.views(),demo=self.demo)

    async def cycle(self):
        if not self.demo:
            last = self.store.get('last_poll')
            elapsed = (utcnow()-datetime.fromisoformat(last)).total_seconds() if last else float('inf')
            if self.poll_stale():
                self.abandon_poll()
            if elapsed >= self.settings().poll_hours*3600:
                self.request_poll()
            if not self.polling:
                await self.notify()
            # Resume a queue left over from a restart or a budget-limited run.
            if self.classify_ended is None or time.monotonic() - self.classify_ended >= self.classify_cooldown:
                self.request_classification()

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
