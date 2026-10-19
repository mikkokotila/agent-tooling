import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from caller_pr_gates.cli import gateway
from caller_pr_gates.store import Store


def setup(tmp_path, approved=True):
    store = Store(tmp_path / 'state')
    store.record_session('s', 'codex', str(tmp_path))
    scope = dict(agreed_scope='Create the requested prototype repository',
                 acceptance_criteria=[], slice_count=0, slice_ids=[], approved_at=1)
    if approved:
        scope['repository_creation'] = dict(full_name='owner/prototype', visibility='private',
                                            approval_evidence='User: put it under owner/prototype')
    store.record_scope('s', scope)
    args = SimpleNamespace(session='s', client='codex', cwd=str(tmp_path),
                           arguments=['repo', 'create', 'owner/prototype', '--private'])
    return store, args


@pytest.mark.parametrize('tail', [[], ['--public'], ['--private', '--push'],
                                  ['--private', '--clone'], ['--private', '--source', '.']])
def test_unapproved_repository_flags_never_write(tmp_path, tail):
    store, args = setup(tmp_path)
    args.arguments = ['repo', 'create', 'owner/prototype', *tail]
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError):
            gateway(args, store)


def test_missing_authorization_never_writes(tmp_path):
    store, args = setup(tmp_path, approved=False)
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError):
            gateway(args, store)


def test_owner_mismatch_never_creates(tmp_path):
    store, args = setup(tmp_path)
    with patch('caller_pr_gates.cli.github_run', return_value=subprocess.CompletedProcess([], 0, '{"login":"other"}')) as run:
        with pytest.raises(ValueError, match='authenticated'):
            gateway(args, store)
    assert run.call_count == 1


def test_verified_private_creation_does_not_adopt_prs(tmp_path, capsys):
    store, args = setup(tmp_path)
    responses = [subprocess.CompletedProcess([], 0, '{"login":"owner"}'),
                 subprocess.CompletedProcess([], 0, 'https://github.com/owner/prototype\n'),
                 subprocess.CompletedProcess([], 0, json.dumps(dict(full_name='owner/prototype',
                    private=True, html_url='https://github.com/owner/prototype')))]
    with patch('caller_pr_gates.cli.github_run', side_effect=responses) as run:
        result = gateway(args, store)
    assert result['private'] is True
    assert capsys.readouterr().out == ''
    assert run.call_args_list[1].args[0] == ['gh', *args.arguments]
    assert store.prs('s') == []


def test_readback_mismatch_is_not_success(tmp_path):
    store, args = setup(tmp_path)
    responses = [subprocess.CompletedProcess([], 0, '{"login":"owner"}'),
                 subprocess.CompletedProcess([], 0, ''),
                 subprocess.CompletedProcess([], 0, '{"private":false,"full_name":"owner/prototype"}')]
    with patch('caller_pr_gates.cli.github_run', side_effect=responses):
        with pytest.raises(RuntimeError, match='verified'):
            gateway(args, store)


def test_canonical_github_case_is_verified_after_creation(tmp_path):
    store, args = setup(tmp_path)
    responses = [subprocess.CompletedProcess([], 0, '{"login":"Owner"}'),
                 subprocess.CompletedProcess([], 0, ''),
                 subprocess.CompletedProcess([], 0, json.dumps(dict(full_name='Owner/Prototype',
                    private=True, html_url='https://github.com/Owner/Prototype')))]
    with patch('caller_pr_gates.cli.github_run', side_effect=responses):
        assert gateway(args, store)['url'] == 'https://github.com/Owner/Prototype'


def test_account_lookup_failure_retains_diagnostic_and_never_creates(tmp_path):
    store, args = setup(tmp_path)
    with patch('caller_pr_gates.cli.github_run', return_value=subprocess.CompletedProcess([], 1, '', 'relay cooldown')) as run:
        with pytest.raises(RuntimeError, match='lookup failed: relay cooldown'):
            gateway(args, store)
    assert run.call_count == 1


@pytest.mark.parametrize('malformed', ['authorized', ['owner/prototype'], True])
def test_malformed_authorization_fails_before_remote_reads(tmp_path, malformed):
    store = Store(tmp_path / 'state')
    store.record_session('s', 'codex', str(tmp_path))
    store.record_scope('s', dict(agreed_scope='Create prototype', acceptance_criteria=[],
        slice_count=0, slice_ids=[], approved_at=1, repository_creation=malformed))
    args = SimpleNamespace(session='s', client='codex', cwd=str(tmp_path),
                           arguments=['repo', 'create', 'owner/prototype', '--private'])
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not read/write')):
        with pytest.raises(ValueError, match='mapping'):
            gateway(args, store)
