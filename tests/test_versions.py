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


def test_build_suffix_pattern_does_not_backtrack_catastrophically():
    import time
    start = time.perf_counter()
    normalize_version('1.0.0-py9' + '_py0' * 5000 + '!')
    assert time.perf_counter() - start < 1
