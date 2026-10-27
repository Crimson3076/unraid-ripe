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
    release=Release(id='github:example/app:1',app_id=name,version='1.1.0',published_at=utcnow()-timedelta(days=30),notes='Fixed an error.')
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
        return release.model_copy(update={'classification':{'status':'ok','tags':['bugfix'],'tag_evidence':{'bugfix':'Fixed an error.'}},'classification_key':classification_key(release,app,settings)})
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
    release.classification={'status':'ok','tags':['breaking'],'breaking':[{'description':'Feature removed','evidence':'Fixed an error.'}]}
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
        return release.model_copy(update={'applicability':[{'kind':'breaking','index':0,'status':'does_not_apply','verified':True,'reason':'Old disabled feature','evidence':'Fixed an error.','profile_evidence':'false'}]})
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
