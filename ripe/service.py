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
from .models import AppRecord, AppView, Profile, Release, Settings, utcnow
from .store import Store
from .sources import collect
from .intelligence import classify_release, analyze_applicability, sanitize_profile, classification_key, prompt_digest
from .intelligence.evidence import conservative_applicability
from .ripeness import build_view, dashboard_sort_key
from .versions import compare_versions
from .notifications import Notifier
from .demo import seed_demo


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

    def views(self, now: datetime | None = None) -> list[AppView]:
        settings = self.settings()
        return sorted([build_view(app, self.store.releases(app.id), settings, now) for app in self.store.apps()],key=dashboard_sort_key)

    def view(self, app_id: str) -> AppView | None:
        app = self.store.app(app_id)
        return build_view(app, self.store.releases(app_id), self.settings()) if app else None

    def dashboard(self) -> dict:
        views = self.views()
        counts = Counter(v.status for v in views)
        return {'apps': [v.model_dump(mode='json') for v in views], 'demo': self.demo,
                'summary': {s:counts[s] for s in ('ripe','waiting','recommended_now','read_first','up_to_date','unknown')},
                'last_poll':self.store.get('last_poll'), 'polling': self.polling,
                **self.progress(),
                'integration_status': self.integration_status(), 'timezone':self.settings().timezone}

    def progress(self) -> dict:
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

    def mark_changed(self, app_id: str):
        self.store.set('generation:' + app_id, self.store.get('generation:' + app_id, 0) + 1)
        if self.polling:
            self.store.set('poll_again', True)

    async def poll(self, token: object | None = None):
        """Collect installed/latest versions and save them; classification is queued, not awaited."""
        if self.demo:
            return
        token = token or object()
        try:
            async with self.lock:
                self.poll_token = token
                self.polling = True
                self.poll_started = time.monotonic()
                await self.collect_inventory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # External response bodies/exception strings can contain secrets.
            self.store.log_error({'source':'collector','message':f'Collection failed ({type(exc).__name__}); last known records preserved.'})
        finally:
            # An abandoned poll no longer owns the flag (a newer poll may).
            if self.poll_token is token:
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
            cache_get=self.store.get, cache_set=self.store.set, existing_apps=self.store.apps())
        for error in errors:
            self.store.log_error(error)
        by_app: dict[str,list[Release]] = {}
        for release in releases:
            by_app.setdefault(release.app_id, []).append(release)
        work: list[Release] = []
        # No awaits below: the stored queue is replaced atomically.
        for app in apps:
            if self.store.get('generation:' + app.id, 0) != generations.get(app.id, 0):
                continue
            preference = self.store.get('profile:' + app.id)
            if preference is not None:
                app.profile = Profile.model_validate(preference)
            app.profile = sanitize_profile(app.profile)
            mapping = self.store.get('mapping:' + app.id)
            if mapping is not None:
                app.upstream_repo = mapping.get('upstream_repo')
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
            work.extend(self.pending_work(app, settings))
        # Mark a source failure as stale rather than silently deleting its
        # previous records; these visible errors prevent a fresh-data claim.
        current_ids = {a.id for a in apps}
        for prior in self.store.apps():
            if prior.id not in current_ids:
                prior.error = 'Not seen in the latest collection; source may be unavailable. Last known data shown.'
                self.store.save_app(prior)
        # Newest first, so the releases that matter most are summarised first.
        work.sort(key=lambda r: r.published_at.timestamp() if r.published_at else 0, reverse=True)
        self.store.set('classification_queue', [{'app_id':r.app_id,'release_id':r.id} for r in work])
        now = utcnow().isoformat()
        self.store.set('last_poll', now)
        self.store.set('last_collection', now)
        self.store.set('poll_errors', errors)
        self.request_classification()
        await self.notify()

    def pending_work(self, app: AppRecord, settings: Settings) -> list[Release]:
        """Apply cached results to pending releases; return those still needing the model."""
        work = []
        # Older records supply history and base-line dates without spending LLM
        # work on releases that aren't pending.
        for release in build_view(app, self.store.releases(app.id), settings).pending:
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
                work.append(release)
        return work

    @staticmethod
    def profile_key(app: AppRecord, release: Release, settings: Settings) -> str:
        return fingerprint([app.profile.model_dump(),release.classification_key,settings.local_model,settings.ollama_url,prompt_digest()])

    def queue_length(self) -> int:
        return len(self.store.get('classification_queue', []))

    def request_classification(self) -> bool:
        if self.demo or self.closing or (self.classify_task and not self.classify_task.done()) or not self.queue_length():
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
        finally:
            self.classify_ended = time.monotonic()

    async def classify_item(self, item: dict, ready: list) -> bool:
        """Process one queued release; False means stop the run and keep the queue."""
        app = self.store.app(item['app_id'])
        release = next((r for r in self.store.releases(item['app_id']) if r.id == item['release_id']), None)
        if not app or not release or compare_versions(release.version, app.installed_version) != 1:
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
        if current and current.notes == release.notes:
            self.store.save_release(release)
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
