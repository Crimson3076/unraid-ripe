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
