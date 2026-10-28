"""SQLite records with independent public classification and private profile caches."""
from __future__ import annotations
import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any
from .models import AppRecord, Release, Settings, utcnow
from .privacy import redact, redact_exact
from .versions import compare_versions


class Store:
    def __init__(self, path: Path, secrets: list[str] | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.secrets = secrets or []
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS apps (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS releases (id TEXT NOT NULL, app_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(app_id,id));
            CREATE INDEX IF NOT EXISTS release_app ON releases(app_id);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS llm_calls (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS errors (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL);
        ''')
        if 'resolved_at' not in [r[1] for r in self.db.execute('PRAGMA table_info(errors)')]:
            self.db.execute('ALTER TABLE errors ADD COLUMN resolved_at TEXT')
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.db.execute('SELECT data FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any):
        # Cached public HTTP bodies must round-trip unchanged: pattern redaction would
        # rewrite changelog text and change the classification key.
        clean = redact_exact(value, self.secrets) if key.startswith('http:') else redact(value, self.secrets)
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO kv(key,data) VALUES(?,?)', (key, json.dumps(clean, default=str)))

    def settings(self, initial: Settings | None = None) -> Settings:
        data = self.get('settings')
        return Settings.model_validate(data) if data else (initial or Settings())

    def save_settings(self, settings: Settings):
        self.set('settings', settings.model_dump(mode='json'))

    def apps(self) -> list[AppRecord]:
        with self.lock:
            return [AppRecord.model_validate_json(row[0]) for row in self.db.execute('SELECT data FROM apps ORDER BY id')]

    def app(self, app_id: str) -> AppRecord | None:
        with self.lock:
            row = self.db.execute('SELECT data FROM apps WHERE id=?', (app_id,)).fetchone()
        return AppRecord.model_validate_json(row[0]) if row else None

    def save_app(self, app: AppRecord):
        # Collectors only retain safe metadata; no environment or inspect payloads.
        data = redact(app.model_dump(mode='json'), self.secrets)
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO apps(id,data) VALUES(?,?)', (app.id, json.dumps(data)))

    def releases(self, app_id: str) -> list[Release]:
        with self.lock:
            rows = self.db.execute('SELECT data FROM releases WHERE app_id=?', (app_id,)).fetchall()
        return [Release.model_validate_json(row[0]) for row in rows]

    def save_release(self, release: Release):
        data = redact(release.model_dump(mode='json'), self.secrets)
        with self.lock, self.db:
            rows = self.db.execute('SELECT id,data FROM releases WHERE app_id=?', (release.app_id,)).fetchall()
            rank = lambda ident: 2 if ident.startswith(('github:', 'unraid:')) else 1
            for ident, stored in rows:
                old = Release.model_validate_json(stored)
                if ident != release.id and compare_versions(old.version, release.version) == 0:
                    if rank(ident) > rank(release.id):
                        return  # Retain dated public notes over an HA fallback.
                    self.db.execute('DELETE FROM releases WHERE app_id=? AND id=?', (release.app_id, ident))
            self.db.execute('INSERT OR REPLACE INTO releases(id,app_id,data) VALUES(?,?,?)', (release.id, release.app_id, json.dumps(data)))

    def log_llm(self, data: dict):
        self._log('llm_calls', data)

    def log_error(self, data: dict):
        self._log('errors', data)

    def resolve_errors(self, failing_sources: set[str]):
        """Mark unresolved errors resolved for every source that did not fail in the latest attempt."""
        now = utcnow().isoformat()
        with self.lock, self.db:
            rows = self.db.execute('SELECT id,data FROM errors WHERE resolved_at IS NULL').fetchall()
            ids = [i for i, d in rows if json.loads(d).get('source') not in failing_sources]
            self.db.executemany('UPDATE errors SET resolved_at=? WHERE id=?', [(now, i) for i in ids])

    def prune_errors(self, retention_days: int = 14):
        cutoff = (utcnow() - timedelta(days=retention_days)).isoformat()
        with self.lock, self.db:
            self.db.execute('DELETE FROM errors WHERE resolved_at IS NOT NULL AND resolved_at < ?', (cutoff,))

    def current_errors(self) -> list[dict]:
        """Unresolved errors grouped per source: latest message, first/last seen and attempt count."""
        with self.lock:
            rows = self.db.execute('SELECT id,created_at,data FROM errors WHERE resolved_at IS NULL ORDER BY id').fetchall()
        groups: dict[str, dict] = {}
        for i, t, d in rows:
            item = json.loads(d)
            g = groups.get(item.get('source'))
            if g is None:
                groups[item.get('source')] = dict(item, id=i, created_at=t, first_seen=t, last_seen=t, attempts=1)
            else:
                g.update(item, id=i, created_at=t, last_seen=t, attempts=g['attempts'] + 1)
        return sorted(groups.values(), key=lambda g: g['id'], reverse=True)

    def _log(self, table: str, data: dict):
        if table not in ('llm_calls', 'errors'):
            raise ValueError('Invalid log table')
        safe = redact(data, self.secrets)
        with self.lock, self.db:
            self.db.execute(f'INSERT INTO {table}(created_at,data) VALUES(?,?)', (utcnow().isoformat(), json.dumps(safe, default=str)))
            # Bound appdata growth; no tokens or config dumps belong in logs.
            self.db.execute(f'DELETE FROM {table} WHERE id <= (SELECT COALESCE(MAX(id),0)-5000 FROM {table})')

    def logs(self, limit: int = 150) -> dict:
        def read(table):
            with self.lock:
                rows = self.db.execute(f'SELECT id,created_at,data FROM {table} ORDER BY id DESC LIMIT ?', (min(limit, 500),)).fetchall()
            return [dict(json.loads(d), id=i, created_at=t) for i,t,d in rows]
        llm, errors = read('llm_calls'), self.current_errors()
        with self.lock:
            records = [json.loads(row[0]) for row in self.db.execute('SELECT data FROM releases')]
        compared = [r for r in records if len((r.get('classification') or {}).get('provenance', [])) > 1]
        disagreement_rate = sum(bool(r.get('model_disagreement')) for r in compared) / len(compared) if compared else 0.0
        return {'llm': llm, 'errors': errors, 'disagreement_rate': disagreement_rate, 'compared_releases': len(compared)}
