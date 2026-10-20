"""A private checker repair needs its own real result, not a renamed old pass."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from caller_pr_gates import cli, github, progress, workflow
from caller_pr_gates.policy import evaluate_pr
from test_post_merge_qualification import merge, run
from test_workflow import local, git, publish


def driver(*, corrected=False, suffix=""):
    # Deterministic stand-in for the reported private Docker name-release race:
    # the first trial completes, but immediate name reuse exits before trial 2.
    names = ['trial-0', 'trial-1'] if corrected else ['reused', 'reused']
    return ("from pathlib import Path\nimport sys\n"
            f"names = {names!r}\nheld = set()\n"
            "for index, name in enumerate(names):\n"
            "    if name in held:\n"
            "        print('container name has not been released', file=sys.stderr)\n"
            "        raise SystemExit(125)\n"
            "    held.add(name)\n"
            "    assert Path('src.txt').read_text() == 'fixed'\n"
            "    assert Path('.frozen/dependency.txt').read_text() == 'pinned'\n"
            "    Path('.results').write_text(str(index + 1))\n" + suffix)


def prepare(local, *, outcome='fail', private=True, closed=True, arguments=None):
    store, root, snap, plan = local
    with (root / '.git/info/exclude').open('a') as target:
        target.write('.frozen/\n.corrections/\n.results\n.started\n.release\n')
    (root / '.frozen').mkdir()
    (root / '.frozen/dependency.txt').write_text('pinned')
    name = '.frozen/check.py' if private else 'tracked-check.py'
    body = driver(corrected=outcome == 'pass')
    if outcome == 'unknown':
        body = 'import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n'
    (root / name).write_text(body)
    (root / 'src.txt').write_text('fixed')
    plan.update(checks=[dict(id='native-matched', argv=[sys.executable, name, *(arguments or ['--mode', 'compare'])],
                            reason='Execute both isolated trials with all pinned producer assertions'),
                        dict(id='remaining', argv=[sys.executable, 'check.py'],
                             reason='Retain the other already-declared required check')],
                input_paths=[name, '.frozen/dependency.txt'])
    publish(local)
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    proof = merge(local) if closed else None
    result = (run(local, 'native-matched') if closed else
              workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'native-matched'))
    return row, result['run'], proof


def correction(local, failed, *, identifier='native-matched-v2', payload=None, body=None):
    _, root, _, original = local
    payload = deepcopy(payload or original)
    target = next(c for c in payload['checks'] if c['id'] == failed['check_id'])
    (root / '.corrections').mkdir(exist_ok=True)
    script = f'.corrections/{identifier}.py'
    evidence = f'.corrections/{identifier}.txt'
    (root / script).write_text(driver(corrected=True) if body is None else body)
    (root / evidence).write_text(
        f"Run {failed['run_id']} ended with {failed['exit_code']}. "
        "The first trial completed; name reuse prevented the second from starting. "
        "Give each trial a distinct name and preserve stderr. All producer assertions remain required.\n")
    check = {**deepcopy(target), 'id': identifier,
             'argv': [target['argv'][0], script, *target['argv'][2:]],
             'input_paths': [*target.get('input_paths', []), script, evidence],
             'correction': dict(check_id=target['id'], run_id=failed['run_id'],
                 diagnosis='Repair the observed private name-reuse race without changing trial acceptance',
                 evidence_path=evidence)}
    check.pop('input_digest', None)
    payload['checks'].append(check)
    return payload, check


def save(local, payload):
    store, root, snap, _ = local
    return workflow.save_plan(store, 's', 'Owner/Repo', 1, root, payload, merged_head=snap['head_sha'])


def test_real_failed_private_checker_gets_audited_correction_not_rewritten_pass(local):
    store, root, snap, plan = local
    original, failed, _ = prepare(local)
    assert failed['status'] == 'fail' and failed['exit_code'] == 125
    assert (root / '.results').read_text() == '1'  # Candidate never started.
    run(local, 'remaining')
    history = store.validation_runs(original['plan_id'])
    payload, check = correction(local, failed)
    # The 0.1.42 route permits an additional passing control, not correction.
    independent = deepcopy(payload)
    independent['checks'][-1].pop('correction')
    bound = save(local, independent)['plan']
    extra = run(local, check['id'])['run']
    assert extra['status'] == 'pass' and workflow.current_phase(store, bound)['failed'] == ['native-matched']
    with pytest.raises(ValueError, match='immutable'):
        save(local, payload)  # A past pass cannot be relabeled as a correction.
    payload, check = correction(local, failed, identifier='native-matched-v3', payload=independent)
    row = save(local, payload)['plan']
    assert row['plan_id'] == original['plan_id'] and row['input_digest'] == original['input_digest']
    assert row['plan']['checks'][:2] == original['plan']['checks']
    assert row['plan']['input_paths'] == original['plan']['input_paths']
    assert store.validation_runs(row['plan_id']) == [*history, extra]
    assert workflow.current_phase(store, row)['failed'] == ['native-matched']
    with pytest.raises(ValueError, match='already completed'):
        run(local, 'native-matched')
    actual = run(local, check['id'])
    corrected = actual['run']
    assert corrected['status'] == 'pass' and corrected['exit_code'] == 0
    assert (root / '.results').read_text() == '2'
    assert corrected['input_digest'] == corrected['before_digest'] == corrected['after_digest'] == row['plan']['checks'][-1]['input_digest']
    assert corrected['input_digest'] != original['input_digest']
    assert store.validation_runs(row['plan_id']) == [*history, extra, corrected]
    assert store.validation_evidence(row['plan_id']) == [*history, extra, corrected]
    assert actual['progress']['corrected_failures'] == [dict(check_id='native-matched',
        failed_run_id=failed['run_id'], exit_code=125, corrected_by=check['id'],
        passed_check_id=check['id'], passed_run_id=corrected['run_id'])]
    assert 'native-matched' not in actual['progress']['passed']
    assert actual['progress']['local_checks_passed'] is True
    assert actual['progress']['completion_verified'] is False
    assert workflow.task_progress(store, 's')[0]['status'] == 'WAIT'
    assert next(g for g in evaluate_pr(snap, []) if g['gate'] == 'MERGE_READY')['status'] == 'WAIT'
    assert progress.working_state(root)['clean'] is True and store.prs('s') == []
    with store._connect() as db:
        events = [json.loads(r['fields']) for r in db.execute("SELECT fields FROM audit WHERE event='validation_checker_correction'")]
        watch = db.execute('SELECT * FROM pr_watch').fetchone()
    assert len(events) == 1 and events[0]['failed_run_id'] == failed['run_id']
    assert events[0]['original_driver_sha256'] != events[0]['corrected_driver_sha256']
    assert watch['state'] == 'merged' and watch['event_id'] is None and watch['delivery_state'] is None
    assert save(local, payload)['plan']['plan_id'] == row['plan_id']
    with pytest.raises(ValueError):
        run(local, check['id'])


@pytest.mark.parametrize('change', ['copied', 'comment-only', 'same-file', 'argument', 'interpreter', 'reason',
                                  'no-diagnosis', 'no-evidence', 'empty-evidence', 'unpinned-evidence',
                                  'unpinned-driver', 'wrong-run', 'wrong-check', 'forged-pin'])
def test_correction_admission_refuses_nonfixes_and_missing_proof(local, change):
    store, root, _, _ = local
    row, failed, _ = prepare(local)
    payload, check = correction(local, failed)
    if change in {'copied', 'comment-only'}:
        body = (root / '.frozen/check.py').read_text()
        (root / check['argv'][1]).write_text(body + ('# no program change\n' if change == 'comment-only' else ''))
    elif change == 'same-file':
        check['argv'][1] = '.frozen/check.py'
        check['input_paths'].append('.frozen/check.py')
    elif change == 'argument':
        check['argv'][-1] = 'skip-candidate'
    elif change == 'interpreter':
        check['argv'][0] = 'python3.13'
    elif change == 'reason':
        check['reason'] = 'Only require the baseline'
    elif change == 'no-diagnosis':
        check['correction']['diagnosis'] = ''
    elif change == 'no-evidence':
        (root / check['correction']['evidence_path']).unlink()
    elif change == 'empty-evidence':
        (root / check['correction']['evidence_path']).write_text(' ')
    elif change == 'unpinned-evidence':
        check['input_paths'].remove(check['correction']['evidence_path'])
    elif change == 'unpinned-driver':
        check['input_paths'].remove(check['argv'][1])
    elif change == 'wrong-run':
        check['correction']['run_id'] = '00000000-0000-4000-8000-000000000001'
    elif change == 'wrong-check':
        check['correction']['check_id'] = 'remaining'
    else:
        check['input_digest'] = 'a' * 64
    with pytest.raises(ValueError):
        save(local, payload)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == row
    assert store.validation_runs(row['plan_id']) == [failed]


@pytest.mark.parametrize('outcome', ['pass', 'unknown'])
def test_only_an_actual_finished_failure_can_be_corrected(local, outcome):
    store, _, _, _ = local
    row, previous, _ = prepare(local, outcome=outcome)
    assert previous['status'] == outcome
    payload, _ = correction(local, previous)
    with pytest.raises(ValueError, match='actual failed run'):
        save(local, payload)
    assert store.validation_runs(row['plan_id']) == [previous]


def test_tracked_checker_still_requires_a_real_product_repair(local):
    _, _, _, _ = local
    _, failed, _ = prepare(local, private=False)
    payload, _ = correction(local, failed)
    with pytest.raises(ValueError, match='tracked changes need a scoped product PR'):
        save(local, payload)


@pytest.mark.parametrize('option', ['-m', '-'])
def test_private_filename_cannot_substitute_a_python_option_for_the_driver(local, option):
    _, root, _, _ = local
    _, failed, _ = prepare(local, arguments=['pytest'])
    payload, check = correction(local, failed)
    # The generic runner recognizes "python -m pytest". A private file named
    # "-m" must not make that different executable program a checker repair.
    with (root / '.git/info/exclude').open('a') as target:
        target.write(option + '\n')
    (root / option).write_text(driver(corrected=True))
    check['argv'][1] = option
    check['input_paths'].append(option)
    with pytest.raises(ValueError, match='preserve its Python interpreter'):
        save(local, payload)


def test_failed_corrected_checker_needs_its_own_evidenced_correction(local):
    store, _, _, _ = local
    row, failed, _ = prepare(local)
    payload, check = correction(local, failed, body=driver(corrected=True, suffix='raise SystemExit(27)\n'))
    save(local, payload)
    result = run(local, check['id'])
    second = result['run']
    assert second['status'] == 'fail' and second['exit_code'] == 27
    assert result['progress']['failed'] == ['native-matched', check['id']]
    fork, _ = correction(local, failed, identifier='fork', payload=payload)
    with pytest.raises(ValueError, match='already has a correction'):
        save(local, fork)
    third, check = correction(local, second, identifier='native-matched-v3', payload=payload)
    save(local, third)
    result = run(local, check['id'])
    assert result['run']['status'] == 'pass'
    corrected = result['progress']['corrected_failures']
    assert {r['failed_run_id'] for r in corrected} == {failed['run_id'], second['run_id']}
    assert {r['passed_run_id'] for r in corrected} == {result['run']['run_id']}
    assert result['progress']['pending'] == ['remaining']
    assert store.validation_runs(row['plan_id'])[:2] == [failed, second]


def test_correction_retains_per_check_dependencies(local, monkeypatch):
    store, root, _, _ = local
    row, failed, _ = prepare(local)
    payload, check = correction(local, failed, body=driver(corrected=True, suffix='raise SystemExit(27)\n'))
    monkeypatch.setenv('CHECKER_CASE', 'original')
    check['env_names'] = ['CHECKER_CASE']
    save(local, payload)
    second = run(local, check['id'])['run']
    revised, new = correction(local, second, identifier='native-matched-v3', payload=payload)
    for name in ['input_paths', 'env_names']:
        missing = deepcopy(revised)
        missing['checks'][-1][name].remove(check[name][0])
        with pytest.raises(ValueError, match='retain every original check dependency'):
            save(local, missing)
    assert store.validation_runs(row['plan_id']) == [failed, second]


@pytest.mark.parametrize('change', ['source', 'commit', 'origin', 'retained-input', 'driver', 'evidence',
                                  'foreign', 'child', 'checkout', 'wrong-head', 'unmerged', 'stale'])
def test_correction_cannot_escape_frozen_source_pins_or_owner(local, tmp_path, change):
    store, root, snap, _ = local
    original, failed, proof = prepare(local)
    payload, check = correction(local, failed)
    row = save(local, payload)['plan']
    sid, cwd, head = 's', root, snap['head_sha']
    if change == 'source':
        (root / 'src.txt').write_text('changed producer')
    elif change == 'commit':
        git(root, 'commit', '--allow-empty', '-qm', 'Different source identity')
        head = git(root, 'rev-parse', 'HEAD')
    elif change == 'origin':
        git(root, 'remote', 'set-url', 'origin', 'https://github.com/Other/Repo.git')
    elif change == 'retained-input':
        (root / '.frozen/dependency.txt').write_text('changed original dependency')
    elif change in {'driver', 'evidence'}:
        path = check['argv'][1] if change == 'driver' else check['correction']['evidence_path']
        with (root / path).open('a') as target:
            target.write('# changed after pinning\n')
    elif change in {'foreign', 'child'}:
        sid = change
        store.record_session(sid, 'codex', str(root), parent_session='s' if change == 'child' else None)
    elif change == 'checkout':
        cwd = tmp_path / 'clone'
        subprocess.run(['git', 'clone', '--quiet', str(root), str(cwd)], check=True)
    elif change == 'wrong-head':
        head = 'c' * 40
    else:
        proof.update(merged=False) if change == 'unmerged' else proof.update(collected_at=0)
        store.set_preflight('Owner/Repo', 1, proof)
    with pytest.raises(ValueError):
        workflow.run_check(store, sid, 'Owner/Repo', 1, cwd, check['id'], merged_head=head)
    assert store.validation_runs(original['plan_id']) == [failed]
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == row


@pytest.mark.parametrize('change', ['driver-link', 'parent-link', 'evidence-link', 'outside-driver', 'outside-evidence', 'cwd-link'])
def test_private_checker_correction_refuses_path_and_symlink_escape(local, tmp_path, change):
    _, root, _, _ = local
    _, failed, _ = prepare(local)
    payload, check = correction(local, failed)
    if change == 'parent-link':
        (root / '.corrections').rename(root / '.frozen/controls')
        (root / '.corrections').symlink_to(root / '.frozen/controls', target_is_directory=True)
    elif change == 'cwd-link':
        (root / '.corrections/alias').symlink_to(root, target_is_directory=True)
        check['cwd'] = '.corrections/alias'
    else:
        driver_path = change.endswith('driver') or change == 'driver-link'
        original = root / (check['argv'][1] if driver_path else check['correction']['evidence_path'])
        outside = tmp_path / ('test_check.py' if driver_path else 'evidence.txt')
        outside.write_bytes(original.read_bytes())
        if change.endswith('-link'):
            original.unlink()
            original.symlink_to(outside)
        elif driver_path:
            check['argv'][1] = str(outside)
        else:
            check['correction']['evidence_path'] = str(outside)
    with pytest.raises(ValueError):
        save(local, payload)


@pytest.mark.parametrize('mutation', ['driver', 'evidence', 'source'])
def test_real_exit_cannot_cover_failure_when_correction_inputs_change_during_execution(local, mutation):
    store, root, _, _ = local
    row, failed, _ = prepare(local)
    path = {'driver': '.corrections/native-matched-v2.py',
            'evidence': '.corrections/native-matched-v2.txt', 'source': 'src.txt'}[mutation]
    body = driver(corrected=True, suffix=f"Path({path!r}).write_text('changed while running')\n")
    payload, check = correction(local, failed, body=body)
    save(local, payload)
    actual = run(local, check['id'])['run']
    assert actual['exit_code'] == 0 and actual['status'] == 'unknown'
    assert actual['before_digest'] != actual['after_digest']
    assert store.validation_runs(row['plan_id']) == [failed, actual]
    assert workflow.current_phase(store, store.validation_plan('s', 'Owner/Repo', 1, root))['phase'] != 'COMPLETE'


def test_active_corrected_process_blocks_other_execution_and_plan_changes(local):
    store, root, _, _ = local
    row, failed, _ = prepare(local)
    body = driver(corrected=True, suffix=(
        "import time\nPath('.started').touch()\ndeadline = time.monotonic() + 10\n"
        "while not Path('.release').exists() and time.monotonic() < deadline: time.sleep(.02)\n"
        "assert Path('.release').exists()\n"))
    payload, check = correction(local, failed, body=body)
    save(local, payload)
    outcomes, errors = [], []
    def execute():
        try:
            outcomes.append(run(local, check['id']))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=execute)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not (root / '.started').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (root / '.started').exists()
        assert workflow.current_phase(store, store.validation_plan('s', 'Owner/Repo', 1, root))['failed'] == ['native-matched']
        with pytest.raises(ValueError, match='active run'):
            run(local, 'remaining')
        fork, _ = correction(local, failed, identifier='fork', payload=payload)
        with pytest.raises(ValueError, match='active run'):
            save(local, fork)
    finally:
        (root / '.release').touch()
        worker.join(12)
    assert not worker.is_alive() and not errors and outcomes[0]['run']['status'] == 'pass'
    assert store.validation_runs(row['plan_id'])[0] == failed


@pytest.mark.parametrize('change', ['none', 'proof', 'driver'])
def test_background_worker_requires_actual_corrected_exit_and_rechecks_pins(local, monkeypatch, change):
    store, root, snap, _ = local
    row, failed, proof = prepare(local)
    payload, check = correction(local, failed)
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
            assert argv[-2:] == ['--merged-head', snap['head_sha']]
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
    code = workers[0].wait(timeout=10)
    runs = store.validation_runs(row['plan_id'])
    assert runs[0] == failed
    if change == 'none':
        assert code == 0 and len(runs) == 2 and runs[-1]['status'] == 'pass'
        assert runs[-1]['exit_code'] == 0 and runs[-1]['before_digest'] == runs[-1]['after_digest']
    else:
        assert code != 0 and runs == [failed]


def test_existing_plan_cli_adds_correction_with_no_extra_remote_collection(local, monkeypatch, tmp_path, capsys):
    store, root, snap, _ = local
    row, failed, proof = prepare(local)
    payload, _ = correction(local, failed)
    file = tmp_path / 'plan.json'
    file.write_text(json.dumps(payload))
    reads = []
    async def narrow(*args):
        reads.append(args)
        return dict(proof, collected_at=time.time())
    async def unexpected(*args):
        pytest.fail('A correction must not add a full GitHub collection')
    monkeypatch.setattr(cli, 'Store', lambda: store)
    monkeypatch.setattr(github, 'collect_validation', narrow)
    monkeypatch.setattr(github, 'collect_pr', unexpected)
    monkeypatch.setattr(sys, 'argv', ['caller-pr-gates', 'validation-plan', '--session', 's',
        '--pr', 'https://github.com/Owner/Repo/pull/1', '--cwd', str(root), '--file', str(file),
        '--merged-head', snap['head_sha']])
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert len(reads) == 1 and output['plan']['plan_id'] == row['plan_id']
    assert output['progress']['failed'] == ['native-matched']
    assert store.validation_runs(row['plan_id']) == [failed]


def test_existing_open_pr_input_repair_behavior_is_unchanged(local):
    store, root, _, plan = local
    original, failed, _ = prepare(local, closed=False)
    payload, _ = correction(local, failed)
    with pytest.raises(ValueError, match='require --merged-head'):
        workflow.save_plan(store, 's', 'Owner/Repo', 1, root, payload)
    (root / '.frozen/check.py').write_text(driver(corrected=True))
    revised = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    assert revised['plan_id'] != original['plan_id']
    actual = workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'native-matched')['run']
    assert actual['status'] == 'pass' and actual['exit_code'] == 0
    assert store.validation_runs(original['plan_id']) == [failed]
