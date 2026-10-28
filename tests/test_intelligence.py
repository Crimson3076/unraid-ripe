import json
from unittest.mock import AsyncMock
import httpx
import pytest
from ripe.models import AppRecord, Profile, Release, Settings
from ripe.intelligence import (classify_release, analyze_applicability, sanitize_profile,
                               has_evidence, public_payload, local_model_url)
from ripe.intelligence.evidence import (Classification, Applicability, verify_classification,
                                       keyword_warnings, verify_applicability)
from ripe.intelligence.service import LLMUnavailable, chunks

NOTES = ('Removed filesystem.roms_folder. RomM refuses to start if this key is set.\n'
         'CORS now defaults to same-origin; configure allowed origins for browser clients on other origins.\n'
         'This is not a breaking change for other users; no migration is needed.\n'
         'Fixed a search bug.')


def classification(**overrides):
    result = {
        'summary': 'RomM refuses to start when the removed setting remains.',
        'summary_evidence': 'RomM refuses to start if this key is set.',
        'tags': ['breaking', 'bugfix'],
        'tag_evidence': {'breaking': 'Removed filesystem.roms_folder. RomM refuses to start if this key is set.',
                         'bugfix': 'Fixed a search bug.'},
        'security': [],
        'breaking': [
            {'description': 'Removed roms_folder can stop startup.',
             'action_needed': 'Remove filesystem.roms_folder.',
             'evidence': 'Removed filesystem.roms_folder. RomM refuses to start if this key is set.'},
            {'description': 'Cross-origin browser clients require allowed origins.',
             'action_needed': 'Configure allowed origins for cross-origin clients.',
             'evidence': 'CORS now defaults to same-origin; configure allowed origins for browser clients on other origins.'}],
        'migration': [], 'big_features': [],
        'keyword_hits_addressed': [
            {'term': 'Removed', 'quote': 'Removed filesystem.roms_folder. RomM refuses to start if this key is set.',
             'matters': True, 'why': 'This setting now prevents startup.'},
            {'term': 'breaking', 'quote': 'This is not a breaking change for other users; no migration is needed.',
             'matters': False, 'why': 'The statement negates a breaking change for other users.'},
            {'term': 'migration', 'quote': 'This is not a breaking change for other users; no migration is needed.',
             'matters': False, 'why': 'No migration needed.'}],
        'confidence': 'high',
    }
    result.update(overrides)
    return result


def app(profile=None):
    return AppRecord(id='romm', name='RomM', source='docker', installed_version='5.2.0',
                     profile=profile or Profile(config={'filesystem': {'roms_folder': 'roms'}},
                         notes='No browser clients on other origins'))


def release():
    return Release(id='github:rommapp/romm:5.3.0', app_id='romm', version='5.3.0', notes=NOTES)


def test_evidence_normalization_and_fabricated_items_removed():
    model = Classification.model_validate(classification(
        summary='Unsafe made-up claim', summary_evidence='Made-up evidence',
        security=[{'description':'CVE fixed','severity':'critical','requires':'none','evidence':'CVE-FAKE'}],
        tags=['security','bugfix'], tag_evidence={'security':'CVE-FAKE','bugfix':'FIXED   A SEARCH BUG.'}))
    result, dropped = verify_classification(model, NOTES)
    assert not result['summary']
    assert not result['security']
    assert result['tags'] == ['bugfix']
    assert {'summary','security[0]','tag:security'} <= set(dropped)
    assert has_evidence('romm REFUSES\n to start', NOTES)
    assert not has_evidence('  ', NOTES)


def test_negated_migration_does_not_invent_tags_but_unaddressed_keyword_warns():
    notes = 'No migration is needed. This is not a breaking change. Security review required.'
    data = classification(summary='', summary_evidence='', tags=[], tag_evidence={}, breaking=[],
        keyword_hits_addressed=[{'term':'migration','quote':'No migration is needed.','matters':False,'why':'Negated.'},
                                {'term':'breaking','quote':'This is not a breaking change.','matters':False,'why':'Negated.'}])
    result, _ = verify_classification(Classification.model_validate(data), notes)
    assert result['tags'] == []
    assert {w['term'] for w in result['keyword_warnings']} == {'security','required'}


def test_keyword_coverage_checks_each_occurrence_and_topic():
    notes = 'No migration is needed. Security was reviewed. Migration is required for old installs.'
    addressed = [{'term':'migration','quote':'No migration is needed. Security was reviewed.','matters':False,'why':'No migration.'}]
    warnings = keyword_warnings(notes, addressed)
    assert {item['term'] for item in warnings} == {'security','migration','required'}


def test_profile_secrets_and_private_addresses_removed_but_relevant_keys_kept(monkeypatch):
    monkeypatch.setenv('HA_TOKEN', 'this-is-a-long-secret-token')
    p = sanitize_profile(Profile(config={'filesystem': {'roms_folder':'roms'},
        'password':'dont-store-me', 'apiKey':'api-secret', 'accessToken':'access-secret', 'nested':{'token':'private','enabled':False},
        'key':'abcd', 'url':'http://test-host.local:1234/test', 'path':'/mnt/user/romm',
        'description':'token=this-is-a-long-secret-token', 'host':'10.20.30.40'},
        notes='No browser clients on other origins'))
    text = p.model_dump_json()
    for forbidden in ('dont-store-me','api-secret','access-secret','private"','abcd','test-host','10.20.30.40','/mnt/user','this-is-a-long-secret-token'):
        assert forbidden not in text
    assert p.config['filesystem']['roms_folder'] == 'roms'
    assert p.config['nested']['enabled'] is False


def test_cloud_payload_is_explicit_allowlist_and_redacts_network_paths_env():
    result = public_payload('RomM @ test-host.local 10.20.30.40', '5.3.0',
        'Public change. /mnt/user/appdata/config.yml HA_TOKEN=very-secret https://private.home/path')
    assert set(result) == {'app','version','public_release_notes'}
    text = json.dumps(result)
    for forbidden in ('test-host','10.20.30.40','/mnt/user','HA_TOKEN','very-secret','private.home'):
        assert forbidden not in text


@pytest.mark.parametrize('url', ['http://10.20.30.40:11434','http://ollama:11434','http://localhost:11434','http://127.0.0.1:11434'])
def test_local_endpoint_allowed(url):
    assert local_model_url(url) == url


@pytest.mark.parametrize('url', ['https://cloud.example.com','http://user:pass@10.20.30.40','http://169.254.169.254','file:///tmp/model'])
def test_public_or_credentialed_endpoint_for_profiles_rejected(url):
    with pytest.raises(ValueError):
        local_model_url(url)


@pytest.mark.asyncio
async def test_romm_52_to_54_removal_applies_cors_does_not(monkeypatch):
    from ripe.intelligence import service
    responses = [json.dumps(classification()), json.dumps({'items':[
        {'kind':'breaking','index':0,'status':'applies','reason':"Won't start until roms_folder is removed.",
         'evidence':'RomM refuses to start if this key is set.', 'profile_evidence':'"roms_folder": "roms"'},
        {'kind':'breaking','index':1,'status':'does_not_apply','reason':'There are no cross-origin browser clients.',
         'evidence':'CORS now defaults to same-origin; configure allowed origins for browser clients on other origins.',
         'profile_evidence':'No browser clients on other origins'}]})]
    call = AsyncMock(side_effect=responses)
    monkeypatch.setattr(service, '_call_model', call)
    logs = []
    classified = await classify_release(release(), app(), Settings(), log_call=logs.append)
    analyzed = await analyze_applicability(classified, app(), Settings(), log_call=logs.append)
    assert analyzed.applicability[0]['status'] == 'applies'
    assert "Won't start" in analyzed.applicability[0]['reason']
    assert analyzed.applicability[1]['status'] == 'does_not_apply'
    assert all(item['verified'] for item in analyzed.applicability)
    assert all(log['evidence_passed'] for log in logs)
    assert logs[0]['prompt_version'] == 'classification-v1'
    assert logs[1]['prompt_version'] == 'applicability-v1'
    assert call.call_args_list[1].args[0] == 'local'


@pytest.mark.asyncio
async def test_invalid_json_retries_once_unknown_and_logs(monkeypatch):
    from ripe.intelligence import service
    call = AsyncMock(return_value='not JSON')
    monkeypatch.setattr(service, '_call_model', call)
    logs = []
    result = await classify_release(release(), app(), Settings(), log_call=logs.append)
    assert call.await_count == 2
    assert result.classification['status'] == 'unknown'
    assert [x['retry'] for x in logs] == [0,1]
    assert all(x['outcome'] == 'invalid_json' for x in logs)


@pytest.mark.asyncio
async def test_gemini_rate_limit_falls_back_local_and_never_sends_profile(monkeypatch):
    from ripe.intelligence import service
    payloads = []
    async def fake(provider, model, prompt, payload, settings, key):
        payloads.append((provider, payload))
        if provider == 'gemini':
            raise LLMUnavailable('Gemini HTTP 429')
        return json.dumps(classification())
    monkeypatch.setattr(service, '_call_model', fake)
    result = await classify_release(release(), app(), Settings(classification_provider='gemini'), gemini_key='x'*20)
    assert result.classification['provider'] == 'local'
    assert result.classification['provider_errors'] == ['gemini: Gemini HTTP 429']
    assert [x[0] for x in payloads] == ['gemini','local']
    assert all(set(x[1]) == {'app','version','public_release_notes'} for x in payloads)
    assert all('profile' not in json.dumps(x[1]) for x in payloads)


@pytest.mark.asyncio
async def test_cached_release_makes_no_new_model_call(monkeypatch):
    from ripe.intelligence import service
    call = AsyncMock(return_value=json.dumps(classification()))
    monkeypatch.setattr(service, '_call_model', call)
    first = await classify_release(release(), app(), Settings())
    second = await classify_release(first, app(), Settings())
    assert second is first
    assert call.await_count == 1


@pytest.mark.asyncio
async def test_second_opinion_disagreement_retains_evidenced_risks(monkeypatch):
    from ripe.intelligence import service
    empty = classification(summary='', summary_evidence='', tags=['bugfix'],
                           tag_evidence={'bugfix':'Fixed a search bug.'}, breaking=[])
    monkeypatch.setattr(service, '_call_model', AsyncMock(side_effect=[json.dumps(empty), json.dumps(classification())]))
    result = await classify_release(release(), app(), Settings(second_opinion=True), gemini_key='secret-key')
    assert result.model_disagreement
    assert result.classification['tags'] == ['bugfix','breaking']
    assert result.classification['breaking']
    assert len(result.classification['provenance']) == 2


def test_invalid_or_missing_profile_evidence_cannot_dismiss_risk():
    classified, _ = verify_classification(Classification.model_validate(classification()), NOTES)
    model = Applicability.model_validate({'items':[
        {'kind':'breaking','index':0,'status':'does_not_apply','reason':'Safe, I guess.',
         'evidence':'RomM refuses to start if this key is set.','profile_evidence':'roms_folder absent'}]})
    result, dropped = verify_applicability(model, classified, NOTES, json.dumps({'roms_folder':'roms'}))
    assert result[0]['status'] == 'applies'
    assert not result[0]['verified']
    assert not result[0]['reason']
    assert result[1]['status'] == 'applies'
    assert dropped


def test_duplicate_applicability_indices_fail_closed():
    classified, _ = verify_classification(Classification.model_validate(classification()), NOTES)
    item = {'kind':'breaking','index':0,'status':'does_not_apply','reason':'No key.',
            'evidence':'RomM refuses to start if this key is set.','profile_evidence':'roms_folder absent'}
    model = Applicability.model_validate({'items':[item,item]})
    result, dropped = verify_applicability(model, classified, NOTES, 'roms_folder absent')
    assert result[0]['status'] == 'applies'
    assert not result[0]['verified']
    assert dropped


@pytest.mark.asyncio
async def test_missing_local_model_is_conservative_and_not_a_fake_claim(monkeypatch):
    from ripe.intelligence import service
    classified, _ = verify_classification(Classification.model_validate(classification()), NOTES)
    r = release().model_copy(update={'classification':classified})
    monkeypatch.setattr(service, '_call_model', AsyncMock(side_effect=httpx.ConnectError('offline')))
    result = await analyze_applicability(r, app(), Settings())
    assert all(item['status'] == 'applies' and not item['verified'] and not item['reason'] for item in result.applicability)


def test_long_note_chunking_keeps_content():
    text = ('One public release paragraph.\n' * 1500).strip()
    parts = chunks(text)
    assert len(parts) > 1
    assert all(len(part) <= 12000 for part in parts)
    assert ''.join(text.split()) == ''.join(''.join(parts).split())


@pytest.mark.asyncio
async def test_home_assistant_notes_never_go_to_gemini_even_with_public_looking_url(monkeypatch):
    from ripe.intelligence import service
    call = AsyncMock(return_value=json.dumps(classification()))
    monkeypatch.setattr(service, '_call_model', call)
    r = release().model_copy(update={'id':'ha:update.private_app:5.3.0', 'source_url':'https://github.com/public/repo'})
    result = await classify_release(r, app(), Settings(classification_provider='gemini', second_opinion=True), gemini_key='not-used')
    assert call.await_count == 1
    assert call.call_args.args[0] == 'local'
    assert result.classification['provider'] == 'local'
    assert 'local-source' in result.classification['provider_errors'][0]


def test_free_text_camel_case_credentials_and_connection_strings_are_redacted():
    from ripe.intelligence.privacy import redact_text
    for text, secret in [('auth_token=abcdefg','abcdefg'), ('accessToken=abc123','abc123'),
                         ('apiKey: foo','foo'), ('redis://user:secret@server:6379','secret'),
                         ('{"apiKey": "abc123"}', 'abc123'), ('Bearer abcdefg', 'abcdefg')]:
        assert secret not in redact_text(text, private=True)


@pytest.mark.asyncio
async def test_unknown_classification_can_retry_after_endpoint_recovers(monkeypatch):
    from ripe.intelligence import service
    call = AsyncMock(side_effect=[LLMUnavailable('Local model HTTP 503'), json.dumps(classification())])
    monkeypatch.setattr(service, '_call_model', call)
    first = await classify_release(release(), app(), Settings())
    assert first.classification['status'] == 'unknown'
    second = await classify_release(first, app(), Settings())
    assert second.classification['status'] == 'ok'
    assert call.await_count == 2


def test_repeated_unknown_applicability_indices_cannot_add_invalid_records():
    classified, _ = verify_classification(Classification.model_validate(classification()), NOTES)
    item = {'kind':'breaking','index':99,'status':'does_not_apply','reason':'Safe.',
            'evidence':'RomM refuses to start if this key is set.','profile_evidence':'roms_folder absent'}
    result, dropped = verify_applicability(Applicability.model_validate({'items':[item,item]}), classified, NOTES, 'roms_folder absent')
    assert len(result) == 2
    assert all(isinstance(entry, dict) and entry['status'] == 'applies' for entry in result)
    assert len(dropped) == 2


@pytest.mark.parametrize('name', ['passwordHash','tokenValue','secretValue','clientCredential',
    'dbPasswordHash','custom_token_value','accessKeyValue','apiKeyValue','privateKeyFile',
    'signingKey','encryption_key','publicKey','credentialsJson','AUTHORIZATION_HEADER'])
def test_sensitive_key_names_are_removed_even_with_prefixes_and_suffixes(name):
    from ripe.intelligence.privacy import sanitize_value
    value = {'filesystem':{'roms_folder':'roms'}, 'keyboard':'us', 'monkey':'keep',
             'nested':[{name:'sensitive-value','enabled':True}]}
    sanitized = sanitize_value(value)
    assert sanitized['filesystem']['roms_folder'] == 'roms'
    assert sanitized['keyboard'] == 'us'
    assert sanitized['monkey'] == 'keep'
    assert sanitized['nested'] == [{'enabled':True}]
    assert 'sensitive-value' not in json.dumps(sanitized)


def test_applicability_prompt_digest_tracks_actual_prompt_content(monkeypatch):
    from ripe.intelligence import service, prompt_digest
    first = prompt_digest('applicability-v1')
    original = service._prompt
    monkeypatch.setattr(service, '_prompt', lambda name: original(name) + '\nEdited instructions.')
    assert prompt_digest('applicability-v1') != first
    assert len(first) == 64
    with pytest.raises(ValueError):
        prompt_digest('../../secret')


def _capture_ollama(monkeypatch):
    from ripe.intelligence import service
    sent = []
    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, **k):
            sent.append(json)
            return httpx.Response(200, json={'message': {'content': '{}'}},
                                  request=httpx.Request('POST', url))
    monkeypatch.setattr(service.httpx, 'AsyncClient', FakeClient)
    return service, sent


@pytest.mark.asyncio
async def test_local_context_is_sized_to_short_changelog(monkeypatch):
    service, sent = _capture_ollama(monkeypatch)
    settings = Settings(ollama_url='http://localhost:11434')
    await service._call_model('local', 'm', 'prompt', {'notes': 'x' * 9000}, settings, None)
    options = sent[0]['options']
    assert options['num_ctx'] == 12288 and options['num_predict'] == 8192
    assert options['num_ctx'] % 4096 == 0


@pytest.mark.asyncio
async def test_local_context_is_capped_at_32k(monkeypatch):
    service, sent = _capture_ollama(monkeypatch)
    settings = Settings(ollama_url='http://localhost:11434')
    await service._call_model('local', 'm', 'p', {'notes': 'x' * 400_000}, settings, None)
    assert sent[0]['options']['num_ctx'] == 32768
    assert service.local_num_ctx('', '') >= service.MIN_NUM_CTX
