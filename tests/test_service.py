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
        await manager.poll()
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
    task=asyncio.create_task(manager.poll())
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


@pytest.mark.asyncio
async def test_unknown_span_classifies_only_the_newest_three(tmp_path,monkeypatch):
    app,releases=_many(30,installed=None)
    app.image_created_at=utcnow()-timedelta(days=200)
    calls,stored=await _run(tmp_path,monkeypatch,[app],releases)
    assert {v for _,v in calls}=={'1.28.0','1.29.0','1.30.0'}
    assert 'not analysed' in stored['1.10.0'].classification['skipped']


@pytest.mark.asyncio
async def test_unorderable_installed_tag_is_not_analysed_beyond_three(tmp_path,monkeypatch):
    app,releases=_many(30,installed='latest')
    app.image_created_at=utcnow()-timedelta(days=200)
    calls,_=await _run(tmp_path,monkeypatch,[app],releases)
    assert len(calls)==3


@pytest.mark.asyncio
async def test_rollback_window_covers_three_releases_at_or_below_installed(tmp_path,monkeypatch):
    app,releases=_many(10,installed='1.6.0')
    manager=Coordinator(runtime(tmp_path))
    sent=[]
    async def collect(*args,**kwargs):
        return [app],releases,[]
    async def ready(*args):
        return True
    async def classify(release,app,settings,**kwargs):
        return release.model_copy(update={'classification':{'status':'ok','tags':[],'tag_evidence':{}},'classification_key':classification_key(release,app,settings)})
    async def applic(release,app,settings,**kwargs):
        return release
    async def rollup(app,installed,latest,pending,settings,**kwargs):
        sent.append((kwargs.get('prompt'),[r.version for r in pending]))
        return {'status':'ok','installed':installed,'latest':latest,'releases':[r.version for r in pending],'groups':{},'headline':'x'}
    monkeypatch.setattr(service,'collect',collect)
    monkeypatch.setattr(service,'classify_release',classify)
    monkeypatch.setattr(service,'analyze_applicability',applic)
    monkeypatch.setattr(service,'build_rollup',rollup)
    monkeypatch.setattr(manager,'local_ready',ready)
    try:
        await manager.poll()
        view=manager.view('big')
    finally:
        manager.close()
    assert [r.version for r in view.rollback]==['1.4.0','1.5.0','1.6.0']
    assert ('rollback-v1',['1.4.0','1.5.0','1.6.0']) in sent
    assert not any(v in ('1.1.0','1.2.0','1.3.0') for _,vs in sent for v in vs)
    assert view.rollback_rollup['releases']==['1.4.0','1.5.0','1.6.0']


def test_rollback_window_setting_bounds():
    from ripe.models import Settings
    from ripe.ripeness import build_view
    app,releases=_many(10,installed='1.6.0')
    assert Settings().rollback_window==3
    assert build_view(app,releases,Settings(rollback_window=0)).rollback==[]
    assert len(build_view(app,releases,Settings(rollback_window=10)).rollback)==6
    with pytest.raises(ValueError):
        Settings(rollback_window=11)


def test_tag_prefixes_and_build_suffixes_are_ordered():
    from ripe.versions import compare_versions
    assert compare_versions('n8n@1.2.3','1.2.2')==1
    assert compare_versions('1.2.3-python3.13-alpine3.23','1.2.3')==0
    assert compare_versions('4.5.6-r0-ls237','4.5.5')==1
    assert compare_versions('1.0.0-rc1','1.0.0')==-1


def test_mapping_with_much_older_releases_is_flagged():
    from ripe.models import Settings
    from ripe.ripeness import build_view
    app,releases=_many(3,installed='11.4.2')
    view=build_view(app,releases,Settings())
    assert any('mapping is probably wrong' in s for s in view.hard_signals)
    app,releases=_many(3,installed='1.2.0')
    assert not any('mapping' in s for s in build_view(app,releases,Settings()).hard_signals)


def test_rollback_window_drops_prereleases_unless_installed_is_one():
    from ripe.ripeness import rollback_window
    history=[Release(id=str(i),app_id='a',version=v,notes='n') for i,v in enumerate(['1.0.0','1.1.0-rc.1','1.1.0'])]
    assert [r.version for r in rollback_window(history,'1.1.0',3)]==['1.0.0','1.1.0']
    assert [r.version for r in rollback_window(history,None,3)]==['1.0.0','1.1.0']
    assert [r.version for r in rollback_window(history,'1.1.0-rc.1',3)]==['1.0.0','1.1.0-rc.1','1.1.0']


def test_rollback_prompt_uses_its_own_reduce_prompt():
    from ripe.intelligence.rollup import _reduce_prompt, ROLLBACK_PROMPT, REDUCE_PROMPT
    from ripe.intelligence import prompt_digest
    assert _reduce_prompt(ROLLBACK_PROMPT)=='rollback-reduce-v1'
    assert _reduce_prompt('rollup-v1')==REDUCE_PROMPT
    assert prompt_digest('rollback-reduce-v1')


async def _rollback(monkeypatch,manager,app,releases,results,ready=True):
    calls=[]
    async def rollup(app,installed,latest,pending,settings,**kwargs):
        calls.append(kwargs)
        return results.pop(0)
    async def local_ready():
        return ready
    monkeypatch.setattr(service,'build_rollup',rollup)
    from ripe.models import Settings
    await manager.refresh_rollback(app,Settings(),local_ready)
    return calls


def _ok(app):
    return {'status':'ok','installed':app.installed_version,'latest':app.installed_version,'releases':['1.4.0','1.5.0','1.6.0'],'groups':{},'headline':'x'}


@pytest.mark.asyncio
async def test_refresh_rollback_caches_retries_and_resets(tmp_path,monkeypatch):
    app,releases=_many(10,installed='1.6.0')
    manager=Coordinator(runtime(tmp_path))
    monkeypatch.setattr(manager.store,'releases',lambda app_id:releases)
    try:
        calls=await _rollback(monkeypatch,manager,app,releases,[_ok(app)])
        assert len(calls)==1
        assert (await _rollback(monkeypatch,manager,app,releases,[_ok(app)]))==[]  # cache hit
        bad={'status':'unavailable','installed':'1.6.0','latest':'1.6.0','releases':[],'groups':{}}
        app2=app.model_copy(update={'installed_version':'1.5.0'})
        await _rollback(monkeypatch,manager,app2,releases,[bad])
        assert manager.store.get('rollback:'+app.id)['key'] is None  # retried next check
        assert len(await _rollback(monkeypatch,manager,app2,releases,[_ok(app2)]))==1
        app3=app.model_copy(update={'installed_version':None})
        await _rollback(monkeypatch,manager,app3,releases,[])
        assert manager.store.get('rollback:'+app.id) is None
    finally:
        manager.close()


def test_stored_rollback_is_dropped_when_installed_or_window_changes(tmp_path,monkeypatch):
    app,releases=_many(10,installed='1.6.0')
    manager=Coordinator(runtime(tmp_path))
    monkeypatch.setattr(manager.store,'releases',lambda app_id:releases)
    monkeypatch.setattr(manager.store,'app',lambda app_id:app)
    try:
        manager.store.set('rollback:'+app.id,{'key':'k','result':_ok(app)})
        assert manager.view(app.id).rollback_rollup is not None
        stale={**_ok(app),'installed':'1.5.0'}
        manager.store.set('rollback:'+app.id,{'key':'k','result':stale})
        assert manager.view(app.id).rollback_rollup is None
        other={**_ok(app),'releases':['1.1.0']}
        manager.store.set('rollback:'+app.id,{'key':'k','result':other})
        assert manager.view(app.id).rollback_rollup is None
    finally:
        manager.close()


def test_card_excludes_rollback_payload():
    from ripe.models import Settings
    from ripe.ripeness import build_view
    app,releases=_many(10,installed='1.6.0')
    view=build_view(app,releases,Settings())
    view.rollback_rollup={'status':'ok'}
    data=Coordinator.card(view)
    assert 'rollback' not in data and 'rollback_rollup' not in data
