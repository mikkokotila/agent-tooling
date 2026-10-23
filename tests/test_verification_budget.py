"""The suite's parent wait never changes assertion results or admits a failed install."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import manage


@pytest.fixture
def verification(tmp_path, monkeypatch):
    for name in ('AGENT_TOOLING_VERIFY_TIMEOUT_SECONDS', 'CI', 'GITHUB_ACTIONS'):
        monkeypatch.delenv(name, raising=False)
    build = tmp_path / '.build'
    build.mkdir()
    report = build / 'verified.json'
    report.write_text(json.dumps({'source_fingerprint': 'prior-source', 'testcases': 1}))
    xml = build / 'verification.xml'
    xml.write_text('<testsuites><testsuite><testcase name="prior"/></testsuite></testsuites>')
    monkeypatch.setattr(manage, 'ROOT', tmp_path)
    monkeypatch.setattr(manage, 'BUILD', build)
    monkeypatch.setattr(manage, 'fingerprint', lambda: 'current-source')
    monkeypatch.setattr(manage, 'build', lambda: Path(sys.executable))
    monkeypatch.setattr(manage, 'verification_git', lambda env, state: None)
    monkeypatch.setattr(manage, 'RELEASE', {'known_defects': []})
    return report, xml


@pytest.mark.parametrize('selected', [None, '900', '1200', '1800'])
def test_full_suite_has_finite_headroom_without_changing_other_command_defaults(verification, monkeypatch, selected):
    if selected is not None:
        monkeypatch.setenv('AGENT_TOOLING_VERIFY_TIMEOUT_SECONDS', selected)
    budget = int(selected or '900')
    report, xml = verification
    calls = []

    def child(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1:3] == ['-m', 'pytest']:
            xml.write_text('<testsuites><testsuite><testcase name="fresh"/></testsuite></testsuites>')
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(manage.subprocess, 'run', child)
    result = manage.verify()
    assert calls[0][0] == [sys.executable, '-m', 'pytest', '-q', '--durations=10',
                           '--junitxml=.build/verification.xml']
    assert calls[0][1]['timeout'] == budget and calls[0][1]['check'] is True
    assert calls[0][1]['cwd'] == report.parent.parent
    assert result['source_fingerprint'] == 'current-source' and result['testcases'] == 1
    assert result['verification_timeout_seconds'] == budget
    assert json.loads(report.read_text()) == result
    manage.run(['ordinary-command'])
    assert len(calls) == 2 and calls[1][1]['timeout'] == 180 and calls[1][1]['check'] is True


@pytest.mark.parametrize('value', ['', '0', '899', '1801', '-1', '900.0', '1800 --retry=3'])
def test_invalid_parent_budget_is_refused_before_build(verification, monkeypatch, value):
    monkeypatch.setenv('AGENT_TOOLING_VERIFY_TIMEOUT_SECONDS', value)
    monkeypatch.setattr(manage, 'build', lambda: pytest.fail('Invalid budget reached build'))
    with pytest.raises(RuntimeError, match='integer from 900 to 1800'):
        manage.verify()


@pytest.mark.parametrize('name', ['CI', 'GITHUB_ACTIONS'])
@pytest.mark.parametrize('indicator', ['true', 'false', '1'])
def test_ci_parent_budget_cannot_be_extended(verification, monkeypatch, name, indicator):
    monkeypatch.setenv(name, indicator)
    assert manage.verification_budget() == 900
    monkeypatch.setenv('AGENT_TOOLING_VERIFY_TIMEOUT_SECONDS', '1800')
    with pytest.raises(RuntimeError, match='CI verification retains'):
        manage.verify()


def test_cached_report_from_a_different_budget_is_not_execution_credit(verification, monkeypatch):
    report, _ = verification
    report.write_text(json.dumps({'source_fingerprint': 'current-source', 'verification_timeout_seconds': 1800}))
    monkeypatch.setattr(manage, 'build', lambda: (_ for _ in ()).throw(RuntimeError('Fresh suite required')))
    with pytest.raises(RuntimeError, match='Fresh suite required'):
        manage.verify()


@pytest.mark.parametrize('outcome', ['timeout', 'assertion_failure'])
def test_unsuccessful_suite_cannot_reuse_prior_reports_or_reach_installation(verification, monkeypatch, outcome):
    report, xml = verification
    before = report.read_bytes(), xml.read_bytes()
    calls = []

    def child(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs['timeout'] == 900 and kwargs['check'] is True
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(manage.subprocess, 'run', child)
    monkeypatch.setattr(sys, 'argv', ['manage.py', 'install', '--wait-for-workers', '600'])
    monkeypatch.setattr(manage, 'wait_for_workers', lambda *args: pytest.fail('Failed verification reached worker admission'))
    monkeypatch.setattr(manage, 'deploy', lambda **kwargs: pytest.fail('Failed verification reached activation'))
    error = subprocess.TimeoutExpired if outcome == 'timeout' else subprocess.CalledProcessError
    with pytest.raises(error):
        manage.main()
    assert len(calls) == 1
    assert (report.read_bytes(), xml.read_bytes()) == before


def test_run_still_enforces_an_explicit_deadline_on_a_real_child():
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        manage.run([sys.executable, '-c', 'import time; time.sleep(60)'],
                   timeout=0.1, capture_output=True)
    assert failure.value.timeout == 0.1


@pytest.mark.parametrize('template_source', ['environment', 'configuration'])
@pytest.mark.parametrize('has_exclude', [False, True])
def test_developer_git_launcher_preserves_version_templates_and_helpers(tmp_path, template_source, has_exclude):
    templates = tmp_path / 'templates'
    templates.mkdir()
    (templates / 'template-marker').write_text('custom template\n')
    if has_exclude:
        (templates / 'info').mkdir()
        (templates / 'info/exclude').write_text('# custom excludes\n')
    config = tmp_path / 'gitconfig'
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path),
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': str(config)}
    if template_source == 'environment':
        env['GIT_TEMPLATE_DIR'] = str(templates)
    else:
        subprocess.run(['/usr/bin/git', 'config', '--file', str(config),
                        'init.templateDir', str(templates)], env=env, check=True)
    expected = subprocess.check_output(['/usr/bin/git', '--version'], env=env)
    helpers = subprocess.check_output(['/usr/bin/git', '--exec-path'], env=env)
    manage.verification_git(env, tmp_path)
    assert subprocess.check_output(['git', '--version'], env=env) == expected
    assert subprocess.check_output(['git', '--exec-path'], env=env) == helpers
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', '--quiet', str(repo)], env=env, check=True)
    assert (repo / '.git/template-marker').read_text() == 'custom template\n'
    assert (repo / '.git/info/exclude').is_file() is has_exclude
    assert subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain'], env=env) == b''


def test_configured_git_is_not_replaced(tmp_path, monkeypatch):
    binary = tmp_path / 'git'
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o700)
    env = {'PATH': str(tmp_path)}
    monkeypatch.setattr(manage, 'run', lambda *a, **k: pytest.fail('Custom Git must not invoke xcrun'))
    manage.verification_git(env, tmp_path / 'state')
    assert env == {'PATH': str(tmp_path)}
    assert not (tmp_path / 'state').exists()
