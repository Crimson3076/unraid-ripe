from __future__ import annotations

import json
from datetime import datetime, timezone

from ripe.models import AppRecord, Release
from ripe.store import Store


def test_same_public_release_can_belong_to_two_apps_and_survives_restart(tmp_path):
    path = tmp_path / 'ripe.sqlite3'
    db = Store(path)
    for name in ('primary', 'secondary'):
        app_id = 'docker:' + name
        db.save_app(AppRecord(id=app_id, name=name, source='docker', installed_version='1.0.0'))
        db.save_release(Release(id='github:example/app:123', app_id=app_id, version='2.0.0', notes='Shared public release'))
    db.close()
    db = Store(path)
    assert len(db.apps()) == 2
    assert len(db.releases('docker:primary')) == 1
    assert len(db.releases('docker:secondary')) == 1
    assert db.releases('docker:primary')[0].app_id == 'docker:primary'
    db.close()


def test_dated_public_release_replaces_equivalent_ha_fallback_without_duplicates(tmp_path):
    db = Store(tmp_path / 'ripe.sqlite3')
    ha = Release(id='ha:update.core:1.2.0', app_id='ha:update.core', version='1.2.0', notes='HA summary')
    public = Release(id='github:example/core:123', app_id=ha.app_id, version='v1.2.0',
                     notes='Full public release notes', published_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    db.save_release(ha)
    db.save_release(public)
    rows = db.releases(ha.app_id)
    assert len(rows) == 1
    assert rows[0].id == public.id
    assert rows[0].published_at == public.published_at
    db.save_release(ha)  # Later fallback must not replace full dated notes.
    assert db.releases(ha.app_id)[0].id == public.id
    db.close()


def test_logs_redact_runtime_secrets_nested_credentials_and_url_userinfo(tmp_path):
    secret = 'a-runtime-secret-value-for-tests'
    db = Store(tmp_path / 'ripe.sqlite3', secrets=[secret])
    db.log_llm({'model': 'qwen3:8b', 'output': f'Bearer unsafe-token {secret}',
                'nested': {'password': 'never-store-this', 'authorization': 'unsafe-header'},
                'url': 'https://user:unsafe-password@example.com/path',
                'prompt_version': 'classification-v1', 'duration_ms': 123,
                'evidence_passed': False, 'retries': 1})
    db.log_error({'source': 'home_assistant', 'message': f'Failure {secret}'})
    output = json.dumps(db.logs())
    for prohibited in (secret, 'unsafe-token', 'never-store-this', 'unsafe-header', 'unsafe-password'):
        assert prohibited not in output
    assert 'classification-v1' in output
    assert 'duration_ms' in output
    assert 'evidence_passed' in output
    raw = ''.join(row[0] for row in db.db.execute('SELECT data FROM llm_calls'))
    assert secret not in raw
    assert 'never-store-this' not in raw
    db.close()
