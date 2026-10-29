"""Inventory and analysis orchestration; deliberately no update executor."""
from __future__ import annotations
import asyncio
import hashlib
import json
from collections import Counter
from datetime import datetime
from typing import Any
import httpx
from .config import Runtime
from .models import AppRecord, AppView, Profile, Release, Settings, utcnow
from .autoprofile import AUTO_FIELDS, merge_profile
from .store import Store
from .sources import collect
from .intelligence import (classify_release, analyze_applicability, sanitize_profile, classification_key,
                           prompt_digest, build_rollup, rollup_key)
from .intelligence.evidence import conservative_applicability, keyword_warnings
from .ripeness import build_view, dashboard_sort_key
from .versions import compare_versions
from .notifications import Notifier
from .demo import seed_demo


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
        return view

    def views(self, now: datetime | None = None) -> list[AppView]:
        settings = self.settings()
        return sorted([self.with_rollup(build_view(app, self.store.releases(app.id), settings, now)) for app in self.store.apps()],key=dashboard_sort_key)

    def view(self, app_id: str) -> AppView | None:
        app = self.store.app(app_id)
        return self.with_rollup(build_view(app, self.store.releases(app_id), self.settings())) if app else None

    async def refresh_rollup(self, app: AppRecord, settings: Settings, local_ready) -> None:
        """Regenerate the app's rollup only when installed, latest, their notes or the model setup changed."""
        pending = build_view(app, self.store.releases(app.id), settings).pending
        image_diff, manifest = app.detected.get('image_diff'), app.detected.get('manifest_diff')
        # No version known (latest-only image): the installed digest identifies the installation.
        installed = app.installed_version or app.installed_digest
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
        self.store.set('rollup:' + app.id, {'key': key if result['status'] == 'ok' else None, 'result': result})

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
        data = view.model_dump(mode='json', exclude={'pending', 'history', 'available_notes', 'no_notes', 'rollup'})
        data['app']['detected'] = {k: v for k, v in data['app']['detected'].items() if k == 'mapping_hint'}
        data['tag_quotes'] = quotes
        data['rollup'] = {'headline': view.rollup['headline']} if view.rollup and view.rollup.get('headline') else None
        return data

    def dashboard(self) -> dict:
        views = self.views()
        counts = Counter(v.status for v in views)
        return {'apps': [self.card(v) for v in views], 'demo': self.demo,
                'summary': {s:counts[s] for s in ('ripe','waiting','recommended_now','read_first','up_to_date','unknown')},
                'last_poll':self.store.get('last_poll'), 'polling': self.polling,
                'integration_status': self.integration_status(), 'timezone':self.settings().timezone,
                'auto_profiles': self.auto_profiles()}

    def auto_profiles(self) -> list[dict]:
        """Apps Ripe filled in by itself that the user has not yet acknowledged."""
        reviewed = set(self.store.get('autoprofile:reviewed', []))
        found = []
        for app in self.store.apps():
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
        self.poll_task = asyncio.create_task(self.poll())
        return True

    def mark_changed(self, app_id: str):
        self.store.set('generation:' + app_id, self.store.get('generation:' + app_id, 0) + 1)
        if self.polling:
            self.store.set('poll_again', True)

    async def poll(self):
        if self.demo or self.lock.locked():
            return
        async with self.lock:
            self.polling = True
            try:
                settings = self.settings()
                prior_apps = self.store.apps()
                generations = {a.id: self.store.get('generation:' + a.id, 0) for a in prior_apps}
                apps, releases, errors = await collect(settings, github_token=self.runtime.github_token,
                    ha_token=self.runtime.ha_token, ha_url=self.runtime.ha_url,
                    docker_socket=self.runtime.docker_socket, templates_dir=self.runtime.templates_dir,
                    unraid_version_file=self.runtime.unraid_version_file,
                    cache_get=self.store.get, cache_set=self.store.set, existing_apps=self.store.apps())
                for error in errors:
                    self.store.log_error(error)
                by_app: dict[str,list[Release]] = {}
                for release in releases:
                    by_app.setdefault(release.app_id, []).append(release)
                local_ready = None
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
                        if compare_versions(release.version, app.installed_version) != 1:
                            self.store.save_release(release)
                            continue
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
                                release = await analyze_applicability(release,app,settings,log_call=self.store.log_llm)
                            else:
                                release.applicability = conservative_applicability(release.classification)
                            self.store.set('applicability:' + profile_key,release.applicability)
                        if self.store.get('generation:' + app.id, 0) != generation:
                            break
                        self.store.save_release(release)
                    if self.store.get('generation:' + app.id, 0) == generation:
                        async def ready():
                            nonlocal local_ready
                            if local_ready is None:
                                local_ready = await self.local_ready(settings)
                            return local_ready
                        await self.refresh_rollup(app, settings, ready)
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
                raise
            except Exception as exc:
                # External response bodies/exception strings can contain secrets.
                self.store.log_error({'source':'collector','message':f'Collection failed ({type(exc).__name__}); last known records preserved.'})
            finally:
                self.polling = False
                if not self.closing and self.store.get('poll_again'):
                    self.store.set('poll_again',False)
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
