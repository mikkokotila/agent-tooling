import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import manage


def activation(tmp_path, monkeypatch, outcomes, running=True):
    calls, waits = [], []
    results = iter(outcomes)

    def command(argv, **kwargs):
        calls.append(argv[1])
        if argv[1] == 'kickstart':
            code = next(results)
            if code:
                raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(manage.subprocess, 'run', command)
    monkeypatch.setattr(manage.time, 'sleep', waits.append)
    monkeypatch.setattr(manage.Service, 'running', lambda self: running)
    return manage.Service(tmp_path), calls, waits


def test_bootstrap_visibility_lag_retries_only_the_existing_activation(tmp_path, monkeypatch):
    service, calls, waits = activation(tmp_path, monkeypatch, [113, 0])
    service.start()
    assert calls == ['bootstrap', 'kickstart', 'kickstart']
    assert waits == [.25]


def test_persistent_missing_job_remains_a_failure_after_three_attempts(tmp_path, monkeypatch):
    service, calls, waits = activation(tmp_path, monkeypatch, [113, 113, 113])
    with pytest.raises(subprocess.CalledProcessError) as error:
        service.start()
    assert error.value.returncode == 113
    assert calls == ['bootstrap'] + ['kickstart'] * 3
    assert waits == [.25, .5]


def test_other_kickstart_errors_are_not_retried_or_suppressed(tmp_path, monkeypatch):
    service, calls, waits = activation(tmp_path, monkeypatch, [77])
    with pytest.raises(subprocess.CalledProcessError) as error:
        service.start()
    assert error.value.returncode == 77
    assert calls == ['bootstrap', 'kickstart']
    assert waits == []


def test_successful_kickstart_still_requires_a_running_monitor(tmp_path, monkeypatch):
    service, calls, waits = activation(tmp_path, monkeypatch, [0], running=False)
    with pytest.raises(RuntimeError, match='Monitor did not start'):
        service.start()
    assert calls == ['bootstrap', 'kickstart']
    assert waits == []


def test_registered_job_without_a_pid_retries_kickstart_without_rebootstrap(tmp_path, monkeypatch):
    calls, waits, kicks = [], [], iter([113, 0])
    state = {'running': False}

    def command(argv, **kwargs):
        calls.append(argv[1])
        if argv[1] == 'bootstrap':
            return subprocess.CompletedProcess(argv, 0 if calls.count('bootstrap') == 1 else 5,
                                               stderr='already registered')
        code = next(kicks)
        if code: raise subprocess.CalledProcessError(code, argv)
        state['running'] = True
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(manage.subprocess, 'run', command)
    monkeypatch.setattr(manage.time, 'sleep', waits.append)
    monkeypatch.setattr(manage.Service, 'running', lambda self: state['running'])
    manage.Service(tmp_path).start()
    assert calls == ['bootstrap', 'kickstart', 'kickstart']
    assert waits == [.25]


def test_failed_bootstrap_never_attempts_kickstart(tmp_path, monkeypatch):
    calls, waits = [], []

    def command(argv, **kwargs):
        assert argv[1] == 'bootstrap'
        calls.append(argv[1])
        return subprocess.CompletedProcess(argv, 5, stderr='bootstrap refused')

    monkeypatch.setattr(manage.subprocess, 'run', command)
    monkeypatch.setattr(manage.time, 'sleep', waits.append)
    monkeypatch.setattr(manage.Service, 'running', lambda self: False)
    with pytest.raises(RuntimeError, match='bootstrap refused'):
        manage.Service(tmp_path).start()
    assert calls == ['bootstrap'] * 3
    assert waits == [.25, .5]
