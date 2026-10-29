from datetime import timedelta
import asyncio
import pytest
from ripe.config import Runtime
from ripe.models import AppRecord, Release, Profile, utcnow
from ripe.service import Coordinator
from ripe.intelligence import classification_key
from ripe.intelligence.evidence import conservative_applicability
import ripe.service as service


def runtime(tmp_path):
    return Runtime(config_dir=tmp_path,disable_scheduler=True,ha_token=None,github_token=None,gemini_key=None)


def inventory(name='one',installed='1.0.0'):
    app=AppRecord(id=name,name=name,source='docker',installed_version=installed,latest_version='1.1.0',upstream_repo='example/app')
    release=Release(id='github:example/app:1',app_id=name,version='1.1.0',published_at=utcnow()-timedelta(days=30),notes='Fixed an error when saving very large files.')
    return app,release


@pytest.mark.asyncio
async def test_public_release_cache_shared_and_installed_version_moves_history(tmp_path,monkeypatch):
    manager=Coordinator(runtime(tmp_path))
    a,r=inventory()
    b,s=inventory('two')
    calls=[]
    async def collect(*args,**kwargs):
        return [a,b],[r,s],[]
    async def ready(*args):
        return True
    async def classify(release,app,settings,**kwargs):
        calls.append(release.id)
        return release.model_copy(update={'classification':{'status':'ok','tags':['bugfix'],'tag_evidence':{'bugfix':'Fixed an error when saving very large files.'}},'classification_key':classification_key(release,app,settings)})
    monkeypatch.setattr(service,'collect',collect)
    monkeypatch.setattr(service,'classify_release',classify)
    monkeypatch.setattr(manager,'local_ready',ready)
    try:
        await manager.poll()
        await manager.classify_task
        await manager.poll()
        if manager.classify_task:
            await manager.classify_task  # may be a rollup refresh; nothing is queued
        assert manager.queue_length()==0
        assert calls==['github:example/app:1']
        assert len(manager.store.releases('one'))==len(manager.store.releases('two'))==1
        assert manager.view('one').releases_behind==1
        a.installed_version='1.1.0'
        await manager.poll()
        assert not manager.view('one').pending
        assert [r.version for r in manager.view('one').history]==['1.1.0']
        assert calls==['github:example/app:1']
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_profile_edit_during_inference_cannot_restore_old_exclusion(tmp_path,monkeypatch):
    manager=Coordinator(runtime(tmp_path))
    app,release=inventory()
    app.profile=Profile(config={'enabled':False})
    release.classification={'status':'ok','tags':['breaking'],'breaking':[{'description':'Feature removed','evidence':'Fixed an error when saving very large files.'}]}
    release.classification_key=classification_key(release,app,manager.settings())
    manager.store.save_app(app)
    manager.store.save_release(release)
    started,finish=asyncio.Event(),asyncio.Event()
    async def collect(*args,**kwargs):
        return [app],[release],[]
    async def ready(*args):
        return True
    async def analyze(release,app,settings,**kwargs):
        started.set()
        await finish.wait()
        return release.model_copy(update={'applicability':[{'kind':'breaking','index':0,'status':'does_not_apply','verified':True,'reason':'Old disabled feature','evidence':'Fixed an error when saving very large files.','profile_evidence':'false'}]})
    monkeypatch.setattr(service,'collect',collect)
    monkeypatch.setattr(service,'analyze_applicability',analyze)
    monkeypatch.setattr(manager,'local_ready',ready)
    await manager.poll()
    task=manager.classify_task
    try:
        await asyncio.wait_for(started.wait(),2)
        manager.mark_changed(app.id)
        edited=Profile(config={'enabled':True})
        manager.store.set('profile:'+app.id,edited.model_dump())
        record=manager.store.app(app.id)
        record.profile=edited
        manager.store.save_app(record)
        current=manager.store.releases(app.id)[0]
        current.applicability=conservative_applicability(current.classification)
        manager.store.save_release(current)
        manager.closing=True  # Test only this generation; do not launch the queued pass.
        finish.set()
        await task
        assert manager.store.app(app.id).profile.config=={'enabled':True}
        assert manager.store.releases(app.id)[0].applicability[0]['status']=='applies'
        assert manager.store.releases(app.id)[0].applicability[0]['verified'] is False
    finally:
        finish.set()
        if not task.done():
            await task
        manager.close()


@pytest.mark.asyncio
async def test_scheduler_loop_survives_an_exception(tmp_path,monkeypatch):
    manager=Coordinator(runtime(tmp_path))
    runs=[]
    async def cycle():
        runs.append(1)
        if len(runs)==1:
            raise RuntimeError('boom secret')
    async def sleep(delay):
        if len(runs)>=3:
            raise asyncio.CancelledError
    monkeypatch.setattr(manager,'cycle',cycle)
    monkeypatch.setattr(service.asyncio,'sleep',sleep)
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager.run()
        assert len(runs)==3
        assert 'secret' not in str(manager.store.logs())
    finally:
        manager.close() if hasattr(manager,'close') else None


def slow_setup(manager,monkeypatch,releases=None):
    a,r=inventory()
    releases=releases or [r]
    async def collect(*args,**kwargs):
        return [a],[x.model_copy() for x in releases],[]
    async def ready(*args):
        return True
    monkeypatch.setattr(service,'collect',collect)
    monkeypatch.setattr(manager,'local_ready',ready)
    return a


def release_at(version,days):
    return Release(id='github:example/app:'+version,app_id='one',version=version,published_at=utcnow()-timedelta(days=days),notes='Fixed an error when saving very large files.')


@pytest.mark.asyncio
async def test_collection_finishes_before_slow_classification(tmp_path,monkeypatch):
    manager=Coordinator(runtime(tmp_path))
    slow_setup(manager,monkeypatch)
    started,finish=asyncio.Event(),asyncio.Event()
    async def classify(release,app,settings,**kwargs):
        started.set()
        await finish.wait()
        return release.model_copy(update={'classification':{'status':'ok','tags':['bugfix'],'tag_evidence':{'bugfix':'Fixed an error.'}},'classification_key':classification_key(release,app,settings)})
    monkeypatch.setattr(service,'classify_release',classify)
    try:
        await asyncio.wait_for(manager.poll(),2)
        assert manager.store.get('last_poll') and manager.store.get('last_collection')
        assert not manager.polling
        data=manager.dashboard()
        assert data['classification_queue_length']==1 and data['last_classification'] is None
        view=manager.view('one')
        assert view.status_label=='Summary pending' and view.status=='unknown'
        await asyncio.wait_for(started.wait(),2)
        finish.set()
        await manager.classify_task
        assert manager.dashboard()['classification_queue_length']==0
        assert manager.dashboard()['last_classification']
        assert manager.view('one').status_label!='Summary pending'
    finally:
        finish.set()
        manager.closing=True
        if manager.classify_task:
            await manager.classify_task
        manager.close()


@pytest.mark.asyncio
async def test_queue_is_newest_first_and_resumes_after_restart(tmp_path,monkeypatch):
    releases=[release_at('1.1.0',30),release_at('1.2.0',20),release_at('1.3.0',10)]
    first=Coordinator(runtime(tmp_path))
    slow_setup(first,monkeypatch,releases).latest_version='1.3.0'
    order=[]
    async def classify(release,app,settings,**kwargs):
        order.append(release.version)
        if len(order)==1:
            assert [i['release_id'] for i in first.store.get('classification_queue')]==['github:example/app:1.3.0','github:example/app:1.2.0','github:example/app:1.1.0']
        if len(order)==2:
            first.closing=True  # simulate the container stopping mid-queue
        return release.model_copy(update={'classification':{'status':'ok','tags':['bugfix'],'tag_evidence':{'bugfix':'Fixed an error.'}},'classification_key':classification_key(release,app,settings)})
    monkeypatch.setattr(service,'classify_release',classify)
    try:
        await first.poll()
        await first.classify_task
        assert order==['1.3.0','1.2.0'] and first.queue_length()==1
    finally:
        first.close()
    second=Coordinator(runtime(tmp_path))
    monkeypatch.setattr(second,'local_ready',lambda *a: asyncio.sleep(0,True))
    try:
        assert second.queue_length()==1
        assert second.request_classification()
        await second.classify_task
        assert order==['1.3.0','1.2.0','1.1.0'] and second.queue_length()==0
    finally:
        second.close()


@pytest.mark.asyncio
async def test_classified_releases_are_not_requeued_or_resent(tmp_path,monkeypatch):
    manager=Coordinator(runtime(tmp_path))
    slow_setup(manager,monkeypatch)
    calls=[]
    async def classify(release,app,settings,**kwargs):
        calls.append(release.id)
        return release.model_copy(update={'classification':{'status':'ok','tags':['bugfix'],'tag_evidence':{'bugfix':'Fixed an error.'}},'classification_key':classification_key(release,app,settings)})
    monkeypatch.setattr(service,'classify_release',classify)
    try:
        await manager.poll()
        await manager.classify_task
        # A fresh store row without the carried-over fields still hits the key cache.
        await manager.poll()
        await manager.classify_task  # may be a rollup refresh; nothing is queued
        assert manager.queue_length()==0
        assert len(calls)==1
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_notes_revised_mid_inference_keep_the_requeued_item(tmp_path,monkeypatch):
    manager=Coordinator(runtime(tmp_path))
    slow_setup(manager,monkeypatch)
    manager.closing=True  # collect and queue only; drive the item by hand
    started,release_gate=asyncio.Event(),asyncio.Event()
    async def classify(release,app,settings,**kwargs):
        started.set()
        await release_gate.wait()
        return release.model_copy(update={'classification':{'status':'ok','tags':['bugfix'],'tag_evidence':{'bugfix':'Fixed an error.'}},'classification_key':classification_key(release,app,settings)})
    monkeypatch.setattr(service,'classify_release',classify)
    try:
        await manager.poll()
        item=manager.store.get('classification_queue')[0]
        task=asyncio.create_task(manager.classify_item(item,[None]))
        await started.wait()
        revised=manager.store.releases('one')[0].model_copy(update={'notes':'Revised notes.'})
        manager.store.save_release(revised)  # a collection refreshed the notes and re-queued the item
        release_gate.set()
        assert await task
        assert manager.store.get('classification_queue')==[item]
        stored=manager.store.releases('one')[0]
        assert stored.notes=='Revised notes.' and not stored.classification
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_polling_resets_after_exception(tmp_path,monkeypatch):
    manager=Coordinator(runtime(tmp_path))
    async def collect(*args,**kwargs):
        raise RuntimeError('boom secret')
    monkeypatch.setattr(service,'collect',collect)
    try:
        assert manager.request_poll()
        await manager.poll_task
        assert manager.polling is False
        assert manager.store.get('last_poll') is None
        assert 'secret' not in str(manager.store.logs())
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_stale_poll_is_abandoned_so_the_next_can_run(tmp_path,monkeypatch):
    manager=Coordinator(runtime(tmp_path))
    slow_setup(manager,monkeypatch)
    hang=asyncio.Event()
    async def hung(*args,**kwargs):
        await hang.wait()
    real=service.collect
    monkeypatch.setattr(service,'collect',hung)
    try:
        assert manager.request_poll()
        await asyncio.sleep(0)
        assert not manager.request_poll()  # healthy poll still blocks duplicates
        manager.poll_timeout=0.01
        await asyncio.sleep(0.05)
        monkeypatch.setattr(service,'collect',real)
        assert manager.request_poll()
        await manager.poll_task
        assert manager.store.get('last_poll')
        assert manager.polling is False
    finally:
        hang.set()
        manager.closing=True
        manager.close()


@pytest.mark.asyncio
async def test_local_provider_down_keeps_queue_and_stops(tmp_path,monkeypatch):
    manager=Coordinator(runtime(tmp_path))
    slow_setup(manager,monkeypatch)
    async def down(*args):
        return False
    monkeypatch.setattr(manager,'local_ready',down)
    calls=[]
    async def classify(release,app,settings,**kwargs):
        calls.append(release.id)
        return release
    monkeypatch.setattr(service,'classify_release',classify)
    try:
        await manager.poll()
        await manager.classify_task
        assert calls==[] and manager.queue_length()==1
        assert manager.store.get('last_classification') is None
        assert 'Pending classifications stay queued' in str(manager.store.logs())
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_classify_budget_stops_run_and_cooldown_delays_restart(tmp_path,monkeypatch):
    releases=[release_at('1.1.0',30),release_at('1.2.0',20)]
    manager=Coordinator(runtime(tmp_path))
    slow_setup(manager,monkeypatch,releases).latest_version='1.2.0'
    manager.classify_budget=0
    async def classify(release,app,settings,**kwargs):
        return release.model_copy(update={'classification':{'status':'ok','tags':['bugfix'],'tag_evidence':{'bugfix':'Fixed an error.'}},'classification_key':classification_key(release,app,settings)})
    monkeypatch.setattr(service,'classify_release',classify)
    try:
        await manager.poll()
        await manager.classify_task
        assert manager.queue_length()==2 and manager.classify_ended is not None
        manager.classify_budget=60
        await manager.cycle()  # inside the cooldown: nothing restarts
        assert manager.classify_task.done() and manager.queue_length()==2
        manager.classify_cooldown=0
        await manager.cycle()
        await manager.classify_task
        assert manager.queue_length()==0
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_abandoned_poll_cannot_clear_newer_poll_flag(tmp_path,monkeypatch):
    manager=Coordinator(runtime(tmp_path))
    slow_setup(manager,monkeypatch)
    hang,release_second=asyncio.Event(),asyncio.Event()
    real=service.collect
    async def hung(*args,**kwargs):
        await hang.wait()
    monkeypatch.setattr(service,'collect',hung)
    try:
        assert manager.request_poll()
        first=manager.poll_task
        await asyncio.sleep(0)
        manager.poll_timeout=0.01
        await asyncio.sleep(0.05)
        async def second_collect(*args,**kwargs):
            await release_second.wait()
            return await real(*args,**kwargs)
        monkeypatch.setattr(service,'collect',second_collect)
        await manager.cycle()  # stale poll is abandoned; the due poll starts fresh
        await asyncio.sleep(0.01)
        assert manager.polling and manager.poll_task is not first
        release_second.set()
        await manager.poll_task
        assert manager.polling is False and manager.store.get('last_poll')
    finally:
        hang.set(); release_second.set()
        manager.closing=True
        manager.close()


@pytest.mark.asyncio
async def test_refreshed_metadata_mid_inference_is_not_reverted(tmp_path,monkeypatch):
    manager=Coordinator(runtime(tmp_path))
    slow_setup(manager,monkeypatch)
    manager.closing=True
    started,release_gate=asyncio.Event(),asyncio.Event()
    async def classify(release,app,settings,**kwargs):
        started.set()
        await release_gate.wait()
        return release.model_copy(update={'classification':{'status':'ok','tags':['bugfix'],'tag_evidence':{'bugfix':'Fixed an error.'}},'classification_key':classification_key(release,app,settings)})
    monkeypatch.setattr(service,'classify_release',classify)
    try:
        await manager.poll()
        item=manager.store.get('classification_queue')[0]
        task=asyncio.create_task(manager.classify_item(item,[None]))
        await started.wait()
        newer=utcnow()
        manager.store.save_release(manager.store.releases('one')[0].model_copy(update={'published_at':newer}))
        release_gate.set()
        assert await task
        stored=manager.store.releases('one')[0]
        assert stored.published_at==newer and stored.classification['status']=='ok'
        assert manager.store.get('classification_queue')==[]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_due_rollup_runs_only_once_the_queue_is_empty(tmp_path,monkeypatch):
    manager=Coordinator(runtime(tmp_path))
    slow_setup(manager,monkeypatch)
    order=[]
    async def classify(release,app,settings,**kwargs):
        order.append('classify')
        return release.model_copy(update={'classification':{'status':'ok','tags':['bugfix'],'tag_evidence':{'bugfix':'Fixed an error.'}},'classification_key':classification_key(release,app,settings)})
    async def rollup(app,settings,local_ready):
        order.append('rollup')
    monkeypatch.setattr(service,'classify_release',classify)
    monkeypatch.setattr(manager,'refresh_rollup',rollup)
    try:
        manager.classify_budget=0  # out of budget: queue stays, so rollups are skipped
        await manager.poll()
        await manager.classify_task
        assert order==[] and manager.queue_length()==1
        manager.classify_budget=60
        manager.classify_cooldown=0
        await manager.cycle()
        await manager.classify_task
        assert order==['classify','rollup']
        # Queue empty but a rollup is due (failed/never stored): the task starts for it alone.
        order.clear()
        monkeypatch.setattr(manager,'rollups_due',lambda: True)
        assert manager.request_classification()
        await manager.classify_task
        assert order==['rollup']
    finally:
        manager.close()


def _many(count, installed='1.0.0', notes='Fixed a crash when the importer reads large files.'):
    app=AppRecord(id='big',name='big',source='docker',installed_version=installed,latest_version=f'1.{count}.0',upstream_repo='example/big')
    releases=[Release(id=f'github:example/big:{i}',app_id='big',version=f'1.{i}.0',published_at=utcnow()-timedelta(days=100-i),notes=f'{notes} ({i})') for i in range(1,count+1)]
    return app,releases


async def _run(tmp_path,monkeypatch,apps,releases):
    manager=Coordinator(runtime(tmp_path))
    calls=[]
    async def collect(*args,**kwargs):
        return apps,releases,[]
    async def ready(*args):
        return True
    async def classify(release,app,settings,**kwargs):
        calls.append((app.id,release.version))
        return release.model_copy(update={'classification':{'status':'ok','tags':['bugfix'],'tag_evidence':{}},'classification_key':classification_key(release,app,settings)})
    async def applic(release,app,settings,**kwargs):
        return release
    monkeypatch.setattr(service,'collect',collect)
    monkeypatch.setattr(service,'classify_release',classify)
    monkeypatch.setattr(service,'analyze_applicability',applic)
    monkeypatch.setattr(manager,'local_ready',ready)
    try:
        await manager.poll()
        if manager.classify_task:
            await manager.classify_task
        stored={r.version:r for r in manager.store.releases(apps[0].id)}
    finally:
        manager.close()
    return calls,stored


@pytest.mark.asyncio
async def test_analysis_is_capped_to_the_newest_releases_per_app(tmp_path,monkeypatch):
    app,releases=_many(30)
    calls,stored=await _run(tmp_path,monkeypatch,[app],releases)
    assert len(calls)==15
    assert {v for _,v in calls}=={f'1.{i}.0' for i in range(16,31)}
    assert 'not analysed' in stored['1.2.0'].classification['skipped']


@pytest.mark.asyncio
async def test_unknown_installed_version_only_queues_releases_since_image_creation(tmp_path,monkeypatch):
    app,releases=_many(30,installed=None)
    app.image_created_at=utcnow()-timedelta(days=100-27)
    calls,_=await _run(tmp_path,monkeypatch,[app],releases)
    assert {v for _,v in calls}=={'1.28.0','1.29.0','1.30.0'}
    app2,releases2=_many(30,installed=None)
    calls,_=await _run(tmp_path/'b' if (tmp_path/'b').mkdir() is None else tmp_path,monkeypatch,[app2],releases2)
    assert calls==[]


@pytest.mark.asyncio
async def test_trivial_notes_skip_the_model(tmp_path,monkeypatch):
    app,releases=_many(2)
    releases[0].notes='**Full Changelog**: https://github.com/example/big/compare/1.0.0...1.1.0'
    releases[1].notes='Small fix'
    calls,stored=await _run(tmp_path,monkeypatch,[app],releases)
    assert calls==[]
    assert stored['1.1.0'].classification['skipped']=='no notes'


@pytest.mark.asyncio
async def test_queue_runs_exposed_then_security_then_newest(tmp_path,monkeypatch):
    plain,plain_r=_many(2,notes='Plain bug fix for the plain application only.')
    plain.id='plain'; plain.upstream_repo='example/plain'; [setattr(r,'app_id','plain') for r in plain_r]
    for r in plain_r: r.id=r.id.replace('big','plain')
    sec,sec_r=_many(2,notes='Fixes a security vulnerability CVE-2026-1 in the parser.')
    sec.id='sec'; sec.upstream_repo='example/sec'; [setattr(r,'app_id','sec') for r in sec_r]
    for r in sec_r: r.id=r.id.replace('big','sec')
    infra,infra_r=_many(2,notes='Infrastructure tweak that touches the daemon.')
    infra.id='infra'; infra.upstream_repo='example/infra'; infra.profile=Profile(tier='infrastructure'); [setattr(r,'app_id','infra') for r in infra_r]
    for r in infra_r: r.id=r.id.replace('big','infra')
    calls,_=await _run(tmp_path,monkeypatch,[plain,sec,infra],plain_r+sec_r+infra_r)
    assert [c for c in calls]==[('infra','1.2.0'),('infra','1.1.0'),('sec','1.2.0'),('sec','1.1.0'),('plain','1.2.0'),('plain','1.1.0')]


@pytest.mark.asyncio
async def test_prereleases_do_not_consume_the_analysis_cap(tmp_path,monkeypatch):
    app,releases=_many(2)
    for i in range(20):
        beta=releases[1].model_copy(update={'id':f'big-beta{i}','version':f'2.0.0b{i}','prerelease':True})
        releases.append(beta)
    calls,_=await _run(tmp_path,monkeypatch,[app],releases)
    assert {v for _,v in calls}=={'1.1.0','1.2.0'}


@pytest.mark.asyncio
async def test_short_security_notes_keep_keyword_warnings(tmp_path,monkeypatch):
    app,releases=_many(2)
    releases[0].notes='Fix CVE-2026-1234'
    releases[1].notes='Small fix'
    calls,stored=await _run(tmp_path,monkeypatch,[app],releases)
    assert calls==[]
    assert stored['1.1.0'].classification['keyword_warnings']


@pytest.mark.asyncio
async def test_stale_exclusions_reset_outside_the_analysis_cap(tmp_path,monkeypatch):
    app,releases=_many(2)
    manager=Coordinator(runtime(tmp_path))
    async def collect(*args,**kwargs):
        return [app],releases,[]
    async def ready(*args):
        return True
    async def classify(release,app,settings,**kwargs):
        return release.model_copy(update={'classification':{'status':'ok','tags':['security'],'tag_evidence':{}},'classification_key':classification_key(release,app,settings)})
    async def applic(release,app,settings,**kwargs):
        return release
    monkeypatch.setattr(service,'collect',collect)
    monkeypatch.setattr(service,'classify_release',classify)
    monkeypatch.setattr(service,'analyze_applicability',applic)
    monkeypatch.setattr(manager,'local_ready',ready)
    try:
        await manager.poll()
        await manager.classify_task
        old=next(r for r in manager.store.releases(app.id) if r.version=='1.1.0')
        old.applicability=[{'kind':'security','index':0,'status':'does_not_apply','reason':'x','evidence':'y','profile_evidence':'z','verified':True}]
        manager.store.save_release(old)
        _,releases[:]=_many(20)
        app.latest_version='1.20.0'
        await manager.poll()
        stored={r.version:r for r in manager.store.releases(app.id)}
    finally:
        manager.close()
    assert stored['1.1.0'].classification['status']=='ok'
    assert not any(i.get('status')=='does_not_apply' for i in stored['1.1.0'].applicability)
