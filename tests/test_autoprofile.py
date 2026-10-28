from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from ripe import autoprofile
from ripe.autoprofile import edited_origin, merge_profile, probe_login, read_cloudflared_targets, read_npm_targets, split_env
from ripe.config import Runtime
from ripe.models import AppRecord, Profile, Release, Settings
from ripe.service import Coordinator
from ripe.sources.collector import DockerReader
import ripe.service as service

IMAGE = 'sha256:' + 'b' * 64


def ident(n):
    return f'{n:x}' * 64


def container(n, name, image, ports=None, networks=('appnet',), labels=None):
    return {'Id': ident(n), 'Names': ['/' + name], 'Image': image, 'ImageID': IMAGE, 'Labels': labels or {},
            'Ports': ports if ports is not None else [], 'Mounts': [],
            'NetworkSettings': {'Networks': {n_: {'IPAddress': f'172.20.0.{n}'} for n_ in networks}}}


def docker(containers, inspect=None, image=None):
    inspect = inspect or {}

    def handler(request):
        assert request.method == 'GET'
        path = request.url.path
        if path == '/containers/json':
            return httpx.Response(200, json=containers)
        if path.startswith('/containers/'):
            return httpx.Response(200, json=inspect.get(path.split('/')[2], {}))
        return httpx.Response(200, json=image or {'Config': {}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='http://docker')


async def scan(containers, inspect=None, image=None, templates=None, targets=None, prober=None):
    async with docker(containers, inspect, image) as client:
        reader = DockerReader(client, host='tower')
        apps = await reader.apps(templates or {}, {}, targets, prober)
    return {a.name: a for a in apps}


@pytest.mark.parametrize('entry,kept', [
    ('DB_HOST=dawarich_db', True), ('REDIS_URL=redis://cache:6379', True), ('DB_PORT=5432', True), ('TZ=Europe/Berlin', True),
    ('APP_VERSION=1.2.3', True), ('DB_PASSWORD=hunter2', False), ('API_TOKEN_URL=x', False), ('SECRET_HOST=x', False),
    ('AUTH_URL=x', False), ('SSH_KEY_HOST=x', False), ('SOMETHING_ELSE=x', False), ('HOSTNAME=x', False)])
def test_env_value_allowlist_and_denylist(entry, kept):
    names, values = split_env([entry])
    name = entry.split('=')[0]
    assert names == [name]
    assert (name in values) is kept


def test_env_with_credentials_in_value_is_never_stored():
    facts = autoprofile.read_facts({}, {}, {'Config': {'Env': ['DATABASE_URL=postgres://u:pw@dawarich_db/x', 'TZ=UTC']}})
    cfg = autoprofile.config_of(facts)
    assert cfg['env'] == {'tz': 'UTC'}
    assert 'pw' not in json.dumps(cfg)


async def test_dawarich_depends_on_db_and_infra_tiers():
    apps = await scan(
        [container(1, 'dawarich_app', 'freikin/dawarich:1.0', [{'IP': '0.0.0.0', 'PrivatePort': 3000, 'PublicPort': 3000, 'Type': 'tcp'}]),
         container(2, 'dawarich_db', 'postgres:16'), container(3, 'dawarich_redis', 'redis:7'),
         container(4, 'AdGuard-Home', 'adguard/adguardhome:latest'), container(5, 'notes', 'x/notes:1', networks=('other',))],
        inspect={ident(1): {'Config': {'Env': ['DATABASE_HOST=dawarich_db', 'REDIS_URL=redis://dawarich_redis:6379', 'SECRET_KEY_BASE=abc123', 'TZ=Europe/Berlin']},
                            'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped'}}}})
    app = apps['dawarich_app']
    assert app.profile.dependencies == ['dawarich_db', 'dawarich_redis']
    assert 'DATABASE_HOST' in app.detected['profile_evidence']['dependencies']
    assert app.profile.config['env'] == {'database_host': 'dawarich_db', 'redis_url': 'redis://dawarich_redis:6379', 'tz': 'Europe/Berlin'}
    assert app.profile.config['restart_policy'] == 'unless-stopped'
    assert 'abc123' not in app.model_dump_json()
    assert app.profile.exposure == 'lan' and app.profile.tier == 'general'
    assert apps['AdGuard-Home'].profile.tier == 'infrastructure'
    assert apps['dawarich_db'].profile.tier == 'infrastructure'
    assert apps['notes'].profile.dependencies == []
    assert all(a.profile.origin['tier'] == 'auto' for a in apps.values())


async def test_dependency_from_small_shared_network_and_link():
    apps = await scan([container(1, 'wiki', 'x/wiki:1', networks=('wikinet',)), container(2, 'wikidb', 'mariadb:11', networks=('wikinet',)),
                       container(3, 'blog', 'x/blog:1', networks=('bridge',)), container(4, 'blogcache', 'redis:7', networks=('bridge',))],
                      inspect={ident(3): {'HostConfig': {'Links': ['/blogcache:/blog/cache']}}})
    assert apps['wiki'].profile.dependencies == ['wikidb']
    assert 'wikinet' in apps['wiki'].detected['profile_evidence']['dependencies']
    assert apps['blog'].profile.dependencies == ['blogcache']
    assert apps['wikidb'].profile.dependencies == []


@pytest.mark.parametrize('ports,network_mode,expected', [
    ([{'IP': '0.0.0.0', 'PrivatePort': 80, 'PublicPort': 8080}], '', 'lan'),
    ([{'IP': '192.168.1.5', 'PrivatePort': 80, 'PublicPort': 8080}], '', 'lan'),
    ([{'IP': '127.0.0.1', 'PrivatePort': 80, 'PublicPort': 8080}], '', 'internal'),
    ([{'IP': '8.8.8.8', 'PrivatePort': 80, 'PublicPort': 80}], '', 'internet'),
    ([{'PrivatePort': 80}], '', 'internal'),
    ([], 'host', 'lan'),
    (None, '', 'unknown')])
async def test_exposure_from_port_bindings(ports, network_mode, expected):
    c = container(1, 'svc', 'x/svc:1', ports)
    if ports is None:
        c['Ports'] = None
    apps = await scan([c], inspect={ident(1): {'HostConfig': {'NetworkMode': network_mode}}} if network_mode else None)
    assert apps['svc'].profile.exposure == expected


async def test_proxy_and_tunnel_targets_make_an_app_internet_facing(tmp_path):
    db = tmp_path / 'database.sqlite'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE proxy_host (domain_names TEXT, forward_host TEXT, forward_port INT, enabled INT, is_deleted INT)')
    con.execute('INSERT INTO proxy_host VALUES (?,?,?,?,?)', ('["photos.example.com"]', 'immich', 2283, 1, 0))
    con.execute('INSERT INTO proxy_host VALUES (?,?,?,?,?)', ('["off.example.com"]', 'disabled', 80, 0, 0))
    con.commit(); con.close()
    cf = tmp_path / 'config.yml'
    cf.write_text('ingress:\n  - hostname: wiki.example.com\n    service: http://wiki:3000\n  - service: http_status:404\n')
    targets = read_npm_targets(str(db)) + read_cloudflared_targets(str(cf))
    assert {t['host'] for t in targets} == {'immich', 'wiki'}
    ports = [{'IP': '0.0.0.0', 'PrivatePort': 2283, 'PublicPort': 2283}]
    apps = await scan([container(1, 'immich', 'x/immich:1', ports), container(2, 'wiki', 'x/wiki:1', [{'PrivatePort': 3000}]),
                       container(3, 'disabled', 'x/d:1', ports), container(4, 'wiki2', 'x/w:1', [{'PrivatePort': 3000}])], targets=targets)
    assert apps['immich'].profile.exposure == apps['wiki'].profile.exposure == 'internet'
    assert apps['immich'].profile.tier == 'internet'
    assert 'Nginx Proxy Manager' in apps['immich'].detected['profile_evidence']['exposure']
    assert 'cloudflared' in apps['wiki'].detected['profile_evidence']['exposure']
    assert apps['disabled'].profile.exposure == 'lan'
    assert apps['wiki2'].profile.exposure == 'internal'


def test_proxy_sources_are_off_when_no_path_is_set():
    assert read_npm_targets('') == [] and read_cloudflared_targets('') == []
    assert read_npm_targets('/nonexistent/db.sqlite') == []
    assert Settings().npm_database_path == '' and Settings().cloudflared_config_path == ''


def probe(handler, url='http://tower:8080/'):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            return await probe_login(client, url)
    return run()


async def test_login_probe_signals():
    assert (await probe(lambda r: httpx.Response(401)))[0] is True
    assert (await probe(lambda r: httpx.Response(302, headers={'location': '/users/sign_in'})))[0] is True
    assert (await probe(lambda r: httpx.Response(200, text='<form><input type="password" name="p"></form>')))[0] is True
    assert (await probe(lambda r: httpx.Response(200, text='<html>hello</html>')))[0] is None
    calls = []
    def hop(request):
        calls.append(request.url.path)
        return httpx.Response(302, headers={'location': '/web/'}) if request.url.path == '/' else httpx.Response(200, text='<input type=password>')
    assert (await probe(hop))[0] is True and calls == ['/', '/web/']
    # Redirects off the host and public hosts are not followed or probed.
    assert (await probe(lambda r: httpx.Response(302, headers={'location': 'http://evil.example.com/'})))[0] is None
    seen = []
    assert (await probe(lambda r: seen.append(1) or httpx.Response(401), url='http://app.example.com/'))[0] is None and not seen


async def test_own_login_comes_from_probe_and_web_ui_label():
    urls = []
    async def prober(url):
        urls.append(url)
        return True, 'web UI answers HTTP 401 (authentication required)'
    ports = [{'IP': '0.0.0.0', 'PrivatePort': 8096, 'PublicPort': 8097}]
    apps = await scan([container(1, 'jelly', 'x/j:1', ports, labels={'net.unraid.docker.webui': 'http://[IP]:[PORT:8096]/web'}), container(2, 'bare', 'x/b:1')], prober=prober)
    assert apps['jelly'].profile.own_login is True and urls == ['http://tower:8097/web']
    assert apps['bare'].profile.own_login is None


async def test_upstream_repo_signals_in_order():
    label = {'org.opencontainers.image.source': 'https://github.com/jellyfin/jellyfin'}
    apps = await scan([container(1, 'a', 'x/a:1', labels=label), container(2, 'b', 'x/b:1'), container(3, 'jellyfin', 'lscr.io/linuxserver/jellyfin:latest',
                       labels={'org.opencontainers.image.source': 'https://github.com/linuxserver/docker-jellyfin'})],
                      templates={'b': {'repo': 'tpl/b'}})
    assert (apps['a'].upstream_repo, apps['a'].detected['profile_evidence']['upstream_repo']) == ('jellyfin/jellyfin', 'OCI image source label')
    assert apps['b'].upstream_repo == 'tpl/b' and 'template' in apps['b'].detected['profile_evidence']['upstream_repo']
    assert apps['jellyfin'].upstream_repo == 'jellyfin/jellyfin' and 'linuxserver' in apps['jellyfin'].detected['profile_evidence']['upstream_repo']


def test_user_edits_survive_rescans():
    auto = Profile(tier='infrastructure', exposure='lan', own_login=True, dependencies=['db'], origin={f: 'auto' for f in autoprofile.AUTO_FIELDS})
    evidence = {'tier': 'x', 'exposure': 'y', 'dependencies': 'z'}
    current, _ = merge_profile(auto, evidence, None)
    edited = current.model_copy(update={'exposure': 'vpn'})
    edited.origin = edited_origin(current, edited)
    assert edited.origin['exposure'] == 'user' and edited.origin['tier'] == 'auto'
    rescan = auto.model_copy(update={'exposure': 'internet', 'tier': 'internet', 'dependencies': ['db', 'redis']})
    merged, kept = merge_profile(rescan, {**evidence, 'tier': 'new'}, edited)
    assert merged.exposure == 'vpn' and 'exposure' not in kept
    assert merged.tier == 'internet' and merged.dependencies == ['db', 'redis'] and kept['tier'] == 'new'


def test_profile_saved_before_origins_existed_is_never_overwritten():
    legacy = Profile(tier='general', exposure='lan', dependencies=['mine'])
    merged, evidence = merge_profile(Profile(tier='infrastructure', exposure='internet'), {'tier': 'x'}, legacy)
    assert (merged.tier, merged.exposure, merged.dependencies) == ('general', 'lan', ['mine']) and evidence == {}
    assert set(merged.origin.values()) == {'user'}


@pytest.mark.asyncio
async def test_poll_autofills_untouched_profiles_but_keeps_user_edits(tmp_path, monkeypatch):
    manager = Coordinator(Runtime(config_dir=tmp_path, disable_scheduler=True, ha_token=None, github_token=None, gemini_key=None))
    def fresh(**kw):
        a = AppRecord(id='docker:one', name='one', source='docker', profile=Profile(tier='internet', exposure='lan', own_login=True, origin={f: 'auto' for f in autoprofile.AUTO_FIELDS}),
                      detected={'profile_evidence': {'tier': 'why', 'exposure': 'because'}})
        return a
    async def collect(*args, **kwargs):
        return [fresh()], [], []
    monkeypatch.setattr(service, 'collect', collect)
    try:
        await manager.poll()
        assert manager.store.app('docker:one').profile.tier == 'internet'
        assert [p['id'] for p in manager.dashboard()['auto_profiles']] == ['docker:one']
        manager.review_auto_profiles()
        assert manager.dashboard()['auto_profiles'] == []
        stored = manager.store.app('docker:one')
        stored.profile.exposure = 'vpn'
        stored.profile.origin = edited_origin(manager.store.app('docker:one').profile.model_copy(update={'exposure': 'lan'}), stored.profile)
        manager.store.set('profile:docker:one', stored.profile.model_dump(mode='json'))
        await manager.poll()
        again = manager.store.app('docker:one')
        assert again.profile.exposure == 'vpn' and again.profile.tier == 'internet'
        assert 'exposure' not in again.detected['profile_evidence']
    finally:
        manager.close()


def test_profile_patch_marks_edited_fields_as_user(tmp_path):
    from fastapi.testclient import TestClient
    from ripe.app import create_app
    app = create_app(Runtime(config_dir=tmp_path, disable_scheduler=True, ha_token=None, github_token=None, gemini_key=None))
    with TestClient(app, base_url='http://localhost') as client:
        manager = client.app.state.manager
        manager.request_poll = lambda: False
        manager.store.save_app(AppRecord(id='docker:x', name='x', source='docker', profile=Profile(tier='general', exposure='lan', origin={f: 'auto' for f in autoprofile.AUTO_FIELDS})))
        response = client.patch('/api/apps/docker:x/profile', json={'exposure': 'vpn', 'tier': 'general'}, headers={'X-Ripe-Request': '1'})
        assert response.status_code == 200
        origin = manager.store.app('docker:x').profile.origin
        assert origin['exposure'] == 'user' and origin['tier'] == 'auto'
        assert client.post('/api/auto-profiles/review', headers={'X-Ripe-Request': '1'}).status_code == 200
        bad = client.patch('/api/settings', json={'npm_database_path': '../etc/passwd'}, headers={'X-Ripe-Request': '1'})
        assert bad.status_code == 422
        good = client.patch('/api/settings', json={'npm_database_path': '/host/npm/database.sqlite'}, headers={'X-Ripe-Request': '1'})
        assert good.status_code == 200


@pytest.mark.asyncio
async def test_env_values_and_probe_results_never_reach_gemini(monkeypatch):
    from ripe.intelligence import service as llm, classify_release
    from test_intelligence import classification, release
    sent = []
    async def fake(provider, model, prompt, payload, settings, key):
        sent.append((provider, json.dumps(payload) + prompt))
        return json.dumps(classification())
    monkeypatch.setattr(llm, '_call_model', fake)
    app = AppRecord(id='docker:x', name='x', source='docker', profile=Profile(config={'env': {'database_host': 'dawarich_db'}}, own_login=True, dependencies=['dawarich_db']))
    await classify_release(release(), app, Settings(classification_provider='gemini'), gemini_key='k' * 20)
    gemini = [body for provider, body in sent if provider == 'gemini']
    assert gemini and all('dawarich_db' not in body and 'own_login' not in body for body in gemini)
