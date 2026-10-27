"""Synthetic, explicitly labelled fixtures. Never mixed with live inventory."""
from datetime import timedelta
from .models import AppRecord, Release, Profile, utcnow
from .store import Store


def seed_demo(store: Store):
    if store.apps():
        return
    now = utcnow()
    def release(app, version, age, notes, tags, **items):
        return Release(id=f'demo:{app.id}:{version}', app_id=app.id, version=version,
            published_at=now-timedelta(days=age), notes=notes,
            source_url=f'https://github.com/{app.upstream_repo}/releases/tag/{version}',
            classification={'status':'ok','summary':'','summary_evidence':'','tags':tags,
                'tag_evidence':{t:notes.split('\n')[0] for t in tags},
                'security':[], 'breaking':[], 'migration':[], 'big_features':[],
                'keyword_hits_addressed':[], 'keyword_warnings':[],
                'confidence':'high','model':'synthetic fixture','provider':'fixture',
                'prompt_version':'fixture-v1','provenance':[{'model':'synthetic fixture','provider':'fixture'}], **items})
    romm = AppRecord(id='demo-romm', name='RomM', source='demo', installed_version='5.2.0',latest_version='5.4.2',upstream_repo='rommapp/romm',
        profile=Profile(tier='general',exposure='lan',own_login=True,config={'filesystem':{'roms_folder':'roms'},'browser_clients_other_origins':False},dependencies=['MariaDB']))
    notes = 'The filesystem.roms_folder option was removed. RomM refuses to start if roms_folder is set.\nCORS defaults now reject browser clients on other origins.'
    r = release(romm,'5.3.0',30,notes,['breaking'],breaking=[
        {'description':'Remove the obsolete roms_folder setting before updating.','action_needed':'Remove filesystem.roms_folder from your config after reviewing the upstream migration guide.','evidence':'RomM refuses to start if roms_folder is set.'},
        {'description':'Cross-origin browser clients require explicit CORS configuration.','action_needed':'Review allowed origins when using browser clients on other origins.','evidence':'CORS defaults now reject browser clients on other origins.'}])
    r.applicability=[{'kind':'breaking','index':0,'status':'applies','reason':'The configured roms_folder is incompatible with this release.','evidence':'RomM refuses to start if roms_folder is set.','profile_evidence':'"roms_folder": "roms"','verified':True},
        {'kind':'breaking','index':1,'status':'does_not_apply','reason':'This profile has no browser clients on other origins.','evidence':'CORS defaults now reject browser clients on other origins.','profile_evidence':'"browser_clients_other_origins": false','verified':True}]
    records=[(romm,[r,release(romm,'5.4.0',18,'Improved library scanning.', ['performance']),release(romm,'5.4.2',6,'Fixed cover artwork caching.', ['bugfix'])])]
    ha=AppRecord(id='demo-ha',name='Home Assistant Core',source='demo',installed_version='2026.9.3',latest_version='2026.10.3',upstream_repo='home-assistant/core',profile=Profile(tier='infrastructure',exposure='lan'))
    records.append((ha,[release(ha,'2026.10.0',27,'New dashboard capabilities.', ['big_feature']),release(ha,'2026.10.2',10,'Fixed automation timing.', ['bugfix']),release(ha,'2026.10.3',4,'Fixed integration reconnects.', ['bugfix'])]))
    security=AppRecord(id='demo-security',name='Example gateway',source='demo',installed_version='1.00',latest_version='1.03',upstream_repo='example/gateway',profile=Profile(tier='infrastructure',exposure='internet'))
    s=release(security,'1.02',12,'Security: fixes an authentication bypass for internet-exposed instances.', ['security'],security=[{'description':'Authentication bypass fixed.','severity':'high','requires':'internet exposure','evidence':'fixes an authentication bypass for internet-exposed instances.'}])
    s.applicability=[{'kind':'security','index':0,'status':'applies','reason':'This gateway is exposed to the internet.','evidence':'fixes an authentication bypass for internet-exposed instances.','profile_evidence':'"exposure": "internet"','verified':True}]
    records.append((security,[release(security,'1.01',20,'Fixed stability issues.', ['bugfix']),s,release(security,'1.03',3,'Added dashboard widgets.', ['big_feature'])]))
    frigate=AppRecord(id='demo-frigate',name='Frigate',source='demo',installed_version='0.15.0',latest_version='0.15.1',upstream_repo='blakeblackshear/frigate',profile=Profile(tier='general',exposure='lan'))
    records.append((frigate,[release(frigate,'0.15.1',12,'Fixed recording playback.', ['bugfix'])]))
    unraid=AppRecord(id='demo-unraid',name='Unraid OS',source='demo',installed_version='7.1.0',latest_version='7.1.0',profile=Profile(tier='infrastructure',exposure='lan'))
    records.append((unraid,[]))
    unknown=AppRecord(id='demo-tool',name='Private utility',source='demo',image='local/utility:latest',latest_only=True,image_created_at=now-timedelta(days=40),profile=Profile(tier='general',exposure='vpn'))
    records.append((unknown,[]))
    for app, releases in records:
        store.save_app(app)
        for r in releases:
            store.save_release(r)
    store.set('demo',True)
    store.set('last_poll',now.isoformat())
