import base64
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from caller_pr_gates.cli import gateway
from caller_pr_gates.monitor import retire
from caller_pr_gates.store import Store


UPSTREAM = 'maintainer/source'
FORK = 'operator/deployment'
PREFIX = f'repos/{FORK}/actions/'
SECRET = PREFIX + 'secrets/POISE_GOOGLE_CLIENT_SECRET'
VARIABLE = PREFIX + 'variables'
CLIENT = '123456-example.apps.googleusercontent.com'


@pytest.fixture
def config(tmp_path):
    store = Store(tmp_path / 'state')
    store.record_session('s', 'codex', str(tmp_path))
    store.register('s', 'codex', UPSTREAM, 11, str(tmp_path))
    store.record_scope('s', {
        'agreed_scope': 'Persist the approved existing Google integration keys.',
        'approval_evidence': 'Human approved this exact bounded configuration.',
        'acceptance_criteria': ['Only the two approved keys are persisted.'],
        'slice_count': 0, 'slice_ids': [], 'approved_at': 1,
        'google_actions_configuration': {'repository': FORK,
            'authorizing_pr': f'https://github.com/{UPSTREAM}/pull/11',
            'approval_evidence': 'Human approved the existing pair in this operator fork.'},
    }, work_item='google-configuration')
    path = tmp_path / 'request.json'
    path.write_text(json.dumps({'key_id': 'github-public-key-id',
        'encrypted_value': base64.b64encode(b'opaque-ciphertext' * 8).decode()}))
    args = SimpleNamespace(session='s', client='codex', cwd=str(tmp_path), kind=None,
        slice_id=None, target=f'https://github.com/{UPSTREAM}/pull/11',
        arguments=['api', SECRET, '--method', 'PUT', '--input', str(path)])
    return store, args, path


def test_retained_upstream_owner_can_persist_only_the_sealed_fork_secret(config):
    store, args, path = config
    retire(store, UPSTREAM, 11, {'state': 'closed', 'merged': True,
        'head': {'sha': 'a' * 40, 'ref': 'topic'}})
    assert store.prs('s') == []
    with store._connect() as db:
        before = tuple(db.execute('SELECT state,owner_session,event_id FROM pr_watch').fetchone())
    admitted = json.loads(path.read_text())

    def child(argv, **kwargs):
        assert argv == ['gh', 'api', SECRET, '--method', 'PUT', '--input', '-']
        path.write_text('{"value":"changed after admission"}')
        assert json.loads(kwargs['input']) == admitted
        return subprocess.CompletedProcess(argv, 0)

    with patch('caller_pr_gates.cli.github_run', side_effect=child) as run:
        gateway(args, store)
    run.assert_called_once()
    assert store.prs('s') == []
    with store._connect() as db:
        assert tuple(db.execute('SELECT state,owner_session,event_id FROM pr_watch').fetchone()) == before


def test_closed_unmerged_work_cannot_authorize_configuration(config):
    store, args, _ = config
    retire(store, UPSTREAM, 11, {'state': 'closed', 'merged': False,
        'head': {'sha': 'a' * 40, 'ref': 'topic'}})
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


@pytest.mark.parametrize('change', ['missing', 'empty-approval', 'other-pr', 'path-escape', 'extra-key'])
def test_only_an_explicit_exact_immutable_configuration_grant_authorizes_the_write(config, change):
    store, args, _ = config
    scope = store.get_scope('s')
    grant = scope['google_actions_configuration']
    if change == 'missing': del scope['google_actions_configuration']
    elif change == 'empty-approval': grant['approval_evidence'] = ''
    elif change == 'other-pr': grant['authorizing_pr'] = f'https://github.com/{UPSTREAM}/pull/12'
    elif change == 'path-escape': grant['repository'] = '../deployment'
    else: grant['extra-key'] = 'OTHER_SECRET'
    store.record_scope('s', scope, work_item='negative-case')
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


def test_an_approved_configuration_receipt_cannot_be_retargeted(config):
    store, _, _ = config
    scope = store.get_scope('s')
    scope['google_actions_configuration']['repository'] = 'other/deployment'
    with pytest.raises(ValueError, match='immutable'):
        store.record_scope('s', scope, work_item='google-configuration')


@pytest.mark.parametrize('outcome', ['timeout', 'failed'])
def test_uncertain_write_keeps_an_incomplete_receipt_without_exposing_child_output(config, outcome, capsys):
    store, args, _ = config
    result = subprocess.CompletedProcess([], 1, 'private-output', 'private-output')
    with patch('caller_pr_gates.cli.github_run',
               side_effect=subprocess.TimeoutExpired('gh', 90) if outcome == 'timeout' else None,
               return_value=result):
        with pytest.raises(RuntimeError, match='read back fork metadata before retrying'):
            gateway(args, store)
    assert 'private-output' not in capsys.readouterr().out
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='github_google_configuration_incomplete'").fetchone()[0] == 1


@pytest.mark.parametrize('method,suffix', [('POST', ''), ('PATCH', '/POISE_GOOGLE_CLIENT_ID')])
def test_only_the_google_client_id_variable_can_be_created_or_updated(config, method, suffix):
    store, args, path = config
    path.write_text(json.dumps({'name': 'POISE_GOOGLE_CLIENT_ID', 'value': CLIENT}))
    args.arguments[1], args.arguments[3] = VARIABLE + suffix, method
    with patch('caller_pr_gates.cli.github_run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(args, store)
    assert json.loads(run.call_args.kwargs['input']) == json.loads(path.read_text())
    assert run.call_args.args[0][-2:] == ['--input', '-']


@pytest.mark.parametrize('payload', [
    {'name': 'OTHER', 'value': CLIENT},
    {'name': 'POISE_GOOGLE_CLIENT_ID', 'value': 'plaintext-secret'},
    {'name': 'POISE_GOOGLE_CLIENT_ID', 'value': 123},
    {'name': 'POISE_GOOGLE_CLIENT_ID', 'value': CLIENT, 'extra': 'unexpected'},
])
def test_variable_payload_cannot_change_other_keys_or_store_a_secret(config, payload):
    store, args, path = config
    args.arguments[1], args.arguments[3] = VARIABLE, 'POST'
    path.write_text(json.dumps(payload))
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


@pytest.mark.parametrize('endpoint,method', [
    (SECRET.replace(FORK, 'other/deployment'), 'PUT'),
    (SECRET.replace('POISE_GOOGLE_CLIENT_SECRET', 'OTHER_SECRET'), 'PUT'),
    (PREFIX + 'permissions/workflow', 'PUT'),
    (SECRET, 'POST'), (SECRET, 'GET'), (SECRET, 'HEAD'), (SECRET, 'DELETE'),
    (VARIABLE, 'PATCH'), (VARIABLE + '/POISE_GOOGLE_CLIENT_ID', 'POST'),
])
def test_wrong_repository_key_settings_or_method_remain_denied(config, endpoint, method):
    store, args, _ = config
    args.arguments[1], args.arguments[3] = endpoint, method
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


@pytest.mark.parametrize('payload', [
    {'value': 'plaintext-secret'},
    {'key_id': 'key', 'encrypted_value': 'GOCSPX-plaintext-secret'},
    {'key_id': 'key', 'encrypted_value': 'c2hvcnQ='},
    {'key_id': '', 'encrypted_value': base64.b64encode(b'x' * 64).decode()},
    {'key_id': 7, 'encrypted_value': base64.b64encode(b'x' * 64).decode()},
    {'key_id': 'key', 'encrypted_value': base64.b64encode(b'x' * 64).decode(), 'value': 'plaintext'},
    [],
])
def test_plaintext_malformed_and_extra_secret_fields_are_denied(config, payload):
    store, args, path = config
    path.write_text(json.dumps(payload))
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


@pytest.mark.parametrize('target', [None, 'https://github.com/maintainer/source/pull/12',
    'https://github.com/operator/deployment/pull/11', 'https://github.com/maintainer/source/issues/11'])
def test_another_target_or_unowned_pr_cannot_authorize_configuration(config, target):
    store, args, _ = config
    args.target = target
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError):
            gateway(args, store)


@pytest.mark.parametrize('extra', [['-f', 'value=plaintext'], ['--header', 'Authorization: other'],
    ['--hostname', 'other.invalid'], ['--method', 'DELETE'], ['--paginate']])
def test_flags_cannot_extend_or_override_the_admitted_operation(config, extra):
    store, args, _ = config
    args.arguments += extra
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


def test_a_different_session_cannot_use_the_recorded_owners_pr(config):
    store, args, _ = config
    args.session = 'unrelated'
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


@pytest.mark.parametrize('mismatch', ['client', 'checkout'])
def test_client_and_checkout_must_match_before_the_configuration_write(config, mismatch):
    store, args, path = config
    if mismatch == 'client':
        args.client = 'claude'
    else:
        other = path.parent / 'other-checkout'
        other.mkdir()
        args.cwd = str(other)
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


def test_stdin_cannot_be_admitted_through_a_decoy_file(config):
    store, args, path = config
    (path.parent / '-').write_text(path.read_text())
    args.arguments[-1] = '-'
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(args, store)


def test_relative_payload_is_read_from_the_owned_checkout(config, tmp_path, monkeypatch):
    store, args, path = config
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    args.arguments[-1] = path.name
    with patch('caller_pr_gates.cli.github_run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(args, store)
    assert json.loads(run.call_args.kwargs['input']) == json.loads(path.read_text())
