from __future__ import annotations

import json
from fastapi.testclient import TestClient
import pytest

from ripe.app import create_app
from ripe.config import Runtime
from ripe.models import AppRecord, Release


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('TZ', 'America/New_York')
    runtime = Runtime(config_dir=tmp_path, disable_scheduler=True, ha_url='http://ha.local:8123',
                      ha_token='ha-test-secret-do-not-display',
                      github_token='github-test-secret-do-not-display',
                      gemini_key='gemini-test-secret-do-not-display')
    app = create_app(runtime)
    with TestClient(app, base_url='http://localhost') as browser:
        # Explicit mutations below test the API, not LAN collection.
        browser.app.state.manager.request_poll = lambda: False
        yield browser


def change(client, method, url, **kwargs):
    return getattr(client, method)(url, headers={'X-Ripe-Request': '1'}, **kwargs)


def test_mutations_require_same_origin_request(client):
    assert client.patch('/api/settings', json={'poll_hours': 24}).status_code == 403
    assert client.patch('/api/settings', json={'poll_hours': 24}, headers={
        'X-Ripe-Request': '1', 'Origin': 'https://hostile.example'}).status_code == 403
    assert client.patch('/api/settings', json={'poll_hours': 24}, headers={
        'X-Ripe-Request': '1', 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    valid = client.patch('/api/settings', json={'poll_hours': 24}, headers={
        'X-Ripe-Request': '1', 'Origin': 'http://localhost'})
    assert valid.status_code == 200
    assert valid.json()['poll_hours'] == 24


def test_settings_are_timezone_aware_persistent_and_never_return_secrets(client):
    assert client.get('/api/settings').json()['timezone'] == 'America/New_York'
    updated = change(client, 'patch', '/api/settings', json={
        'timezone': 'Pacific/Auckland', 'quiet_start': '21:30', 'quiet_end': '08:15',
        'digest_weekday': 4, 'digest_time': '10:05',
        'soak_days': {'infrastructure': 28, 'internet': 14, 'general': 10}})
    assert updated.status_code == 200
    saved = client.app.state.manager.real_store.settings()
    assert saved.timezone == 'Pacific/Auckland'
    assert saved.quiet_start == '21:30'
    for path in ('/api/settings', '/api/dashboard', '/api/logs'):
        output = client.get(path).text
        assert 'ha-test-secret-do-not-display' not in output
        assert 'github-test-secret-do-not-display' not in output
        assert 'gemini-test-secret-do-not-display' not in output


@pytest.mark.parametrize('data', [
    {'timezone': 'Not/A-Timezone'}, {'quiet_start': '25:30'},
    {'quiet_end': '9:00'}, {'digest_time': '09:30:10'}, {'poll_hours': 0},
    {'soak_days': {'general': 14}}, {'ollama_url': 'https://public-model.example.com'},
    {'ollama_url': 'http://user:secret@10.20.30.40:11434'},
    {'dashboard_url': 'http://localhost/?token=do-not-display'},
    {'demo_mode': True}, {'HA_TOKEN': 'do-not-display'},
])
def test_invalid_settings_are_rejected_without_echoing_inputs(client, data):
    before = client.get('/api/settings').json()
    response = change(client, 'patch', '/api/settings', json=data)
    assert response.status_code == 422
    assert 'do-not-display' not in response.text
    assert client.get('/api/settings').json() == before


def test_demo_is_isolated_from_real_inventory_and_cannot_notify(client):
    manager = client.app.state.manager
    real = manager.real_store
    assert real.apps() == []
    assert change(client, 'post', '/api/demo').status_code == 200
    dashboard = client.get('/api/dashboard').json()
    assert dashboard['demo'] is True
    assert len(dashboard['apps']) >= 5
    assert all(v['app']['source'] == 'demo' for v in dashboard['apps'])
    assert real.apps() == []
    change(client, 'patch', '/api/settings', json={'notifications_enabled': True, 'notification_script':'script.send_update_alert'})
    response = change(client, 'post', '/api/notifications/test')
    assert response.status_code == 409
    assert 'Demo' in response.json()['detail']
    assert real.settings().notifications_enabled is False
    assert change(client, 'delete', '/api/demo').status_code == 200
    assert client.get('/api/dashboard').json()['apps'] == []
    assert manager.store is real


def test_demo_never_replaces_existing_inventory(client):
    manager = client.app.state.manager
    manager.store.save_app(AppRecord(id='docker:real', name='Real app', source='docker'))
    assert change(client, 'post', '/api/demo').status_code == 409
    assert manager.store.app('docker:real') is not None
    assert manager.demo is False


def test_profile_edit_scrubs_secrets_and_invalidates_old_exclusions(client):
    change(client, 'post', '/api/demo')
    manager = client.app.state.manager
    original = client.get('/api/apps/demo-romm').json()
    assert any(item['status'] == 'does_not_apply' for r in original['pending'] for item in r['applicability'])
    response = change(client, 'patch', '/api/apps/demo-romm/profile', json={
        'exposure': 'internet', 'config': {
            'filesystem': {'roms_folder': 'roms'}, 'oidc_enabled': False,
            'adminPassword': 'private-account-password', 'accessToken': 'private-access-token',
            'nested': {'clientSecret': 'private-client-secret'}},
    })
    assert response.status_code == 200
    profile = response.json()['profile']
    assert profile['config']['filesystem']['roms_folder'] == 'roms'
    assert profile['exposure'] == 'internet'
    persisted = json.dumps(manager.store.get('profile:demo-romm'))
    rendered = client.get('/api/apps/demo-romm').text
    for secret in ('private-account-password', 'private-access-token', 'private-client-secret'):
        assert secret not in persisted
        assert secret not in rendered
    view = client.get('/api/apps/demo-romm').json()
    for item in (a for r in view['pending'] for a in r['applicability']):
        assert item['status'] == 'applies'
        assert item['verified'] is False


def test_mapping_change_clears_old_project_releases(client):
    manager = client.app.state.manager
    app = AppRecord(id='docker:app', name='App', source='docker', installed_version='1.0.0', upstream_repo='old/project')
    manager.store.save_app(app)
    manager.store.save_release(Release(id='github:old/project:2', app_id=app.id, version='2.0.0', notes='Old project notes'))
    response = change(client, 'patch', '/api/apps/docker:app/mapping', json={'upstream_repo': 'https://github.com/new/project.git'})
    assert response.status_code == 200
    assert manager.store.app(app.id).upstream_repo == 'new/project'
    assert manager.store.releases(app.id) == []
    assert manager.store.get('mapping:' + app.id) == {'upstream_repo': 'new/project'}


def test_pages_have_read_only_stage1_surface_and_security_headers(client):
    for path in ('/', '/settings', '/logs'):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    assert client.get('/health').json() == {'status': 'ok', 'stage': 1, 'read_only': True, 'last_collection': None, 'classification_queue_length': 0, 'last_classification': None, 'polling': False}
    for path in ('/api/update', '/api/exec', '/api/pull', '/api/install'):
        assert change(client, 'post', path).status_code == 404
    assert client.get('/api/settings', headers={'Host': 'hostile.example'}).status_code == 400

@pytest.mark.parametrize('data', [[{'token':'do-not-display'}], 'do-not-display', 123])
def test_malformed_body_never_echoes_secret_input(client, data):
    response=change(client,'patch','/api/settings',json=data)
    assert response.status_code==422
    assert 'do-not-display' not in response.text


def test_mapping_rejects_non_string_without_internal_error(client):
    change(client,'post','/api/demo')
    response=change(client,'patch','/api/apps/demo-romm/mapping',json={'upstream_repo':123})
    assert response.status_code==422


def test_public_defaults_do_not_choose_a_deployment_or_recipient():
    from ripe.models import Settings
    settings=Settings()
    assert settings.timezone=='UTC'
    assert settings.ollama_url==''
    assert settings.dashboard_url=='http://localhost:8087'
    assert settings.notification_script==''
    assert settings.notification_people==[]
    assert settings.notification_channel==''
    assert settings.notifications_enabled is False


def test_optional_integration_url_can_be_cleared(client):
    response=change(client,'patch','/api/settings',json={'ollama_url':''})
    assert response.status_code==200
    assert response.json()['ollama_url']==''


@pytest.mark.parametrize('script', ['notify.mobile_app_example', '../send_update_alert',
    'script.send_update_alert/other', 'script.send_update_alert?token=private', 'script.', 'script.invalid-name'])
def test_notification_script_is_limited_to_script_entity_ids(client,script):
    response=change(client,'patch','/api/settings',json={'notification_script':script})
    assert response.status_code==422


def test_notification_route_is_user_configured_and_required_before_enable(client):
    response=change(client,'patch','/api/settings',json={'notifications_enabled':True})
    assert response.status_code==422
    configured=change(client,'patch','/api/settings',json={
        'notifications_enabled':True, 'notification_script':'script.send_update_alert',
        'notification_people':['sample_user'], 'notification_channel':'Updates'})
    assert configured.status_code==200
    assert configured.json()['notification_script']=='script.send_update_alert'


def test_unconfigured_runtime_does_not_choose_integration_hosts(tmp_path,monkeypatch):
    for name in ('HA_URL','OLLAMA_URL','DASHBOARD_URL'):
        monkeypatch.delenv(name,raising=False)
    runtime=Runtime(config_dir=tmp_path,disable_scheduler=True)
    assert runtime.ha_url==''
    assert runtime.initial_settings().ollama_url==''
    assert runtime.initial_settings().dashboard_url=='http://localhost:8087'


def test_pages_show_the_ripe_version_number(client):
    from ripe import __version__
    page = client.get('/')
    assert page.status_code == 200
    assert f'Ripe v{__version__}' in page.text
