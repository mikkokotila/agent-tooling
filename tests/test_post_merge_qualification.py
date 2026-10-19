"""Retirement must not revoke retained qualification or manufacture acceptance."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from caller_pr_gates import cli, github, monitor, progress, workflow
from caller_pr_gates.policy import evaluate_pr
from test_github import Client
from test_workflow import local, git, publish


def prepare(local, *, live=False, failing=False, inputs=False):
    store, root, snap, plan = local
    (root / 'src.txt').write_text('old' if failing else 'fixed')
    (root / 'remaining.py').write_text("from pathlib import Path\nassert Path('src.txt').read_text() == 'fixed'\n")
    (root / 'diagnose.py').write_text("from pathlib import Path\nassert Path('src.txt').read_text() == 'old'\n")
    if live:
        (root / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n.started\n.release\n')
        (root / 'check.py').write_text(
            "from pathlib import Path\nimport time\nPath('.started').touch()\n"
            "deadline = time.monotonic() + 10\n"
            "while not Path('.release').exists() and time.monotonic() < deadline: time.sleep(.02)\n"
            "assert Path('.release').exists()\nassert Path('src.txt').read_text() == 'fixed'\n")
    if inputs:
        with (root / '.gitignore').open('a') as target:
            target.write('.inputs\n')
        (root / '.inputs').write_text('original input')
        plan['input_paths'] = ['.inputs']
    plan['checks'][0]['id'] = 'native-live-crash'
    plan['checks'].append(dict(id='remaining', argv=[sys.executable, 'remaining.py'],
                               reason='Finish the already-declared local qualification'))
    publish(local)
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    return row


def merge(local):
    store, root, snap, _ = local
    snap.update(state='closed', merged=True, collected_at=time.time())
    store.set_snapshot('Owner/Repo', 1, snap, evaluate_pr(snap, []))
    monitor.retire(store, 'Owner/Repo', 1, dict(state='closed', merged=True,
                   head={'sha': snap['head_sha']}), checklist=True)
    proof = dict(snap, profile='validation', merge_commit_sha='b' * 40,
                 head_repository='Owner/Repo', base_repository='Owner/Repo')
    store.set_preflight('Owner/Repo', 1, proof)
    return proof


def run(local, check='remaining', *, sid='s', cwd=None, head=None):
    store, root, snap, _ = local
    return workflow.run_check(store, sid, 'Owner/Repo', 1, cwd or root, check,
                              merged_head=head or snap['head_sha'])


def test_merge_during_real_process_preserves_exit_and_remaining_owner(local):
    store, root, snap, plan = local
    row = prepare(local, live=True)
    outcomes, errors = [], []
    def active_check():
        try:
            outcomes.append(workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'native-live-crash'))
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=active_check)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not (root / '.started').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (root / '.started').exists()
        before = store.validation_runs(row['plan_id'])[0]
        assert before['status'] == 'running' and before['exit_code'] is None
        merge(local)
        assert store.prs('s') == []
        assert store.validation_runs(row['plan_id'])[0] == before
        for call in (lambda: workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'remaining'),
                     lambda: workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)):
            with pytest.raises(ValueError, match='PR is not registered to this task'):
                call()
        assert workflow.task_progress(store, 's')[0]['status'] == 'WAIT'
        with pytest.raises(ValueError, match='active run'):
            run(local)
    finally:
        (root / '.release').touch()
        worker.join(12)
    assert not worker.is_alive() and not errors
    assert outcomes[0]['run']['status'] == 'pass'
    assert outcomes[0]['run']['exit_code'] == 0
    assert outcomes[0]['run']['before_digest'] == outcomes[0]['run']['after_digest'] == row['input_digest']
    assert workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan,
                              merged_head=snap['head_sha'])['plan']['plan_id'] == row['plan_id']
    result = run(local)
    assert result['run']['status'] == 'pass' and result['run']['exit_code'] == 0
    assert result['progress']['local_checks_passed'] is True
    assert result['progress']['completion_verified'] is False
    assert result['progress']['phase'] != 'COMPLETE'
    assert len(store.validation_runs(row['plan_id'])) == 2
    assert workflow.task_progress(store, 's')[0]['status'] == 'WAIT'
    with store._connect() as db:
        watch = dict(db.execute('SELECT * FROM pr_watch').fetchone())
    assert watch['state'] == 'merged' and watch['owner_session'] == 's' and watch['cwd'] == str(root)
    assert watch['event_id'] is None and watch['delivery_state'] is None
    assert next(g for g in evaluate_pr(snap, []) if g['gate'] == 'MERGE_READY')['status'] == 'WAIT'


@pytest.mark.parametrize('sid', ['foreign', 'child'])
def test_retained_mode_refuses_other_chat_even_with_legacy_registration(local, sid):
    store, root, snap, plan = local
    prepare(local)
    store.register('foreign', 'codex', 'Owner/Repo', 1, str(root))
    workflow.save_plan(store, 'foreign', 'Owner/Repo', 1, root, plan)
    store.record_session('child', 'codex', str(root), parent_session='s')
    merge(local)
    with pytest.raises(ValueError, match='exact recorded owner'):
        run(local, sid=sid)
    with pytest.raises(ValueError, match='exact recorded owner'):
        workflow.save_plan(store, sid, 'Owner/Repo', 1, root, plan, merged_head=snap['head_sha'])
    assert all(not store.validation_runs(p['plan_id']) for p in store.validation_plans('s'))


def test_retained_mode_refuses_other_checkout_or_missing_plan(local, tmp_path):
    store, root, snap, plan = local
    merge(local)
    with pytest.raises(ValueError, match='retained plan'):
        workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan, merged_head=snap['head_sha'])
    assert store.validation_plans('s') == []


def test_retained_mode_refuses_clone_and_subdirectory(local, tmp_path):
    _, root, _, _ = local
    prepare(local)
    merge(local)
    clone = tmp_path / 'clone'
    subprocess.run(['git', 'clone', '--quiet', str(root), str(clone)], check=True)
    child = root / 'subdir'
    child.mkdir()
    for cwd in (clone, child):
        with pytest.raises(ValueError, match='exact recorded owner'):
            run(local, cwd=cwd)


@pytest.mark.parametrize('change', ['dirty', 'commit', 'pin', 'origin', 'inputs', 'environment'])
def test_altered_source_or_inputs_cannot_be_admitted(local, monkeypatch, change):
    store, root, snap, plan = local
    if change == 'environment':
        plan['env_names'] = ['QUALIFICATION_FIXTURE_INPUT']
        monkeypatch.setenv('QUALIFICATION_FIXTURE_INPUT', 'before')
    row = prepare(local, inputs=True)
    merge(local)
    head = snap['head_sha']
    if change == 'dirty':
        (root / 'src.txt').write_text('altered')
    elif change == 'commit':
        git(root, 'commit', '--allow-empty', '-qm', 'Different unpublished commit')
        head = git(root, 'rev-parse', 'HEAD')
    elif change == 'pin':
        head = 'c' * 40
    elif change == 'origin':
        git(root, 'remote', 'set-url', 'origin', 'https://github.com/Other/Repo.git')
    elif change == 'inputs':
        (root / '.inputs').write_text('altered input')
    else:
        monkeypatch.setenv('QUALIFICATION_FIXTURE_INPUT', 'after')
    with pytest.raises(ValueError, match='Post-merge validation'):
        run(local, head=head)
    assert store.validation_runs(row['plan_id']) == []


@pytest.mark.parametrize('change', [
    {'state': 'open', 'merged': False}, {'merged': False}, {'head_sha_after': 'c' * 40},
    {'head_repository': 'Other/Repo'}, {'base_repository': 'Other/Repo'},
    {'merge_commit_sha': None}, {'collected_at': 0}, {'collected_at': None},
])
def test_retained_mode_requires_fresh_exact_merged_proof(local, change):
    store, _, _, _ = local
    row = prepare(local)
    proof = merge(local)
    store.set_preflight('Owner/Repo', 1, {**proof, **change})
    with pytest.raises(ValueError, match='fresh stable proof'):
        run(local)
    assert store.validation_runs(row['plan_id']) == []


def test_post_merge_failure_keeps_diagnosis_and_frozen_plan_rules(local):
    store, root, snap, plan = local
    row = prepare(local, failing=True)
    merge(local)
    result = run(local, 'native-live-crash')
    failed = result['run']
    assert failed['status'] == 'fail' and failed['exit_code'] != 0
    with pytest.raises(ValueError, match='already completed'):
        run(local, 'native-live-crash')
    removed = deepcopy(plan)
    removed['checks'] = removed['checks'][1:]
    with pytest.raises(ValueError, match='cannot remove'):
        workflow.save_plan(store, 's', 'Owner/Repo', 1, root, removed, merged_head=snap['head_sha'])
    changed = deepcopy(plan)
    changed['checks'][0]['argv'] = [sys.executable, 'diagnose.py']
    with pytest.raises(ValueError, match='immutable'):
        workflow.save_plan(store, 's', 'Owner/Repo', 1, root, changed, merged_head=snap['head_sha'])
    diagnosis = deepcopy(plan)
    diagnosis['checks'].append(dict(id='diagnosis', argv=[sys.executable, 'diagnose.py'],
                                   reason='Verify the observed failing source condition without rerunning the failed command'))
    assert workflow.save_plan(store, 's', 'Owner/Repo', 1, root, diagnosis,
                              merged_head=snap['head_sha'])['plan']['plan_id'] == row['plan_id']
    assert run(local, 'diagnosis')['run']['status'] == 'pass'
    assert store.validation_runs(row['plan_id'])[0] == failed
    assert workflow.current_phase(store, row)['phase'] == 'VALIDATE_FAILED'


@pytest.mark.parametrize('mutation', ['inputs', 'head'])
def test_post_merge_real_exit_preserves_fingerprints_when_source_changes(local, mutation):
    store, root, _, plan = local
    prepare(local, inputs=True)
    # Publish the actual mutating check before its original plan is bound.
    if mutation == 'inputs':
        script = "from pathlib import Path\nPath('.inputs').write_text('changed during check')\n"
    else:
        script = "import subprocess\nsubprocess.run(['git', 'commit', '--allow-empty', '-qm', 'Changed head'], check=True)\n"
    (root / 'remaining.py').write_text(script)
    publish(local)
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    merge(local)
    result = run(local)['run']
    assert result['status'] == 'unknown' and result['exit_code'] == 0
    assert result['before_digest'] == row['input_digest']
    assert result['after_digest'] == progress.fingerprint(root, row['plan'])
    assert (result['before_digest'] == result['after_digest']) is (mutation == 'head')


@pytest.mark.parametrize('bad_worker_proof', [False, True])
def test_background_cli_carries_pin_and_worker_rechecks_remote(local, monkeypatch, bad_worker_proof):
    store, root, snap, _ = local
    row = prepare(local)
    proof = merge(local)
    if bad_worker_proof:
        proof['merged'] = False
    original = subprocess.Popen
    workers = []
    bootstrap = ('import json, time\nfrom caller_pr_gates import github, cli\n'
                 f'remote = json.loads({json.dumps(proof)!r})\n'
                 'async def collect(*args):\n    return dict(remote, collected_at=time.time())\n'
                 'github.collect_validation = collect\ncli.main()\n')
    def start(argv, **kwargs):
        is_worker = argv[1:4] == ['-P', '-m', 'caller_pr_gates.cli']
        if is_worker:
            assert argv[-2:] == ['--merged-head', snap['head_sha']]
            argv = [argv[0], '-c', bootstrap, *argv[4:]]
        child = original(argv, **kwargs)
        if is_worker:
            workers.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', start)
    started = workflow.launch_check(store, 's', 'Owner/Repo', 1, root, 'remaining', merged_head=snap['head_sha'])
    assert started['completion_verified'] is False
    code = workers[0].wait(timeout=10)
    runs = store.validation_runs(row['plan_id'])
    if bad_worker_proof:
        assert code != 0 and runs == []
        assert 'fresh stable proof' in Path(started['log']).read_text()
    else:
        assert code == 0 and len(runs) == 1 and runs[0]['status'] == 'pass'
        assert runs[0]['exit_code'] == 0 and runs[0]['before_digest'] == runs[0]['after_digest']


def test_plan_cli_continues_existing_registration_without_rearming(local, monkeypatch, capsys, tmp_path):
    store, root, snap, plan = local
    row = prepare(local)
    proof = merge(local)
    payload = tmp_path / 'plan.json'
    payload.write_text(json.dumps(plan))
    monkeypatch.setenv('CALLER_PR_GATES_STATE_DIR', str(store.state_dir))
    async def collect(*args):
        return proof
    monkeypatch.setattr(github, 'collect_validation', collect)
    monkeypatch.setattr(sys, 'argv', ['gate', 'validation-plan', '--session', 's', '--pr',
        'https://github.com/Owner/Repo/pull/1', '--cwd', str(root), '--file', str(payload),
        '--merged-head', snap['head_sha']])
    cli.main()
    assert json.loads(capsys.readouterr().out)['plan']['plan_id'] == row['plan_id']
    assert store.prs('s') == []


@pytest.mark.parametrize('race', [False, True])
def test_merged_preflight_binds_original_head_repositories_and_merge_commit(race):
    class MergedClient(Client):
        async def get(self, path, params=None):
            value = await super().get(path, params)
            if path.endswith('/pulls/50'):
                value['head']['repo'] = {'full_name': 'mikkokotila/caller'}
                value['base']['repo'] = {'full_name': 'mikkokotila/caller'}
                value['merge_commit_sha'] = ('b' if race and self.reads > 1 else 'c') * 40
            return value
    client = MergedClient()
    if race:
        with pytest.raises(github.EvidenceError, match='Merged source identity changed'):
            asyncio.run(github.collect_validation_with_client(client, 'mikkokotila/caller', 50))
    else:
        proof = asyncio.run(github.collect_validation_with_client(client, 'mikkokotila/caller', 50))
        assert proof['head_repository'] == proof['base_repository'] == 'mikkokotila/caller'
        assert proof['merge_commit_sha'] == 'c' * 40 and len(client.paths) == 5


def test_closed_or_missing_checkout_remains_incomplete_without_monitor_revival(local):
    store, root, snap, _ = local
    prepare(local)
    merge(local)
    root.rename(root.with_name('gone'))
    item = workflow.task_progress(store, 's')[0]
    assert item['status'] == 'UNKNOWN' and item['evidence']['phase'] == 'UNKNOWN'
    assert store.prs('s') == []
    snap['merged'] = False
    assert next(g for g in evaluate_pr(snap, []) if g['gate'] == 'MERGE_READY')['status'] == 'WAIT'
