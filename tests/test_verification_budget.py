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
    monkeypatch.setattr(manage, 'RELEASE', {'known_defects': []})
    return report, xml


def test_full_suite_has_finite_headroom_without_changing_other_command_defaults(verification, monkeypatch):
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
    assert calls[0][1]['timeout'] == 900 and calls[0][1]['check'] is True
    assert calls[0][1]['cwd'] == report.parent.parent
    assert result['source_fingerprint'] == 'current-source' and result['testcases'] == 1
    assert json.loads(report.read_text()) == result
    manage.run(['ordinary-command'])
    assert len(calls) == 2 and calls[1][1]['timeout'] == 180 and calls[1][1]['check'] is True


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
