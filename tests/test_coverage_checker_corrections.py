"""Keep the existing coverage invocation frozen while repairing its private script.

The dependency-free module fixture checks literal runner controls and executes
real Python assertions. It does not emulate coverage measurement or add coverage
to the tool's runtime; the product owner's installed coverage remains theirs.
"""
from copy import deepcopy
import json
import sys

import pytest

from caller_pr_gates import progress, workflow
from test_checker_corrections import save
from test_post_merge_qualification import merge, run
from test_workflow import local, publish


MODULE = '''from pathlib import Path
import json, runpy, sys
args = sys.argv[1:]
assert args[0] == 'run' and args[1].startswith('--data-file=')
output = args[1].partition('=')[2]
assert output and len(args) >= 3
sys.argv = args[2:]
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
finally:
    Path(output).write_text(json.dumps(args))
'''


def body(*, corrected=False, exit_code=0):
    expected = "os.environ['EXPECTED_SOURCE_HEAD']" if corrected else repr('0' * 40)
    return ("from pathlib import Path\nimport os, subprocess, sys\n"
            "head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
            f"assert head == {expected}, 'retained checker hardcodes an obsolete source head'\n"
            "assert len(head) == 40\n"
            "assert Path('src.txt').read_text() == 'fixed'\n"
            "assert Path('.frozen/dependency.txt').read_text() == 'pinned'\n"
            "assert sys.argv[1:] == ['--case', 'retained']\n"
            "Path('.result').write_text('all retained assertions executed')\n"
            f"raise SystemExit({exit_code})\n")


def prepare_coverage(local, monkeypatch, *, prefix=(), execute=True, original_pass=False):
    store, root, snap, plan = local
    with (root / '.git/info/exclude').open('a') as exclude:
        exclude.write('.frozen/\n.corrections/\n.coverage-proof\n.result\n')
    (root / '.frozen').mkdir()
    (root / '.frozen/original.py').write_text(body(corrected=original_pass))
    (root / '.frozen/dependency.txt').write_text('pinned')
    (root / 'coverage.py').write_text(MODULE)
    (root / 'src.txt').write_text('fixed')
    argv = [*prefix, sys.executable, '-m', 'coverage', 'run',
            '--data-file=' + str(root / '.coverage-proof'), '.frozen/original.py', '--case', 'retained']
    plan.update(checks=[dict(id='profile', argv=argv, cwd='.', reason='Execute every retained recorded-case assertion',
                            input_paths=['.frozen/dependency.txt'], env_names=[])],
                input_paths=['.frozen/original.py'], env_names=['EXPECTED_SOURCE_HEAD'])
    publish(local)
    monkeypatch.setenv('EXPECTED_SOURCE_HEAD', snap['head_sha'])
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    merge(local)
    actual = run(local, 'profile')['run'] if execute else None
    return row, actual, len(prefix) + 5


def corrected(local, failed, index, *, exit_code=0, identifier='profile-current'):
    _, root, _, old = local
    payload = deepcopy(old)
    (root / '.corrections').mkdir(exist_ok=True)
    script, evidence = f'.corrections/{identifier}.py', f'.corrections/{identifier}.txt'
    (root / script).write_text(body(corrected=True, exit_code=exit_code))
    diagnosis = 'Compare the full actual head to the already frozen expected source instead of the obsolete literal.'
    (root / evidence).write_text(diagnosis + ' Preserve every original assertion and all coverage controls.\n')
    check = deepcopy(payload['checks'][0])
    check.update(id=identifier, correction=dict(check_id='profile', run_id=failed['run_id'],
                 diagnosis=diagnosis, evidence_path=evidence))
    check['argv'][index] = script
    check['input_paths'] += [script, evidence]
    payload['checks'].append(check)
    return payload, check


@pytest.mark.parametrize('prefix,exit_code', [([], 0), ([], 29), (['env', 'PROFILE_MODE=retained'], 0)])
def test_coverage_correction_preserves_runner_and_requires_fresh_actual_exit(local, monkeypatch, prefix, exit_code):
    store, root, snap, _ = local
    original, failed, index = prepare_coverage(local, monkeypatch, prefix=prefix)
    assert failed['status'] == 'fail' and failed['exit_code'] == 1 and not (root / '.result').exists()
    payload, check = corrected(local, failed, index, exit_code=exit_code)
    row = save(local, payload)['plan']
    assert row['plan_id'] == original['plan_id'] and row['input_digest'] == original['input_digest']
    assert row['plan']['checks'][0] == original['plan']['checks'][0]
    assert row['plan']['input_paths'] == original['plan']['input_paths']
    assert row['plan']['env_names'] == original['plan']['env_names']
    assert check['argv'][:index] == original['plan']['checks'][0]['argv'][:index]
    assert check['argv'][index + 1:] == original['plan']['checks'][0]['argv'][index + 1:]
    assert store.validation_runs(row['plan_id']) == [failed]
    assert workflow.current_phase(store, row)['failed'] == ['profile']
    with pytest.raises(ValueError, match='already completed'):
        run(local, 'profile')
    result = run(local, check['id']); actual = result['run']
    assert actual['exit_code'] == exit_code and actual['status'] == ('pass' if exit_code == 0 else 'fail')
    assert actual['run_id'] != failed['run_id'] and (root / '.result').read_text() == 'all retained assertions executed'
    assert json.loads((root / '.coverage-proof').read_text()) == check['argv'][index - 2:]
    assert actual['input_digest'] == actual['before_digest'] == actual['after_digest'] == row['plan']['checks'][-1]['input_digest']
    assert store.validation_runs(row['plan_id']) == [failed, actual]
    if exit_code == 0:
        assert result['progress']['corrected_failures'][0]['failed_run_id'] == failed['run_id']
        assert result['progress']['corrected_failures'][0]['passed_run_id'] == actual['run_id']
        assert result['progress']['passed'] == [check['id']] and result['progress']['completion_verified'] is False
    else:
        assert result['progress']['failed'] == ['profile', check['id']]
        assert not result['progress'].get('corrected_failures')
    with store._connect() as db:
        events = [json.loads(r[0]) for r in db.execute("SELECT fields FROM audit WHERE event='validation_checker_correction'")]
        watch = db.execute('SELECT * FROM pr_watch').fetchone()
    assert len(events) == 1 and events[0]['failed_run_id'] == failed['run_id']
    assert events[0]['original_driver'] == '.frozen/original.py' and events[0]['corrected_driver'] == check['argv'][index]
    assert events[0]['original_driver_sha256'] != events[0]['corrected_driver_sha256']
    assert progress.working_state(root)['clean'] and watch['state'] == 'merged' and not store.prs('s')


def test_coverage_correction_refuses_changed_invocation_or_false_fix(local, monkeypatch, subtests):
    store, root, _, _ = local
    row, failed, index = prepare_coverage(local, monkeypatch, prefix=['env', 'PROFILE_MODE=retained'])
    original, _ = corrected(local, failed, index)
    changes = {
        'env value': lambda c: c['argv'].__setitem__(1, 'PROFILE_MODE=other'),
        'env name': lambda c: c['argv'].__setitem__(1, 'OTHER_MODE=retained'),
        'env flag': lambda c: c['argv'].insert(1, '-i'),
        'interpreter': lambda c: c['argv'].__setitem__(2, 'python3'),
        'module flag': lambda c: c['argv'].__setitem__(3, '-c'),
        'module': lambda c: c['argv'].__setitem__(4, 'other_runner'),
        'subcommand': lambda c: c['argv'].__setitem__(5, 'report'),
        'data file': lambda c: c['argv'].__setitem__(6, '--data-file=other.coverage'),
        'extra option': lambda c: c['argv'].insert(6, '--branch'),
        'script option': lambda c: c['argv'].__setitem__(index, '-m'),
        'argument': lambda c: c['argv'].__setitem__(-1, 'other-case'),
        'cwd': lambda c: c.__setitem__('cwd', '.frozen'),
        'reason': lambda c: c.__setitem__('reason', 'Accept a smaller gate'),
        'dependency': lambda c: c['input_paths'].remove('.frozen/dependency.txt'),
        'unpinned driver': lambda c: c['input_paths'].remove(c['argv'][index]),
        'invented failure': lambda c: c['correction'].__setitem__('run_id', 'not-an-actual-run'),
    }
    for name, change in changes.items():
        with subtests.test(change=name):
            payload = deepcopy(original); change(payload['checks'][-1])
            with pytest.raises(ValueError): save(local, payload)
            assert store.validation_plan('s', 'Owner/Repo', 1, root) == row
            assert store.validation_runs(row['plan_id']) == [failed]
    for suffix in ('', '\n# identical program\n'):
        with subtests.test(copied=suffix):
            (root / original['checks'][-1]['argv'][index]).write_text((root / '.frozen/original.py').read_text() + suffix)
            with pytest.raises(ValueError, match='unchanged, copied or comment-only'): save(local, original)
    assert store.validation_runs(row['plan_id']) == [failed]


def test_coverage_correction_preserves_source_shared_inputs_environment_and_owner(local, monkeypatch, subtests):
    store, root, snap, _ = local
    row, failed, index = prepare_coverage(local, monkeypatch)
    payload, check = corrected(local, failed, index)
    bound = save(local, payload)['plan']
    for path in ['src.txt', '.frozen/original.py', '.frozen/dependency.txt', check['argv'][index], check['correction']['evidence_path']]:
        with subtests.test(path=path):
            file = root / path; before = file.read_bytes()
            try:
                file.write_bytes(before + b'\n# changed pinned input\n')
                with pytest.raises(ValueError): run(local, check['id'])
            finally: file.write_bytes(before)
    with monkeypatch.context() as patch:
        patch.setenv('EXPECTED_SOURCE_HEAD', 'f' * 40)
        with pytest.raises(ValueError): run(local, check['id'])
    with pytest.raises(ValueError):
        workflow.run_check(store, 'foreign', 'Owner/Repo', 1, root, check['id'], merged_head=snap['head_sha'])
    assert store.validation_runs(row['plan_id']) == [failed]
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == bound


def test_prior_independent_coverage_pass_cannot_gain_correction_credit(local, monkeypatch):
    store, root, _, _ = local
    row, failed, index = prepare_coverage(local, monkeypatch)
    payload, check = corrected(local, failed, index)
    independent = deepcopy(payload); independent['checks'][-1].pop('correction')
    saved = save(local, independent)['plan']
    actual = run(local, check['id'])['run']
    assert actual['status'] == 'pass' and workflow.current_phase(store, saved)['failed'] == ['profile']
    with pytest.raises(ValueError, match='immutable'): save(local, payload)
    assert store.validation_runs(row['plan_id']) == [failed, actual]


@pytest.mark.parametrize('execute', [False, True])
def test_coverage_correction_does_not_invent_a_finished_failure(local, monkeypatch, execute):
    store, root, _, _ = local
    row, previous, index = prepare_coverage(local, monkeypatch, execute=execute, original_pass=True)
    payload, _ = corrected(local, previous or {'run_id': 'absent-run'}, index)
    with pytest.raises(ValueError, match='actual failed run'): save(local, payload)
    assert store.validation_runs(row['plan_id']) == ([previous] if execute else [])


def test_only_the_literal_coverage_runner_form_is_recognized(subtests):
    good = ['-m', 'coverage', 'run', '--data-file=case.coverage', 'private.py', '--case', 'retained']
    assert progress._python_script_index([sys.executable, *good]) == 5
    forms = [[], ['-m'], ['-m', 'coverage'], ['-m', 'coverage', 'run'],
        ['-m', 'coverage', 'run', 'private.py'],
        ['-m', 'coverage', 'run', '--data-file=case.coverage'],
        ['-m', 'coverage', 'run', '--data-file=', 'private.py'],
        ['-m', 'coverage', 'run', '--data-file', 'case.coverage', 'private.py'],
        ['-m', 'coverage', 'run', '--branch', '--data-file=case.coverage', 'private.py'],
        ['-m', 'coverage', 'run', '--data-file=case.coverage', '-m', 'pytest'],
        ['-m', 'coverage', 'run', '--data-file=case.coverage', '--', 'private.py'],
        ['-m', 'coverage', 'report', '--data-file=case.coverage', 'private.py'],
        ['-m', 'other', 'run', '--data-file=case.coverage', 'private.py'], ['-I', *good]]
    for form in forms:
        with subtests.test(argv=form): assert progress._python_script_index([sys.executable, *form]) is None
    for prefix in [['env'], ['env', '-i'], ['env', '-S', 'MODE=full'], ['/usr/bin/env', 'MODE=full'], ['sh', '-c'], ['uv', 'run']]:
        with subtests.test(prefix=prefix): assert progress._python_script_index([*prefix, sys.executable, *good]) is None


def test_coverage_driver_requires_ordinary_declared_private_input(tmp_path, subtests):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'private.py').write_text('assert 2 + 2 == 4\n')
    (root / 'dependency').write_text('pinned')
    outside = tmp_path / 'outside.py'; outside.write_text('assert True\n')
    (root / 'linked.py').symlink_to(outside)
    for driver in ['private.py', 'linked.py', str(outside)]:
        with subtests.test(driver=driver):
            check = dict(id='profile', argv=[sys.executable, '-m', 'coverage', 'run', '--data-file=out', driver],
                         reason='Run the full private assertion', input_paths=['dependency'])
            with pytest.raises(ValueError): progress.normalize_plan({'checks': [check]}, root)
    check['argv'][-1] = 'private.py'
    accepted = progress.normalize_plan({'checks': [check], 'input_paths': ['private.py']}, root)
    assert accepted['checks'][0]['input_paths'] == ['dependency']  # Retain an existing shared driver pin as-is.
