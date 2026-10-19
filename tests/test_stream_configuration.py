import base64
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from caller_pr_gates.cli import gateway
from caller_pr_gates.store import Store

REPO = 'mikkokotila/mikkokotila-com'


@pytest.fixture
def config(tmp_path):
    store = Store(tmp_path / 'state')
    store.record_session('s', 'codex', str(tmp_path))
    store.register('s', 'codex', REPO, 11, str(tmp_path))
    path = tmp_path / 'request.json'
    payload = {'key_id': 'public-key-id', 'encrypted_value': base64.b64encode(b'opaque-ciphertext' * 8).decode()}
    path.write_text(json.dumps(payload))
    args = SimpleNamespace(session='s', client='codex', cwd=str(tmp_path), kind=None, slice_id=None,
                           target=f'https://github.com/{REPO}/pull/11',
                           arguments=['api', f'repos/{REPO}/actions/secrets/LINKEDIN_TOKEN', '--method', 'PUT', '--input', str(path)])
    return store, args, path


def test_existing_linkedin_credential_can_be_restored_as_encrypted_data(config):
    store, args, path = config
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(args, store)
    run.assert_called_once()
    assert run.call_args.args[0][-2:] == ['--input', '-']
    assert json.loads(run.call_args.kwargs['input']) == json.loads(path.read_text())


def test_stream_workflow_can_create_prs_with_default_read_permissions(config):
    store, args, path = config
    path.write_text(json.dumps({'default_workflow_permissions': 'read', 'can_approve_pull_request_reviews': True}))
    args.arguments[1] = f'repos/{REPO}/actions/permissions/workflow'
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(args, store)
    run.assert_called_once()


def test_stream_auto_merge_can_be_enabled_without_altering_branch_requirements(config):
    store, args, path = config
    path.write_text(json.dumps({'allow_auto_merge': True}))
    args.arguments[1], args.arguments[3] = f'repos/{REPO}', 'PATCH'
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(args, store)
    run.assert_called_once()
    assert json.loads(run.call_args.kwargs['input']) == {'allow_auto_merge': True}


@pytest.mark.parametrize('endpoint,verb', [
    (f'repos/{REPO}/actions/secrets/OTHER', 'PUT'),
    (f'repos/{REPO}/actions/secrets/LINKEDIN_TOKEN', 'DELETE'),
    ('repos/other/repo/actions/secrets/LINKEDIN_TOKEN', 'PUT'),
    (f'repos/{REPO}/branches/main/protection', 'PUT'),
])
def test_unrelated_credentials_and_branch_protection_stay_denied(config, endpoint, verb):
    store, args, _ = config
    args.arguments[1], args.arguments[3] = endpoint, verb
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


def test_configuration_requires_this_chats_owned_pr(config):
    store, args, _ = config
    args.target = f'https://github.com/{REPO}/pull/99'
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


@pytest.mark.parametrize('payload', [
    {'key_id': 'key', 'encrypted_value': 'plaintext'},
    {'key_id': 'key', 'encrypted_value': 'a' * 120, 'value': 'plaintext'},
    {'default_workflow_permissions': 'write', 'can_approve_pull_request_reviews': True},
    {'default_workflow_permissions': 'read', 'can_approve_pull_request_reviews': True, 'extra': True},
])
def test_plaintext_extra_fields_and_default_write_access_are_denied(config, payload):
    store, args, path = config
    path.write_text(json.dumps(payload))
    if 'default_workflow_permissions' in payload:
        args.arguments[1] = f'repos/{REPO}/actions/permissions/workflow'
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


def test_additional_query_fields_cannot_escape_the_exact_payload(config):
    store, args, _ = config
    args.arguments += ['-f', 'value=plaintext']
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


def test_enabling_auto_merge_cannot_modify_other_repository_settings(config):
    store, args, path = config
    path.write_text(json.dumps({'allow_auto_merge': True, 'allow_merge_commit': False}))
    args.arguments[1], args.arguments[3] = f'repos/{REPO}', 'PATCH'
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


def test_relative_input_uses_the_gh_working_directory(config, tmp_path, monkeypatch):
    store, args, path = config
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / path.name).write_text('{"default_workflow_permissions":"write"}')
    monkeypatch.chdir(elsewhere)
    args.arguments[-1] = path.name
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(args, store)
    assert json.loads(run.call_args.kwargs['input']) == json.loads(path.read_text())


def test_stdin_cannot_be_admitted_using_a_decoy_file(config):
    store, args, path = config
    (path.parent / '-').write_text(path.read_text())
    args.arguments[-1] = '-'
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


def test_mutating_the_input_file_cannot_change_the_admitted_request(config):
    store, args, path = config
    admitted = path.read_text()

    def write(argv, **kwargs):
        path.write_text('{"default_workflow_permissions":"write"}')
        assert argv[-2:] == ['--input', '-']
        assert json.loads(kwargs['input']) == json.loads(admitted)
        return subprocess.CompletedProcess(argv, 0)

    with patch('caller_pr_gates.cli.subprocess.run', side_effect=write):
        gateway(args, store)
