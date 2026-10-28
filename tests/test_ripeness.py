from datetime import datetime, timedelta, timezone

from ripe.models import AppRecord, Profile, Release, Settings
from ripe.ripeness import build_view, dashboard_sort_key, soak_deadline

NOW = datetime(2026,10,27,12,tzinfo=timezone.utc)


def release(version,age,**kwargs):
    classification = kwargs.pop('classification', {'summary':'A fix','tags':['bugfix']})
    return Release(id=version,app_id='a',version=version,published_at=NOW-timedelta(days=age) if age is not None else None,
                   notes='Fixed bug. Security fix. Breaks old config.',classification=classification,**kwargs)


def app(version='1.0.0',**kwargs):
    return AppRecord(id='a',name='app',source='docker',installed_version=version,**kwargs)


def test_security_earliest_fixing_version_example():
    releases = [release('1.01',20),release('1.02',12,classification={'tags':['security'],'security':[{'description':'Fix','evidence':'Security fix.'}]}),release('1.03',3)]
    v = build_view(app('1.00'),releases,Settings(),NOW)
    assert v.status == 'recommended_now'
    assert v.recommended_version == '1.02'
    assert v.days_remaining == 11
    assert v.releases_behind == 3
    # Card-level tags only when they apply to the whole pending span.
    assert v.tags == []


def test_card_tags_are_those_shared_by_every_pending_release():
    tagged=lambda version,age,*tags: release(version,age,classification={'status':'ok','tags':list(tags)})
    v = build_view(app('1.00'),[tagged('1.01',20,'bugfix','docs_only'),tagged('1.02',12,'bugfix'),tagged('1.03',3,'bugfix','performance')],Settings(),NOW)
    assert v.tags == ['bugfix']


def test_newer_release_resets_unfinished_quiet_soak():
    v = build_view(app(),[release('1.1.0',20),release('1.2.0',10)],Settings(),NOW)
    assert v.status == 'waiting'
    assert v.recommended_version is None
    assert v.days_remaining == 4
    assert 'clock restarted' in v.reason


def test_previously_completed_soak_retains_older_target():
    v = build_view(app(),[release('1.1.0',30),release('1.2.0',2)],Settings(),NOW)
    assert v.status == 'ripe'
    assert v.recommended_version == '1.1.0'
    assert v.days_remaining == 12


def test_ha_patch_exception_uses_base_in_history():
    a=app('2026.10.1',profile=Profile(tier='infrastructure'))
    releases=[release('2026.10.0',30),release('2026.10.1',25),release('2026.10.2',4)]
    v=build_view(a,releases,Settings(),NOW)
    assert v.status == 'ripe'
    assert v.recommended_version == '2026.10.2'
    assert [r.version for r in v.history] == ['2026.10.0','2026.10.1']
    assert len(v.pending) == 1


def test_patch_does_not_guess_line_start_when_base_missing():
    v=build_view(app('2026.10.1',profile=Profile(tier='infrastructure')),[release('2026.10.2',4)],Settings(),NOW)
    assert v.status == 'waiting'
    assert v.days_remaining == 17


def test_patch_waits_until_base_is_old_enough():
    releases=[release('2026.10.0',15),release('2026.10.2',4)]
    d=soak_deadline(releases[1],releases,Settings(),'infrastructure')
    assert d == NOW + timedelta(days=6)


def test_infrastructure_default_and_patch_exception_disabled():
    a=app('2026.10.1',profile=Profile(tier='infrastructure'))
    v=build_view(a,[release('2026.10.0',40),release('2026.10.2',4)],Settings(patch_exception=False),NOW)
    assert v.days_remaining == 17


def test_explicit_evidenced_nonapplicable_security_does_not_escalate():
    r=release('1.1.0',4,classification={'tags':['security'],'security':[{'evidence':'Security fix.'}]},
              applicability=[{'kind':'security','index':0,'status':'does_not_apply','evidence':'Security fix.','profile_evidence':'feature disabled'}])
    v=build_view(app(),[r],Settings(),NOW)
    assert v.status == 'waiting'
    assert v.tags == ['security']


def test_unevidenced_nonapplicability_does_not_hide_security():
    r=release('1.1.0',4,classification={'tags':['security'],'security':[{'evidence':'Security fix.'}]},
              applicability=[{'kind':'security','index':0,'status':'does_not_apply','evidence':'Security fix.'}])
    assert build_view(app(),[r],Settings(),NOW).status == 'recommended_now'


def test_unquoted_security_claim_is_not_a_security_fix():
    r=release('1.1.0',4,classification={'tags':['security'],'security':[{'description':'Guess'}]})
    assert build_view(app(),[r],Settings(),NOW).status == 'waiting'


def test_later_security_fix_not_omitted():
    rs=[release('1.1.0',15,classification={'tags':['security'],'security':[{'evidence':'Security fix.'}]}),
        release('1.2.0',2,classification={'tags':['security'],'security':[{'severity':'high','evidence':'Security fix.'}]}),release('1.3.0',1)]
    assert build_view(app(),rs,Settings(),NOW).recommended_version == '1.2.0'


def test_major_database_and_prerelease_hard_signals():
    v=build_view(AppRecord(id='a',name='Postgres',source='docker',image='postgres:16',installed_version='15.0.0',latest_version='16.0.0rc1'),
                 [release('16.0.0rc1',30,prerelease=True)],Settings(),NOW)
    assert v.status == 'read_first'
    assert 'Database image major version change' in v.hard_signals
    assert 'Pending span includes a pre-release' in v.hard_signals
    assert v.recommended_version is None


def test_applicable_breaking_is_read_first():
    r=release('1.1.0',30,classification={'tags':['breaking'],'breaking':[{'evidence':'Breaks old config.'}]})
    assert build_view(app(),[r],Settings(),NOW).status == 'read_first'


def test_missing_dates_and_classifications_are_honestly_unknown():
    assert build_view(app(),[release('1.1.0',None)],Settings(),NOW).status == 'unknown'
    assert build_view(app(),[release('1.1.0',30,classification=None)],Settings(),NOW).status == 'unknown'
    assert build_view(app(None),[release('1.1.0',30)],Settings(),NOW).status == 'unknown'


def test_update_moves_pending_into_history():
    rs=[release('1.1.0',30),release('1.2.0',2)]
    v=build_view(app('1.1.0'),rs,Settings(),NOW)
    assert len(v.pending) == 1 and len(v.history) == 1
    assert v.days_remaining == 12


def test_up_to_date_without_pending_notes():
    assert build_view(app(latest_version='1.0.0'),[],Settings(),NOW).status == 'up_to_date'


def test_latest_only_digest_and_changed_date():
    a=app(None,latest_only=True,installed_digest='sha256:old',latest_digest='sha256:new',image_changed_at=NOW-timedelta(days=3))
    v=build_view(a,[],Settings(),NOW)
    assert v.status == 'waiting' and v.days_remaining == 11
    assert v.recommended_version is None
    assert v.hard_signals
    a.image_changed_at=NOW-timedelta(days=20)
    assert build_view(a,[],Settings(),NOW).recommended_version == 'latest'
    a.latest_digest='sha256:old'
    assert build_view(a,[],Settings(),NOW).status == 'up_to_date'


def test_latest_only_missing_remote_date_does_not_invent_one():
    a=app(None,latest_only=True,update_available=True,image_created_at=NOW-timedelta(days=50))
    assert build_view(a,[],Settings(),NOW).status == 'unknown'


def test_tier_then_severity_sort():
    security=build_view(app('1.00'),[release('1.02',2,classification={'tags':['security'],'security':[{'evidence':'Security fix.'}]})],Settings(),NOW)
    infra=build_view(app(latest_version='1.0.0',profile=Profile(tier='infrastructure')),[],Settings(),NOW)
    assert sorted([security,infra],key=dashboard_sort_key)[0] == infra


def test_missing_latest_record_cannot_use_older_clock():
    v=build_view(app(latest_version='1.2.0'),[release('1.1.0',30)],Settings(),NOW)
    assert v.status == 'unknown'
    assert v.recommended_version is None
    assert v.days_remaining is None
    assert 'Latest version' in v.reason


def test_authoritative_latest_bounds_span_and_ignores_next_beta():
    a=app('2026.10.1',latest_version='2026.10.3',profile=Profile(tier='infrastructure'))
    rs=[release('2026.10.0',45),release('2026.10.2',10),release('2026.10.3',5),release('2026.11.0b1',2,prerelease=True)]
    v=build_view(a,rs,Settings(),NOW)
    assert v.latest_version == '2026.10.3'
    assert v.recommended_version == '2026.10.3'
    assert v.status == 'ripe'
    assert [r.version for r in v.pending] == ['2026.10.2','2026.10.3']
    assert 'Pending span includes a pre-release' not in v.hard_signals


def test_stable_default_does_not_promote_upstream_prerelease():
    v=build_view(app(),[release('1.1.0',30),release('2.0.0b1',2,prerelease=True)],Settings(),NOW)
    assert v.latest_version == '1.1.0'
    assert v.recommended_version == '1.1.0'
    assert v.releases_behind == 1


def test_intermediate_prereleases_not_counted_for_stable_channel():
    v=build_view(app(latest_version='1.1.0'),[release('1.1.0rc1',30,prerelease=True),release('1.1.0',20)],Settings(),NOW)
    assert v.releases_behind == 1
    assert v.status == 'ripe'


def test_stale_ripe_or_current_record_cannot_claim_fresh_readiness():
    for a,rs in [(app(latest_version='1.0.0',error='Source failed'),[]),
                 (app(error='Source failed'),[release('1.1.0',30)]),
                 (app(error='Source failed'),[release('1.1.0',2)])]:
        v=build_view(a,rs,Settings(),NOW)
        assert v.status == 'unknown'
        assert v.recommended_version is None
        assert v.days_remaining is None
        assert 'last known data' in v.reason


def test_stale_data_retains_supported_security_warning():
    r=release('1.1.0',2,classification={'tags':['security'],'security':[{'severity':'high','evidence':'Security fix.'}]})
    v=build_view(app(error='Source failed'),[r],Settings(),NOW)
    assert v.status == 'recommended_now'
    assert v.recommended_version == '1.1.0'
    assert 'last known data' in v.reason


def test_stale_data_retains_read_first_caution():
    r=release('1.1.0',2,classification={'tags':['breaking'],'breaking':[{'evidence':'Breaks old config.'}]})
    v=build_view(app(error='Source failed'),[r],Settings(),NOW)
    assert v.status == 'read_first'
    assert 'last known data' in v.reason


def test_latest_only_evidenced_security_overrides_soak_and_targets_latest():
    a=app(latest_version='1.1.0',latest_only=True,installed_digest='sha256:old',latest_digest='sha256:new',image_changed_at=NOW-timedelta(days=1))
    r=release('1.1.0',1,classification={'tags':['security'],'security':[{'severity':'high','evidence':'Security fix.'}]})
    v=build_view(a,[r],Settings(),NOW)
    assert v.status == 'recommended_now'
    assert v.recommended_version == 'latest'
    assert v.days_remaining == 13


def test_latest_only_breaking_change_requires_review():
    a=app(latest_version='1.1.0',latest_only=True,update_available=True,image_changed_at=NOW-timedelta(days=30))
    r=release('1.1.0',30,classification={'tags':['breaking'],'breaking':[{'evidence':'Breaks old config.'}]})
    v=build_view(a,[r],Settings(),NOW)
    assert v.status == 'read_first'
    assert v.recommended_version == 'latest'


def test_latest_only_does_not_recommend_repulling_identical_digest_for_upstream_security():
    a=app(latest_version='1.1.0',latest_only=True,installed_digest='sha256:same',latest_digest='sha256:same')
    r=release('1.1.0',30,classification={'tags':['security'],'security':[{'evidence':'Security fix.'}]})
    v=build_view(a,[r],Settings(),NOW)
    assert v.status == 'read_first'
    assert v.recommended_version is None
    assert 'Verify whether this image contains' in v.reason


def test_keyword_coverage_warning_requires_review_without_inventing_breaking_tag():
    r=release('1.1.0',30,classification={'tags':['bugfix'],'keyword_warnings':[{'term':'migration','quote':'no migration needed'}]})
    v=build_view(app(),[r],Settings(),NOW)
    assert v.status == 'read_first'
    assert v.tags == ['bugfix']
    assert 'Review incomplete keyword coverage/model disagreement' in v.reason


def test_model_disagreement_requires_review_without_inventing_breaking_tag():
    r=release('1.1.0',30,model_disagreement=True)
    v=build_view(app(),[r],Settings(),NOW)
    assert v.status == 'read_first'
    assert v.tags == ['bugfix']
    assert 'Review incomplete keyword coverage/model disagreement' in v.reason


def test_latest_only_model_disagreement_requires_review():
    a=app(latest_version='1.1.0',latest_only=True,update_available=True,image_changed_at=NOW-timedelta(days=30))
    v=build_view(a,[release('1.1.0',30,model_disagreement=True)],Settings(),NOW)
    assert v.status == 'read_first'
    assert v.recommended_version == 'latest'


def test_security_still_recommended_but_keyword_coverage_caution_survives():
    r=release('1.1.0',1,classification={'tags':['security'],'security':[{'severity':'high','evidence':'Security fix.'}],'keyword_warnings':[{'term':'must'}]})
    v=build_view(app(),[r],Settings(),NOW)
    assert v.status == 'recommended_now'
    assert 'Review incomplete keyword coverage/model disagreement' in v.reason


def test_unknown_installed_span_still_exposes_available_notes():
    r=release('1.1.0',30)
    v=build_view(app(None,latest_version='1.1.0'),[r],Settings(),NOW)
    assert v.status=='unknown'
    assert not v.pending and not v.history
    assert v.releases_behind==0
    assert v.available_notes==[r]


SEC=lambda **kw:{'tags':['security'],'security':[{'evidence':'Security fix.',**kw}]}


def test_unrated_security_claim_still_waits_patch_soak():
    v=build_view(app(),[release('1.1.0',1,classification=SEC())],Settings(),NOW)
    assert v.status != 'recommended_now'
    assert v.recommended_version is None


def test_unrated_security_claim_recommended_after_patch_soak():
    v=build_view(app(),[release('1.1.0',4,classification=SEC())],Settings(),NOW)
    assert v.status == 'recommended_now'
    assert v.recommended_version == '1.1.0'


def test_low_severity_security_waits_but_high_skips_soak():
    assert build_view(app(),[release('1.1.0',1,classification=SEC(severity='low'))],Settings(),NOW).status != 'recommended_now'
    v=build_view(app(),[release('1.1.0',1,classification=SEC(severity='critical'))],Settings(),NOW)
    assert v.status == 'recommended_now'
    assert 'severity critical' in v.reason


def test_advisory_id_in_notes_skips_soak_and_is_shown():
    r=release('1.1.0',1,classification={'tags':['security'],'security':[{'evidence':'Security: fixes CVE-2026-12345 and GHSA-abcd-2345-wxyz.'}]})
    r.notes='Security: fixes CVE-2026-12345 and GHSA-abcd-2345-wxyz.'
    v=build_view(app(),[r],Settings(),NOW)
    assert v.status == 'recommended_now'
    assert 'CVE-2026-12345' in v.reason



def test_unsoaked_later_security_fix_still_constrains_target():
    v=build_view(app(),[release('1.1.0',15,classification=SEC(severity='low')),
                        release('1.2.0',1,classification=SEC(severity='low'))],Settings(),NOW)
    assert v.status == 'recommended_now'
    assert v.recommended_version == '1.2.0'


def test_advisory_of_excluded_fix_does_not_make_other_fix_urgent():
    r=release('1.1.0',1,classification={'tags':['security'],'security':[
        {'evidence':'Fixes CVE-2026-12345 in the proxy.'},{'severity':'low','evidence':'Security fix.'}]})
    r.notes='Fixes CVE-2026-12345 in the proxy. Security fix.'
    r.applicability=[{'kind':'security','index':0,'status':'does_not_apply','evidence':'x','profile_evidence':'y'}]
    v=build_view(app(),[r],Settings(),NOW)
    assert v.status != 'recommended_now'


def test_waiting_countdown_reflects_security_soak():
    v=build_view(app(),[release('1.1.0',1,classification=SEC(severity='low'))],Settings(),NOW)
    assert v.status == 'waiting'
    assert v.days_remaining == 2
