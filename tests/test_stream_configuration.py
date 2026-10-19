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
    store, args, _ = config
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(args, store)
    run.assert_called_once()


def test_stream_workflow_can_create_prs_with_default_read_permissions(config):
    store, args, path = config
    path.write_text(json.dumps({'default_workflow_permissions': 'read', 'can_approve_pull_request_reviews': True}))
    args.arguments[1] = f'repos/{REPO}/actions/permissions/workflow'
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(args, store)
    run.assert_called_once()


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
