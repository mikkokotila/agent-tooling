"""New private controls must not reset retained merged-source qualification."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from caller_pr_gates import cli, github, progress, workflow
from test_post_merge_qualification import prepare, merge, run
from test_workflow import local, git


def retained(local, **kwargs):
    _, root, _, _ = local
    with (root / '.git/info/exclude').open('a') as target:
        target.write('.qualification/\n')
    row = prepare(local, inputs=True, **kwargs)
    proof = merge(local)
    return row, proof


def add_control(local, identifier='retry-cancel', *, script=None, value='05Z..06Z', env=False):
    _, root, _, plan = local
    directory = root / '.qualification'
    directory.mkdir(exist_ok=True)
    driver, case = f'.qualification/{identifier}.py', f'.qualification/{identifier}.txt'
    (root / case).write_text(value)
    body = ("from pathlib import Path\n"
            "assert Path('.inputs').read_text() == 'original input'\n"
            f"assert Path({case!r}).read_text() == '05Z..06Z'\n")
    (root / driver).write_text(script or body)
    check = dict(id=identifier, argv=[sys.executable, driver],
                 reason='Qualify the newly pinned range control against the retained producer',
                 input_paths=[driver, case])
    if env:
        check['env_names'] = ['QUALIFICATION_CONTROL_MODE']
    payload = deepcopy(plan)
    payload['checks'].append(check)
    return payload, check


def save(local, payload, **kwargs):
    store, root, snap, _ = local
    return workflow.save_plan(store, 's', 'Owner/Repo', 1, root, payload,
                              merged_head=snap['head_sha'], **kwargs)


@pytest.mark.parametrize('failing', [False, True])
def test_new_control_executes_without_resetting_prior_results(local, failing):
    store, root, snap, _ = local
    original, _ = retained(local, failing=failing)
    first = run(local, 'native-live-crash')['run']
    if not failing:
        run(local, 'remaining')
    history = store.validation_runs(original['plan_id'])
    payload, control = add_control(local)
    # This is the observed 0.1.41 rejection: adding the private driver to the
    # shared input set changes every check's identity despite unchanged source.
    shared = deepcopy(payload)
    shared['input_paths'] += shared['checks'][-1].pop('input_paths')
    with pytest.raises(ValueError, match='Post-merge validation inputs changed'):
        save(local, shared)
    bound = save(local, payload)
    row = bound['plan']
    assert row['plan_id'] == original['plan_id'] and row['input_digest'] == original['input_digest']
    assert store.validation_runs(row['plan_id']) == history
    assert 'retry-cancel' in bound['progress']['pending']
    frozen = row['plan']['checks'][-1]['input_digest']
    assert frozen != row['input_digest']
    assert frozen == progress.check_fingerprint(root, row['plan'], control)
    result = run(local, 'retry-cancel')
    actual = result['run']
    assert actual['status'] == 'pass' and actual['exit_code'] == 0
    assert actual['input_digest'] == actual['before_digest'] == actual['after_digest'] == frozen
    assert store.validation_runs(row['plan_id']) == [*history, actual]
    assert store.validation_evidence(row['plan_id']) == [*history, actual]
    assert 'retry-cancel' in result['progress']['passed']
    if failing:
        assert first['status'] == 'fail' and result['progress']['phase'] == 'VALIDATE_FAILED'
        assert result['progress']['failed'] == ['native-live-crash']
        with pytest.raises(ValueError, match='already completed'):
            run(local, 'native-live-crash')
    else:
        assert result['progress']['local_checks_passed'] is True
        assert result['progress']['completion_verified'] is False
        for check_id in ('native-live-crash', 'remaining', 'retry-cancel'):
            with pytest.raises(ValueError):
                run(local, check_id)
    assert save(local, payload)['plan']['plan_id'] == original['plan_id']
    assert progress.working_state(root) == dict(head=snap['head_sha'], clean=True, root=str(root))
    assert store.prs('s') == [] and workflow.task_progress(store, 's')[0]['status'] == 'WAIT'
    with store._connect() as db:
        watch = db.execute('SELECT * FROM pr_watch').fetchone()
    assert watch['state'] == 'merged' and watch['event_id'] is None and watch['delivery_state'] is None


def test_failed_new_control_stays_failed_when_another_control_is_added(local):
    store, _, _, plan = local
    original, _ = retained(local)
    payload, _ = add_control(local, value='wrong range')
    save(local, payload)
    failed = run(local, 'retry-cancel')['run']
    assert failed['status'] == 'fail' and failed['exit_code'] != 0
    second, _ = add_control(local, 'boundary')
    second['checks'].insert(-1, payload['checks'][-1])
    saved = save(local, second)['plan']
    assert saved['plan_id'] == original['plan_id']
    outcome = run(local, 'boundary')
    assert outcome['run']['status'] == 'pass'
    assert outcome['progress']['failed'] == ['retry-cancel']
    assert store.validation_runs(original['plan_id'])[0] == failed
    with pytest.raises(ValueError, match='already completed'):
        run(local, 'retry-cancel')
    assert plan['checks'][0]['id'] == 'native-live-crash'


@pytest.mark.parametrize('change', ['remove', 'argv', 'reason', 'rename', 'global-inputs', 'control-inputs', 'forged-pin'])
def test_extension_cannot_waive_or_rewrite_existing_obligations(local, change):
    store, _, _, _ = local
    row, _ = retained(local, failing=True)
    failed = run(local, 'native-live-crash')['run']
    payload, _ = add_control(local)
    bound = save(local, payload)['plan']
    changed = deepcopy(payload)
    if change == 'remove':
        changed['checks'].pop(0)
    elif change in {'argv', 'reason'}:
        changed['checks'][0][change] = [sys.executable, 'diagnose.py'] if change == 'argv' else 'Pretend failure was optional'
    elif change == 'rename':
        changed['checks'].append({**changed['checks'][0], 'id': 'renamed'})
    elif change == 'global-inputs':
        changed['input_paths'] = []
    elif change == 'control-inputs':
        changed['checks'][-1]['input_paths'].pop()
    else:
        changed['checks'][-1]['input_digest'] = 'a' * 64
    with pytest.raises(ValueError):
        save(local, changed)
    assert store.validation_plan('s', 'Owner/Repo', 1, row['cwd']) == bound
    assert store.validation_runs(row['plan_id']) == [failed]


@pytest.mark.parametrize('change', ['driver', 'case', 'environment'])
def test_frozen_control_changes_refuse_save_and_execution(local, monkeypatch, change):
    store, root, _, _ = local
    retained(local)
    monkeypatch.setenv('QUALIFICATION_CONTROL_MODE', 'original')
    payload, control = add_control(local, env=True)
    row = save(local, payload)['plan']
    if change == 'driver':
        with (root / control['input_paths'][0]).open('a') as target:
            target.write('# changed control\n')
    elif change == 'case':
        (root / control['input_paths'][1]).write_text('different range')
    else:
        monkeypatch.setenv('QUALIFICATION_CONTROL_MODE', 'changed')
    assert progress.fingerprint(root, row['plan']) == row['input_digest']
    assert workflow.current_phase(store, row)['changed_checks'] == ['retry-cancel']
    with pytest.raises(ValueError, match='immutable'):
        save(local, payload)
    with pytest.raises(ValueError, match='INPUTS_CHANGED'):
        run(local, 'retry-cancel')
    assert store.validation_runs(row['plan_id']) == []


@pytest.mark.parametrize('change', ['dirty', 'commit', 'origin', 'retained-inputs', 'wrong-head', 'foreign', 'child', 'checkout', 'unmerged', 'stale'])
def test_new_control_cannot_escape_original_source_and_owner(local, tmp_path, change):
    store, root, snap, _ = local
    row, proof = retained(local)
    payload, _ = add_control(local)
    sid, cwd, head = 's', root, snap['head_sha']
    if change == 'dirty':
        (root / 'src.txt').write_text('changed producer')
    elif change == 'commit':
        git(root, 'commit', '--allow-empty', '-qm', 'Changed producer identity')
        head = git(root, 'rev-parse', 'HEAD')
    elif change == 'origin':
        git(root, 'remote', 'set-url', 'origin', 'https://github.com/Other/Repo.git')
    elif change == 'retained-inputs':
        (root / '.inputs').write_text('changed retained input')
    elif change == 'wrong-head':
        head = 'c' * 40
    elif change in {'foreign', 'child'}:
        sid = change
        store.record_session(sid, 'codex', str(root), parent_session='s' if change == 'child' else None)
    elif change == 'checkout':
        cwd = tmp_path / 'clone'
        subprocess.run(['git', 'clone', '--quiet', str(root), str(cwd)], check=True)
    else:
        proof.update(merged=False) if change == 'unmerged' else proof.update(collected_at=0)
        store.set_preflight('Owner/Repo', 1, proof)
    with pytest.raises(ValueError, match='Post-merge validation'):
        workflow.save_plan(store, sid, 'Owner/Repo', 1, cwd, payload, merged_head=head)
    assert store.validation_runs(row['plan_id']) == []


@pytest.mark.parametrize('mutation', ['inputs', 'source', 'head'])
def test_actual_control_exit_and_changed_input_fingerprints_are_retained(local, mutation):
    store, root, _, _ = local
    original, _ = retained(local)
    if mutation == 'head':
        script = "import subprocess\nsubprocess.run(['git', 'commit', '--allow-empty', '-qm', 'Moved head'], check=True)\n"
    else:
        target = '.qualification/retry-cancel.txt' if mutation == 'inputs' else 'src.txt'
        script = f"from pathlib import Path\nPath({target!r}).write_text('changed during real process')\n"
    payload, control = add_control(local, script=script)
    row = save(local, payload)['plan']
    result = run(local, 'retry-cancel')['run']
    assert result['status'] == 'unknown' and result['exit_code'] == 0
    assert result['before_digest'] == row['plan']['checks'][-1]['input_digest']
    assert result['after_digest'] == progress.check_fingerprint(root, row['plan'], control)
    assert (result['before_digest'] == result['after_digest']) is (mutation == 'head')
    assert store.validation_runs(original['plan_id']) == [result]


def test_active_process_finishes_naturally_before_control_plan_extension(local):
    store, root, _, _ = local
    row, _ = retained(local, live=True)
    outcomes, errors = [], []
    def execute():
        try:
            outcomes.append(run(local, 'native-live-crash'))
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=execute)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not (root / '.started').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (root / '.started').exists()
        payload, _ = add_control(local)
        with pytest.raises(ValueError, match='active run'):
            save(local, payload)
    finally:
        (root / '.release').touch()
        worker.join(12)
    assert not worker.is_alive() and not errors
    assert outcomes[0]['run']['status'] == 'pass'
    assert save(local, payload)['plan']['plan_id'] == row['plan_id']


@pytest.mark.parametrize('worker_change', ['none', 'proof', 'input'])
def test_background_worker_rechecks_new_control_and_merged_producer(local, monkeypatch, worker_change):
    store, root, snap, _ = local
    _, proof = retained(local)
    payload, control = add_control(local)
    row = save(local, payload)['plan']
    if worker_change == 'proof':
        proof['merged'] = False
    original, workers = subprocess.Popen, []
    bootstrap = ('import json, time\nfrom caller_pr_gates import github, cli\n'
                 f'remote = json.loads({json.dumps(proof)!r})\n'
                 'async def collect(*args):\n    return dict(remote, collected_at=time.time())\n'
                 'github.collect_validation = collect\ncli.main()\n')
    def start(argv, **kwargs):
        is_worker = argv[1:4] == ['-P', '-m', 'caller_pr_gates.cli']
        if is_worker:
            assert argv[-2:] == ['--merged-head', snap['head_sha']]
            if worker_change == 'input':
                (root / control['input_paths'][1]).write_text('changed before worker')
            argv = [argv[0], '-c', bootstrap, *argv[4:]]
        child = original(argv, **kwargs)
        if is_worker:
            workers.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', start)
    started = workflow.launch_check(store, 's', 'Owner/Repo', 1, root, 'retry-cancel', merged_head=snap['head_sha'])
    assert started['completion_verified'] is False
    code = workers[0].wait(timeout=10)
    runs = store.validation_runs(row['plan_id'])
    if worker_change == 'none':
        assert code == 0 and len(runs) == 1 and runs[0]['status'] == 'pass'
        assert runs[0]['exit_code'] == 0
        assert runs[0]['before_digest'] == runs[0]['after_digest'] == row['plan']['checks'][-1]['input_digest']
    else:
        assert code != 0 and runs == []
        expected = 'fresh stable proof' if worker_change == 'proof' else 'Frozen qualification control inputs changed'
        assert expected in Path(started['log']).read_text()


def test_existing_plan_cli_pins_control_without_extra_remote_reads(local, monkeypatch, capsys, tmp_path):
    store, root, snap, _ = local
    original, proof = retained(local)
    payload, _ = add_control(local)
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(payload))
    monkeypatch.setenv('CALLER_PR_GATES_STATE_DIR', str(store.state_dir))
    reads = []
    async def collect(*args):
        reads.append(args)
        return proof
    monkeypatch.setattr(github, 'collect_validation', collect)
    monkeypatch.setattr(sys, 'argv', ['gate', 'validation-plan', '--session', 's', '--pr',
        'https://github.com/Owner/Repo/pull/1', '--cwd', str(root), '--file', str(path),
        '--merged-head', snap['head_sha']])
    cli.main()
    row = json.loads(capsys.readouterr().out)['plan']
    assert row['plan_id'] == original['plan_id'] and row['plan']['checks'][-1]['input_digest']
    assert reads == [('Owner/Repo', 1)] and store.prs('s') == []


def test_open_pr_does_not_gain_retained_control_admission(local):
    store, root, _, _ = local
    with (root / '.gitignore').open('a') as target:
        target.write('.qualification/\n')
    row = prepare(local, inputs=True)
    payload, _ = add_control(local)
    with pytest.raises(ValueError, match='require --merged-head'):
        workflow.save_plan(store, 's', 'Owner/Repo', 1, root, payload)
    assert store.validation_runs(row['plan_id']) == []


@pytest.mark.parametrize('bad_inputs', ['missing-driver', 'missing-input', 'outside', 'metadata'])
def test_control_requires_real_declared_local_inputs(local, bad_inputs):
    store, _, _, _ = local
    row, _ = retained(local)
    payload, _ = add_control(local)
    inputs = payload['checks'][-1]['input_paths']
    if bad_inputs == 'missing-driver':
        inputs.pop(0)
    else:
        inputs.append({'missing-input': '.qualification/missing.txt',
                       'outside': '../outside.txt', 'metadata': '.git/config'}[bad_inputs])
    with pytest.raises(ValueError):
        save(local, payload)
    assert store.validation_runs(row['plan_id']) == []
