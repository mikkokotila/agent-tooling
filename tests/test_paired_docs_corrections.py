"""Admission integrity is separate from original-owner documentation execution."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shlex
import sys
import time

import pytest

from caller_pr_gates import progress, workflow
from test_post_merge_qualification import merge
from test_workflow import git, local, publish


@pytest.fixture
def paired(local, monkeypatch):
    store, root, snap, plan = local
    monkeypatch.setenv('LIMEN_PYTHON', sys.executable)
    monkeypatch.setenv('POLARS_MAX_THREADS', '1')
    with (root / '.git/info/exclude').open('a') as target:
        target.write('.corrections/\n.frozen/\n')
    (root / 'docs/examples').mkdir(parents=True)
    (root / 'docs-site').mkdir()
    (root / 'docs/examples/provenance.py').write_text('raise SystemExit(125)\n')
    plan.update(checks=[
        dict(id='provenance', argv=[sys.executable, 'docs/examples/provenance.py', '--output', '.docs-evidence/source.json'],
             cwd='.', reason='Acquire all immutable candidate source evidence'),
        dict(id='book', argv=[sys.executable, '-m', 'pytest', '--import-mode=importlib', 'docs/examples/tests', '-q', '-x', '--tb=short'],
             cwd='.', reason='Run every original recorded book scenario and candidate wheel'),
        dict(id='reader', argv=['npm', 'run', 'check'], cwd='docs-site', reason='Qualify the existing complete reader')],
        env_names=['LIMEN_PYTHON', 'POLARS_MAX_THREADS'])
    publish(local)
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    failed = workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'provenance')['run']
    assert failed['status'] == 'fail' and failed['exit_code'] == 125
    original_head = git(root, 'rev-parse', 'HEAD')
    # These isolated source files must never execute during admission. Real
    # research proof remains required from the actual product consumers.
    bodies = {'retained_checker.py': b"raise AssertionError('admission executed checker')\n",
              'provenance.py': b"raise AssertionError('admission executed provenance')\n"}
    for name, body in bodies.items():
        (root / 'docs/examples' / name).write_bytes(body)
    git(root, 'add', 'docs/examples')
    git(root, 'commit', '-qm', 'Recorded target checker source')
    source = git(root, 'rev-parse', 'HEAD')
    git(root, 'checkout', '--detach', original_head)
    proof = merge(local)
    proof.update(base_sha=source, base_sha_after=source, collected_at=time.time())
    store.set_preflight('Owner/Repo', 1, proof)
    controls = root / '.corrections/docs'
    controls.mkdir(parents=True)
    for name, body in bodies.items():
        (controls / name).write_bytes(body)
    evidence = controls / 'diagnosis.txt'
    evidence.write_text(f"Original run {failed['run_id']} failed with125. Use the committed target checker pair; retain every original scenario and argument.\n")
    return dict(local=local, store=store, root=root, row=row, failed=failed, source=source,
                bodies=bodies, controls=controls, evidence=evidence, proof=proof)


def candidate(data, identifier):
    payload = deepcopy(data['local'][3])
    old = next(c for c in payload['checks'] if c['id'] == identifier)
    paths = data['controls']; root = data['root']
    pair = {'source_commit': data['source'], 'checker': str((paths / 'retained_checker.py').relative_to(root)),
            'provenance': str((paths / 'provenance.py').relative_to(root))}
    check = {**deepcopy(old), 'id': identifier + '-paired',
             'input_paths': [pair['checker'], pair['provenance'], str(data['evidence'].relative_to(root))],
             'correction': dict(check_id=identifier, diagnosis='Use the committed reviewed checker without changing original inputs',
                 evidence_path=str(data['evidence'].relative_to(root)), retained_docs=pair,
                 **({'run_id': data['failed']['run_id']} if identifier == 'provenance' else {'unexecuted': True}))}
    if identifier == 'reader':
        launcher = paths / 'python'
        launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(paths / 'retained_checker.py')) + ' "$@"\n')
        launcher.chmod(0o700)
        pair['launcher'] = str(launcher.relative_to(root))
        check['input_paths'].append(pair['launcher'])
        check['argv'] = ['env', 'LIMEN_PYTHON=' + str(launcher), *old['argv']]
    else:
        check['argv'] = [sys.executable, str(paths / 'retained_checker.py'), *old['argv'][1:]]
    payload['checks'].append(check)
    return payload, check


def save(data, payload):
    store, root, snap, _ = data['local']
    return workflow.save_plan(store, 's', 'Owner/Repo', 1, root, payload, merged_head=snap['head_sha'])


@pytest.mark.parametrize('identifier', ['provenance', 'book', 'reader'])
def test_exact_pair_admission_retains_failed_and_unexecuted_obligations(paired, identifier):
    data = paired; store = data['store']; row = data['row']
    before = store.validation_runs(row['plan_id'])
    payload, check = candidate(data, identifier)
    admitted = save(data, payload)['plan']
    assert admitted['plan_id'] == row['plan_id'] and admitted['input_digest'] == row['input_digest']
    assert admitted['plan']['checks'][:3] == row['plan']['checks']
    assert admitted['plan']['env_names'] == row['plan']['env_names']
    assert store.validation_runs(row['plan_id']) == before == [data['failed']]
    phase = workflow.current_phase(store, admitted)
    assert phase['phase'] == 'VALIDATE_FAILED' and not phase.get('local_checks_passed')
    assert not phase.get('corrected_failures')
    with store._connect() as db:
        receipts = [json.loads(r[0]) for r in db.execute("SELECT fields FROM audit WHERE event='validation_checker_correction'")]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt['checker_source_commit'] == receipt['checker_source_tip'] == data['source']
    assert receipt['original_interpreter'] == sys.executable
    assert receipt['checker_sha256'] == hashlib.sha256(data['bodies']['retained_checker.py']).hexdigest()
    assert receipt['provenance_sha256'] == hashlib.sha256(data['bodies']['provenance.py']).hexdigest()
    assert ('failed_run_id' in receipt) is (identifier == 'provenance')
    assert ('unexecuted' in receipt) is (identifier != 'provenance')


@pytest.mark.parametrize('identifier', ['provenance', 'book', 'reader'])
def test_pair_refuses_changed_identity_invocation_controls_and_pins(paired, identifier, subtests):
    data = paired; store = data['store']; root = data['root']; row = data['row']
    original, _ = candidate(data, identifier)
    before = store.validation_runs(row['plan_id'])
    changes = ['source', 'target', 'pair', 'checker', 'interpreter', 'argument', 'cwd', 'reason', 'pin', 'symlink', 'evidence', 'global']
    if identifier == 'reader': changes += ['launcher', 'launcher-mode', 'launcher-evidence', 'environment-name']
    for change in changes:
        with subtests.test(change=change):
            payload = deepcopy(original); check = payload['checks'][-1]; pair = check['correction']['retained_docs']
            private = {p: (p.read_bytes(), p.stat().st_mode & 0o777) for p in data['controls'].iterdir()}
            try:
                if change == 'source': pair['source_commit'] = data['local'][2]['head_sha']
                elif change == 'target':
                    store.set_preflight('Owner/Repo', 1, dict(data['proof'], base_sha_after=data['local'][2]['head_sha']))
                elif change == 'pair': (data['controls'] / 'provenance.py').write_text('changed dependency\n')
                elif change == 'checker': (data['controls'] / 'retained_checker.py').write_text('changed checker\n')
                elif change == 'interpreter':
                    if identifier == 'reader':
                        launcher = data['controls'] / 'python'; launcher.write_text(launcher.read_text().replace(sys.executable, '/other/python'))
                    else: check['argv'][0] = '/other/python'
                elif change == 'argument': check['argv'].append('--skip-candidate')
                elif change == 'cwd': check['cwd'] = 'docs/examples'
                elif change == 'reason': check['reason'] = 'Only require a baseline'
                elif change == 'pin': check['input_paths'].remove(pair['provenance'])
                elif change == 'symlink':
                    path = data['controls'] / 'provenance.py'; path.unlink(); path.symlink_to(data['evidence'])
                elif change == 'evidence': data['evidence'].write_text('')
                elif change == 'global':
                    with pytest.MonkeyPatch.context() as env:
                        env.setenv('POLARS_MAX_THREADS', '2')
                        with pytest.raises(ValueError): save(data, payload)
                    continue
                elif change == 'launcher': (data['controls'] / 'python').write_text('#!/bin/sh\nexit 0\n')
                elif change == 'launcher-mode': (data['controls'] / 'python').chmod(0o777)
                elif change == 'launcher-evidence': check['correction']['evidence_path'] = pair['launcher']
                else: check['argv'][1] = check['argv'][1].replace('LIMEN_PYTHON=', 'OTHER_PYTHON=')
                with pytest.raises(ValueError): save(data, payload)
                assert store.validation_plan('s', 'Owner/Repo', 1, root) == row
                assert store.validation_runs(row['plan_id']) == before
            finally:
                for path, (body, mode) in private.items():
                    if path.is_symlink(): path.unlink()
                    path.write_bytes(body); path.chmod(mode)
                store.set_preflight('Owner/Repo', 1, data['proof'])


def test_pair_requires_current_normal_target_proof(paired, subtests):
    data = paired; store = data['store']; payload, _ = candidate(data, 'book')
    for patch in ({'collected_at': time.time() - 1000}, {'profile': 'full'}, {'collected_at': True},
                  {'branch_error': 'target unavailable'}, {'base_sha': None}):
        with subtests.test(patch=patch):
            store.set_preflight('Owner/Repo', 1, dict(data['proof'], **patch))
            with pytest.raises(ValueError): save(data, payload)
            assert store.validation_runs(data['row']['plan_id']) == [data['failed']]
    store.set_preflight('Owner/Repo', 1, data['proof'])
