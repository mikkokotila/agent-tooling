"""Correct a known-invalid pending checker without running it or reusing a pass."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from caller_pr_gates import cli, github, progress, workflow
from caller_pr_gates.policy import evaluate_pr
from test_checker_corrections import save
from test_post_merge_qualification import merge, run
from test_workflow import local, git, publish


TRACE = 'original measured ingestion\n'


def driver(*, corrected=False, suffix=''):
    # The original opens the ingestion measurement for writing. Never execute
    # that known-invalid program to obtain a failure for correction admission.
    child = ("from pathlib import Path\nimport hashlib, json, os, sys\n"
             "trace = Path(sys.argv[1])\n"
             "with trace.open('w') as stream: stream.write('verifier measurement\\n')\n"
             f"assert Path('.frozen/ingestion.trace').read_text() == {TRACE!r}\n"
             "assert Path('src.txt').read_text() == 'fixed'\n"
             "assert Path('.frozen/dependency.txt').read_text() == 'pinned'\n"
             "print(json.dumps(dict(pid=os.getpid(), trace=str(trace), "
             "ingestion_sha256=hashlib.sha256(Path('.frozen/ingestion.trace').read_bytes()).hexdigest())))\n")
    trace = "f'.results/verifier-{index}.trace'" if corrected else "'.frozen/ingestion.trace'"
    return ("from pathlib import Path\nimport json, subprocess, sys\n"
            "assert sys.argv[1:] == ['--mode', 'retain']\n"
            "Path('.results').mkdir(exist_ok=True)\nreceipts = []\n"
            "for index in range(2):\n"
            f"    trace = {trace}\n"
            f"    completed = subprocess.run([sys.executable, '-c', {child!r}, trace], "
            "check=True, stdout=subprocess.PIPE, text=True)\n"
            "    receipts.append(json.loads(completed.stdout))\n"
            "Path('.results/receipts.json').write_text(json.dumps(receipts))\n" + suffix)


def prepare(local, *, body=None, closed=True):
    store, root, _, plan = local
    with (root / '.git/info/exclude').open('a') as stream:
        stream.write('.frozen/\n.corrections/\n.results/\n.started\n.release\n')
    (root / '.frozen').mkdir()
    (root / '.frozen/check.py').write_text(driver() if body is None else body)
    (root / '.frozen/second.py').write_text(driver())
    (root / '.frozen/dependency.txt').write_text('pinned')
    (root / '.frozen/ingestion.trace').write_text(TRACE)
    (root / 'src.txt').write_text('fixed')
    plan.update(checks=[dict(id='retained', argv=[sys.executable, '.frozen/check.py', '--mode', 'retain'],
                            reason='Two independent verifier processes must preserve the original ingestion measurement'),
                        dict(id='remaining', argv=[sys.executable, 'check.py'],
                             reason='Keep the other necessary producer check')],
                input_paths=['.frozen'], env_names=['CHECKER_SHARED_CASE'])
    publish(local)
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    proof = merge(local) if closed else None
    return row, proof


def correction(local, *, target='retained', identifier='retained-fixed', payload=None, body=None, failed=None):
    _, root, _, plan = local
    payload = deepcopy(payload or plan)
    original = next(c for c in payload['checks'] if c['id'] == target)
    (root / '.corrections').mkdir(exist_ok=True)
    script = f'.corrections/{identifier}.py'
    evidence = f'.corrections/{identifier}.txt'
    (root / script).write_text(driver(corrected=True) if body is None else body)
    diagnosis = ('The pending verifier opens the ingestion measurement with w. '
                 'Use distinct verifier traces and retain both actual verification processes and original assertions.')
    if failed:
        diagnosis = f"Correct the private post-verification exit {failed['exit_code']}; retain both verifiers and all assertions."
    (root / evidence).write_text(diagnosis + '\n')
    metadata = dict(check_id=target, diagnosis=diagnosis, evidence_path=evidence)
    metadata.update(run_id=failed['run_id']) if failed else metadata.update(unexecuted=True)
    check = {**deepcopy(original), 'id': identifier,
             'argv': [original['argv'][0], script, *original['argv'][2:]],
             'input_paths': [*original.get('input_paths', []), script, evidence], 'correction': metadata}
    check.pop('input_digest', None)
    payload['checks'].append(check)
    return payload, check


def test_unexecuted_correction_requires_fresh_real_verifiers_and_preserves_history(local):
    store, root, snap, _ = local
    original, _ = prepare(local)
    original_plan = deepcopy(original['plan'])
    original_trace = (root / '.frozen/ingestion.trace').read_bytes()
    payload, check = correction(local)
    independent = deepcopy(payload)
    independent['checks'][-1].pop('correction')
    bound = save(local, independent)['plan']
    old_pass = run(local, check['id'])['run']
    assert old_pass['status'] == 'pass'
    old_pids = {r['pid'] for r in json.loads((root / '.results/receipts.json').read_text())}
    assert workflow.current_phase(store, bound)['pending'] == ['remaining', 'retained']
    with pytest.raises(ValueError, match='immutable'):
        save(local, payload)  # A genuine independent pass cannot acquire correction credit.
    payload, check = correction(local, identifier='retained-fresh', payload=independent)
    result = save(local, payload)
    row = result['plan']
    assert row['plan_id'] == original['plan_id'] and row['input_digest'] == original['input_digest']
    assert row['plan']['checks'][:2] == original_plan['checks']
    assert {k: v for k, v in row['plan'].items() if k != 'checks'} == {k: v for k, v in original_plan.items() if k != 'checks'}
    assert store.validation_runs(row['plan_id']) == [old_pass]
    assert result['progress']['pending'] == ['remaining', 'retained', check['id']]
    assert 'corrected_unexecuted' not in result['progress']
    for start in (lambda: run(local, 'retained'), lambda: workflow.launch_check(
            store, 's', 'Owner/Repo', 1, root, 'retained', merged_head=snap['head_sha'])):
        with pytest.raises(ValueError, match='unexecuted checker has an admitted correction'):
            start()
    remaining = run(local, 'remaining')['run']
    actual = run(local, check['id'])
    fresh = actual['run']
    receipts = json.loads((root / '.results/receipts.json').read_text())
    assert fresh['status'] == 'pass' and fresh['exit_code'] == 0 and fresh['run_id'] != old_pass['run_id']
    assert len(receipts) == len({r['pid'] for r in receipts}) == len({r['trace'] for r in receipts}) == 2
    assert old_pids.isdisjoint({r['pid'] for r in receipts})
    assert all(r['ingestion_sha256'] == hashlib.sha256(original_trace).hexdigest() for r in receipts)
    assert (root / '.frozen/ingestion.trace').read_bytes() == original_trace
    assert fresh['input_digest'] == fresh['before_digest'] == fresh['after_digest'] == row['plan']['checks'][-1]['input_digest']
    assert store.validation_runs(row['plan_id']) == store.validation_evidence(row['plan_id']) == [old_pass, remaining, fresh]
    assert actual['progress']['corrected_unexecuted'] == [dict(check_id='retained', unexecuted=True,
        corrected_by=check['id'], passed_check_id=check['id'], passed_run_id=fresh['run_id'])]
    assert 'corrected_failures' not in actual['progress'] and 'retained' not in actual['progress']['passed']
    assert actual['progress']['local_checks_passed'] is True and actual['progress']['completion_verified'] is False
    assert workflow.task_progress(store, 's')[0]['status'] == 'WAIT'
    assert next(g for g in evaluate_pr(snap, []) if g['gate'] == 'MERGE_READY')['status'] == 'WAIT'
    assert progress.working_state(root)['clean'] is True and store.prs('s') == []
    with store._connect() as db:
        events = [json.loads(r['fields']) for r in db.execute("SELECT fields FROM audit WHERE event='validation_checker_correction'")]
        watch = db.execute('SELECT * FROM pr_watch').fetchone()
    assert len(events) == 1 and events[0]['unexecuted'] is True
    assert not {'run_id', 'failed_run_id', 'failed_exit_code'} & events[0].keys()
    assert events[0]['original_input_digest'] == original['input_digest']
    assert events[0]['corrected_input_digest'] == fresh['input_digest']
    assert events[0]['original_driver_sha256'] == hashlib.sha256((root / '.frozen/check.py').read_bytes()).hexdigest()
    assert events[0]['corrected_driver_sha256'] == hashlib.sha256((root / check['argv'][1]).read_bytes()).hexdigest()
    assert events[0]['evidence_sha256'] == hashlib.sha256((root / check['correction']['evidence_path']).read_bytes()).hexdigest()
    assert watch['state'] == 'merged' and watch['event_id'] is None and watch['delivery_state'] is None
    assert save(local, payload)['plan'] == row
    with pytest.raises(ValueError):
        run(local, check['id'])


@pytest.mark.parametrize('outcome', ['pass', 'fail', 'unknown'])
def test_any_actual_original_run_refuses_unexecuted_admission(local, outcome):
    suffix = {'pass': '', 'fail': 'raise SystemExit(27)\n',
              'unknown': 'import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n'}[outcome]
    original, _ = prepare(local, body=driver(corrected=True, suffix=suffix))
    previous = run(local, 'retained')['run']
    assert previous['status'] == outcome
    payload, _ = correction(local)
    with pytest.raises(ValueError, match='without any run history'):
        save(local, payload)
    assert local[0].validation_runs(original['plan_id']) == [previous]
    if outcome == 'fail':
        payload, check = correction(local, failed=previous)
        save(local, payload)
        actual = run(local, check['id'])
        assert actual['run']['status'] == 'pass'
        assert actual['progress']['corrected_failures'][0]['failed_run_id'] == previous['run_id']
        assert 'corrected_unexecuted' not in actual['progress']


def test_prior_generation_run_cannot_be_claimed_unexecuted(local):
    store, root, _, plan = local
    original, _ = prepare(local, closed=False, body=driver(corrected=True))
    previous = workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'retained')['run']
    (root / '.frozen/dependency.txt').write_text('changed input generation')
    revised = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    assert revised['plan_id'] != original['plan_id'] and store.validation_evidence(revised['plan_id']) == []
    merge(local)
    payload, _ = correction(local)
    with pytest.raises(ValueError, match='without any run history'):
        save(local, payload)
    assert store.validation_runs(original['plan_id']) == [previous]
    assert store.validation_runs(revised['plan_id']) == []


@pytest.mark.parametrize('change', ['false', 'number', 'string', 'null', 'missing', 'both', 'extra', 'fake-failure'])
def test_unexecuted_metadata_is_explicit_and_cannot_fabricate_failure(local, change):
    row, _ = prepare(local)
    payload, check = correction(local)
    metadata = check['correction']
    if change in {'false', 'number', 'string', 'null'}:
        metadata['unexecuted'] = {'false': False, 'number': 1, 'string': 'true', 'null': None}[change]
    elif change in {'missing', 'fake-failure'}:
        metadata.pop('unexecuted')
    elif change == 'extra':
        metadata['waive'] = True
    if change in {'both', 'fake-failure'}:
        metadata['run_id'] = '00000000-0000-4000-8000-000000000001'
    with pytest.raises(ValueError):
        save(local, payload)
    assert local[0].validation_runs(row['plan_id']) == []


@pytest.mark.parametrize('change', ['copied', 'comment-only', 'same-file', 'arguments', 'interpreter', 'reason',
                                  'no-diagnosis', 'empty-evidence', 'unpinned-driver', 'unpinned-evidence',
                                  'driver-link', 'evidence-link', 'parent-link', 'outside-driver', 'cwd',
                                  'remove-original', 'shared-pin', 'shared-setting', 'shared-environment', 'source', 'foreign', 'wrong-head'])
def test_unexecuted_correction_preserves_existing_admission_boundaries(local, tmp_path, monkeypatch, change):
    store, root, snap, _ = local
    monkeypatch.setenv('CHECKER_SHARED_CASE', 'original')
    row, _ = prepare(local)
    payload, check = correction(local)
    sid, head = 's', snap['head_sha']
    if change in {'copied', 'comment-only'}:
        (root / check['argv'][1]).write_text((root / '.frozen/check.py').read_text() + ('# no fix\n' if change == 'comment-only' else ''))
    elif change == 'same-file':
        check['argv'][1] = '.frozen/check.py'
        check['input_paths'].append('.frozen/check.py')
    elif change == 'arguments':
        check['argv'][-1] = 'skip'
    elif change == 'interpreter':
        check['argv'][0] = 'python3.13'
    elif change == 'reason':
        check['reason'] = 'Skip the original verifier obligation'
    elif change == 'no-diagnosis':
        check['correction']['diagnosis'] = ''
    elif change == 'empty-evidence':
        (root / check['correction']['evidence_path']).write_text(' ')
    elif change.startswith('unpinned-'):
        check['input_paths'].remove(check['argv'][1] if change == 'unpinned-driver' else check['correction']['evidence_path'])
    elif change in {'driver-link', 'evidence-link', 'outside-driver'}:
        path = root / (check['correction']['evidence_path'] if change == 'evidence-link' else check['argv'][1])
        outside = tmp_path / 'outside.py'
        outside.write_bytes(path.read_bytes())
        if change == 'outside-driver':
            check['argv'][1] = str(outside)
        else:
            path.unlink()
            path.symlink_to(outside)
    elif change == 'parent-link':
        with (root / '.git/info/exclude').open('a') as stream:
            stream.write('.private/\n')
        (root / '.corrections').rename(root / '.private')
        (root / '.corrections').symlink_to(root / '.private', target_is_directory=True)
    elif change == 'cwd':
        check['cwd'] = '.corrections'
    elif change == 'remove-original':
        payload['checks'].pop(0)
    elif change == 'shared-pin':
        (root / '.frozen/dependency.txt').write_text('new shared input')
    elif change == 'shared-setting':
        payload['input_paths'] = []
    elif change == 'shared-environment':
        monkeypatch.setenv('CHECKER_SHARED_CASE', 'changed')
    elif change == 'source':
        (root / 'src.txt').write_text('different producer')
    elif change == 'foreign':
        sid = 'foreign'
        store.record_session(sid, 'codex', str(root))
    elif change == 'wrong-head':
        head = 'c' * 40
    with pytest.raises(ValueError):
        workflow.save_plan(store, sid, 'Owner/Repo', 1, root, payload, merged_head=head)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == row
    assert store.validation_runs(row['plan_id']) == []


@pytest.mark.parametrize('change', ['foreign', 'child', 'checkout', 'origin', 'source', 'shared-pin', 'driver', 'evidence', 'unmerged', 'stale'])
def test_admitted_pending_correction_rechecks_owner_source_and_pins_before_execution(local, tmp_path, change):
    store, root, snap, _ = local
    original, proof = prepare(local)
    payload, check = correction(local)
    bound = save(local, payload)['plan']
    sid, cwd = 's', root
    if change in {'foreign', 'child'}:
        sid = change
        store.record_session(sid, 'codex', str(root), parent_session='s' if change == 'child' else None)
    elif change == 'checkout':
        cwd = tmp_path / 'clone'
        subprocess.run(['git', 'clone', '--quiet', str(root), str(cwd)], check=True)
    elif change == 'origin':
        git(root, 'remote', 'set-url', 'origin', 'https://github.com/Other/Repo.git')
    elif change in {'source', 'shared-pin', 'driver', 'evidence'}:
        path = {'source': 'src.txt', 'shared-pin': '.frozen/dependency.txt',
                'driver': check['argv'][1], 'evidence': check['correction']['evidence_path']}[change]
        with (root / path).open('a') as stream:
            stream.write('# changed after admission\n')
    else:
        proof.update(merged=False) if change == 'unmerged' else proof.update(collected_at=0)
        store.set_preflight('Owner/Repo', 1, proof)
    with pytest.raises(ValueError):
        workflow.run_check(store, sid, 'Owner/Repo', 1, cwd, check['id'], merged_head=snap['head_sha'])
    assert store.validation_runs(original['plan_id']) == []
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == bound
    assert (root / '.frozen/ingestion.trace').read_text() == TRACE


def test_failed_corrected_execution_remains_failed_until_a_real_failed_route_success(local):
    store, root, _, _ = local
    original, _ = prepare(local)
    payload, check = correction(local, body=driver(corrected=True, suffix='raise SystemExit(27)\n'))
    save(local, payload)
    actual = run(local, check['id'])
    failed = actual['run']
    assert failed['status'] == 'fail' and failed['exit_code'] == 27
    assert actual['progress']['failed'] == [check['id']]
    assert 'retained' in actual['progress']['pending'] and 'corrected_unexecuted' not in actual['progress']
    with pytest.raises(ValueError, match='already completed'):
        run(local, check['id'])
    fork, _ = correction(local, identifier='fork', payload=payload)
    with pytest.raises(ValueError, match='already has a correction'):
        save(local, fork)
    fake_pending, _ = correction(local, target=check['id'], identifier='retry', payload=payload)
    with pytest.raises(ValueError, match='without any run history'):
        save(local, fake_pending)
    next_plan, successor = correction(local, target=check['id'], identifier='retry', payload=payload, failed=failed)
    save(local, next_plan)
    final = run(local, successor['id'])
    assert final['run']['status'] == 'pass' and final['run']['exit_code'] == 0
    assert final['progress']['corrected_failures'][0]['failed_run_id'] == failed['run_id']
    assert final['progress']['corrected_unexecuted'][0]['passed_run_id'] == final['run']['run_id']
    assert final['progress']['pending'] == ['remaining']
    assert store.validation_runs(original['plan_id']) == [failed, final['run']]
    assert (root / '.frozen/ingestion.trace').read_text() == TRACE


def test_existing_pending_control_keeps_dependencies_and_is_a_distinct_obligation(local, monkeypatch):
    store, root, _, plan = local
    original, _ = prepare(local)
    monkeypatch.setenv('CHECKER_PRIVATE_CASE', 'original')
    payload = deepcopy(plan)
    second = {**deepcopy(plan['checks'][0]), 'id': 'retained-two',
              'argv': [sys.executable, '.frozen/second.py', '--mode', 'retain'],
              'input_paths': ['.frozen/second.py', '.frozen/dependency.txt'], 'env_names': ['CHECKER_PRIVATE_CASE']}
    payload['checks'].append(second)
    before = save(local, payload)['plan']
    first_plan, first = correction(local, payload=payload)
    save(local, first_plan)
    first_run = run(local, first['id'])
    assert first_run['progress']['pending'] == ['remaining', 'retained-two']
    revised, check = correction(local, target='retained-two', identifier='second-fixed', payload=first_plan)
    for name in ['input_paths', 'env_names']:
        missing = deepcopy(revised)
        missing['checks'][-1][name].remove(second[name][0])
        with pytest.raises(ValueError, match='retain every original check dependency'):
            save(local, missing)
    row = save(local, revised)['plan']
    assert row['plan']['checks'][:3] == before['plan']['checks']
    actual = run(local, check['id'])
    assert actual['progress']['pending'] == ['remaining']
    assert {c['check_id'] for c in actual['progress']['corrected_unexecuted']} == {'retained', 'retained-two'}
    assert {c['passed_run_id'] for c in actual['progress']['corrected_unexecuted']} == {first_run['run']['run_id'], actual['run']['run_id']}
    assert store.validation_runs(original['plan_id']) == [first_run['run'], actual['run']]


@pytest.mark.parametrize('mutation', ['driver', 'evidence', 'source'])
def test_real_zero_exit_with_changed_correction_inputs_is_not_credit(local, mutation):
    store, root, _, _ = local
    row, _ = prepare(local)
    path = {'driver': '.corrections/retained-fixed.py', 'evidence': '.corrections/retained-fixed.txt', 'source': 'src.txt'}[mutation]
    payload, check = correction(local, body=driver(corrected=True, suffix=f"Path({path!r}).write_text('mutated')\n"))
    bound = save(local, payload)['plan']
    actual = run(local, check['id'])['run']
    assert actual['exit_code'] == 0 and actual['status'] == 'unknown' and actual['before_digest'] != actual['after_digest']
    assert store.validation_runs(row['plan_id']) == [actual]
    phase = workflow.current_phase(store, bound)
    assert phase['phase'] == 'INPUTS_CHANGED' and 'corrected_unexecuted' not in phase
    assert (root / '.frozen/ingestion.trace').read_text() == TRACE


@pytest.mark.parametrize('active', ['original', 'correction'])
def test_live_process_blocks_admission_or_competing_execution(local, active):
    store, root, _, _ = local
    waiting = driver(corrected=True, suffix=(
        "import time\nPath('.started').touch()\ndeadline = time.monotonic() + 10\n"
        "while not Path('.release').exists() and time.monotonic() < deadline: time.sleep(.02)\n"
        "assert Path('.release').exists()\n"))
    row, _ = prepare(local, body=waiting if active == 'original' else None)
    payload, check = correction(local, body=waiting)
    if active == 'correction':
        save(local, payload)
    outcomes, errors = [], []
    def execute():
        try:
            outcomes.append(run(local, 'retained' if active == 'original' else check['id']))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=execute)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not (root / '.started').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (root / '.started').exists()
        with pytest.raises(ValueError, match='active run'):
            run(local, 'remaining')
        proposed = payload if active == 'original' else correction(local, identifier='fork', payload=payload)[0]
        with pytest.raises(ValueError, match='active run'):
            save(local, proposed)
        assert 'corrected_unexecuted' not in workflow.current_phase(store, store.validation_plan('s', 'Owner/Repo', 1, root))
    finally:
        (root / '.release').touch()
        worker.join(12)
    assert not worker.is_alive() and not errors and outcomes[0]['run']['status'] == 'pass'
    assert len(store.validation_runs(row['plan_id'])) == 1
    if active == 'original':
        with pytest.raises(ValueError, match='without any run history'):
            save(local, payload)


def test_already_launched_original_worker_rechecks_after_correction_admission(local, monkeypatch):
    store, root, snap, _ = local
    row, proof = prepare(local)
    payload, _ = correction(local)
    bootstrap = ('from pathlib import Path\nimport json, time\nfrom caller_pr_gates import github, cli\n'
                 f'remote = json.loads({json.dumps(proof)!r})\n'
                 'async def collect(*args):\n    return dict(remote, collected_at=time.time())\n'
                 'github.collect_validation = collect\n'
                 "Path('.started').touch()\ndeadline = time.monotonic() + 10\n"
                 "while not Path('.release').exists() and time.monotonic() < deadline: time.sleep(.02)\n"
                 "assert Path('.release').exists()\ncli.main()\n")
    original, workers = subprocess.Popen, []
    def start(argv, **kwargs):
        is_worker = argv[1:4] == ['-P', '-m', 'caller_pr_gates.cli']
        if is_worker:
            argv = [argv[0], '-c', bootstrap, *argv[4:]]
        child = original(argv, **kwargs)
        if is_worker:
            workers.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', start)
    started = workflow.launch_check(store, 's', 'Owner/Repo', 1, root, 'retained', merged_head=snap['head_sha'])
    assert started['completion_verified'] is False
    try:
        deadline = time.monotonic() + 5
        while not (root / '.started').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (root / '.started').exists()
        save(local, payload)
    finally:
        (root / '.release').touch()
        code = workers[0].wait(timeout=12)
    assert code != 0 and store.validation_runs(row['plan_id']) == []
    assert 'unexecuted checker has an admitted correction' in Path(started['log']).read_text()
    assert (root / '.frozen/ingestion.trace').read_text() == TRACE


@pytest.mark.parametrize('change', ['none', 'proof', 'driver'])
def test_fresh_corrected_background_worker_requires_its_own_exit_and_current_inputs(local, monkeypatch, change):
    store, root, snap, _ = local
    row, proof = prepare(local)
    payload, check = correction(local)
    save(local, payload)
    if change == 'proof':
        proof['merged'] = False
    bootstrap = ('import json, time\nfrom caller_pr_gates import github, cli\n'
                 f'remote = json.loads({json.dumps(proof)!r})\n'
                 'async def collect(*args):\n    return dict(remote, collected_at=time.time())\n'
                 'github.collect_validation = collect\ncli.main()\n')
    original, workers = subprocess.Popen, []
    def start(argv, **kwargs):
        is_worker = argv[1:4] == ['-P', '-m', 'caller_pr_gates.cli']
        if is_worker:
            if change == 'driver':
                (root / check['argv'][1]).write_text('raise SystemExit(0)\n')
            argv = [argv[0], '-c', bootstrap, *argv[4:]]
        child = original(argv, **kwargs)
        if is_worker:
            workers.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', start)
    started = workflow.launch_check(store, 's', 'Owner/Repo', 1, root, check['id'], merged_head=snap['head_sha'])
    assert started['completion_verified'] is False
    code = workers[0].wait(timeout=12)
    runs = store.validation_runs(row['plan_id'])
    if change == 'none':
        assert code == 0 and len(runs) == 1 and runs[0]['status'] == 'pass' and runs[0]['exit_code'] == 0
        assert runs[0]['before_digest'] == runs[0]['after_digest'] == runs[0]['input_digest']
        current = workflow.current_phase(store, store.validation_plan('s', 'Owner/Repo', 1, root))
        assert current['corrected_unexecuted'][0]['passed_run_id'] == runs[0]['run_id']
    else:
        assert code != 0 and runs == []
    assert (root / '.frozen/ingestion.trace').read_text() == TRACE


def test_plan_cli_uses_existing_narrow_collection_and_no_new_execution(local, monkeypatch, tmp_path, capsys):
    store, root, snap, _ = local
    row, proof = prepare(local)
    payload, _ = correction(local)
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(payload))
    reads = []
    async def narrow(*args):
        reads.append(args)
        return dict(proof, collected_at=time.time())
    async def unexpected(*args):
        pytest.fail('Unexecuted correction must not add full GitHub collection')
    monkeypatch.setattr(cli, 'Store', lambda: store)
    monkeypatch.setattr(github, 'collect_validation', narrow)
    monkeypatch.setattr(github, 'collect_pr', unexpected)
    monkeypatch.setattr(sys, 'argv', ['caller-pr-gates', 'validation-plan', '--session', 's',
        '--pr', 'https://github.com/Owner/Repo/pull/1', '--cwd', str(root), '--file', str(path),
        '--merged-head', snap['head_sha']])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert len(reads) == 1 and result['plan']['plan_id'] == row['plan_id']
    assert set(result['progress']['pending']) == {'remaining', 'retained', 'retained-fixed'}
    assert store.validation_runs(row['plan_id']) == []


def test_open_pr_does_not_gain_unexecuted_merged_correction_authority(local):
    store, root, _, _ = local
    row, _ = prepare(local, closed=False)
    payload, _ = correction(local)
    with pytest.raises(ValueError, match='require --merged-head'):
        workflow.save_plan(store, 's', 'Owner/Repo', 1, root, payload)
    with pytest.raises(ValueError, match='fresh stable proof'):
        save(local, payload)
    assert store.validation_runs(row['plan_id']) == []
