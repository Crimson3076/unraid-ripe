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


def test_http_cache_keeps_changelog_text_and_classification_key(tmp_path):
    from ripe.intelligence import classification_key
    from ripe.models import Settings
    secret = 'a-runtime-secret-value-for-tests'
    notes = 'Bearer authentication is now supported. token: optional for public repos.'
    db = Store(tmp_path / 'ripe.sqlite3', secrets=[secret])
    db.set('http:abc', {'body': [{'body': notes + ' ' + secret, 'token': 'x'}], 'fetched': 1})
    cached = db.get('http:abc')['body'][0]
    assert cached['body'] == notes + ' [redacted]'  # exact known secret still removed
    assert cached['token'] == 'x'
    db.set('http:def', {'body': notes})
    assert db.get('http:def')['body'] == notes
    app = AppRecord(id='docker:a', name='a', source='docker', installed_version='1.0.0')
    fresh = Release(id='github:o/a:1', app_id=app.id, version='2.0.0', notes=notes)
    again = fresh.model_copy(update={'notes': db.get('http:def')['body']})
    assert classification_key(fresh, app, Settings()) == classification_key(again, app, Settings())
    db.close()


def test_errors_group_per_source_and_resolve_when_source_recovers(tmp_path):
    db = Store(tmp_path / 'ripe.sqlite3')
    for _ in range(3):
        db.log_error({'source': 'docker:a', 'message': 'GitHub: HTTP 403'})
    db.log_error({'source': 'unraid', 'message': 'boom'})
    current = {e['source']: e for e in db.logs()['errors']}
    assert current['docker:a']['attempts'] == 3
    assert current['docker:a']['first_seen'] <= current['docker:a']['last_seen']
    db.resolve_errors({'unraid'})  # docker:a succeeded on the latest attempt
    assert [e['source'] for e in db.logs()['errors']] == ['unraid']
    db.log_error({'source': 'docker:a', 'message': 'fails again'})
    again = {e['source']: e for e in db.logs()['errors']}
    assert again['docker:a']['attempts'] == 1
    db.close()


def test_resolved_errors_are_pruned_after_retention(tmp_path):
    db = Store(tmp_path / 'ripe.sqlite3')
    db.log_error({'source': 'old', 'message': 'x'})
    db.log_error({'source': 'recent', 'message': 'x'})
    db.log_error({'source': 'open', 'message': 'x'})
    db.resolve_errors({'open'})
    db.db.execute("UPDATE errors SET resolved_at='2000-01-01T00:00:00+00:00' WHERE data LIKE '%\"old\"%'")
    db.prune_errors(14)
    sources = [json.loads(r[0])['source'] for r in db.db.execute('SELECT data FROM errors')]
    assert sorted(sources) == ['open', 'recent']
    db.close()
