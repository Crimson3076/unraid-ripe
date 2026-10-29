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
