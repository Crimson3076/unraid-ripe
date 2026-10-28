import json
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from ripe.config import Runtime
from ripe.intelligence import build_rollup, rollup_key
from ripe.intelligence import service as intelligence
from ripe.intelligence.rollup import CHUNK_SIZE
from ripe.models import AppRecord, Release, Settings, utcnow
from ripe.sources.collector import RateLimited
from ripe.service import Coordinator
import ripe.service as service
from ripe.sources.collector import CachedHTTP, RegistryReader, SourceError, manifest_diff

SENSORS = 'Added new sensors, siren and shock switches, and trunk and locate buttons.'
TOKEN = 'Token storage moved to the config entry; reauthenticate after updating.'


def app(installed='2026.4.10', **kwargs):
    return AppRecord(id='drone', name='DroneMobile', source='docker', installed_version=installed,
                     latest_version='2026.7.2', upstream_repo='example/dronemobile', **kwargs)


def release(version, notes, age=10):
    return Release(id=f'github:example/dronemobile:{version}', app_id='drone', version=version,
                   published_at=utcnow() - timedelta(days=age), notes=notes)


def pending():
    return [release('2026.6.4', SENSORS), release('2026.6.5', TOKEN), release('2026.6.6', ''),
            release('2026.6.26', 'Docs only: HACS metadata.'), release('2026.7.2', '')]


def item(text, version, quote, source='release'):
    return {'text': text, 'source': source, 'release': version, 'evidence': quote}


def reply(**groups):
    return json.dumps({g: groups.get(g, []) for g in ('action', 'security', 'features', 'fixes', 'under_the_hood')})


@pytest.mark.asyncio
async def test_rollup_groups_items_and_drops_unsupported_ones(monkeypatch):
    call = AsyncMock(return_value=reply(
        features=[item('New sensors, siren/shock switches and trunk/locate buttons.', '2026.6.4', SENSORS)],
        action=[item('Reauthenticate: token storage moved.', '2026.6.5', TOKEN)],
        fixes=[item('Invented fix.', '2026.6.5', 'This sentence is not in any release.')],
        security=[item('Cites a release outside the span.', '9.9.9', TOKEN)]))
    monkeypatch.setattr(intelligence, '_call_model', call)
    result = await build_rollup(app(), '2026.4.10', '2026.7.2', pending(), Settings())
    assert result['status'] == 'ok'
    assert list(result['groups']) == ['action', 'features']
    assert result['groups']['features'][0]['release'] == '2026.6.4'
    assert result['groups']['features'][0]['evidence'] == SENSORS
    assert result['groups']['action'][0]['release'] == '2026.6.5'
    assert result['headline'].startswith('What you need to do: Reauthenticate')
    assert result['headline'].endswith('(+1 more)')
    assert result['no_notes'] == ['2026.6.6', '2026.7.2']
    # Releases without notes are never sent to the model.
    sent = call.call_args.args[3]
    assert [r['version'] for r in sent['releases']] == ['2026.6.4', '2026.6.5', '2026.6.26']


@pytest.mark.asyncio
async def test_prompt_names_every_category(monkeypatch):
    call = AsyncMock(return_value=reply())
    monkeypatch.setattr(intelligence, '_call_model', call)
    await build_rollup(app(), '2026.4.10', '2026.7.2', pending(), Settings())
    system = call.call_args.args[2]
    for category in ('action', 'security', 'features', 'fixes', 'under_the_hood'):
        assert category in system


@pytest.mark.asyncio
async def test_nothing_notable_is_said_plainly(monkeypatch):
    monkeypatch.setattr(intelligence, '_call_model', AsyncMock(return_value=reply()))
    result = await build_rollup(app(), '2026.4.10', '2026.7.2', pending(), Settings())
    assert result['status'] == 'ok' and result['groups'] == {}
    assert result['headline'] == 'Nothing notable found since 2026.4.10; 2 releases have no notes.'


@pytest.mark.asyncio
async def test_image_env_diff_becomes_under_the_hood_without_a_model_call(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(intelligence, '_call_model', call)
    diff = [{'name': 'PHP', 'key': 'PHP_VERSION', 'installed': '8.2.12', 'latest': '8.3.4'}]
    result = await build_rollup(app(), '2026.4.10', '2026.4.10', [], Settings(), image_diff=diff)
    call.assert_not_awaited()
    [fact] = result['groups']['under_the_hood']
    assert fact['text'] == 'PHP changes from 8.2.12 to 8.3.4 in the container image.'
    assert fact['source'] == 'image' and fact['evidence'] == 'PHP_VERSION: 8.2.12 → 8.3.4'
    assert result['headline'].startswith('Under the hood: PHP changes')


@pytest.mark.asyncio
async def test_manifest_changes_cite_the_diff_and_fabrications_are_dropped(monkeypatch):
    manifest = [{'file': 'composer.json', 'removed': ['"php": ">=8.2"'], 'added': ['"php": ">=8.3"']}]
    monkeypatch.setattr(intelligence, '_call_model', AsyncMock(return_value=reply(under_the_hood=[
        item('PHP requirement raised to 8.3.', '', '"php": ">=8.3"', 'manifest'),
        item('Node 22.', '', '"node": ">=22"', 'manifest')])))
    result = await build_rollup(app(), '2026.4.10', '2026.7.2', [], Settings(), manifest_diff=manifest)
    assert [i['text'] for i in result['groups']['under_the_hood']] == ['PHP requirement raised to 8.3.']


@pytest.mark.asyncio
async def test_long_span_is_mapped_in_chunks_then_reduced(monkeypatch):
    big = [release(f'2026.6.{n}', f'Added feature number {n}. ' + 'Filler text. ' * (CHUNK_SIZE // 20)) for n in range(1, 4)]
    seen = []
    async def fake(provider, model, prompt, payload, settings, key):
        seen.append(payload)
        if 'candidate_items' in payload:
            merged = [i for items in payload['candidate_items'].values() for i in items]
            return reply(features=merged[:1])
        version = payload['releases'][0]['version']
        return reply(features=[item(f'Feature {version}.', version, f'Added feature number {version[-1]}.')])
    monkeypatch.setattr(intelligence, '_call_model', fake)
    result = await build_rollup(app(), '2026.5.0', '2026.6.3', big, Settings())
    assert len(seen) >= 3 and 'candidate_items' in seen[-1]
    assert all(len(json.dumps(p)) < CHUNK_SIZE * 2 for p in seen)
    assert [i['release'] for i in result['groups']['features']] == ['2026.6.1']


@pytest.mark.asyncio
async def test_local_only_notes_never_reach_gemini(monkeypatch):
    providers = []
    async def fake(provider, model, prompt, payload, settings, key):
        providers.append(provider)
        return reply()
    monkeypatch.setattr(intelligence, '_call_model', fake)
    ha = [Release(id='ha:update.x:2', app_id='drone', version='2', notes='Local notes.')]
    await build_rollup(app('1'), '1', '2', ha, Settings(classification_provider='gemini', second_opinion=True), gemini_key='k' * 20)
    assert providers == ['local']


@pytest.mark.asyncio
async def test_model_failure_is_unavailable_and_keeps_image_facts(monkeypatch):
    monkeypatch.setattr(intelligence, '_call_model', AsyncMock(side_effect=intelligence.LLMUnavailable('Local model HTTP 500')))
    diff = [{'name': 'Node', 'key': 'NODE_VERSION', 'installed': '20.1', 'latest': '22.0'}]
    result = await build_rollup(app(), '2026.4.10', '2026.7.2', pending(), Settings(), image_diff=diff)
    assert result['status'] == 'unavailable' and 'HTTP 500' in result['error']
    assert result['groups']['under_the_hood']


def test_rollup_key_tracks_versions_notes_and_facts():
    a, p, s = app(), pending(), Settings()
    key = rollup_key(a, '2026.4.10', '2026.7.2', p, None, None, s)
    assert key == rollup_key(a, '2026.4.10', '2026.7.2', p, None, None, s)
    assert key != rollup_key(a, '2026.5.1', '2026.7.2', p, None, None, s)
    assert key != rollup_key(a, '2026.4.10', '2026.7.3', p, None, None, s)
    assert key != rollup_key(a, '2026.4.10', '2026.7.2', p[:-1] + [release('2026.7.2', 'Now with notes.')], None, None, s)
    assert key != rollup_key(a, '2026.4.10', '2026.7.2', p, [{'name': 'PHP'}], None, s)
    assert key != rollup_key(a, '2026.4.10', '2026.7.2', p, None, None, Settings(local_model='other'))


def runtime(tmp_path):
    return Runtime(config_dir=tmp_path, disable_scheduler=True, ha_token=None, github_token=None, gemini_key=None)


@pytest.mark.asyncio
async def test_rollup_is_cached_until_installed_or_latest_changes(tmp_path, monkeypatch):
    manager = Coordinator(runtime(tmp_path))
    record = app()
    for r in pending():
        manager.store.save_release(r)
    manager.store.save_app(record)
    call = AsyncMock(return_value=reply(features=[item('New sensors.', '2026.6.4', SENSORS)]))
    monkeypatch.setattr(intelligence, '_call_model', call)
    async def ready():
        return True
    try:
        settings = manager.settings().model_copy(update={'ollama_url': 'http://localhost:11434'})
        await manager.refresh_rollup(record, settings, ready)
        await manager.refresh_rollup(record, settings, ready)
        assert call.await_count == 1
        assert manager.view('drone').rollup['groups']['features'][0]['text'] == 'New sensors.'
        # A newer release changes "latest": regenerate.
        manager.store.save_release(release('2026.7.9', 'Fixed a thing.'))
        record.latest_version = '2026.7.9'
        manager.store.save_app(record)
        await manager.refresh_rollup(record, settings, ready)
        assert call.await_count == 2
        # Moving the installed version regenerates, and the old rollup stops matching the view.
        record.installed_version = '2026.6.4'
        manager.store.save_app(record)
        assert manager.view('drone').rollup is None
        await manager.refresh_rollup(record, settings, ready)
        assert call.await_count == 3
        assert manager.view('drone').rollup['installed'] == '2026.6.4'
        assert manager.view('drone').no_notes == ['2026.6.6', '2026.7.2']
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_unavailable_rollup_is_retried_not_cached(tmp_path, monkeypatch):
    manager = Coordinator(runtime(tmp_path))
    record = app()
    for r in pending():
        manager.store.save_release(r)
    manager.store.save_app(record)
    call = AsyncMock(return_value=reply())
    monkeypatch.setattr(intelligence, '_call_model', call)
    state = {'up': False}
    async def ready():
        return state['up']
    try:
        await manager.refresh_rollup(record, manager.settings(), ready)
        call.assert_not_awaited()
        assert manager.view('drone').rollup['status'] == 'unavailable'
        state['up'] = True
        await manager.refresh_rollup(record, manager.settings(), ready)
        assert manager.view('drone').rollup['status'] == 'ok'
    finally:
        manager.close()


async def public_resolver(name, port):
    return ['93.184.216.34']


def registry_handler(installed_env, latest_env, hits):
    def config(env, labels=None):
        return {'config': {'Env': env, 'Labels': labels or {}}}
    def handler(request):
        hits.append(request.url.path)
        path = request.url.path
        if path.endswith('/manifests/latest'):
            return httpx.Response(200, json={'config': {'digest': 'sha256:' + 'b' * 64}}, headers={'docker-content-digest': 'sha256:latest'})
        if path.endswith('/manifests/sha256:' + 'a' * 64):
            return httpx.Response(200, json={'manifests': [
                {'digest': 'sha256:' + 'c' * 64, 'platform': {'os': 'linux', 'architecture': 'arm64'}},
                {'digest': 'sha256:' + 'd' * 64, 'platform': {'os': 'linux', 'architecture': 'amd64'}}]})
        if path.endswith('/manifests/sha256:' + 'd' * 64):
            return httpx.Response(200, json={'config': {'digest': 'sha256:' + 'e' * 64}})
        if path.endswith('/blobs/sha256:' + 'e' * 64):
            return httpx.Response(307, headers={'location': 'https://cdn.example-registry.test/blob'})
        if request.url.host == 'cdn.example-registry.test':
            assert 'authorization' not in request.headers
            return httpx.Response(200, json=config(installed_env))
        if path.endswith('/blobs/sha256:' + 'b' * 64):
            return httpx.Response(200, json=config(latest_env, {'org.opencontainers.image.base.name': 'docker.io/library/debian:bookworm'}))
        raise AssertionError(str(request.url))
    return handler


@pytest.mark.asyncio
async def test_registry_image_config_diff_without_pulling_layers():
    hits = []
    installed = ['PATH=/usr/bin', 'PHP_VERSION=8.2.12', 'NODE_VERSION=20.1.0', 'GITHUB_TOKEN_VERSION=zzz', 'DB_PASSWORD=hunter2', 'APP_VERSION=1.0']
    latest = ['PATH=/usr/bin', 'PHP_VERSION=8.3.4', 'NODE_VERSION=20.1.0', 'DB_PASSWORD=other', 'APP_VERSION=2.0']
    async with httpx.AsyncClient(transport=httpx.MockTransport(registry_handler(installed, latest, hits))) as client:
        reader = RegistryReader(client, min_interval=0)
        reader.resolve = public_resolver
        diff = await reader.image_diff(
            'ghcr.io/owner/app:latest', 'sha256:' + 'a' * 64, '2.0', ('1.0', '2.0'))
    assert {d['key'] for d in diff} == {'PHP_VERSION', 'org.opencontainers.image.base.name'}
    php = next(d for d in diff if d['key'] == 'PHP_VERSION')
    assert (php['name'], php['installed'], php['latest']) == ('PHP', '8.2.12', '8.3.4')
    assert 'hunter2' not in json.dumps(diff) and 'PASSWORD' not in json.dumps(diff)
    # Only manifests and the two config blobs are read, never a layer.
    assert all('/manifests/' in p or p.endswith(('/blobs/sha256:' + 'e' * 64, '/blobs/sha256:' + 'b' * 64, '/blob')) for p in hits)


@pytest.mark.asyncio
async def test_blob_redirect_to_private_or_http_host_is_refused():
    def handler(request):
        return httpx.Response(307, headers={'location': 'http://169.254.169.254/latest'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceError, match='blob redirect'):
            await RegistryReader(client, min_interval=0).request('ghcr.io', 'owner/app', 'blobs/sha256:' + 'e' * 64)


@pytest.mark.asyncio
async def test_manifest_diff_keeps_only_dependency_manifest_changes():
    def handler(request):
        assert request.url.path.endswith('/compare/v1.0.0...v1.1.0')
        return httpx.Response(200, json={'files': [
            {'filename': 'composer.json', 'patch': '@@\n-    "php": ">=8.2",\n+    "php": ">=8.3",\n unchanged'},
            {'filename': 'src/main.py', 'patch': '+print("hi")'},
            {'filename': 'package-lock.json', 'patch': '+x'},
            {'filename': 'web/package.json', 'patch': '--- a\n+++ b\n+    "vite": "^6.0.0"'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        changes = await manifest_diff(CachedHTTP(client), 'owner/app', 'v1.0.0', 'v1.1.0')
    assert changes == [{'file': 'composer.json', 'removed': ['"php": ">=8.2",'], 'added': ['"php": ">=8.3",']},
                       {'file': 'web/package.json', 'removed': [], 'added': ['"vite": "^6.0.0"']}]


def blob_client(handler, resolver=public_resolver):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = RegistryReader(client, min_interval=0)
    reader.resolve = resolver
    return client, reader


@pytest.mark.asyncio
@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.5', '169.254.169.254', '192.168.1.2', '::1', 'fd00::1'])
async def test_blob_redirect_to_hostname_resolving_privately_is_refused(address):
    hits = []
    def handler(request):
        hits.append(request.url.host)
        return httpx.Response(307, headers={'location': 'https://cdn.example.test/blob'})
    async def resolver(name, port):
        return ['93.184.216.34', address]
    client, reader = blob_client(handler, resolver)
    async with client:
        with pytest.raises(SourceError, match='blob redirect'):
            await reader.request('ghcr.io', 'owner/app', 'blobs/sha256:' + 'e' * 64)
    assert hits == ['ghcr.io']


@pytest.mark.asyncio
async def test_blob_redirect_that_does_not_resolve_is_refused():
    def handler(request):
        return httpx.Response(307, headers={'location': 'https://cdn.example.test/blob'})
    async def resolver(name, port):
        raise OSError('nxdomain')
    client, reader = blob_client(handler, resolver)
    async with client:
        with pytest.raises(SourceError, match='blob redirect'):
            await reader.request('ghcr.io', 'owner/app', 'blobs/sha256:' + 'e' * 64)


@pytest.mark.asyncio
async def test_config_blob_is_capped_at_one_megabyte():
    big = b'{"config": {"Labels": {"x": "' + b'a' * (1024 * 1024) + b'"}}}'
    def handler(request):
        if request.url.host == 'ghcr.io':
            return httpx.Response(307, headers={'location': 'https://cdn.example.test/blob'})
        return httpx.Response(200, content=big)
    client, reader = blob_client(handler)
    async with client:
        with pytest.raises(SourceError, match='too large'):
            await reader.request('ghcr.io', 'owner/app', 'blobs/sha256:' + 'e' * 64)
    ok_client, ok_reader = blob_client(lambda r: httpx.Response(200, json={'config': {}}))
    async with ok_client:
        assert (await ok_reader.request('ghcr.io', 'owner/app', 'blobs/sha256:' + 'e' * 64)).json() == {'config': {}}


@pytest.mark.asyncio
async def test_manifest_diff_drops_lines_that_may_carry_credentials():
    def handler(request):
        return httpx.Response(200, json={'files': [
            {'filename': 'requirements.txt', 'patch': '+--index-url https://user:pass@pkgs.example.com/simple\n'
             '+git+ssh://deploy:hunter2@host/repo.git\n+private @ user:pw@host\n+flask==3.0.0'},
            {'filename': 'package.json', 'patch': '+    "@types/node": "^22.0.0"'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        changes = await manifest_diff(CachedHTTP(client), 'owner/app', 'v1', 'v2')
    dumped = json.dumps(changes)
    assert 'pass' not in dumped and 'hunter2' not in dumped and 'user:pw' not in dumped
    assert changes[0]['added'] == ['flask==3.0.0']
    assert changes[1]['added'] == ['"@types/node": "^22.0.0"']


@pytest.mark.asyncio
async def test_latest_only_image_gets_a_rollup_from_its_digest(tmp_path, monkeypatch):
    manager = Coordinator(runtime(tmp_path))
    record = AppRecord(id='docker:img', name='img', source='docker', image='ghcr.io/o/img:latest', installed_digest='sha256:' + 'a' * 64,
                       latest_digest='sha256:' + 'b' * 64, detected={'image_diff': [{'name': 'PHP', 'key': 'PHP_VERSION', 'installed': '8.2', 'latest': '8.3'}]})
    manager.store.save_app(record)
    async def ready():
        return True
    try:
        await manager.refresh_rollup(record, manager.settings(), ready)
        rollup = manager.store.get('rollup:docker:img')
        assert rollup and rollup['result']['status'] == 'ok' and rollup['key']
        assert rollup['result']['groups']['under_the_hood'][0]['text'].startswith('PHP changes from 8.2 to 8.3')
        # The read side uses the same digest identity, so the rollup is actually shown.
        assert manager.view('docker:img').rollup == rollup['result']
        # A different target digest is a different rollup key.
        key = rollup['key']
        record.latest_digest = 'sha256:' + 'c' * 64
        await manager.refresh_rollup(record, manager.settings(), ready)
        assert manager.store.get('rollup:docker:img')['key'] != key
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_direct_config_blob_without_redirect_is_capped_while_streaming():
    big = b'{"config": {"Labels": {"x": "' + b'a' * (1024 * 1024) + b'"}}}'
    client, reader = blob_client(lambda r: httpx.Response(200, content=big))
    async with client:
        with pytest.raises(SourceError, match='too large'):
            await reader.request('ghcr.io', 'owner/app', 'blobs/sha256:' + 'e' * 64)


@pytest.mark.asyncio
async def test_blob_hop_rate_limit_and_error_statuses():
    def redirecting(final):
        return lambda r: httpx.Response(307, headers={'location': 'https://cdn.example.test/blob'}) if r.url.host == 'ghcr.io' else final(r)
    client, reader = blob_client(redirecting(lambda r: httpx.Response(429, headers={'retry-after': '7'})))
    async with client:
        with pytest.raises(RateLimited):
            await reader.request('ghcr.io', 'owner/app', 'blobs/sha256:' + 'e' * 64)
    for status, headers in ((503, {}), (302, {'location': 'https://other.example.test/x'})):
        client, reader = blob_client(redirecting(lambda r, s=status, h=headers: httpx.Response(s, headers=h)))
        async with client:
            with pytest.raises(SourceError):
                await reader.request('ghcr.io', 'owner/app', 'blobs/sha256:' + 'e' * 64)


@pytest.mark.asyncio
async def test_compressed_blob_is_decoded_once():
    import gzip
    body = json.dumps({'config': {'Labels': {'a': 'b'}}}).encode()
    zipped = gzip.compress(body)
    direct = lambda r: httpx.Response(200, headers={'content-encoding': 'gzip'}, content=zipped)
    hop = lambda r: httpx.Response(307, headers={'location': 'https://cdn.example.test/blob'}) if r.url.host == 'ghcr.io' else direct(r)
    for handler in (direct, hop):
        client, reader = blob_client(handler)
        async with client:
            response = await reader.request('ghcr.io', 'owner/app', 'blobs/sha256:' + 'e' * 64)
            assert response.json() == {'config': {'Labels': {'a': 'b'}}}
