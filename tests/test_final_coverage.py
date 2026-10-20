"""Final scope coverage is an audited conclusion, never a copied execution result."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from caller_pr_gates import cli, closure, github, monitor, progress, workflow
from caller_pr_gates.policy import evaluate_pr
from caller_pr_gates.store import Store
from test_policy import fresh_branch, closing_link
from test_workflow import git


REPO = 'Owner/Repo'
CRITERION = 'Unit and end-to-end coverage, docs, npm run check and npm run test:e2e pass.'
SCOPE = dict(agreed_scope='Deliver the feature and qualify its full repository gates',
             acceptance_criteria=[CRITERION], slice_count=1, slice_ids=['feature'], approved_at=100,
             approval_evidence='Implement and qualify the agreed feature')


def declarations(prefix):
    return {'checks': [
        dict(id='check', argv=[*prefix, 'npm', 'run', 'check'], reason='Full application qualification', cwd='.'),
        dict(id='browser', argv=[*prefix, 'npm', 'run', 'test:e2e', '--', '--config=private/browser.mjs', '--workers=2'],
             reason='Full browser qualification', cwd='.')], 'input_paths': ['private/browser.mjs'], 'env_names': []}


def final_declarations(prefix):
    return {'checks': [
        dict(id='check', argv=[*prefix, 'VITEST_MAX_WORKERS=1', 'npm', 'run', 'check'],
             reason='Full application gate with native worker limit', cwd='.'),
        dict(id='browser', argv=[*prefix, 'POISE_E2E_PORT=34567', 'npm', 'run', 'test:e2e', '--', '--workers=1'],
             reason='Full official browser gate on an isolated native port', cwd='.')],
        'input_paths': [], 'env_names': ['VITEST_MAX_WORKERS']}


def declaration(original, final):
    return {'original_plan_id': original['plan_id'], 'final_plan_id': final['plan_id'],
        'scope_reason': 'The delivered successor repairs the final probe and qualifies the original full-gate obligation.',
        'checks': {'check': 'Full unchanged application script on the delivered final source.',
                   'browser': 'Full official browser script on final source; no filtered or retried subset.'},
        'criteria': ['The final delivered feature has actual full application and browser proofs; original outcomes are retained.'],
        'changes': {'checks/check/argv': 'Native worker limit only; literal existing environment retained.',
                    'checks/check/reason': 'Explain the same full gate with its native execution control.',
                    'checks/browser/argv': 'Use the current full official gate, native isolated port and one worker.',
                    'checks/browser/reason': 'Explain the current full official gate and controls.',
                    'input_paths': 'Replace the historical private configuration with the current official full gate; historical bytes remain unknown.',
                    'env_names': 'Pin the native worker environment used by the final plan.'}}


def snapshot(number, head, *, merged=False, merge=None):
    stamp = '2026-10-20T10:00:00Z'
    result = dict(repository=REPO, number=number, state='closed' if merged else 'open', merged=merged,
        draft=False, mergeable=True, collected_at=time.time(), head_sha=head, head_sha_after=head,
        head_ref='feature', head_repository=REPO, base_repository=REPO, head_repository_id=1, base_repository_id=1,
        merge_commit_sha=merge, merged_at=stamp if merged else None,
        actor_login='agent', author_login='agent', comments=[], commit_shas=[head], commit_dates={},
        checks=[dict(id='1', kind='check', name='full', status='completed', conclusion='success', head_sha=head)],
        expected_checks=['full'], **fresh_branch(head, head), **closing_link(head, REPO, number + 10, 'CLOSED' if merged else 'OPEN'))
    result['submitted_reviews'] = [dict(id='5', state='APPROVED', commit_sha=head, author_login='reviewer',
        author_type='User', submitted_at=stamp, updated_at=stamp, url=f'https://github.com/{REPO}/pull/{number}#pullrequestreview-5')]
    return result


@pytest.fixture
def delivered(tmp_path, monkeypatch, request):
    common = tmp_path / 'common'
    common.mkdir()
    git(common, 'init', '-q')
    with (common / '.git/config').open('a') as config:
        config.write('[user]\n\tname = Gate test\n\temail = gate@example.invalid\n'
                     '[remote "origin"]\n\turl = https://github.com/Owner/Repo.git\n')
    (common / '.gitignore').write_text('private/\nresults/\n__pycache__/\n')
    (common / 'src.txt').write_text('base')
    (common / 'gate.py').write_text("from pathlib import Path\nimport sys\nassert Path('src.txt').read_text() == 'fixed'\n"
                                  "Path('results').mkdir(exist_ok=True)\nPath('results/' + sys.argv[1]).write_text('actual child passed')\n")
    scripts = {name: sys.executable + ' gate.py ' + name for name in ('check', 'test:e2e')}
    (common / 'package.json').write_text(json.dumps({'scripts': scripts}))
    git(common, 'add', '.')
    git(common, 'commit', '-qm', 'Initial')
    base = git(common, 'rev-parse', 'HEAD')
    original, final = tmp_path / 'original', tmp_path / 'final'
    git(common, 'worktree', 'add', '-qb', 'original', str(original), base)
    (original / 'src.txt').write_text('old')
    git(original, 'commit', '-qam', 'Feature before its genuine repair')
    if getattr(request, 'param', None) == 'tracked_config':
        (original / 'private').mkdir()
        (original / 'private/browser.mjs').write_text('historical private configuration bytes')
        git(original, 'add', '-f', 'private/browser.mjs')
        git(original, 'commit', '-qm', 'Tracked configuration is a source obligation')
    old_head = git(original, 'rev-parse', 'HEAD')
    old_tree = git(original, 'rev-parse', 'HEAD^{tree}')
    # An actual squash-like merge: the published branch head is not an ancestor.
    old_merge = git(common, 'commit-tree', old_tree, '-p', base, '-m', 'Delivered predecessor')
    bin_path = tmp_path / 'bin'
    bin_path.mkdir()
    npm = bin_path / 'npm'
    npm.write_text('#!' + sys.executable + '\nimport json,shlex,subprocess,sys\n'
                   "assert sys.argv[1] == 'run'\nscript=sys.argv[2]\n"
                   "argv=shlex.split(json.load(open('package.json'))['scripts'][script])\n"
                   'sys.exit(subprocess.run(argv).returncode)\n')
    npm.chmod(0o755)
    prefix = ['/usr/bin/env', 'PATH=' + str(bin_path) + os.pathsep + os.environ['PATH']]
    monkeypatch.setenv('VITEST_MAX_WORKERS', '1')
    store = Store(tmp_path / 'state')
    store.record_session('s', 'codex', str(original))
    store.record_scope('s', SCOPE)
    store.register('s', 'codex', REPO, 1, original)
    old_live = snapshot(1, old_head)
    store.set_snapshot(REPO, 1, old_live, evaluate_pr(old_live, []))
    (original / 'private').mkdir(exist_ok=True)
    (original / 'private/browser.mjs').write_text('historical private configuration bytes')
    old_row = workflow.save_plan(store, 's', REPO, 1, original, declarations(prefix))['plan']
    assert workflow.run_check(store, 's', REPO, 1, original, 'check')['run']['status'] == 'fail'
    old_closed = snapshot(1, old_head, merged=True, merge=old_merge)
    monitor.retire(store, REPO, 1, dict(state='closed', merged=True, head={'sha': old_head}), checklist=True)
    git(common, 'worktree', 'add', '-qb', 'final', str(final), old_merge)
    (final / 'src.txt').write_text('fixed')
    git(final, 'commit', '-qam', 'Actual final feature and proof repair')
    head = git(final, 'rev-parse', 'HEAD')
    if getattr(request, 'param', None) == 'hidden_source':
        git(final, 'update-index', '--assume-unchanged', 'gate.py')
        (final / 'gate.py').write_text("from pathlib import Path\nPath('results').mkdir(exist_ok=True)\n"
                                     "Path('results/changed-gate').write_text('different assertions actually ran')\n")
        assert not git(final, 'status', '--porcelain')
    final_scope = dict(SCOPE, agreed_scope='Repair the final deterministic probe only',
                       acceptance_criteria=['Retain every assertion and qualify the final repair'], slice_ids=['probe'], approved_at=200)
    store.record_scope('s', final_scope, work_item='probe')
    store.register('s', 'codex', REPO, 2, final)
    live = snapshot(2, head)
    store.set_snapshot(REPO, 2, live, evaluate_pr(live, []))
    row = workflow.save_plan(store, 's', REPO, 2, final, final_declarations(prefix))['plan']
    for check in ('check', 'browser'):
        assert workflow.run_check(store, 's', REPO, 2, final, check)['run']['status'] == 'pass'
    closed = snapshot(2, head, merged=True, merge=head)
    monitor.retire(store, REPO, 2, dict(state='closed', merged=True, head={'sha': head}), checklist=True)
    context = store.final_coverage_context('s', REPO, 1, 2)
    proof = dict(original=old_closed, final=closed,
        original_closure={**closing_link(old_head, REPO, 11, 'CLOSED'), 'closure_merged': True, 'closure_state': 'MERGED'},
        ancestry={'base_commit': {'sha': old_merge}, 'merge_base_commit': {'sha': old_merge},
                  'ahead_by': 1, 'behind_by': 0, 'status': 'ahead'},
        merge={'sha': head, 'tree': {'sha': git(final, 'rev-parse', 'HEAD^{tree}')}},
        declaration=declaration(old_row, row))
    calls = []
    async def collect_original(repo, number):
        calls.append(('review', repo, number)); return deepcopy(proof['original'])
    async def collect_final(repo, number):
        calls.append(('full', repo, number)); return deepcopy(proof['final'])
    async def collect_closure(repo, number):
        calls.append(('closure', repo, number)); return deepcopy(proof['original_closure'])
    monkeypatch.setattr(github, 'collect_review', collect_original)
    monkeypatch.setattr(github, 'collect_pr', collect_final)
    monkeypatch.setattr(closure, 'read_live', collect_closure)
    class Client:
        async def get(self, path):
            calls.append(path)
            if path == f'/repos/{REPO}/compare/{old_merge}...{head}': return deepcopy(proof['ancestry'])
            if path == f'/repos/{REPO}/git/commits/{head}': return deepcopy(proof['merge'])
            raise AssertionError(path)
    return dict(store=store, original=original, final=final, context=context, proof=proof, client=Client(), calls=calls)


def history(store):
    with store._connect() as db:
        return {table: [dict(row) for row in db.execute('SELECT * FROM ' + table)] for table in
                ('prs', 'pr_watch', 'validation_plans', 'validation_runs', 'scopes', 'scope_items', 'pr_scope_bindings')}


def qualify(data):
    return asyncio.run(workflow.qualify_successor(data['store'], 's', REPO, 1, 2, data['final'],
                                                 data['proof']['declaration'], client=data['client']))


def test_absent_original_checkout_unknown_private_bytes_and_divergent_heads_allow_current_full_coverage(delivered):
    data = delivered
    before = history(data['store'])
    final_phase = workflow.current_phase(data['store'], data['context']['final']['row'])
    shutil.rmtree(data['original'])
    result = qualify(data)
    assert result['qualification_verified'] and result['completion_verified'] is False
    assert result['coverage']['historical_input_continuity'] == 'not_established'
    assert result['coverage']['historical_results_changed'] is False
    assert result['historical_runs'][0]['status'] == 'fail'
    assert len(result['historical_runs']) == 1  # Original browser remains unexecuted.
    assert {r['status'] for r in result['final_runs']} == {'pass'}
    assert data['context']['original']['scope'] != data['context']['final']['scope']
    assert history(data['store']) == before and not data['store'].prs('s')
    assert not data['store'].validation_evidence(data['context']['original']['row']['plan_id'])[-1]['status'] == 'pass'
    gates = {g['number']: g for g in workflow.task_progress(data['store'], 's')}
    assert gates[1]['status'] == 'PASS'
    assert gates[2]['evidence']['phase'] == final_phase['phase']
    assert 'qualification_verified' not in gates[2]['evidence']
    assert data['store'].final_coverage_receipt('s', data['context']['final']['row']['plan_id']) is None
    assert len(data['calls']) == 5
    assert next(g for g in evaluate_pr(data['proof']['final'], []) if g['gate'] == 'MERGE_READY')['status'] == 'WAIT'


def test_truthful_source_proof_has_no_retroactive_execution_receipt(delivered, subtests):
    data = delivered
    for fault in ('no_run', 'failed', 'unknown', 'nonzero', 'before', 'after', 'wrong_plan', 'duplicate', 'future'):
        with subtests.test(fault=fault):
            context = deepcopy(data['context'])
            runs = context['final']['runs']
            if fault == 'no_run': runs.pop()
            elif fault == 'duplicate': runs.append(deepcopy(runs[0]))
            elif fault == 'failed': runs[0].update(status='fail', exit_code=1)
            elif fault == 'unknown': runs[0].update(status='unknown', exit_code=None)
            elif fault == 'nonzero': runs[0]['exit_code'] = 1
            elif fault in {'before', 'after'}: runs[0][fault + '_digest'] = 'different'
            elif fault == 'wrong_plan': runs[0]['plan_id'] = 'foreign'
            else: runs[0]['finished_at'] = time.time() + 100
            with pytest.raises(ValueError):
                workflow._final_coverage_evidence(context, data['proof'], data['final'])
    assert data['store'].final_coverage_receipt('s', data['context']['original']['row']['plan_id']) is None


@pytest.mark.parametrize('delivered', ['tracked_config'], indirect=True)
def test_tracked_browser_configuration_is_not_a_private_input_replacement(delivered):
    before = history(delivered['store'])
    with pytest.raises(ValueError, match='tracked browser configuration'):
        qualify(delivered)
    receipt = delivered['store'].final_coverage_receipt('s', delivered['context']['original']['row']['plan_id'])
    assert receipt['proof'] is None
    result = workflow.successor_progress(delivered['store'], delivered['context']['original']['row'])
    assert result['phase'] == 'SUCCESSOR_INCOMPLETE' and result['blocked'] is True
    assert result['qualification_verified'] is False and result['completion_verified'] is False
    assert history(delivered['store']) == before


@pytest.mark.parametrize('delivered', ['hidden_source'], indirect=True)
def test_real_passes_on_hidden_tracked_edits_do_not_qualify_published_source(delivered):
    assert {run['status'] for run in delivered['context']['final']['runs']} == {'pass'}
    with pytest.raises(ValueError, match='published final tree'):
        qualify(delivered)
    assert delivered['store'].final_coverage_receipt('s', delivered['context']['original']['row']['plan_id']) is None


def test_real_failed_final_process_is_not_qualification(delivered):
    data = delivered
    # Retain the fixture's original actual passes, then declare and execute a
    # genuinely different final input epoch whose child assertion really fails.
    store, root = data['store'], data['final']
    (root / 'src.txt').write_text('broken')
    git(root, 'commit', '-qam', 'Genuine different source fails its assertion')
    head = git(root, 'rev-parse', 'HEAD')
    store.register('s', 'codex', REPO, 3, root)
    snap = snapshot(3, head)
    store.set_snapshot(REPO, 3, snap, evaluate_pr(snap, []))
    plan = deepcopy(data['context']['final']['row']['plan'])
    row = workflow.save_plan(store, 's', REPO, 3, root, plan)['plan']
    failed = workflow.run_check(store, 's', REPO, 3, root, 'check')['run']
    assert failed['exit_code'] == 1 and failed['status'] == 'fail'
    monitor.retire(store, REPO, 3, dict(state='closed', merged=True, head={'sha': head}), checklist=True)
    context = store.final_coverage_context('s', REPO, 1, 3)
    proof = deepcopy(data['proof'])
    proof['final'] = snapshot(3, head, merged=True, merge=head)
    proof['merge'] = {'sha': head, 'tree': {'sha': git(root, 'rev-parse', 'HEAD^{tree}')}}
    proof['declaration']['final_plan_id'] = row['plan_id']
    with pytest.raises(ValueError, match='actual execution'):
        workflow._final_coverage_evidence(context, proof, root)


def test_remote_identity_review_ci_closure_and_delivered_ancestry_stay_required(delivered, subtests):
    data = delivered
    for fault in ('foreign_repo', 'open', 'unmerged', 'head', 'identity', 'stale', 'approval', 'self_approval',
                  'other_head_approval', 'changes_requested', 'finding', 'ci', 'pending_ci', 'service_only',
                  'skipped_ci', 'neutral_ci', 'wrong_ci_head', 'issue', 'ancestry', 'tree'):
        with subtests.test(fault=fault):
            proof = deepcopy(data['proof']); final = proof['final']
            if fault == 'foreign_repo': final['repository'] = 'Other/Repo'
            elif fault == 'open': final['state'] = 'open'
            elif fault == 'unmerged': final['merged'] = False
            elif fault == 'head': final['head_sha_after'] = '0' * 40
            elif fault == 'identity': final['base_repository_id'] = 5
            elif fault == 'stale': final['collected_at'] -= 500
            elif fault == 'approval': final['submitted_reviews'] = []
            elif fault == 'self_approval': final['submitted_reviews'][0]['author_login'] = 'agent'
            elif fault == 'other_head_approval': final['submitted_reviews'][0]['commit_sha'] = '0' * 40
            elif fault == 'changes_requested': final['submitted_reviews'][0]['state'] = 'CHANGES_REQUESTED'
            elif fault == 'finding': final['comments'] = [{'id': 'review:8', 'body': 'Real unresolved finding'}]
            elif fault == 'ci': final['checks'][0]['conclusion'] = 'failure'
            elif fault == 'pending_ci': final['checks'][0].update(status='in_progress', conclusion=None)
            elif fault == 'service_only':
                final['checks'][0].update(name='Running Copilot Code Review', conclusion='failure',
                    workflow_event='dynamic', workflow_path='dynamic/agents/copilot-pull-request-reviewer', workflow_id='5')
                final['expected_checks'] = ['Running Copilot Code Review']
            elif fault == 'skipped_ci': final['checks'][0]['conclusion'] = 'skipped'
            elif fault == 'neutral_ci': final['checks'][0]['conclusion'] = 'neutral'
            elif fault == 'wrong_ci_head': final['checks'][0]['head_sha'] = '0' * 40
            elif fault == 'issue': proof['original_closure']['closing_issues'][0]['state'] = 'OPEN'
            elif fault == 'ancestry': proof['ancestry'].update(behind_by=1, status='diverged')
            else: proof['merge']['tree']['sha'] = '0' * 40
            with pytest.raises(ValueError): workflow._final_coverage_evidence(data['context'], proof, data['final'])
    proof = deepcopy(data['proof'])
    proof['final']['checks'].append(dict(proof['final']['checks'][0], id='2', name='Running Copilot Code Review',
        conclusion='failure', workflow_event='dynamic', workflow_path='dynamic/agents/copilot-pull-request-reviewer', workflow_id='5'))
    assert workflow._final_coverage_evidence(data['context'], proof, data['final'])['qualification_verified']
    proof['final']['checks'] = [github._check(dict(id=9, context='full', state='success',
        head_sha=proof['final']['head_sha']), 'status')]
    assert workflow._final_coverage_evidence(data['context'], proof, data['final'])['qualification_verified']


def test_owner_checkout_active_run_and_transaction_races_do_not_record_coverage(delivered, tmp_path, monkeypatch, subtests):
    data = delivered; store = data['store']
    with pytest.raises(ValueError): store.final_coverage_context('other', REPO, 1, 2)
    store.record_session('child', 'codex', data['final'], parent_session='s')
    with pytest.raises(ValueError): store.final_coverage_context('child', REPO, 1, 2)
    with pytest.raises(ValueError): store.final_coverage_context('s', REPO, 1, 1)
    with pytest.raises(ValueError): workflow._final_coverage_local(data['context'], data['original'])
    before = history(store)
    real = store.record_final_coverage
    def race(context, proof, guard, **kwargs):
        if proof is not None:
            with store._connect() as db:
                db.execute("UPDATE validation_runs SET status='running' WHERE run_id=?", (context['final']['runs'][0]['run_id'],))
        return real(context, proof, guard, **kwargs)
    monkeypatch.setattr(store, 'record_final_coverage', race)
    with pytest.raises(ValueError, match='active execution'): qualify(data)
    assert store.final_coverage_receipt('s', data['context']['original']['row']['plan_id'])['proof'] is None
    assert history(store)['validation_plans'] == before['validation_plans']


def test_current_final_input_and_review_drift_invalidate_only_the_coverage_receipt(delivered):
    data = delivered; store = data['store']
    qualify(data)
    before = history(store)
    (data['final'] / 'src.txt').write_text('changed')
    result = {g['number']: g for g in workflow.task_progress(store, 's')}
    assert result[1]['status'] == 'WAIT' and result[1]['evidence']['phase'] == 'SUCCESSOR_INCOMPLETE'
    assert result[2]['evidence']['phase'] == workflow.current_phase(store, data['context']['final']['row'])['phase']
    assert 'qualification_verified' not in result[2]['evidence']
    assert history(store) == before


def test_failed_refresh_revokes_prior_success_without_rewriting_results(delivered, monkeypatch, subtests):
    data = delivered; store = data['store']; before = history(store)
    good = deepcopy(data['proof'])
    for fault in ('ci', 'review', 'closure', 'collection'):
        with subtests.test(fault=fault):
            data['proof'].clear(); data['proof'].update(deepcopy(good))
            previous = qualify(data)
            assert workflow.successor_progress(store, data['context']['original']['row'])['qualification_verified']
            if fault == 'ci': data['proof']['final']['checks'][0]['conclusion'] = 'failure'
            elif fault == 'review': data['proof']['final']['submitted_reviews'][0]['state'] = 'CHANGES_REQUESTED'
            elif fault == 'closure': data['proof']['original_closure']['closing_issues'][0]['state'] = 'OPEN'
            with monkeypatch.context() as patch:
                if fault == 'collection':
                    async def unavailable(*args):
                        raise github.EvidenceError('Current review collection unavailable')
                    patch.setattr(github, 'collect_review', unavailable)
                with pytest.raises((ValueError, github.EvidenceError)):
                    qualify(data)
            result = workflow.successor_progress(store, data['context']['original']['row'])
            assert result['phase'] == 'SUCCESSOR_INCOMPLETE' and result['qualification_verified'] is False
            assert result['receipt_id'] > previous['receipt_id'] and result['completion_verified'] is False
            with store._connect() as db:
                saved = json.loads(db.execute('SELECT fields FROM audit WHERE id=?', (previous['receipt_id'],)).fetchone()[0])
            assert saved['proof'] is not None  # Preserve the earlier genuine conclusion, without replaying it.
            assert history(store) == before


def test_older_inflight_refresh_cannot_overwrite_newer_failed_refresh(delivered, monkeypatch):
    data = delivered; store = data['store']; before = history(store)
    qualify(data)
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def collect(*args):
            nonlocal count
            count += 1
            proof = deepcopy(data['proof']['final'])
            if count == 1:
                started.set()
                await release.wait()
            else:
                proof['checks'][0]['conclusion'] = 'failure'
            return proof
        monkeypatch.setattr(github, 'collect_pr', collect)
        def refresh():
            return workflow.qualify_successor(store, 's', REPO, 1, 2, data['final'],
                data['proof']['declaration'], client=data['client'])
        older = asyncio.create_task(refresh())
        await started.wait()
        try:
            pending = workflow.successor_progress(store, data['context']['original']['row'])
            assert pending['phase'] == 'SUCCESSOR_INCOMPLETE' and not pending['qualification_verified']
            with pytest.raises(ValueError, match='CI, review'):
                await refresh()
            latest = store.final_coverage_receipt('s', data['context']['original']['row']['plan_id'])
        finally:
            release.set()
        with pytest.raises(ValueError, match='newer final coverage refresh'):
            await older
        assert store.final_coverage_receipt('s', data['context']['original']['row']['plan_id']) == latest
    asyncio.run(exercise())
    assert not workflow.successor_progress(store, data['context']['original']['row'])['qualification_verified']
    assert history(store) == before


def test_coverage_metadata_rejects_command_waivers_and_silent_differences(subtests):
    prefix = ['/usr/bin/env', 'PATH=/fixed/bin']
    original = {'plan_id': 'old', 'plan': declarations(prefix)}
    final = {'plan_id': 'new', 'plan': final_declarations(prefix)}
    payload = declaration(original, final)
    assert progress.final_coverage(original, final, SCOPE, payload)['historical_input_continuity'] == 'not_established'
    for fault in ('missing_check', 'unknown_id', 'shell', 'env_flag', 'path_value', 'env_order', 'skip_env',
                  'inherited_skip', 'inherited_node_options', 'inherited_browser', 'inherited_input', 'inherited_env_pin',
                  'empty_workers', 'bad_port', 'grep', 'config', 'extra_arg', 'cwd', 'script', 'reason',
                  'missing_change', 'invented_change', 'criteria', 'plan', 'input', 'named_subset', 'scope'):
        with subtests.test(fault=fault):
            previous, candidate, fields, scope = deepcopy(original), deepcopy(final), deepcopy(payload), deepcopy(SCOPE)
            check, browser = candidate['plan']['checks']
            if fault == 'missing_check': candidate['plan']['checks'].pop()
            elif fault == 'unknown_id': check['id'] = 'different'
            elif fault == 'shell': check['argv'] = ['sh', '-c', 'npm run check']
            elif fault == 'env_flag': check['argv'].insert(1, '-i')
            elif fault == 'path_value': check['argv'][1] = 'PATH=/other'
            elif fault == 'env_order': check['argv'][1:3] = reversed(check['argv'][1:3])
            elif fault == 'skip_env': check['argv'][2] = 'SKIP_TESTS=1'
            elif fault in {'inherited_skip', 'inherited_node_options', 'inherited_browser'}:
                index = 1 if fault == 'inherited_browser' else 0
                assignment = 'NODE_OPTIONS=--require=private/filter.cjs' if fault == 'inherited_node_options' else 'SKIP_TESTS=1'
                previous['plan']['checks'][index]['argv'].insert(2, assignment)
                candidate['plan']['checks'][index]['argv'].insert(2, assignment)
            elif fault == 'inherited_input':
                previous['plan']['input_paths'].append('private/extra-fixture.json')
                candidate['plan']['input_paths'].append('private/extra-fixture.json')
            elif fault == 'inherited_env_pin':
                previous['plan']['env_names'].append('SKIP_TESTS')
                candidate['plan']['env_names'].append('SKIP_TESTS')
            elif fault == 'empty_workers': check['argv'][2] = 'VITEST_MAX_WORKERS=0'
            elif fault == 'bad_port': browser['argv'][2] = 'POISE_E2E_PORT=70000'
            elif fault == 'grep': browser['argv'].append('--grep=one')
            elif fault == 'config': browser['argv'].append('--config=private/other.mjs')
            elif fault == 'extra_arg': check['argv'].append('one.test.js')
            elif fault == 'cwd': check['cwd'] = 'other'
            elif fault == 'script': check['argv'][-1] = 'lint'
            elif fault == 'reason': fields['checks']['check'] = ''
            elif fault == 'missing_change': fields['changes'].pop('input_paths')
            elif fault == 'invented_change': fields['changes']['imaginary'] = 'Unknown'
            elif fault == 'criteria': fields['criteria'] = []
            elif fault == 'plan': fields['original_plan_id'] = 'unrelated'
            elif fault == 'input': candidate['plan']['input_paths'] = ['private/new-driver.py']
            elif fault == 'named_subset': scope['acceptance_criteria'] = ['npm run check-subset and npm run test:e2e pass.']
            else: scope['acceptance_criteria'] = ['Only a subset is required']
            with pytest.raises(ValueError): progress.final_coverage(previous, candidate, scope, fields)


def test_cli_uses_one_frozen_declaration_and_records_no_consumer_execution(delivered, tmp_path, monkeypatch, capsys):
    data = delivered; path = tmp_path / 'coverage.json'
    path.write_text(json.dumps(data['proof']['declaration']))
    before = history(data['store'])
    monkeypatch.setattr(cli, 'Store', lambda: data['store'])
    monkeypatch.setattr('caller_pr_gates.transport.GitHubClient', lambda: data['client'])
    monkeypatch.setattr(sys, 'argv', ['caller-pr-gates', 'qualify-successor', '--session', 's',
        '--pr', f'https://github.com/{REPO}/pull/1', '--successor', f'https://github.com/{REPO}/pull/2',
        '--cwd', str(data['final']), '--file', str(path)])
    cli.main()
    assert json.loads(capsys.readouterr().out)['qualification_verified'] is True
    assert history(data['store']) == before


def test_collector_retains_empty_exact_head_approval_and_source_identity_without_new_reads():
    from test_github import Client, SHA
    class Reviewed(Client):
        async def get(self, path, params=None):
            result = await super().get(path, params)
            if path.endswith('/pulls/50'):
                result.update(merge_commit_sha='d' * 40, merged_at='2026-10-20T10:00:00Z')
                result['head'].update(ref='feature', repo={'id': 7, 'full_name': 'mikkokotila/caller'})
                result['base'].update(repo={'id': 7, 'full_name': 'mikkokotila/caller'})
            return result
        async def graphql(self, query, variables):
            result = await super().graphql(query, variables)
            if 'reviews(first' in query:
                assert 'commit { oid }' in query
                result['repository']['pullRequest']['reviews']['nodes'] = [dict(id='node', databaseId=10, body='', state='APPROVED',
                    commit={'oid': SHA}, author={'login': 'reviewer', '__typename': 'User'},
                    url='https://github.com/mikkokotila/caller/pull/50#pullrequestreview-10',
                    submittedAt='2026-10-20T09:00:00Z', updatedAt='2026-10-20T09:00:00Z')]
            return result
    client = Reviewed()
    result = asyncio.run(github.collect_with_client(client, 'mikkokotila/caller', 50))
    assert client.reads == 2 and result['submitted_reviews'][0]['commit_sha'] == SHA
    assert result['merge_commit_sha'] == 'd' * 40 and result['head_repository_id'] == 7
    assert not any(c['id'] == 'review:10' for c in result['comments'])


def test_commit_status_identity_comes_from_the_existing_exact_head_read(subtests):
    from test_github import Client, SHA, OTHER
    class StatusOnly(Client):
        async def get(self, path, params=None):
            if path.endswith('/check-runs'): return {'total_count': 0, 'check_runs': []}
            if path.endswith('/actions/runs'): return {'total_count': 0, 'workflow_runs': []}
            if path.endswith('/statuses'):
                assert path == f'/repos/mikkokotila/caller/commits/{SHA}/statuses'
                self.status_reads += 1
                return [dict(id=12, context='test', state='success', **self.status_head)]
            return await super().get(path, params)
    for supplied in ({}, {'head_sha': SHA}, {'head_sha': OTHER}):
        with subtests.test(supplied=supplied):
            client = StatusOnly(); client.status_reads = 0; client.status_head = supplied
            if supplied.get('head_sha') == OTHER:
                with pytest.raises(github.EvidenceError, match='different head'):
                    asyncio.run(github.collect_with_client(client, 'mikkokotila/caller', 50))
            else:
                result = asyncio.run(github.collect_with_client(client, 'mikkokotila/caller', 50))
                assert client.reads == 2 and len(result['checks']) == 1
                assert result['checks'][0]['head_sha'] == SHA and result['checks'][0]['kind'] == 'status'
                assert result['checks'][0]['conclusion'] == 'success'
            assert client.status_reads == 1
