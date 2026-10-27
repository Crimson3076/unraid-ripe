from __future__ import annotations

from datetime import datetime, timezone
import json

import httpx
import pytest

from ripe.models import AppRecord, AppView, Release, Settings
from ripe.notifications import Notifier, is_quiet
from ripe.store import Store


def configured_settings(**overrides):
    return Settings(notifications_enabled=True,
                    notification_script='script.send_update_alert',
                    notification_people=['sample_user'],
                    notification_channel='Updates', **overrides)


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


@pytest.mark.parametrize('stamp,quiet', [
    ('2026-10-26T01:59:59Z', False),  # Sunday 21:59:59 EDT
    ('2026-10-26T02:00:00Z', True),   # 22:00 EDT
    ('2026-10-26T12:59:59Z', True),   # Monday 08:59:59 EDT
    ('2026-10-26T13:00:00Z', False),  # 09:00 EDT
    ('2026-11-01T05:30:00Z', True),   # First 01:30, DST fallback
    ('2026-11-01T06:30:00Z', True),   # Second 01:30
    ('2026-03-08T07:01:00Z', True),   # 03:01 after spring skip
])
def test_quiet_hours_use_configured_timezone_through_dst(stamp, quiet):
    assert is_quiet(Settings(timezone='America/New_York'), instant(stamp)) is quiet


def test_custom_daytime_or_all_day_quiet_window():
    settings = Settings(timezone='UTC', quiet_start='10:00', quiet_end='12:00')
    assert is_quiet(settings, instant('2026-10-26T10:00:00Z'))
    assert not is_quiet(settings, instant('2026-10-26T12:00:00Z'))
    settings.quiet_end = '10:00'
    assert not is_quiet(settings, instant('2026-10-26T17:00:00Z'))


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / 'ripe.sqlite3', secrets=['test-ha-secret'])
    yield db
    db.close()


def recorder():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])
    return calls, httpx.MockTransport(handler)


async def test_send_only_uses_configured_script_and_required_fields(store):
    calls, transport = recorder()
    notifier = Notifier(store, 'http://homeassistant.local:8123', 'test-ha-secret', transport=transport)
    settings = configured_settings(timezone='UTC', dashboard_url='http://10.20.30.40:8087')
    assert await notifier.send(settings, 'A test update notice', 'ripe-test', now=instant('2026-10-26T12:00:00Z'))
    assert len(calls) == 1
    request = calls[0]
    assert request.method == 'POST'
    assert request.url.path == '/api/services/script/send_update_alert'
    assert request.headers['authorization'] == 'Bearer test-ha-secret'
    body = json.loads(request.content)
    assert body == {'people': ['sample_user'], 'title': 'Ripe',
                    'message': 'A test update notice\nhttp://10.20.30.40:8087',
                    'home_only': False, 'tag': 'ripe-test', 'channel': 'Updates'}
    assert 'test-ha-secret' not in json.dumps(store.logs())


async def test_default_off_missing_token_and_demo_never_send(store):
    calls, transport = recorder()
    notifier = Notifier(store, 'http://ha.local:8123', 'test-ha-secret', transport=transport)
    daytime = instant('2026-10-26T14:00:00Z')
    settings = Settings(timezone='UTC')
    assert not await notifier.send(settings, 'Notice', 'test', now=daytime)
    settings.notifications_enabled = True
    await notifier.tick(settings, [], now=daytime, demo=True)
    no_token = Notifier(store, 'http://ha.local:8123', None, transport=transport)
    assert not await no_token.send(configured_settings(timezone='UTC'), 'Notice', 'test', now=daytime)
    assert calls == []


def security_view():
    app = AppRecord(id='docker:gateway', name='Gateway', source='docker', installed_version='1.00', latest_version='1.03')
    release = Release(id='github:example/gateway:102', app_id=app.id, version='1.02',
                      notes='Fixed authentication bypass.', classification={
                          'tags': ['security'], 'security': [{'description': 'Authentication bypass fixed.',
                          'severity': 'high', 'requires': 'none', 'evidence': 'Fixed authentication bypass.'}]})
    return AppView(app=app, status='recommended_now', status_label='Recommended now', recommended_version='1.02', pending=[release])


async def test_security_alert_defers_quiet_hours_then_sends_once_across_restart(store):
    calls, transport = recorder()
    settings = configured_settings(timezone='America/New_York')
    store.set('digest-sent-for', '2026-10-26')
    notifier = Notifier(store, 'http://ha.local:8123', 'test-ha-secret', transport=transport)
    await notifier.tick(settings, [security_view()], now=instant('2026-10-26T12:59:59Z'))
    assert calls == []
    await notifier.tick(settings, [security_view()], now=instant('2026-10-26T13:00:00Z'))
    assert len(calls) == 1
    message = json.loads(calls[0].content)
    assert message['tag'].startswith('ripe-security-')
    assert '1.02' in message['message']
    restarted = Notifier(store, 'http://ha.local:8123', 'test-ha-secret', transport=transport)
    await restarted.tick(settings, [security_view()], now=instant('2026-10-26T14:00:00Z'))
    assert len(calls) == 1


async def test_weekly_digest_during_quiet_hours_is_deferred_and_persistently_deduplicated(store):
    calls, transport = recorder()
    settings = configured_settings(timezone='America/New_York',
                        digest_weekday=0, digest_time='07:00')
    store.set('digest-sent-for', '2026-10-19')
    notifier = Notifier(store, 'http://ha.local:8123', 'test-ha-secret', transport=transport)
    await notifier.tick(settings, [], now=instant('2026-10-26T11:00:00Z'))
    assert calls == []
    await notifier.tick(settings, [], now=instant('2026-10-26T13:00:00Z'))
    assert len(calls) == 1
    assert json.loads(calls[0].content)['tag'] == 'ripe-weekly'
    assert store.get('digest-sent-for') == '2026-10-26'
    restarted = Notifier(store, 'http://ha.local:8123', 'test-ha-secret', transport=transport)
    await restarted.tick(settings, [], now=instant('2026-10-27T14:00:00Z'))
    assert len(calls) == 1
    await restarted.tick(settings, [], now=instant('2026-11-02T14:00:00Z'))
    assert len(calls) == 2


async def test_failed_script_is_not_marked_delivered_or_logged_with_response_secrets(store):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(401, text='private rejection payload test-ha-secret')
    notifier = Notifier(store, 'http://ha.local:8123', 'test-ha-secret', transport=httpx.MockTransport(handler))
    settings = configured_settings(timezone='UTC')
    store.set('digest-sent-for', '2026-10-19')
    await notifier.tick(settings, [], now=instant('2026-10-26T12:00:00Z'))
    assert store.get('digest-sent-for') == '2026-10-19'
    assert len(calls) == 1
    output = json.dumps(store.logs())
    assert 'private rejection payload' not in output
    assert 'test-ha-secret' not in output
    assert 'script request failed' in output


async def test_enabled_without_a_configured_script_never_sends(store):
    calls,transport=recorder()
    notifier=Notifier(store,'http://ha.local:8123','test-ha-secret',transport=transport)
    settings=Settings(timezone='UTC',notifications_enabled=True)
    assert not await notifier.send(settings,'Notice','test',now=instant('2026-10-26T14:00:00Z'))
    assert calls==[]


def test_equal_quiet_times_mean_no_quiet_hours():
    settings = Settings(timezone='UTC', quiet_start='10:00', quiet_end='10:00')
    assert not is_quiet(settings, instant('2026-10-26T10:00:00Z'))
    assert not is_quiet(settings, instant('2026-10-26T03:00:00Z'))


async def test_first_run_seeds_digest_instead_of_sending(store):
    calls, transport = recorder()
    settings = configured_settings(timezone='UTC', digest_weekday=0, digest_time='07:00')
    notifier = Notifier(store, 'http://ha.local:8123', 'test-ha-secret', transport=transport)
    await notifier.tick(settings, [], now=instant('2026-10-28T12:00:00Z'))
    assert calls == []
    assert store.get('digest-sent-for') == '2026-10-26'
    await notifier.tick(settings, [], now=instant('2026-11-02T12:00:00Z'))
    assert len(calls) == 1
