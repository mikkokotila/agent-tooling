"""Literal env assignments remain frozen while a real private checker is repaired."""
from copy import deepcopy
import json
import subprocess
import sys
import time

import pytest

from caller_pr_gates import cli, github, progress, workflow
from test_checker_corrections import correction, driver, prepare, save
from test_post_merge_qualification import merge, run
from test_workflow import local, publish


PREFIX = ['env', 'PYTHONPATH=.frozen', 'POLARS_MAX_THREADS=2']
SCRIPT_INDEX = 4


def corrected_body(*, exit_code=0, arguments=None):
    arguments = ['--mode', 'compare'] if arguments is None else arguments
    return ("import os\nassert os.environ['PYTHONPATH'] == '.frozen'\n"
            "assert os.environ['POLARS_MAX_THREADS'] == '2'\n" +
            driver(corrected=True, suffix=(
                f"assert sys.argv[1:] == {arguments!r}\n"
                f"raise SystemExit({exit_code})\n")))


@pytest.fixture
def env_failure(local, monkeypatch):
    monkeypatch.setenv('POLARS_MAX_THREADS', '9')
    monkeypatch.setenv('PINNED_CORRECTION_ENV', 'original')
    local[3]['env_names'] = ['PINNED_CORRECTION_ENV']
    row, failed, proof = prepare(local, prefix=PREFIX)
    assert failed['status'] == 'fail' and failed['exit_code'] == 125
    return row, failed, proof


@pytest.mark.parametrize('exit_code', [0, 27])
def test_literal_env_correction_needs_its_own_actual_exit(local, env_failure, exit_code):
    store, root, _, _ = local
    original, failed, _ = env_failure
    run(local, 'remaining')
    history = store.validation_runs(original['plan_id'])
    payload, check = correction(local, failed, script_index=SCRIPT_INDEX,
                                body=corrected_body(exit_code=exit_code))
    row = save(local, payload)['plan']
    assert row['plan_id'] == original['plan_id'] and row['input_digest'] == original['input_digest']
    assert row['plan']['checks'][:2] == original['plan']['checks']
    for name in ['input_paths', 'env_names']:
        assert row['plan'][name] == original['plan'][name]
    assert check['argv'][:SCRIPT_INDEX] == [*PREFIX, sys.executable]
    assert check['argv'][SCRIPT_INDEX + 1:] == ['--mode', 'compare']
    assert workflow.current_phase(store, row)['failed'] == ['native-matched']
    assert store.validation_runs(row['plan_id']) == history
    with pytest.raises(ValueError, match='already completed'):
        run(local, 'native-matched')
    result = run(local, check['id'])
    actual = result['run']
    assert actual['exit_code'] == exit_code and actual['status'] == ('pass' if exit_code == 0 else 'fail')
    assert actual['run_id'] != failed['run_id'] and (root / '.results').read_text() == '2'
    assert actual['input_digest'] == actual['before_digest'] == actual['after_digest'] == row['plan']['checks'][-1]['input_digest']
    assert store.validation_runs(row['plan_id']) == [*history, actual]
    assert store.validation_evidence(row['plan_id']) == [*history, actual]
    if exit_code == 0:
        assert result['progress']['corrected_failures'] == [dict(check_id='native-matched',
            failed_run_id=failed['run_id'], exit_code=125, corrected_by=check['id'],
            passed_check_id=check['id'], passed_run_id=actual['run_id'])]
        assert 'native-matched' not in result['progress']['passed']
        assert result['progress']['local_checks_passed'] is True
        assert result['progress']['completion_verified'] is False
    else:
        assert result['progress']['failed'] == ['native-matched', check['id']]
        assert not result['progress'].get('corrected_failures')
    with store._connect() as db:
        receipts = [json.loads(r['fields']) for r in db.execute(
            "SELECT fields FROM audit WHERE event='validation_checker_correction'")]
    assert len(receipts) == 1 and receipts[0]['failed_run_id'] == failed['run_id']
    assert receipts[0]['original_driver'] == '.frozen/check.py'
    assert receipts[0]['corrected_driver'] == check['argv'][SCRIPT_INDEX]
    assert receipts[0]['original_driver_sha256'] != receipts[0]['corrected_driver_sha256']


def test_literal_env_correction_preserves_every_other_invocation_field(local, env_failure, subtests):
    store, root, _, _ = local
    row, failed, _ = env_failure
    original, _ = correction(local, failed, script_index=SCRIPT_INDEX, body=corrected_body())
    changes = {
        'assignment value': lambda c: c['argv'].__setitem__(2, 'POLARS_MAX_THREADS=1'),
        'assignment name': lambda c: c['argv'].__setitem__(2, 'OTHER_THREADS=2'),
        'assignment order': lambda c: c['argv'].__setitem__(slice(1, 3), c['argv'][1:3][::-1]),
        'extra assignment': lambda c: c['argv'].insert(1, 'EXTRA=1'),
        'removed assignment': lambda c: c['argv'].pop(1),
        'removed env': lambda c: c['argv'].__delitem__(slice(0, 3)),
        'env flag': lambda c: c['argv'].insert(1, '-i'),
        'env executable': lambda c: c['argv'].__setitem__(0, '/usr/bin/env'),
        'interpreter': lambda c: c['argv'].__setitem__(3, 'python3'),
        'python option': lambda c: c['argv'].__setitem__(SCRIPT_INDEX, '-m'),
        'trailing argument': lambda c: c['argv'].__setitem__(-1, 'skip-candidate'),
        'cwd': lambda c: c.__setitem__('cwd', '.frozen'),
        'acceptance reason': lambda c: c.__setitem__('reason', 'Only require the baseline'),
        'fabricated failure': lambda c: c['correction'].__setitem__('run_id', '00000000-0000-4000-8000-000000000001'),
        'unpinned driver': lambda c: c['input_paths'].remove(c['argv'][SCRIPT_INDEX]),
    }
    for name, change in changes.items():
        with subtests.test(change=name):
            payload = deepcopy(original)
            change(payload['checks'][-1])
            with pytest.raises(ValueError):
                save(local, payload)
            assert store.validation_plan('s', 'Owner/Repo', 1, root) == row
            assert store.validation_runs(row['plan_id']) == [failed]


@pytest.mark.parametrize('option', ['-c', '-e', '--eval', '--print'])
def test_env_script_options_remain_literal_through_fingerprinting_and_correction(local, option):
    store, root, _, _ = local
    arguments = [option, 'config.json']
    original, failed, _ = prepare(local, prefix=PREFIX, arguments=arguments)
    assert failed['status'] == 'fail' and failed['exit_code'] == 125
    assert progress.fingerprint(root, original['plan']) == original['input_digest']
    payload, check = correction(local, failed, script_index=SCRIPT_INDEX,
                                body=corrected_body(arguments=arguments))
    row = save(local, payload)['plan']
    assert row['input_digest'] == original['input_digest']
    assert row['plan']['checks'][:2] == original['plan']['checks']
    assert row['plan']['checks'][-1]['argv'][SCRIPT_INDEX + 1:] == arguments
    assert store.validation_runs(row['plan_id']) == [failed]
    actual = run(local, check['id'])
    assert actual['run']['status'] == 'pass' and actual['run']['exit_code'] == 0
    assert actual['run']['before_digest'] == actual['run']['after_digest']
    assert actual['progress']['corrected_failures'][0]['failed_run_id'] == failed['run_id']
    assert actual['progress']['pending'] == ['remaining']
    assert store.validation_runs(row['plan_id']) == [failed, actual['run']]


@pytest.mark.parametrize('prefix', [[], PREFIX])
@pytest.mark.parametrize('option', ['-c', '-e', '--eval', '--print'])
def test_actual_inline_programs_stay_refused(tmp_path, prefix, option):
    argv = [*prefix, sys.executable, option, 'print(42)']
    with pytest.raises(ValueError):
        progress.normalize_plan({'checks': [dict(id='inline', argv=argv,
            reason='An inline program is not a private checker')]}, tmp_path)


def test_existing_direct_python_option_guard_is_unchanged(tmp_path):
    (tmp_path / 'check.py').write_text('assert True\n')
    with pytest.raises(ValueError, match='inline programs'):
        progress.normalize_plan({'checks': [dict(id='direct',
            argv=[sys.executable, 'check.py', '-c', 'config.json'],
            reason='Preserve the existing direct-runner guard')]}, tmp_path)


@pytest.mark.parametrize('shared_kind', ['file', 'directory'])
def test_frozen_env_driver_shared_pin_needs_no_declaration_rewrite(local, monkeypatch, subtests, shared_kind):
    store, root, _, plan = local
    monkeypatch.setenv('CHECKER_DEPENDENCY', 'original')
    with (root / '.git/info/exclude').open('a') as target:
        target.write('.frozen/\n.corrections/\n.results\n')
    (root / '.frozen').mkdir()
    (root / '.frozen/dependency.txt').write_text('pinned')
    (root / '.frozen/extra.txt').write_text('extra dependency')
    extra_assertion = "from pathlib import Path\nassert Path('.frozen/extra.txt').read_text() == 'extra dependency'\n"
    (root / '.frozen/check.py').write_text(extra_assertion + driver())
    (root / 'src.txt').write_text('fixed')
    plan['input_paths'] = (['.frozen/check.py', '.frozen/dependency.txt'] if shared_kind == 'file' else ['.frozen'])
    publish(local)
    baseline = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    merge(local)
    payload = deepcopy(plan)
    original_check = dict(id='env-original', argv=[*PREFIX, sys.executable, '.frozen/check.py', '--mode', 'compare'],
        reason='Execute both isolated trials with all pinned producer assertions',
        input_paths=['.frozen/extra.txt'], env_names=['CHECKER_DEPENDENCY'])
    payload['checks'].append(original_check)
    retained = save(local, payload)['plan']
    assert retained['plan_id'] == baseline['plan_id'] and retained['input_digest'] == baseline['input_digest']
    normalized = progress.normalize_plan(retained['plan'], root, stored=True)
    assert normalized == retained['plan']
    assert normalized['checks'][-1]['input_paths'] == ['.frozen/extra.txt']
    assert normalized['input_paths'] == baseline['plan']['input_paths']
    assert progress.fingerprint(root, retained['plan']) == baseline['input_digest']
    assert progress.check_fingerprint(root, retained['plan'], original_check) == retained['plan']['checks'][-1]['input_digest']
    failed = run(local, original_check['id'])['run']
    assert failed['status'] == 'fail' and failed['exit_code'] == 125
    revised, check = correction(local, failed, payload=payload, identifier='env-corrected',
        script_index=SCRIPT_INDEX, body=extra_assertion + corrected_body())
    for change in ['missing new driver pin', 'new driver moved to shared pins']:
        with subtests.test(change=change):
            invalid = deepcopy(revised)
            invalid['checks'][-1]['input_paths'].remove(check['argv'][SCRIPT_INDEX])
            if change == 'new driver moved to shared pins':
                invalid['input_paths'].append(check['argv'][SCRIPT_INDEX])
            with pytest.raises(ValueError):
                save(local, invalid)
            assert store.validation_plan('s', 'Owner/Repo', 1, root) == retained
            assert store.validation_runs(retained['plan_id']) == [failed]
    row = save(local, revised)['plan']
    assert row['input_digest'] == baseline['input_digest']
    assert row['plan']['checks'][:2] == retained['plan']['checks']
    assert row['plan']['input_paths'] == retained['plan']['input_paths']
    dependency = root / '.frozen/extra.txt'
    dependency.write_text('changed dependency')
    try:
        with pytest.raises(ValueError):
            run(local, check['id'])
    finally:
        dependency.write_text('extra dependency')
    with monkeypatch.context() as context:
        context.setenv('CHECKER_DEPENDENCY', 'changed')
        with pytest.raises(ValueError):
            run(local, check['id'])
    assert store.validation_runs(row['plan_id']) == [failed]
    actual = run(local, check['id'])
    assert actual['run']['status'] == 'pass' and actual['run']['exit_code'] == 0
    assert actual['run']['before_digest'] == actual['run']['after_digest']
    assert actual['progress']['corrected_failures'][0]['failed_run_id'] == failed['run_id']
    assert actual['progress']['pending'] == ['regression']
    assert store.validation_runs(row['plan_id']) == [failed, actual['run']]
    assert row['plan']['checks'][1] == retained['plan']['checks'][1]


@pytest.mark.parametrize('shared', [[], ['other.txt'], ['.'], ['../outside.txt']])
def test_env_driver_still_needs_a_valid_shared_or_per_check_pin(tmp_path, shared):
    (tmp_path / 'check.py').write_text('assert True\n')
    (tmp_path / 'dependency.txt').write_text('pinned')
    (tmp_path / 'other.txt').write_text('unrelated')
    with pytest.raises(ValueError):
        progress.normalize_plan({'checks': [dict(id='control',
            argv=[*PREFIX, sys.executable, 'check.py'], reason='Run all assertions',
            input_paths=['dependency.txt'])], 'input_paths': shared}, tmp_path)


@pytest.mark.parametrize('prefix', [
    ['env'], ['env', '-i'], ['env', '--ignore-environment', 'MODE=full'],
    ['env', '-S', 'MODE=full'], ['env', '--', 'MODE=full'],
    ['env', 'MODE=full', '-u', 'OTHER'], ['env', 'MODE=full', '--'],
    ['env', '=empty'], ['env', '1MODE=full'], ['env', 'BAD-NAME=full'],
    ['env', 'MODE'], ['env', 'MODE=full', 'env', 'OTHER=1'],
    ['env', 'MODE=full', 'sh', '-c'], ['env', 'MODE=full', 'uv', 'run'],
    ['/usr/bin/env', 'MODE=full'], ['sh', '-c'], ['nice'],
])
def test_correction_does_not_recognize_env_flags_or_other_wrappers(prefix):
    assert progress._python_script_index([*prefix, sys.executable, 'private.py', '--mode', 'compare']) is None


def test_literal_env_values_are_not_split_or_expanded(tmp_path):
    (tmp_path / 'private.py').write_text('assert 2 + 2 == 4\n')
    argv = ['env', 'EMPTY=', 'VALUE=one two=$HOME', sys.executable, 'private.py']
    plan = progress.normalize_plan({'checks': [dict(id='control', argv=argv,
        reason='Execute the declared private assertion', input_paths=['private.py'])]}, tmp_path)
    assert plan['checks'][0]['argv'] == argv
    # The same underlying driver must be pinned even though argv[0] is env.
    with pytest.raises(ValueError, match='Include the local qualification driver'):
        progress.normalize_plan({'checks': [dict(id='control', argv=argv,
            reason='Execute the declared private assertion', input_paths=[])]}, tmp_path)


def test_env_driver_pin_still_refuses_symlink_and_outside_checkout(tmp_path):
    root = tmp_path / 'checkout'
    root.mkdir()
    external = tmp_path / 'check_external.py'
    external.write_text('assert True\n')
    (root / 'check_link.py').symlink_to(external)
    for script in ['check_link.py', str(external)]:
        with pytest.raises(ValueError):
            progress.normalize_plan({'checks': [dict(id='control',
                argv=[*PREFIX, sys.executable, script], reason='Run the complete assertion',
                input_paths=['missing.txt'])]}, root)


def test_env_correction_still_rejects_copied_or_unchanged_drivers(local, env_failure, subtests):
    store, root, _, _ = local
    row, failed, _ = env_failure
    original_body = (root / '.frozen/check.py').read_text()
    for change in ['copied', 'comment-only', 'same-file']:
        with subtests.test(change=change):
            payload, check = correction(local, failed, script_index=SCRIPT_INDEX,
                body=original_body + ('# No program change\n' if change == 'comment-only' else ''))
            if change == 'same-file':
                check['argv'][SCRIPT_INDEX] = '.frozen/check.py'
                check['input_paths'].append('.frozen/check.py')
            with pytest.raises(ValueError):
                save(local, payload)
            assert store.validation_plan('s', 'Owner/Repo', 1, root) == row
            assert store.validation_runs(row['plan_id']) == [failed]


@pytest.mark.parametrize('outcome', ['pass', 'unknown'])
def test_env_correction_still_requires_an_actual_finished_failure(local, outcome):
    store, _, _, _ = local
    row, previous, _ = prepare(local, prefix=PREFIX, outcome=outcome)
    assert previous['status'] == outcome
    payload, _ = correction(local, previous, script_index=SCRIPT_INDEX)
    with pytest.raises(ValueError, match='actual failed run'):
        save(local, payload)
    assert store.validation_runs(row['plan_id']) == [previous]


def test_env_correction_keeps_source_dependency_environment_and_owner_guards(local, env_failure, monkeypatch, subtests):
    store, root, snap, _ = local
    original, failed, _ = env_failure
    payload, check = correction(local, failed, script_index=SCRIPT_INDEX, body=corrected_body())
    row = save(local, payload)['plan']
    for path in ['src.txt', '.frozen/dependency.txt', check['argv'][SCRIPT_INDEX]]:
        with subtests.test(changed_path=path):
            file = root / path
            before = file.read_bytes()
            try:
                file.write_bytes(before + b'\n# changed after pinning\n')
                with pytest.raises(ValueError):
                    run(local, check['id'])
            finally:
                file.write_bytes(before)
    with monkeypatch.context() as context:
        context.setenv('PINNED_CORRECTION_ENV', 'changed')
        with pytest.raises(ValueError):
            run(local, check['id'])
    store.record_session('foreign', 'codex', str(root))
    with pytest.raises(ValueError):
        workflow.run_check(store, 'foreign', 'Owner/Repo', 1, root, check['id'], merged_head=snap['head_sha'])
    assert store.validation_runs(original['plan_id']) == [failed]
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == row


def test_existing_cli_and_real_background_worker_execute_the_frozen_env_correction(local, env_failure, monkeypatch, tmp_path, capsys):
    store, root, snap, _ = local
    row, failed, proof = env_failure
    payload, check = correction(local, failed, script_index=SCRIPT_INDEX, body=corrected_body())
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
    common = ['--session', 's', '--pr', 'https://github.com/Owner/Repo/pull/1',
              '--cwd', str(root), '--merged-head', snap['head_sha']]
    monkeypatch.setattr(sys, 'argv', ['caller-pr-gates', 'validation-plan', *common, '--file', str(file)])
    cli.main()
    admitted = json.loads(capsys.readouterr().out)
    assert len(reads) == 1 and admitted['plan']['plan_id'] == row['plan_id']
    assert admitted['progress']['failed'] == ['native-matched']
    assert store.validation_runs(row['plan_id']) == [failed]
    bootstrap = ('import json, time\nfrom caller_pr_gates import github, cli\n'
                 f'remote = json.loads({json.dumps(proof)!r})\n'
                 'async def collect(*args):\n    return dict(remote, collected_at=time.time())\n'
                 'github.collect_validation = collect\ncli.main()\n')
    original, workers = subprocess.Popen, []
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
    monkeypatch.setattr(sys, 'argv', ['caller-pr-gates', 'validate', *common, '--check', check['id'], '--background'])
    cli.main()
    launched = json.loads(capsys.readouterr().out)
    assert launched['completion_verified'] is False and len(reads) == 2
    assert workers[0].wait(timeout=10) == 0
    history = store.validation_runs(row['plan_id'])
    assert len(history) == 2 and history[0] == failed
    assert history[-1]['status'] == 'pass' and history[-1]['exit_code'] == 0
    assert history[-1]['before_digest'] == history[-1]['after_digest']
    current = workflow.current_phase(store, store.validation_plan('s', 'Owner/Repo', 1, root))
    assert current['corrected_failures'][0]['passed_run_id'] == history[-1]['run_id']
    assert current['pending'] == ['remaining']
