import json
import stat
import httpx
import pytest
from fastapi.testclient import TestClient
from ripe.app import create_app
from ripe.config import Runtime
from ripe.connections import HomeAssistantConnection, ha_url
from ripe.intelligence.privacy import public_payload, sanitize_profile
from ripe.models import Profile
from ripe.privacy import redact


TOKEN = 'synthetic-home-assistant-token-for-tests'
URL = 'http://homeassistant.local:8123'


def test_private_settings_override_environment_and_disconnect_survives_restart(tmp_path):
    connection = HomeAssistantConnection(tmp_path, URL, TOKEN)
    assert connection.public()['source'] == 'environment'
    connection.save({'url': URL, 'token': ''})
    assert connection.credentials() == (URL, TOKEN)
    assert connection.public()['source'] == 'app'
    assert stat.S_IMODE(connection.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(connection.path.parent.stat().st_mode) == 0o700
    assert json.loads(connection.path.read_text())['token'] == TOKEN
    restarted = HomeAssistantConnection(tmp_path, 'https://old.example', 'legacy-environment-token')
    assert restarted.credentials() == (URL, TOKEN)
    restarted.disconnect()
    restarted = HomeAssistantConnection(tmp_path, URL, TOKEN)
    assert restarted.credentials() == ('', None)
    assert restarted.public()['configured'] is False
    assert restarted.public()['has_token'] is False


def test_invalid_values_and_changed_servers_cannot_reuse_a_token(tmp_path):
    connection = HomeAssistantConnection(tmp_path, URL, TOKEN)
    for data in ({'url': 'https://new.example'}, {'token': 'bad\nprivate-token'}, {'token': 'short'},
                 {'token': None}, {'clear_token': 'true'}, {'url': None}, {'unknown': TOKEN}):
        with pytest.raises(ValueError):
            connection.save(data)
    assert connection.credentials() == (URL, TOKEN)
    connection.save({'url': 'https://new.example', 'token': 'new-private-connection-token'})
    assert connection.credentials()[0] == 'https://new.example'
    for value in ('file:///etc/passwd', 'http://u:private@ha.local', 'http://ha.local?token=private',
                  'http://ha.local/%2e%2e/private', 'http://ha.local:invalid', 'http://bad host'):
        with pytest.raises(ValueError):
            ha_url(value)
    assert ha_url('http://ha') == 'http://ha'


def test_symlinks_and_corrupt_credentials_are_refused(tmp_path):
    connection = HomeAssistantConnection(tmp_path)
    connection.save({'url': URL, 'token': TOKEN})
    connection.path.write_text('{bad json')
    with pytest.raises(ValueError, match='could not be read'):
        HomeAssistantConnection(tmp_path)
    connection.path.unlink()
    elsewhere = tmp_path / 'outside.json'
    elsewhere.write_text('{}')
    connection.path.symlink_to(elsewhere)
    with pytest.raises(ValueError, match='symlinks'):
        HomeAssistantConnection(tmp_path)
    with pytest.raises(ValueError, match='symlinks'):
        connection.save({'url': URL, 'token': TOKEN})
    assert elsewhere.read_text() == '{}'


@pytest.mark.asyncio
async def test_probe_sends_bearer_token_and_never_follows_redirects(tmp_path):
    connection = HomeAssistantConnection(tmp_path)
    connection.save({'url': URL, 'token': TOKEN})
    requests = []
    def respond(request):
        requests.append(request)
        assert request.url.path == '/api/'
        assert request.headers['authorization'] == 'Bearer ' + TOKEN
        return httpx.Response(200, json={'message': 'API running.'})
    transport = httpx.MockTransport(respond)
    assert (await connection.test(transport=transport))['ok'] is True
    assert len(requests) == 1
    for status in (301, 302, 307, 308, 401, 403, 500):
        transport = httpx.MockTransport(lambda request: httpx.Response(status, headers={'location': 'https://evil.example'}, text=TOKEN))
        response = await connection.test(transport=transport)
        assert response['ok'] is False
        assert TOKEN not in json.dumps(connection.public())
    response = await connection.test(transport=httpx.MockTransport(lambda request: httpx.Response(200, text='x' * 70000)))
    assert response['ok'] is False


def test_tokens_are_redacted_from_store_profiles_and_public_model_payloads(tmp_path):
    connection = HomeAssistantConnection(tmp_path)
    connection.save({'url': URL, 'token': TOKEN})
    connection.save({'url': URL, 'token': 'replacement-home-assistant-test-token'})
    assert TOKEN not in json.dumps(redact({'message': TOKEN}))
    assert TOKEN not in json.dumps(public_payload('App', '1.2.3', 'Release text ' + TOKEN))
    assert TOKEN not in sanitize_profile(Profile(notes=TOKEN)).model_dump_json()


@pytest.fixture
def browser(tmp_path):
    app = create_app(Runtime(config_dir=tmp_path, ha_url=URL, ha_token=TOKEN, disable_scheduler=True,
                             github_token=None, gemini_key=None))
    with TestClient(app, base_url='http://localhost') as client:
        client.app.state.manager.request_poll = lambda: False
        yield client


HEADERS = {'X-Ripe-Request': '1'}


def test_connection_endpoints_are_private_and_protected(browser):
    route = '/api/connections/home-assistant'
    original = browser.get('/api/settings').json()
    public = browser.get(route).json()
    assert public['source'] == 'environment' and public['has_token'] is True
    assert TOKEN not in json.dumps(public)
    assert browser.put(route, json={'token': TOKEN}).status_code == 403
    assert browser.delete(route, headers={**HEADERS, 'Origin': 'https://hostile.example'}).status_code == 403
    saved = browser.put(route, json={'url': URL, 'token': 'replacement-private-home-assistant-token'}, headers=HEADERS)
    assert saved.status_code == 200 and saved.json()['configured'] is True
    manager = browser.app.state.manager
    assert manager.runtime.ha_token == 'replacement-private-home-assistant-token'
    assert manager.runtime.ha_url == URL
    assert browser.get('/api/settings').json() == original
    manager.store.log_error({'source': 'home_assistant', 'message': 'Error ' + TOKEN + ' replacement-private-home-assistant-token'})
    for path in ('/api/settings', '/api/dashboard', '/api/logs', route):
        body = browser.get(path).text
        assert TOKEN not in body and 'replacement-private-home-assistant-token' not in body
    assert browser.delete(route, headers=HEADERS).status_code == 200
    assert manager.runtime.ha_token is None
    assert manager.runtime.ha_url == ''


def test_saved_connection_is_loaded_by_coordinator_after_restart(tmp_path):
    connection = HomeAssistantConnection(tmp_path)
    connection.save({'url': URL, 'token': TOKEN})
    from ripe.service import Coordinator
    manager = Coordinator(Runtime(config_dir=tmp_path, ha_url='', ha_token=None, disable_scheduler=True))
    try:
        assert manager.runtime.ha_url == URL
        assert manager.runtime.ha_token == TOKEN
        assert TOKEN in manager.real_store.secrets
    finally:
        manager.close()


def test_demo_cannot_read_edit_or_test_live_connection(browser):
    route = '/api/connections/home-assistant'
    manager = browser.app.state.manager
    assert browser.post('/api/demo', headers=HEADERS).status_code == 200
    assert browser.get(route).json()['url'] == ''
    for method, suffix, data in (('put', '', {'url': URL, 'token': TOKEN}), ('delete', '', None), ('post', '/test', None)):
        kwargs = {'headers': HEADERS}
        if data is not None:
            kwargs['json'] = data
        assert getattr(browser, method)(route + suffix, **kwargs).status_code == 409
    assert manager.runtime.ha_token == TOKEN
    assert not manager.ha_connection.path.exists()


def test_validation_responses_never_echo_submitted_secrets(browser):
    route = '/api/connections/home-assistant'
    for data in ({'token': 'private\ninvalid-secret'}, {'token': 'x' * 4097}, {'url': 'http://user:private-password@ha.local'}):
        response = browser.put(route, headers=HEADERS, json=data)
        assert response.status_code == 422
        assert 'private-password' not in response.text and 'invalid-secret' not in response.text


def test_icon_and_connection_form_are_shipped_by_correct_app(browser):
    page = browser.get('/settings')
    assert page.status_code == 200
    assert 'id="ha-connection-form"' in page.text
    assert 'type="password"' in page.text
    assert 'Crimson3076/unraid-ripe/main/ripe/static/icon.png' in page.text
    response = browser.get('/static/icon.png')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('image/png')
    assert response.content.startswith(b'\x89PNG\r\n\x1a\n')


# ---- GitHub token / Gemini API key -------------------------------------------------
import logging
from ripe.connections import ApiCredential
from ripe.service import Coordinator

PROVIDERS = {'github': ('token', 'github_token', 'GH'), 'gemini': ('api_key', 'gemini_key', 'GM')}


def secret(kind, tag):
    return f'synthetic-{kind}-{tag}-value-1234567890'


def make_runtime(tmp_path, **env):
    return Runtime(config_dir=tmp_path, ha_url='', ha_token=None, disable_scheduler=True,
                   github_token=env.get('github'), gemini_key=env.get('gemini'))


@pytest.mark.parametrize('name', PROVIDERS)
def test_saved_value_overrides_env_survives_restart_and_clear_restores_env(tmp_path, name):
    field = PROVIDERS[name][0]
    env, saved = secret(name, 'env'), secret(name, 'saved')
    credential = ApiCredential(tmp_path, name, field, env)
    assert credential.public()['status'] == 'Set (from environment)'
    # Blank submissions keep the source and never copy the environment value.
    credential.save({field: ''})
    credential.save({})
    assert credential.source() == 'environment' and not credential.path.exists()
    credential.save({field: saved})
    assert credential.effective() == saved and credential.public()['status'] == 'Set (from Settings)'
    assert stat.S_IMODE(credential.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(credential.path.parent.stat().st_mode) == 0o700
    credential.save({field: ''})
    assert ApiCredential(tmp_path, name, field, env).effective() == saved
    credential.clear()
    assert credential.effective() == env and credential.source() == 'environment'
    assert ApiCredential(tmp_path, name, field, env).effective() == env
    unset = ApiCredential(tmp_path, name, field, None)
    assert unset.effective() is None and unset.public()['status'] == 'Not set'
    for bad in ({field: 'short'}, {field: 'bad value here'}, {field: None}, {'other': saved}, {field: 'x' * 5000}):
        with pytest.raises(ValueError):
            credential.save(bad)


def test_credential_symlinks_and_corruption_refused(tmp_path):
    ApiCredential(tmp_path, 'github', 'token').save({'token': secret('github', 'a')})
    path = tmp_path / 'secrets' / 'github.json'
    path.write_text('{bad')
    with pytest.raises(ValueError):
        ApiCredential(tmp_path, 'github', 'token')
    path.unlink()
    path.symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(ValueError):
        ApiCredential(tmp_path, 'github', 'token')


@pytest.mark.parametrize('name', PROVIDERS)
def test_coordinator_applies_credentials_without_restart_and_clear_is_independent(tmp_path, name):
    field, attr, _ = PROVIDERS[name]
    other = 'gemini' if name == 'github' else 'github'
    env, saved, replaced = secret(name, 'env'), secret(name, 'saved'), secret(name, 'replaced')
    manager = Coordinator(make_runtime(tmp_path, github=secret('github', 'env'), gemini=secret('gemini', 'env')))
    manager.request_poll = lambda: False
    try:
        assert getattr(manager.runtime, attr) == secret(name, 'env')
        manager.change_api_credential(name, {field: saved})
        assert getattr(manager.runtime, attr) == saved and saved in manager.real_store.secrets
        manager.change_api_credential(name, {field: replaced})
        assert getattr(manager.runtime, attr) == replaced
        manager.change_api_credential(name, clear=True)
        assert getattr(manager.runtime, attr) == secret(name, 'env')
        assert getattr(manager.runtime, PROVIDERS[other][1]) == secret(other, 'env')
        manager.change_api_credential(name, {field: saved})
    finally:
        manager.close()
    restarted = Coordinator(make_runtime(tmp_path, github=secret('github', 'env'), gemini=secret('gemini', 'env')))
    try:
        assert getattr(restarted.runtime, attr) == saved
        assert getattr(restarted.runtime, PROVIDERS[other][1]) == secret(other, 'env')
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_poll_passes_current_credentials_to_collect_and_classify(tmp_path, monkeypatch):
    from datetime import timedelta
    from ripe import service
    from ripe.models import AppRecord, Release, utcnow
    seen = {'collect': [], 'classify': []}
    async def fake_collect(settings, **kwargs):
        seen['collect'].append(kwargs['github_token'])
        app = AppRecord(id='a', name='A', source='docker', installed_version='1.0.0', latest_version='2.0.0')
        release = Release(id='r', app_id='a', version='2.0.0', published_at=utcnow() - timedelta(days=30), notes='Bug fixes and small improvements for the importer.')
        return [app], [release], []
    async def fake_classify(release, app, settings, *, gemini_key=None, log_call=None):
        seen['classify'].append(gemini_key)  # status is not ok, so nothing is cached
        return release.model_copy(update={'classification': {'status': 'unknown', 'tags': []}, 'classification_key': 'k'})
    async def ready(settings):
        return True
    async def analyze(release, *args, **kwargs):
        release.applicability = []
        return release
    monkeypatch.setattr(service, 'collect', fake_collect)
    monkeypatch.setattr(service, 'classify_release', fake_classify)
    monkeypatch.setattr(service, 'analyze_applicability', analyze)
    manager = Coordinator(make_runtime(tmp_path, github='env-github-token-value', gemini='env-gemini-key-value'))
    manager.request_poll = lambda: False
    manager.local_ready = ready
    try:
        await manager.poll()
        manager.change_api_credential('github', {'token': 'saved-github-token-value'})
        manager.change_api_credential('gemini', {'api_key': 'saved-gemini-key-value'})
        await manager.poll()
        manager.change_api_credential('github', clear=True)
        manager.change_api_credential('gemini', clear=True)
        await manager.poll()
    finally:
        manager.close()
    assert seen['collect'] == ['env-github-token-value', 'saved-github-token-value', 'env-github-token-value']
    assert seen['classify'] == ['env-gemini-key-value', 'saved-gemini-key-value', 'env-gemini-key-value']


@pytest.fixture
def cred_client(tmp_path):
    app = create_app(make_runtime(tmp_path, github=secret('github', 'env'), gemini=secret('gemini', 'env')))
    with TestClient(app, base_url='http://localhost') as client:
        client.app.state.manager.request_poll = lambda: False
        yield client


@pytest.mark.parametrize('name', PROVIDERS)
def test_credential_endpoints_never_expose_values_and_are_protected(cred_client, name, caplog):
    field = PROVIDERS[name][0]
    route = '/api/connections/' + name
    env, saved, replaced = secret(name, 'env'), secret(name, 'saved'), secret(name, 'replaced')
    assert cred_client.get(route).json()['status'] == 'Set (from environment)'
    assert cred_client.put(route, json={field: saved}).status_code == 403
    assert cred_client.delete(route, headers={**HEADERS, 'Origin': 'https://hostile.example'}).status_code == 403
    assert cred_client.post(route + '/test').status_code == 403
    with caplog.at_level(logging.DEBUG):
        bodies = [cred_client.put(route, headers=HEADERS, json={field: saved}),
                  cred_client.put(route, headers=HEADERS, json={field: ''}),
                  cred_client.put(route, headers=HEADERS, json={field: replaced})]
        assert [b.json()['status'] for b in bodies] == ['Set (from Settings)'] * 3
        bad = cred_client.put(route, headers=HEADERS, json={field: 'bad value ' + saved})
        assert bad.status_code == 422
        bodies += [bad, cred_client.get(route), cred_client.delete(route, headers=HEADERS)]
        assert bodies[-1].json()['status'] == 'Set (from environment)'
        manager = cred_client.app.state.manager
        manager.store.log_error({'source': 'x', 'message': ' '.join((env, saved, replaced))})
        for path in ('/api/settings', '/api/dashboard', '/api/logs', '/settings', route):
            bodies.append(cred_client.get(path))
    for response in bodies:
        for value in (env, saved, replaced):
            assert value not in response.text
    assert env not in caplog.text and saved not in caplog.text and replaced not in caplog.text
    assert cred_client.put(route, headers=HEADERS, json={'bogus': saved}).status_code == 422


def test_credential_demo_restrictions(cred_client):
    assert cred_client.post('/api/demo', headers=HEADERS).status_code == 200
    manager = cred_client.app.state.manager
    for name, (field, attr, _) in PROVIDERS.items():
        route = '/api/connections/' + name
        assert cred_client.get(route).json()['source'] == 'demo'
        assert cred_client.put(route, headers=HEADERS, json={field: secret(name, 'x')}).status_code == 409
        assert cred_client.delete(route, headers=HEADERS).status_code == 409
        assert cred_client.post(route + '/test', headers=HEADERS).status_code == 409
        assert not manager.api_credentials[name].path.exists()


def test_test_endpoint_without_credential_is_fixed_error(tmp_path):
    app = create_app(make_runtime(tmp_path))
    with TestClient(app, base_url='http://localhost') as client:
        for name in PROVIDERS:
            response = client.post(f'/api/connections/{name}/test', headers=HEADERS)
            assert response.status_code == 409


@pytest.mark.asyncio
async def test_github_probe_destination_headers_limit_and_failures(tmp_path):
    credential = ApiCredential(tmp_path, 'github', 'token', 'env-github-token-value')
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'resources': {'core': {'limit': 5000}}})
    result = await credential.test(transport=httpx.MockTransport(respond))
    assert result['ok'] and result['limit'] == 5000 and '5000' in result['message']
    assert str(requests[0].url) == 'https://api.github.com/rate_limit'
    assert requests[0].headers['authorization'] == 'Bearer env-github-token-value'
    higher = await credential.test(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'resources': {'core': {'limit': 12500}}})))
    assert higher['limit'] == 12500
    cases = [httpx.Response(401, text='env-github-token-value'), httpx.Response(302, headers={'location': 'https://evil.example'}),
             httpx.Response(500, text='env-github-token-value'), httpx.Response(200, json={'nope': 1}),
             httpx.Response(200, text='not json env-github-token-value'), httpx.Response(200, json={'resources': {'core': {'limit': 'x'}}})]
    for response in cases:
        out = await credential.test(transport=httpx.MockTransport(lambda r, response=response: response))
        assert out['ok'] is False and 'env-github-token-value' not in json.dumps(out)
    def boom(request):
        raise httpx.ConnectError('failed env-github-token-value')
    out = await credential.test(transport=httpx.MockTransport(boom))
    assert out['ok'] is False and 'env-github-token-value' not in json.dumps(out)


@pytest.mark.asyncio
async def test_gemini_probe_destination_header_and_redacted_errors(tmp_path):
    key = 'saved-gemini-key-value'
    credential = ApiCredential(tmp_path, 'gemini', 'api_key', None)
    with pytest.raises(ValueError):
        await credential.test()
    credential.save({'api_key': key})
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'models': []})
    assert (await credential.test(transport=httpx.MockTransport(respond)))['ok'] is True
    request = requests[0]
    assert request.url.scheme + '://' + request.url.host + request.url.path == 'https://generativelanguage.googleapis.com/v1beta/models'
    assert request.headers['x-goog-api-key'] == key and key not in str(request.url)
    err = httpx.Response(400, json={'error': {'message': f'API key not valid: {key}'}})
    out = await credential.test(transport=httpx.MockTransport(lambda r: err))
    assert out['ok'] is False and 'API key not valid' in out['message'] and key not in json.dumps(out)
    for response in (httpx.Response(302, headers={'location': 'https://evil.example'}), httpx.Response(200, json={'x': 1}), httpx.Response(200, text='nope')):
        out = await credential.test(transport=httpx.MockTransport(lambda r, response=response: response))
        assert out['ok'] is False
    def boom(request):
        raise httpx.ConnectError(key)
    out = await credential.test(transport=httpx.MockTransport(boom))
    assert out['ok'] is False and key not in json.dumps(out)
