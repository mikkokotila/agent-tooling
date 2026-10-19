import json
import subprocess
from unittest.mock import patch

import pytest
from caller_pr_gates.cli import gateway
from test_cli import args, store
from test_policy import snapshot

REPO = 'Vaquum/Limen'
ENDPOINT = f'repos/{REPO}/actions/workflows/pr_checks_slice_sweep.yml/dispatches'


@pytest.fixture
def sweep_case(store, tmp_path):
    store.register('s', 'codex', REPO, 1, str(tmp_path))
    path = tmp_path / 'request.json'
    path.write_text(json.dumps({'ref': 'main'}))
    value = args(['api', ENDPOINT, '--method', 'POST', '--input', str(path)])
    value.cwd = str(tmp_path)
    value.target = f'https://github.com/{REPO}/pull/1'
    snap = snapshot()
    async def validation(*a): return snap
    return value, snap, path, validation


def test_owned_slice_sweep_binds_admitted_main_ref(store, sweep_case):
    value, _, path, _ = sweep_case
    async def validation(*a):
        path.write_text(json.dumps({'ref': 'unreviewed', 'inputs': {'anything': True}}))
        return snapshot()
    with patch('caller_pr_gates.github.collect_validation', side_effect=validation), patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(value, store)
    run.assert_called_once()
    assert run.call_args.args[0] == ['gh', 'api', ENDPOINT, '--method', 'POST', '--input', '-']
    assert json.loads(run.call_args.kwargs['input']) == {'ref': 'main'}


@pytest.mark.parametrize('change', ['unowned', 'other_target', 'issue', 'closed', 'merged', 'unknown_merged', 'head_race', 'behind', 'target_race', 'other_branch', 'other_repo', 'other_workflow', 'bad_ref', 'extra_inputs', 'extra_fields', 'stdin', 'wrong_method'])
def test_slice_sweep_cannot_escape_its_exact_recovery(store, sweep_case, change):
    value, snap, path, validation = sweep_case
    if change == 'unowned': value.session = 'other'
    elif change == 'other_target': value.target = f'https://github.com/{REPO}/pull/99'
    elif change == 'issue': value.target = f'https://github.com/{REPO}/issues/1'
    elif change == 'closed': snap['state'] = 'closed'
    elif change == 'merged': snap['merged'] = True
    elif change == 'unknown_merged': snap.pop('merged')
    elif change == 'head_race': snap['head_sha_after'] = 'b' * 40
    elif change == 'behind': snap['branch_comparison']['merge_base_sha'] = 'd' * 40
    elif change == 'target_race': snap['base_sha_after'] = 'b' * 40
    elif change == 'other_branch': snap['base_ref'] = 'release'
    elif change == 'other_repo': value.arguments[1] = ENDPOINT.replace(REPO, 'other/repo')
    elif change == 'other_workflow': value.arguments[1] = ENDPOINT.replace('pr_checks_slice_sweep.yml', 'deploy.yml')
    elif change == 'bad_ref': path.write_text(json.dumps({'ref': 'feature'}))
    elif change == 'extra_inputs': path.write_text(json.dumps({'ref': 'main', 'inputs': {'x': 1}}))
    elif change == 'extra_fields': value.arguments += ['-f', 'inputs[x]=1']
    elif change == 'stdin': value.arguments[-1] = '-'
    elif change == 'wrong_method': value.arguments[3] = 'PUT'
    with patch('caller_pr_gates.github.collect_validation', side_effect=validation), patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError): gateway(value, store)


def test_relative_payload_is_read_from_the_owned_checkout(store, sweep_case, tmp_path, monkeypatch):
    value, _, path, validation = sweep_case
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / path.name).write_text(json.dumps({'ref': 'feature'}))
    monkeypatch.chdir(elsewhere)
    value.arguments[-1] = path.name
    with patch('caller_pr_gates.github.collect_validation', side_effect=validation), patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(value, store)
    assert json.loads(run.call_args.kwargs['input']) == {'ref': 'main'}


def test_foreign_registration_receipt_does_not_authorize_the_sweep(store, sweep_case):
    value, _, _, _ = sweep_case
    store.register('other', 'claude', REPO, 1, value.cwd)
    value.session = 'other'
    assert any(row['repository'] == REPO for row in store.prs('other'))
    with patch('caller_pr_gates.github.collect_validation', side_effect=AssertionError('foreign receipt must fail before network')), patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError): gateway(value, store)


@pytest.mark.parametrize('change', ['owner', 'state', 'checkout'])
def test_sweep_rechecks_current_owner_after_remote_validation(store, sweep_case, tmp_path, change):
    value, snap, _, _ = sweep_case
    async def validation(*a):
        with store._connect() as db:
            if change == 'owner': db.execute("UPDATE pr_watch SET owner_session='other' WHERE repository=? AND number=1", (REPO,))
            elif change == 'state': db.execute("UPDATE pr_watch SET state='closed' WHERE repository=? AND number=1", (REPO,))
            else: db.execute("UPDATE pr_watch SET cwd=? WHERE repository=? AND number=1", (str(tmp_path / 'other'), REPO))
        return snap
    with patch('caller_pr_gates.github.collect_validation', side_effect=validation), patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='owner or checkout changed'): gateway(value, store)
