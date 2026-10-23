"""A native worker pin requires new execution, never a disguised retry or pass."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from caller_pr_gates import monitor, progress, workflow
from caller_pr_gates.policy import evaluate_pr
from caller_pr_gates.store import Store
from test_policy import fresh_branch, closing_link
from test_workflow import git, local


REPO = 'autonomio/poise'


@pytest.fixture
def native(local, tmp_path, monkeypatch):
    _, root, snap, plan = local
    store = Store(tmp_path / 'native-state')
    store.record_session('s', 'codex', str(root))
    git(root, 'remote', 'set-url', 'origin', f'https://github.com/{REPO}.git')
    store.register('s', 'codex', REPO, 1, str(root))
    with (root / '.gitignore').open('a') as stream:
        stream.write('.inputs\n.executions\n')
    (root / '.inputs').write_text('original dependency')
    (root / 'gate.py').write_text(
        "from pathlib import Path\nimport os\n"
        "target = Path('.executions')\n"
        "target.write_text((target.read_text() if target.exists() else '') + 'full check\\n')\n"
        "assert Path('.inputs').read_text() == 'original dependency'\n"
        "assert os.environ.get('VITEST_MAX_WORKERS') == '1'\n")
    (root / 'package.json').write_text(json.dumps({'scripts': {'check': [sys.executable, 'gate.py']}}))
    # An executable fixture runs the full published package script as a real
    # child; no npm installation or fake passing result is needed by the test.
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    npm = bin_dir / 'npm'
    npm.write_text('#!' + sys.executable + '\nimport json,subprocess,sys\n'
                   "assert sys.argv[1:] == ['run', 'check']\n"
                   "sys.exit(subprocess.call(json.load(open('package.json'))['scripts']['check']))\n")
    npm.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
    monkeypatch.delenv('VITEST_MAX_WORKERS', raising=False)
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Published full gate')
    head = git(root, 'rev-parse', 'HEAD')
    snap.update(repository=REPO, head_sha=head, head_sha_after=head, commit_shas=[head], collected_at=time.time(),
                **fresh_branch(head, base=head), **closing_link(head, REPO, 11))
    store.set_snapshot(REPO, 1, snap, evaluate_pr(snap, []))
    plan.clear()
    plan.update(checks=[dict(id='app-check', argv=['npm', 'run', 'check'], reason='Full application gate')],
                input_paths=['.inputs'], env_names=['PATH'])
    old = workflow.save_plan(store, 's', REPO, 1, root, plan)['plan']
    return store, root, snap, plan, old


def retire(native):
    store, root, snap, _, _ = native
    snap.update(state='closed', merged=True, collected_at=time.time())
    store.set_snapshot(REPO, 1, snap, evaluate_pr(snap, []))
    monitor.retire(store, REPO, 1, dict(state='closed', merged=True, head={'sha': snap['head_sha']}), checklist=True)
    proof = dict(snap, profile='validation', merge_commit_sha='b' * 40,
                 base_ref_after=snap['base_ref'], head_repository=REPO, base_repository=REPO)
    store.set_preflight(REPO, 1, proof)


def execute(native, *, merged=False):
    store, root, snap, _, _ = native
    return workflow.run_check(store, 's', REPO, 1, root, 'app-check',
                              **({'merged_head': snap['head_sha']} if merged else {}))


def bind(native, payload=None, **kwargs):
    store, root, snap, plan, _ = native
    return workflow.save_plan(store, 's', REPO, 1, root,
                              payload or {**plan, 'env_names': ['PATH', 'VITEST_MAX_WORKERS']},
                              merged_head=snap['head_sha'], **kwargs)


@pytest.mark.parametrize('unknown', [False, True])
def test_native_pin_keeps_actual_result_and_requires_new_full_execution(native, monkeypatch, unknown):
    store, root, snap, plan, old = native
    if unknown:
        original = workflow._run_process
        def interrupted(argv, **kwargs):
            original(argv, **kwargs)
            raise subprocess.TimeoutExpired(argv, 1)
        with monkeypatch.context() as patch:
            patch.setattr(workflow, '_run_process', interrupted)
            with pytest.raises(subprocess.TimeoutExpired):
                execute(native)
    else:
        assert execute(native)['run']['status'] == 'fail'
    history = store.validation_runs(old['plan_id'])
    assert history[0]['status'] == ('unknown' if unknown else 'fail')
    retire(native)
    monkeypatch.setenv('VITEST_MAX_WORKERS', '1')
    saved = bind(native)
    new = saved['plan']
    assert new['plan_id'] != old['plan_id'] and new['input_digest'] != old['input_digest']
    assert saved['progress']['passed'] == [] and saved['progress']['pending'] == ['app-check']
    assert store.validation_evidence(new['plan_id']) == []
    receipt = new['plan']['native_worker_epoch']
    assert receipt == dict(previous_plan_id=old['plan_id'], previous_input_digest=old['input_digest'],
        previous_run_id=history[0]['run_id'], head_sha=snap['head_sha'], check_id='app-check',
        env_name='VITEST_MAX_WORKERS', value='1')
    assert store.validation_runs(old['plan_id']) == history
    assert bind(native)['plan'] == new
    result = execute(native, merged=True)
    assert result['run']['status'] == 'pass' and result['run']['exit_code'] == 0
    assert result['run']['before_digest'] == result['run']['after_digest'] == new['input_digest']
    assert result['progress']['local_checks_passed'] is True
    assert result['progress']['completion_verified'] is False
    assert (root / '.executions').read_text() == 'full check\nfull check\n'
    assert store.validation_runs(old['plan_id']) == history
    with pytest.raises(ValueError):
        execute(native, merged=True)
    with store._connect() as db:
        event = json.loads(db.execute("SELECT fields FROM audit WHERE event='validation_native_worker_epoch'").fetchone()[0])
    assert event['historical_results_changed'] is False and event['previous_run_id'] == history[0]['run_id']
    assert store.prs('s') == []


@pytest.mark.parametrize('fault', ['zero', 'negative', 'unset', 'options', 'path', 'source', 'scripts', 'private',
                                 'argv', 'reason', 'remove-input', 'rename', 'other-env', 'forged-receipt'])
def test_native_pin_refuses_other_changes(native, monkeypatch, fault):
    store, root, _, plan, old = native
    execute(native)
    retire(native)
    history = store.validation_runs(old['plan_id'])
    monkeypatch.setenv('VITEST_MAX_WORKERS', '1')
    payload = {**deepcopy(plan), 'env_names': ['PATH', 'VITEST_MAX_WORKERS']}
    if fault in {'zero', 'negative', 'options'}:
        monkeypatch.setenv('VITEST_MAX_WORKERS', {'zero': '0', 'negative': '-1', 'options': '1 --retry=3'}[fault])
    elif fault == 'unset':
        monkeypatch.delenv('VITEST_MAX_WORKERS')
    elif fault == 'path':
        monkeypatch.setenv('PATH', os.environ['PATH'] + os.pathsep + '/different')
    elif fault in {'source', 'scripts', 'private'}:
        name = {'source': 'src.txt', 'scripts': 'package.json', 'private': '.inputs'}[fault]
        (root / name).write_text('changed')
    elif fault == 'argv':
        payload['checks'][0]['argv'] += ['--', '--retry=3']
    elif fault == 'reason':
        payload['checks'][0]['reason'] = 'Replace the obligation'
    elif fault == 'remove-input':
        payload['input_paths'] = []
    elif fault == 'rename':
        payload['checks'][0]['id'] = 'new-check'
    elif fault == 'other-env':
        payload['env_names'].append('SKIP_TESTS')
    else:
        payload['native_worker_epoch'] = {'value': '1'}
    with pytest.raises(ValueError):
        bind(native, payload)
    assert store.validation_plan('s', REPO, 1, root) == old
    assert store.validation_runs(old['plan_id']) == history


def test_native_pin_is_frozen_after_its_real_failure(native, monkeypatch):
    store, root, _, _, old = native
    execute(native)
    retire(native)
    monkeypatch.setenv('VITEST_MAX_WORKERS', '2')
    saved = bind(native)['plan']
    assert execute(native, merged=True)['run']['status'] == 'fail'
    history = store.validation_runs(saved['plan_id'])
    assert bind(native)['plan'] == saved
    monkeypatch.setenv('VITEST_MAX_WORKERS', '1')
    with pytest.raises(ValueError, match='Post-merge validation inputs changed'):
        bind(native)
    with pytest.raises(ValueError, match='Post-merge validation inputs changed'):
        execute(native, merged=True)
    assert store.validation_runs(saved['plan_id']) == history
    assert store.validation_runs(old['plan_id'])[0]['status'] == 'fail'


def test_native_pin_requires_an_actual_unsuccessful_result(native, monkeypatch):
    store, root, _, _, old = native
    retire(native)
    monkeypatch.setenv('VITEST_MAX_WORKERS', '1')
    with pytest.raises(ValueError, match='actual failed or unverifiable'):
        bind(native)
    assert store.validation_runs(old['plan_id']) == []
    assert store.validation_plan('s', REPO, 1, root) == old


def test_native_pin_refuses_hidden_tracked_script_changes(native, monkeypatch):
    store, root, _, _, old = native
    execute(native)
    retire(native)
    git(root, 'update-index', '--assume-unchanged', 'package.json')
    (root / 'package.json').write_text('{"scripts":{"check":["true"]}}')
    assert progress.working_state(root)['clean'] is True
    monkeypatch.setenv('VITEST_MAX_WORKERS', '1')
    with pytest.raises(ValueError, match='original source'):
        bind(native)
    assert store.validation_plan('s', REPO, 1, root) == old
