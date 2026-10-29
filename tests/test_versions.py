import pytest
from ripe.versions import compare_versions, normalize_version, semver_major_bump


@pytest.mark.parametrize('old,new', [
    ('1.00','1.01'), ('1.01','1.02'), ('1.02','1.03'),
    ('2026.10.3','2026.11.0'), ('v5.2.0','5.4.0'),
    ('1.2.3-rc.1','1.2.3'), ('1.2.3','1.2.3-post1'),
    ('RELEASE.2025-03-12T18-04-18Z','RELEASE.2025-03-13T18-04-18Z'),
])
def test_real_version_order(old,new):
    assert compare_versions(old,new) == -1
    assert compare_versions(new,old) == 1


def test_linuxserver_version_tags_compare_upstream_version():
    assert normalize_version('version-5.4.0-ls123') == '5.4.0'
    assert compare_versions('version-5.4.0-ls123','v5.4.0') == 0


@pytest.mark.parametrize('bad', ['latest','main','sha-1234abcd','unknown','abc','2026-potato',None])
def test_unknown_not_lexically_sorted(bad):
    assert compare_versions(bad,'1.0.0') is None


def test_calendar_versions_are_not_semver_major_warnings():
    assert not semver_major_bump('2025.12.0','2026.1.0')
    assert semver_major_bump('5.4.0','6.0.0')


def test_linuxserver_ubuntu_build_suffix_compares_upstream_version():
    assert normalize_version('12.2ubu2604-ls54') == '12.2'
    assert compare_versions('12.2ubu2604-ls54', 'v12.4') == -1
    assert compare_versions('v12.4', '12.2ubu2604-ls54') == 1


def test_case_and_leading_zero_minor_compare_equal():
    assert compare_versions('V1.07', 'v1.7.0') == 0


def test_null_version_label_is_not_a_version():
    from ripe.sources.collector import usable_version_label
    assert usable_version_label('null') is None
    assert usable_version_label('None') is None


def test_revision_suffix_kept_without_linuxserver_tag():
    assert compare_versions('1.0-r1', '1.0-r2') == -1
    assert normalize_version('1.0-r1-ls5') == '1.0'


def test_tag_prefixes_and_build_suffixes_are_stripped_before_ordering():
    from ripe.versions import compare_versions, parse_version
    assert str(parse_version('n8n@1.2.3')) == '1.2.3'
    assert compare_versions('1.2.3-python3.13-alpine3.23', '1.2.3') == 0
    assert compare_versions('10.11.6-r0-ls237', '10.11.6') == 0
    assert compare_versions('2.0.1-bookworm-slim', '2.0.0') == 1
    assert compare_versions('1.0.0-rc1', '1.0.0') == -1
    assert compare_versions('1.0.0-beta.2', '1.0.0') == -1


def test_mapped_repo_far_behind_installed_is_flagged():
    from ripe.models import AppRecord, Release, Settings
    from ripe.ripeness import build_view
    app = AppRecord(id='m', name='mariadb', source='docker', installed_version='11.4.2', upstream_repo='x/mariadb')
    old = Release(id='github:x/mariadb:1', app_id='m', version='5.5.1', notes='n')
    view = build_view(app, [old], Settings())
    assert any('wrong repository' in s for s in view.hard_signals)
    app.installed_version = '5.5.0'
    assert not any('wrong repository' in s for s in build_view(app, [old], Settings()).hard_signals)
