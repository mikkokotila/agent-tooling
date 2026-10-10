import asyncio
import base64
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from caller_pr_gates.cli import gateway
from caller_pr_gates.github import _rest_pages, EvidenceError
from caller_pr_gates.store import Store

FILES = '/repos/autonomio/resolvent/pulls/2/files'
REPO = 'autonomio/resolvent'


def test_identical_bytes_in_different_files_are_distinct():
    client = SimpleNamespace(get=AsyncMock(return_value=[
        {'filename': 'first.json', 'sha': 'same'}, {'filename': 'second.json', 'sha': 'same'}]))
    assert len(asyncio.run(_rest_pages(client, FILES))) == 2


def test_identical_blobs_across_pages_remain_complete():
    first = [{'filename': f'file-{i}', 'sha': 'same'} for i in range(100)]
    client = SimpleNamespace(get=AsyncMock(side_effect=[first, [{'filename': 'last', 'sha': 'same'}]]))
    assert len(asyncio.run(_rest_pages(client, FILES))) == 101
    assert client.get.call_count == 2


def test_repeated_path_and_missing_path_still_fail_closed():
    for data in ([{'filename': 'a', 'sha': 'one'}, {'filename': 'a', 'sha': 'two'}], [{'sha': 'one'}]):
        client = SimpleNamespace(get=AsyncMock(return_value=data))
        with pytest.raises(EvidenceError):
            asyncio.run(_rest_pages(client, FILES))


def test_non_file_duplicate_ids_still_fail_closed():
    client = SimpleNamespace(get=AsyncMock(return_value=[{'id': 1}, {'id': 1}]))
    with pytest.raises(EvidenceError):
        asyncio.run(_rest_pages(client, '/repos/o/r/check-runs'))


@pytest.fixture
def config(tmp_path):
    store = Store(tmp_path / 'state')
    store.record_session('s', 'codex', str(tmp_path))
    store.register('s', 'codex', REPO, 2, str(tmp_path))
    path = tmp_path / 'request.json'
    payload = {'key_id': 'public-key-id', 'encrypted_value': base64.b64encode(b'opaque-ciphertext' * 8).decode()}
    path.write_text(json.dumps(payload))
    args = SimpleNamespace(session='s', client='codex', cwd=str(tmp_path), kind=None, slice_id=None,
                           target=f'https://github.com/{REPO}/pull/2',
                           arguments=['api', f'repos/{REPO}/actions/secrets/RESEND_API_KEY', '--method', 'PUT', '--input', str(path)])
    return store, args, path


@pytest.mark.parametrize('name', ['RESEND_API_KEY', 'RESOLVENT_NOTIFY_TO'])
def test_only_authorized_encrypted_secret_puts_are_allowed(config, name):
    store, args, _ = config
    args.arguments[1] = f'repos/{REPO}/actions/secrets/{name}'
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(args, store)
    run.assert_called_once()
    assert [(p['repository'], p['number']) for p in store.prs('s')] == [(REPO, 2)]


@pytest.mark.parametrize('verb,suffix', [('POST', ''), ('PATCH', '/RESOLVENT_NOTIFY_FROM')])
def test_verified_sender_variable_is_allowed(config, verb, suffix):
    store, args, path = config
    path.write_text(json.dumps({'name': 'RESOLVENT_NOTIFY_FROM', 'value': 'Resolvent <resolvent@autonom.io>'}))
    args.arguments[1], args.arguments[3] = f'repos/{REPO}/actions/variables' + suffix, verb
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(args, store)
    run.assert_called_once()


@pytest.mark.parametrize('endpoint,verb', [
    (f'repos/{REPO}/actions/secrets/OTHER', 'PUT'),
    (f'repos/{REPO}/actions/secrets/RESEND_API_KEY', 'DELETE'),
    ('repos/other/repo/actions/secrets/RESEND_API_KEY', 'PUT'),
    (f'repos/{REPO}/branches/main/protection', 'PUT'),
    (f'repos/{REPO}/actions/variables/OTHER', 'PATCH'),
])
def test_unrelated_configuration_and_policy_writes_are_denied(config, endpoint, verb):
    store, args, _ = config
    args.arguments[1], args.arguments[3] = endpoint, verb
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


def test_unowned_pr_cannot_write_configuration(config):
    store, args, _ = config
    args.target = f'https://github.com/{REPO}/pull/99'
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


def test_plaintext_or_extra_secret_fields_are_denied(config):
    store, args, path = config
    for payload in ({'key_id': 'key', 'encrypted_value': 'plaintext'},
                    {'key_id': 'key', 'encrypted_value': 'a' * 120, 'value': 'plaintext'}):
        path.write_text(json.dumps(payload))
        with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
            with pytest.raises(ValueError, match='API gateway permits'):
                gateway(args, store)
