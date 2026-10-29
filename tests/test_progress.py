import pytest
from fastapi.testclient import TestClient
from ripe.app import create_app
from ripe.config import Runtime
from ripe.progress import PollProgress
import ripe.service as service
from ripe.service import Coordinator
from ripe.intelligence import classification_key
from test_service import runtime, _many


def test_idle_status_shape():
    status = PollProgress().snapshot()
    assert status['running'] is False and status['phase'] == 'done' and status['started_at'] is None
    assert status['sources'] == {'done': 0, 'total': 0}
    assert status['releases'] == {'done': 0, 'total': 0, 'skipped': 0}
    assert status['eta_seconds'] is None and status['last_error'] is None and status['current'] == ''


def test_phase_transitions_and_counts():
    p = PollProgress()
    p.start()
    assert p.snapshot()['running'] and p.phase == 'collecting' and p.started_at
    p.add_sources(3)
    p.source_done('Docker')
    p.add_sources(2)
    p.working_on('jellyfin')
    p.source_done('jellyfin')
    assert p.snapshot()['sources'] == {'done': 2, 'total': 5} and p.current == 'jellyfin'
    p.end_collecting()
    assert p.sources == {'done': 2, 'total': 5} or p.sources['done'] == p.sources['total']
    p.begin_analysis(4, skipped=2)
    assert p.phase == 'analysing' and p.releases == {'done': 0, 'total': 4, 'skipped': 2}
    p.release_done('a')
    assert p.releases['done'] == 1 and p.current == 'a'
    p.begin_summaries()
    assert p.phase == 'summarising' and p.running
    p.finish()
    status = p.snapshot()
    assert status['phase'] == 'done' and status['running'] is False and status['last_error'] is None and status['finished_at']


def test_done_never_exceeds_total():
    p = PollProgress()
    p.start()
    p.begin_analysis(1)
    p.release_done()
    p.release_done()
    assert p.releases['done'] == 1


def test_eta_needs_three_model_calls_then_uses_rolling_average():
    p = PollProgress()
    p.start()
    p.begin_analysis(20)
    for seconds in (10, 10):
        p.release_done('x', seconds)
    assert p.eta_seconds() is None
    p.release_done('x', 10)
    assert p.eta_seconds() == 170
    # Cached releases (no model call) advance the count but not the average.
    p.release_done('x')
    assert p.eta_seconds() == 160
    # Only the most recent calls count.
    for _ in range(10):
        p.release_done('x', 2)
    assert p.eta_seconds() == 2 * (20 - 14)
    p.begin_summaries()
    assert p.eta_seconds() is None


def test_failure_keeps_reason_and_restart_clears_it():
    p = PollProgress()
    p.start()
    p.add_errors([{'source': 'docker', 'message': 'socket missing'}])
    p.finish('Collection failed (RuntimeError); last known records preserved.')
    status = p.snapshot()
    assert not status['running'] and status['last_error'].startswith('Collection failed')
    assert status['errors'] == [{'source': 'docker', 'message': 'socket missing'}]
    p.start()
    assert p.snapshot()['last_error'] is None and p.errors == []


def test_error_list_is_capped():
    p = PollProgress()
    p.start()
    p.add_errors([{'source': str(i), 'message': 'x'} for i in range(50)])
    assert len(p.snapshot()['errors']) == 10


def test_endpoint_and_dashboard_expose_progress(tmp_path):
    with TestClient(create_app(Runtime(config_dir=tmp_path, disable_scheduler=True, ha_token=None)), base_url='http://localhost') as client:
        manager = client.app.state.manager
        assert client.get('/api/poll/status').json()['running'] is False
        manager.progress.start()
        manager.progress.begin_analysis(210, 4)
        body = client.get('/api/poll/status').json()
        assert body['running'] and body['phase'] == 'analysing' and body['releases']['total'] == 210
        assert client.get('/api/dashboard').json()['poll']['releases']['skipped'] == 4


@pytest.mark.asyncio
async def test_poll_reports_progress_from_start_to_finish(tmp_path, monkeypatch):
    app, releases = _many(20)
    manager = Coordinator(runtime(tmp_path))
    seen = []

    async def collect(settings, **kwargs):
        progress = kwargs['progress']
        progress.add_sources(3)
        progress.source_done('Docker')
        seen.append(manager.progress.snapshot())
        return [app], releases, [{'source': 'docker', 'message': 'no socket'}]

    async def ready(*args):
        return True

    async def classify(release, app, settings, **kwargs):
        seen.append(manager.progress.snapshot())
        return release.model_copy(update={'classification': {'status': 'ok', 'tags': [], 'tag_evidence': {}}, 'classification_key': classification_key(release, app, settings)})

    async def applic(release, app, settings, **kwargs):
        return release

    monkeypatch.setattr(service, 'collect', collect)
    monkeypatch.setattr(service, 'classify_release', classify)
    monkeypatch.setattr(service, 'analyze_applicability', applic)
    monkeypatch.setattr(manager, 'local_ready', ready)
    try:
        assert manager.request_poll() is True
        assert manager.progress.snapshot()['running'] and manager.request_poll() is False
        await manager.poll_task
        assert seen[0]['phase'] == 'collecting' and seen[0]['sources']['done'] == 1
        assert all(s['phase'] == 'analysing' and s['releases']['total'] == 15 for s in seen[1:])
        assert [s['releases']['done'] for s in seen[1:]] == list(range(15))
        final = manager.progress.snapshot()
        assert final['running'] is False and final['phase'] == 'done' and final['last_error'] is None
        assert final['releases'] == {'done': 15, 'total': 15, 'skipped': 5}
        assert final['errors'] == [{'source': 'docker', 'message': 'no socket'}]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_poll_failure_is_reported(tmp_path, monkeypatch):
    manager = Coordinator(runtime(tmp_path))

    async def collect(*args, **kwargs):
        raise RuntimeError('token=secret-value')

    monkeypatch.setattr(service, 'collect', collect)
    try:
        await manager.poll()
        status = manager.progress.snapshot()
        assert status['running'] is False and 'RuntimeError' in status['last_error']
        assert 'secret-value' not in status['last_error']
    finally:
        manager.close()
