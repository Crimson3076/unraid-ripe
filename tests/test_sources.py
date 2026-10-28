import json
from pathlib import Path

import httpx
import pytest

from ripe.models import AppRecord
from ripe.sources.collector import (
    CachedHTTP, DockerReader, SourceError, RegistryReader, github_repo,
    github_releases, ha_records, ha_states, installed_unraid, parse_image,
    read_templates, usable_version_label,
)


def test_template_reads_metadata_only(tmp_path):
    (tmp_path / 'app.xml').write_text('''<Container><Name>RomM</Name><Repository>ghcr.io/rommapp/romm:latest</Repository><Project>https://github.com/rommapp/romm</Project><Config Name="TOKEN">never-store-this</Config><Environment><Variable><Value>secret</Value></Variable></Environment><Icon>https://example.com/icon.png</Icon></Container>''')
    data = read_templates(str(tmp_path))
    assert data['romm']['repo'] == 'rommapp/romm'
    assert 'never-store-this' not in json.dumps(data)
    assert 'secret' not in json.dumps(data)


def test_template_rejects_external_xml_and_symlinks(tmp_path):
    outside = tmp_path.parent / 'outside-template.xml'
    outside.write_text('<Container><Name>evil</Name></Container>')
    (tmp_path / 'escape.xml').symlink_to(outside)
    (tmp_path / 'entity.xml').write_text('<!DOCTYPE a [<!ENTITY b "bad">]><Container><Name>&b;</Name></Container>')
    assert read_templates(str(tmp_path)) == {}


@pytest.mark.parametrize('value,expected', [
    ('https://github.com/rommapp/romm', 'rommapp/romm'),
    ('rommapp/romm', 'rommapp/romm'),
    ('https://github.com/rommapp/romm/releases/tag/5.3.0', 'rommapp/romm'),
    ('https://token@github.com/a/b', None),
    ('http://github.com/a/b', None),
    ('https://evil.test/a/b', None),
    ('../../etc/passwd', None),
])
def test_repo_identity(value, expected):
    assert github_repo(value) == expected


@pytest.mark.parametrize('image,expected', [
    ('postgres:17', ('registry-1.docker.io', 'library/postgres', '17')),
    ('linuxserver/sonarr:latest', ('registry-1.docker.io', 'linuxserver/sonarr', 'latest')),
    ('ghcr.io/rommapp/romm:5.3.0', ('ghcr.io', 'rommapp/romm', '5.3.0')),
    ('lscr.io/linuxserver/sonarr', ('lscr.io', 'linuxserver/sonarr', 'latest')),
])
def test_parse_image(image, expected):
    assert parse_image(image) == expected


async def test_docker_only_allowed_get_and_no_env_persistence():
    seen = []
    ident = 'a' * 64
    image_id = 'sha256:' + 'b' * 64
    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == '/containers/json':
            return httpx.Response(200, json=[{'Id':ident, 'Names':['/romm'], 'Image':'ghcr.io/rommapp/romm:latest', 'ImageID':image_id}])
        if request.url.path.startswith('/images/'):
            return httpx.Response(200, json={'Created':'2026-10-01T00:00:00Z', 'RepoDigests':['ghcr.io/rommapp/romm@sha256:1234'], 'Config': {'Env':['TOKEN=secret'], 'Labels':{'org.opencontainers.image.version':'5.2.0','org.opencontainers.image.source':'https://github.com/rommapp/romm'}}})
        raise AssertionError(str(request.url))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='http://docker') as client:
        reader = DockerReader(client)
        apps = await reader.apps({}, {})
        for path in ('/containers/a/exec', '/images/create', '/containers/json?bad=1', '/containers/../json'):
            with pytest.raises(SourceError):
                await reader.read(path)
    assert apps[0].installed_version == '5.2.0'
    assert apps[0].upstream_repo == 'rommapp/romm'
    assert 'secret' not in apps[0].model_dump_json()
    assert all(method == 'GET' for method, _ in seen)
    assert len(seen) == 2


async def test_manual_repo_mapping_survives_redetection():
    ident = 'a' * 64
    def handler(request):
        return httpx.Response(200, json=[{'Id':ident,'Names':['/app'],'Image':'x:2.0','Labels':{'org.opencontainers.image.source':'https://github.com/upstream/default'}}])
    previous = AppRecord(id='docker:app',name='app',source='docker',upstream_repo='owner/manual')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='http://docker') as client:
        apps = await DockerReader(client).apps({}, {'docker:app':previous})
    assert apps[0].upstream_repo == 'owner/manual'


async def test_http_cache_etag_and_safe_errors():
    cache = {}
    hits = []
    def handler(request):
        hits.append(request)
        if len(hits) == 1:
            return httpx.Response(200, json={'answer':42}, headers={'etag':'abc'})
        assert request.headers['if-none-match'] == 'abc'
        return httpx.Response(304)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reader = CachedHTTP(client, cache.get, cache.__setitem__)
        assert await reader.get('https://api.github.com/x', ttl=0) == {'answer':42}
        assert await reader.get('https://api.github.com/x', ttl=0) == {'answer':42}
    assert len(hits) == 2
    def fail(request):
        return httpx.Response(403, text='TOKEN=do-not-log')
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(SourceError, match='HTTP 403') as error:
            await CachedHTTP(client).get('https://api.github.com/x')
        assert 'TOKEN' not in str(error.value)


async def test_github_preserves_each_release_and_dates():
    def handler(request):
        assert request.url.host == 'api.github.com'
        assert request.headers['authorization'] == 'Bearer invisible'
        return httpx.Response(200, json=[{'id':123,'tag_name':'5.3.0','body':'Removed old key','published_at':'2026-10-01T00:00:00Z','prerelease':False}, {'id':124,'tag_name':'5.4.0-rc1','body':'Preview','published_at':'2026-10-02T00:00:00Z','prerelease':True}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        releases = await github_releases(CachedHTTP(client), 'rommapp/romm', 'docker:romm', 'invisible')
    assert len(releases) == 2
    assert releases[0].notes == 'Removed old key'
    assert releases[0].published_at.year == 2026
    assert releases[1].prerelease
    assert 'invisible' not in releases[0].model_dump_json()


async def test_registry_challenge_not_allowed_to_exfiltrate():
    def handler(request):
        return httpx.Response(401, headers={'www-authenticate':'Bearer realm="https://attacker.test/token",service="ghcr.io"'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceError, match='unsupported authentication'):
            await RegistryReader(client).request('ghcr.io', 'owner/app', 'tags/list')


async def test_registry_matches_arch_manifest_and_tag():
    installed = 'sha256:platform'
    def handler(request):
        path = request.url.path
        if path.endswith('tags/list'):
            return httpx.Response(200, json={'tags':['latest','v5.2.0','v5.3.0']})
        if path.endswith('/latest'):
            return httpx.Response(200, json={'manifests':[{'digest':'sha256:new'}]},headers={'docker-content-digest':'sha256:latest'})
        if path.endswith('/v5.3.0'):
            return httpx.Response(200, json={}, headers={'docker-content-digest':'sha256:other'})
        if path.endswith('/v5.2.0'):
            return httpx.Response(200, json={'manifests':[{'digest':installed}]}, headers={'docker-content-digest':'sha256:list'})
        raise AssertionError(str(request.url))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        info = await RegistryReader(client).metadata('ghcr.io/owner/app:latest', installed)
    assert info['installed_version'] == 'v5.2.0'
    assert info['update_available'] is True
    assert info['latest_only'] is False


async def test_home_assistant_only_update_fields_retained():
    def handler(request):
        assert request.method == 'GET'
        assert request.url.path == '/api/states'
        return httpx.Response(200, json=[{'entity_id':'update.home_assistant_core','state':'on','attributes':{'installed_version':'2026.10.1','latest_version':'2026.10.3','title':'Home Assistant Core','release_summary':'Fix issue','secret':'never'}}, {'entity_id':'sensor.secret','state':'not-allowed'}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        states = await ha_states(client, 'http://10.20.30.41:8123', 'token')
    assert len(states) == 1
    assert 'secret' not in json.dumps(states)
    apps, releases = ha_records(states, {}, {})
    assert apps[0].upstream_repo == 'home-assistant/core'
    assert apps[0].profile.tier == 'infrastructure'
    assert releases[0].published_at is None


def test_unraid_version_only(tmp_path):
    path = tmp_path / 'unraid-version'
    path.write_text('version="7.2.0"\n')
    assert installed_unraid(str(path)) == '7.2.0'
    assert installed_unraid(str(tmp_path/'missing')) is None


async def test_unraid_official_docs_header_overrides_prerelease_filename():
    import base64
    from ripe.sources.collector import unraid_releases
    notes = {
        '7.3.0.md': '# Version 7.3.0 2026-09-01\n\nStable baseline.',
        '7.3.4.md': '# Version 7.3.4 2026-10-22\n\nSecurity fixes.',
        '7.4.0.md': '# Version 7.4.0-rc.2 2026-10-22\n\nRelease candidate.',
    }
    def handler(request):
        assert request.url.host == 'api.github.com'
        if request.url.path.endswith('/release-notes'):
            return httpx.Response(200, json=[{'name':name} for name in notes])
        name = request.url.path.rsplit('/', 1)[-1]
        return httpx.Response(200, json={'encoding':'base64','content':base64.b64encode(notes[name].encode()).decode()})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        releases = await unraid_releases(CachedHTTP(client), '7.3.1')
    assert [r.version for r in releases] == ['7.3.0', '7.3.4', '7.4.0-rc.2']
    assert releases[-1].prerelease
    assert not releases[1].prerelease
    assert releases[1].published_at.isoformat() == '2026-10-22T00:00:00+00:00'
    assert releases[1].source_url == 'https://docs.unraid.net/unraid-os/release-notes/7.3.4/'


async def test_unraid_webui_placeholders_resolve_with_published_ports(tmp_path):
    (tmp_path/'app.xml').write_text('<Container><Name>ollama</Name><Project>https://github.com/ollama/ollama</Project><WebUI>http://[IP]:[PORT:11434]/</WebUI></Container>')
    templates = read_templates(str(tmp_path))
    assert templates['ollama']['repo'] == 'ollama/ollama'
    ident = 'a' * 64
    def handler(request):
        return httpx.Response(200, json=[{'Id':ident,'Names':['/ollama'],'Image':'ollama/ollama:latest','Ports':[{'PrivatePort':11434,'PublicPort':11435}]}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='http://docker') as client:
        apps = await DockerReader(client, host='10.20.30.40').apps(templates, {})
    assert apps[0].web_url == 'http://10.20.30.40:11435/'
    assert apps[0].detected['ports'][0]['public'] == 11435


async def test_linuxserver_known_app_mapped_to_upstream():
    def handler(request):
        return httpx.Response(200,json=[{'Id':'a'*64,'Names':['/sonarr'],'Image':'lscr.io/linuxserver/sonarr:latest','Labels':{'org.opencontainers.image.source':'https://github.com/linuxserver/docker-sonarr'}}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='http://docker') as client:
        apps = await DockerReader(client).apps({}, {})
    assert apps[0].upstream_repo == 'Sonarr/Sonarr'


async def test_same_repo_different_installed_versions_fetch_complete_spans(monkeypatch, tmp_path):
    from ripe.models import Settings
    from ripe.sources import collector
    socket = tmp_path/'docker.sock'
    socket.touch()
    apps = [
        AppRecord(id='docker:new', name='new', source='docker', installed_version='2.0.1', upstream_repo='owner/app'),
        AppRecord(id='docker:old', name='old', source='docker', installed_version='1.0.1', upstream_repo='owner/app'),
    ]
    async def docker_apps(*args, **kwargs):
        return apps
    async def no_unraid(*args, **kwargs):
        return []
    monkeypatch.setattr(collector.DockerReader, 'apps', docker_apps)
    monkeypatch.setattr(collector, 'unraid_releases', no_unraid)
    pages = {
        1: ['2.1.' + str(i) for i in range(98, 0, -1)] + ['2.0.1','2.0.0'],
        2: ['1.9.' + str(i) for i in range(100, 0, -1)],
        3: ['1.0.1','1.0.0','0.9.9'],
    }
    requests = []
    def handler(request):
        assert request.url.host == 'api.github.com'
        page = int(request.url.params['page'])
        requests.append(page)
        return httpx.Response(200,json=[{'id':version,'tag_name':version,'body':'Public release notes','published_at':'2026-10-01T00:00:00Z','prerelease':False} for version in pages[page]])
    original_client = httpx.AsyncClient
    def make_client(*args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)
    monkeypatch.setattr(collector.httpx, 'AsyncClient', make_client)
    cache = {}
    found_apps, releases, errors = await collector.collect(Settings(), docker_socket=str(socket), unraid_version_file=str(tmp_path/'no-version'), cache_get=cache.get, cache_set=cache.__setitem__)
    spans = {app.id:[r.version for r in releases if r.app_id==app.id] for app in apps}
    assert '1.0.0' not in spans['docker:new']
    assert '1.0.0' in spans['docker:old']
    assert '0.9.9' in spans['docker:old']
    assert [r for r in releases if r.version=='2.1.98' and r.app_id=='docker:new']
    assert [r for r in releases if r.version=='2.1.98' and r.app_id=='docker:old']
    assert next(a for a in found_apps if a.id=='docker:old').latest_version == '2.1.98'
    # Public page response caching still avoids duplicate network traffic.
    assert requests == [1,2,3]


@pytest.mark.parametrize('value', ['24.04', '22.04', 'debian', 'Alpine', 'bookworm', 'jammy', 'noble', '$APP_VERSION', '${APP_VERSION}', 'latest', '', None])
def test_base_image_version_label_is_ignored(value):
    assert usable_version_label(value) is None


@pytest.mark.parametrize('value', ['1.37.4', 'v0.9.0-rc.3', '2024.10.1', '24.04.1'])
def test_real_version_label_is_accepted(value):
    assert usable_version_label(value) == value


def _gh(tag, prerelease=False):
    return {'id': tag, 'tag_name': tag, 'body': 'notes', 'published_at': '2026-10-01T00:00:00Z', 'prerelease': prerelease}


async def _collect(monkeypatch, tmp_path, apps, github=None, registry_digest='sha256:' + 'f' * 64):
    from ripe.models import Settings
    from ripe.sources import collector
    socket = tmp_path / 'docker.sock'
    socket.touch()
    async def docker_apps(self, *args, **kwargs):
        return apps
    async def no_unraid(*args, **kwargs):
        return []
    async def registry_meta(self, image, installed_digest, match_version=True):
        return {'latest_digest': registry_digest, 'latest_only': True, 'update_available': installed_digest != registry_digest}
    monkeypatch.setattr(collector.DockerReader, 'apps', docker_apps)
    monkeypatch.setattr(collector.RegistryReader, 'metadata', registry_meta)
    monkeypatch.setattr(collector, 'unraid_releases', no_unraid)
    def handler(request):
        return httpx.Response(200, json=github or [])
    original_client = httpx.AsyncClient
    def make_client(*args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)
    monkeypatch.setattr(collector.httpx, 'AsyncClient', make_client)
    cache = {}
    return await collector.collect(Settings(), docker_socket=str(socket), unraid_version_file=str(tmp_path / 'no-version'), cache_get=cache.get, cache_set=cache.__setitem__)


async def test_unmapped_latest_app_uses_digest_logic(monkeypatch, tmp_path):
    from datetime import timedelta
    from ripe.models import Settings, utcnow
    from ripe.ripeness import build_view
    digest = 'sha256:' + 'f' * 64
    same = AppRecord(id='docker:same', name='same', source='docker', image='ghcr.io/o/same:latest', installed_digest=digest)
    old = AppRecord(id='docker:old', name='old', source='docker', image='ghcr.io/o/old:latest', installed_digest='sha256:' + '1' * 64)
    apps, releases, errors = await _collect(monkeypatch, tmp_path, [same, old])
    by_id = {a.id: a for a in apps}
    assert not by_id['docker:same'].error and not by_id['docker:old'].error
    assert by_id['docker:same'].detected['mapping_hint']
    assert not any(e['source'].startswith('docker:') for e in errors)
    assert build_view(by_id['docker:same'], [], Settings()).status == 'up_to_date'
    by_id['docker:old'].image_changed_at = utcnow()
    assert build_view(by_id['docker:old'], [], Settings()).status == 'waiting'
    by_id['docker:old'].image_changed_at = utcnow() - timedelta(days=400)
    assert build_view(by_id['docker:old'], [], Settings()).status == 'ripe'


async def test_rc_without_prerelease_flag_does_not_beat_final(monkeypatch, tmp_path):
    app = AppRecord(id='docker:a', name='a', source='docker', installed_version='1.0.0', upstream_repo='o/a')
    data = [_gh('2.0.0-rc.1'), _gh('2.0.0'), _gh('nightly-20261027'), _gh('chart-4.5.6'), _gh('1.9.0')]
    apps, _, _ = await _collect(monkeypatch, tmp_path, [app], github=data)
    assert next(a for a in apps if a.id == 'docker:a').latest_version == '2.0.0'


async def test_only_unparseable_tags_leave_latest_unset(monkeypatch, tmp_path):
    app = AppRecord(id='docker:a', name='a', source='docker', installed_version='1.0.0', upstream_repo='o/a')
    apps, _, _ = await _collect(monkeypatch, tmp_path, [app], github=[_gh('nightly-20261027'), _gh('chart-4.5.6')])
    assert next(a for a in apps if a.id == 'docker:a').latest_version is None


async def test_malformed_docker_record_only_fails_its_own_app():
    ident_ok, ident_bad = 'a' * 64, 'b' * 64
    image_id = 'sha256:' + 'c' * 64
    def handler(request):
        if request.url.path == '/containers/json':
            return httpx.Response(200, json=[
                {'Id': ident_bad, 'Names': ['/bad'], 'Image': 'x:1', 'ImageID': image_id, 'Ports': None, 'Labels': 5},
                {'Id': ident_ok, 'Names': ['/ok'], 'Image': 'x:1', 'ImageID': image_id, 'Ports': None},
            ])
        return httpx.Response(200, json={'Config': None, 'RepoDigests': None})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='http://docker') as client:
        reader = DockerReader(client)
        apps = await reader.apps({}, {})
    assert [a.name for a in apps] == ['ok']
    assert reader.skipped == ['docker:' + ident_bad[:12]]


async def test_malformed_ha_entity_only_fails_its_own_app(monkeypatch, tmp_path):
    from ripe.sources import collector
    def handler(request):
        return httpx.Response(200, json=[{'entity_id': 'update.bad', 'state': 'on', 'attributes': None}, {'entity_id': 'update.ok', 'state': 'off', 'attributes': {'title': 'Ok', 'installed_version': '1', 'latest_version': '1'}}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        states = await ha_states(client, 'http://ha.local', 'token')
    apps, _ = ha_records(states, {}, {})
    assert {a.id for a in apps} == {'ha:update.bad', 'ha:update.ok'}


async def test_docker_allowlist_excludes_container_inspect_and_version():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), base_url='http://docker') as client:
        reader = DockerReader(client)
        for path in ('/containers/' + 'a' * 64 + '/json', '/version'):
            with pytest.raises(SourceError):
                await reader.read(path)


async def test_long_release_history_stops_at_installed_version():
    pages = []
    def handler(request):
        page = int(request.url.params['page'])
        pages.append(page)
        start = 300 - (page - 1) * 100
        return httpx.Response(200, json=[_gh(f'1.{minor}.0') for minor in range(start, start - 100, -1)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        releases = await github_releases(CachedHTTP(client), 'o/a', 'docker:a', None, '1.150.0')
    assert pages == [1, 2]
    assert any(r.version == '1.150.0' for r in releases)


async def test_long_unparseable_history_is_not_an_error():
    def handler(request):
        return httpx.Response(200, json=[_gh(f'nightly-{request.url.params["page"]}-{i}') for i in range(100)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        releases = await github_releases(CachedHTTP(client), 'o/a', 'docker:a', None, '1.0.0')
    assert len(releases) == 1000


async def test_404_mapping_flagged_once_and_not_retried(monkeypatch, tmp_path):
    from ripe.sources import collector
    hits = []
    def make(repo='HaveAGitGat/tdarr_express_be'):
        return AppRecord(id='docker:tdarr', name='tdarr', source='docker', installed_version='1.0.0', upstream_repo=repo, image='ghcr.io/haveagitgat/tdarr:latest', detected={'image_source': 'https://github.com/HaveAGitGat/Tdarr'})
    app = make()
    apps, _, errors = await _collect_404(monkeypatch, tmp_path, [app], hits)
    flagged = next(a for a in apps if a.id == 'docker:tdarr')
    assert hits and flagged.detected['invalid_mapping']['status'] == 404
    assert flagged.detected['invalid_mapping']['suggestion'] == 'HaveAGitGat/Tdarr'
    assert 'mapping needs attention' in flagged.error
    assert sum(e['source'] == 'docker:tdarr' for e in errors) == 1
    # Next poll: same mapping is skipped entirely, with no new error entries.
    hits.clear()
    apps, _, errors = await _collect_404(monkeypatch, tmp_path, [make()], hits, existing=[flagged])
    again = next(a for a in apps if a.id == 'docker:tdarr')
    assert not hits and again.detected['invalid_mapping'] == flagged.detected['invalid_mapping']
    assert 'mapping needs attention' in again.error
    assert not any(e['source'] == 'docker:tdarr' for e in errors)
    # A token change or an expired flag retries too.
    hits.clear()
    await _collect_404(monkeypatch, tmp_path, [make()], hits, existing=[flagged], token='x')
    assert hits
    hits.clear()
    stale = flagged.model_copy(deep=True)
    stale.detected['invalid_mapping']['at'] -= 7 * 3600
    await _collect_404(monkeypatch, tmp_path, [make()], hits, existing=[stale])
    assert hits
    hits.clear()
    # Changing the mapping retries.
    apps, _, _ = await _collect_404(monkeypatch, tmp_path, [make('HaveAGitGat/Tdarr')], hits, existing=[flagged])
    assert hits


async def _collect_404(monkeypatch, tmp_path, apps, hits, existing=None, token=None):
    from ripe.models import Settings
    from ripe.sources import collector
    socket = tmp_path / 'docker.sock'
    socket.touch()
    async def docker_apps(self, *args, **kwargs):
        return apps
    async def no_unraid(*args, **kwargs):
        return []
    async def registry_meta(self, image, installed_digest, match_version=True):
        return {}
    monkeypatch.setattr(collector.DockerReader, 'apps', docker_apps)
    monkeypatch.setattr(collector.RegistryReader, 'metadata', registry_meta)
    monkeypatch.setattr(collector, 'unraid_releases', no_unraid)
    def handler(request):
        hits.append(request.url.path)
        return httpx.Response(404, json={'message': 'Not Found'})
    original_client = httpx.AsyncClient
    def make_client(*args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)
    monkeypatch.setattr(collector.httpx, 'AsyncClient', make_client)
    return await collector.collect(Settings(), docker_socket=str(socket), unraid_version_file=str(tmp_path / 'none'), existing_apps=existing, github_token=token)


async def test_backport_at_page_boundary_does_not_stop_early():
    pages = []
    def handler(request):
        page = int(request.url.params['page'])
        pages.append(page)
        if page == 1:  # backport published late sits above the newer minor
            return httpx.Response(200, json=[_gh('1.149.5')] + [_gh(f'1.150.{i}') for i in range(99, 0, -1)])
        return httpx.Response(200, json=[_gh('1.150.0')] + [_gh(f'1.149.{i}') for i in range(4, -95, -1)][:99])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        releases = await github_releases(CachedHTTP(client), 'o/a', 'docker:a', None, '1.150.0')
    assert 2 in pages and any(r.version == '1.150.0' for r in releases)
