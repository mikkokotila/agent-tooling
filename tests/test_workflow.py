import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from caller_pr_gates import hook, workflow
from caller_pr_gates.policy import evaluate_pr
from caller_pr_gates.store import Store
from test_policy import fresh_branch, closing_link


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def local(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Gate test')
    git(root, 'config', 'user.email', 'gate@example.invalid')
    git(root, 'remote', 'add', 'origin', 'https://github.com/Owner/Repo.git')
    (root / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n')
    (root / 'src.txt').write_text('old')
    (root / 'check.py').write_text("from pathlib import Path\nassert Path('src.txt').read_text() == 'fixed'\n")
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Initial')
    head = git(root, 'rev-parse', 'HEAD')
    store = Store(tmp_path / 'state')
    store.record_session('s', 'codex', str(root))
    store.register('s', 'codex', 'Owner/Repo', 1, str(root))
    snap = dict(repository='Owner/Repo', number=1, head_sha=head, head_sha_after=head,
                state='open', merged=False, draft=False, mergeable=True, collected_at=time.time(),
                actor_login='agent', author_login='agent', checks=[], expected_checks=[],
                comments=[], commit_shas=[head], commit_dates={}, changed_files=['src.txt'], **fresh_branch(head, base=head), **closing_link(head))
    store.set_snapshot('Owner/Repo', 1, snap, evaluate_pr(snap, []))
    plan = dict(checks=[dict(id='regression', argv=[sys.executable, 'check.py'],
                            reason='Assert the repaired behavior')])
    return store, root, snap, plan


def publish(local):
    store, root, snap, _ = local
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Publish useful change')
    head = git(root, 'rev-parse', 'HEAD')
    snap.update(head_sha=head, head_sha_after=head, collected_at=time.time(),
                commit_shas=[*snap['commit_shas'], head], **fresh_branch(head, base=snap['base_sha']), **closing_link(head))
    store.set_snapshot('Owner/Repo', 1, snap, evaluate_pr(snap, []))


def bind(local):
    store, root, _, plan = local
    return workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)


def run(local):
    store, root, _, _ = local
    return workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'regression')


def test_publication_precedes_checks_and_identical_retry_is_blocked(local):
    store, root, _, _ = local
    (root / 'src.txt').write_text('fixed')
    bind(local)
    with patch('caller_pr_gates.workflow._run_process', side_effect=AssertionError('cannot run before publishing')):
        with pytest.raises(ValueError, match='PUBLISH_REQUIRED'):
            run(local)
    assert store.validation_runs(store.validation_plans('s')[0]['plan_id']) == []
    publish(local)
    outcome = run(local)
    assert outcome['run']['status'] == 'pass'
    assert outcome['progress']['phase'] == 'COMPLETE'
    with patch('caller_pr_gates.workflow._run_process', side_effect=AssertionError('must not repeat')):
        with pytest.raises(ValueError, match='COMPLETE'):
            run(local)
    assert len(store.validation_runs(outcome['run']['plan_id'])) == 1


def test_commit_alone_does_not_reset_tests_and_remote_head_proves_publication(local):
    store, root, snap, _ = local
    (root / 'src.txt').write_text('fixed')
    publish(local)
    row = bind(local)['plan']
    run(local)
    git(root, 'add', 'src.txt')
    git(root, 'commit', '--allow-empty', '-qm', 'Metadata-only commit')
    assert workflow.current_phase(store, row)['phase'] == 'PUBLISH_REQUIRED'
    new_head = git(root, 'rev-parse', 'HEAD')
    snap.update(head_sha=new_head, head_sha_after=new_head, commit_shas=[snap['head_sha'], new_head], collected_at=time.time(), **fresh_branch(new_head, base=snap['base_sha']), **closing_link(new_head))
    store.set_snapshot('Owner/Repo', 1, snap, evaluate_pr(snap, []))
    assert workflow.current_phase(store, row)['phase'] == 'COMPLETE'
    with pytest.raises(ValueError, match='COMPLETE'):
        run(local)
    assert len(store.validation_runs(row['plan_id'])) == 1


def test_failed_command_not_repeated_until_inputs_change(local):
    store, root, _, _ = local
    row = bind(local)['plan']
    assert run(local)['run']['status'] == 'fail'
    with pytest.raises(ValueError, match='already completed'):
        run(local)
    (root / 'src.txt').write_text('fixed')
    publish(local)
    outcome = run(local)
    assert outcome['run']['status'] == 'pass'
    assert outcome['run']['plan_id'] != row['plan_id']


def test_yield_or_interrupt_never_records_pass(local):
    store, root, _, _ = local
    (root / 'src.txt').write_text('fixed')
    publish(local)
    row = bind(local)['plan']
    with patch('caller_pr_gates.workflow._run_process', side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            run(local)
    assert store.validation_runs(row['plan_id'])[0]['status'] == 'unknown'
    assert run(local)['run']['status'] == 'pass'


def test_changed_inputs_during_successful_process_are_unknown(local):
    store, root, _, _ = local
    (root / 'src.txt').write_text('fixed')
    publish(local)
    row = bind(local)['plan']
    def process(*args, **kwargs):
        (root / 'src.txt').write_text('changed while running')
        return subprocess.CompletedProcess(args[0], 0)
    with patch('caller_pr_gates.workflow._run_process', side_effect=process):
        outcome = run(local)
    assert outcome['run']['status'] == 'unknown'
    assert store.validation_runs(row['plan_id'])[0]['status'] == 'unknown'


def test_handled_comment_must_be_concluded_before_more_checks(local):
    store, root, snap, _ = local
    (root / 'src.txt').write_text('fixed')
    publish(local)
    comment = dict(id='inline:7', url='https://github.com/Owner/Repo/pull/1#discussion_r7',
                   body='Fix the behavior', author_login='reviewer', kind='inline',
                   created_at='2026-10-04T10:00:00Z', updated_at='2026-10-04T10:00:00Z',
                   thread_id='thread7', is_resolved=False, is_outdated=False, replies=[])
    snap['comments'] = [comment]
    store.set_snapshot('Owner/Repo', 1, snap, evaluate_pr(snap, []))
    store.disposition('s', 'Owner/Repo', 1, dict(comment_id=comment['id'],
        comment_updated_at=comment['updated_at'], disposition='fixed',
        commit_sha=snap['head_sha'], reason='Existing published repair addresses this feedback'))
    bind(local)
    with pytest.raises(ValueError, match='CONCLUDE_REQUIRED'):
        run(local)
    assert store.validation_runs(store.validation_plans('s')[0]['plan_id']) == []


@pytest.mark.parametrize('client', ['codex', 'claude'])
def test_native_pretool_guards_raw_tests_but_allows_publication_and_ci_reads(local, client, monkeypatch):
    store, root, _, _ = local
    monkeypatch.setenv('CALLER_HOOK_CLIENT', client)
    def event(command):
        return dict(hook_event_name='PreToolUse', session_id='s', cwd=str(root),
                    tool_name='Bash', tool_input={'command': command})
    denied = hook.handle(event('python -m pytest tests'), store)
    assert denied['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert 'validate --session s' in denied['hookSpecificOutput']['permissionDecisionReason']
    for command in ('git add src.txt', 'git commit -m Fix', 'git push origin HEAD',
                    'caller-pr-gates validate --session s --pr URL --check regression',
                    'caller-pr-gates status --session s --fresh',
                    'caller-pr-gates resolve --session s --pr URL --thread thread7'):
        assert hook.handle(event(command), store) is None


def test_raw_tests_in_completed_or_unowned_pr_are_not_reopened(local):
    store, root, snap, _ = local
    snap.update(state='closed', merged=True)
    store.set_snapshot('Owner/Repo', 1, snap, evaluate_pr(snap, []))
    event = dict(hook_event_name='PreToolUse', session_id='s', cwd=str(root),
                 tool_name='Bash', tool_input={'command': 'pytest'})
    assert hook.handle(event, store) is None
    event['session_id'] = 'other'
    assert hook.handle(event, store) is None


def test_remote_refresh_timestamp_and_pending_ci_do_not_reset_validation_evidence(local):
    _, _, snap, _ = local
    original = workflow.evidence_digest(snap, [])
    snap.update(collected_at=time.time()+20, head_sha='another pushed head')
    snap['checks'] = [dict(name='CI', status='in_progress', conclusion=None)]
    assert workflow.evidence_digest(snap, []) == original
    snap['checks'][0].update(status='completed', conclusion='failure', id=10)
    assert workflow.evidence_digest(snap, []) != original


def test_plan_cannot_bind_completed_work(local):
    store, _, snap, _ = local
    snap.update(state='closed', merged=True)
    store.set_snapshot('Owner/Repo', 1, snap, evaluate_pr(snap, []))
    with pytest.raises(ValueError, match='completed work is not reopened'):
        bind(local)


def test_subdirectory_cannot_reset_an_already_passed_plan(local):
    store, root, _, plan = local
    (root / 'src.txt').write_text('fixed')
    publish(local)
    subdir = root / 'subdir'
    subdir.mkdir()
    row = bind(local)['plan']
    run(local)
    with pytest.raises(ValueError, match='repository root'):
        workflow.save_plan(store, 's', 'Owner/Repo', 1, subdir, plan)
    with pytest.raises(ValueError, match='COMPLETE'):
        workflow.run_check(store, 's', 'Owner/Repo', 1, subdir, 'regression')
    assert len(store.validation_runs(row['plan_id'])) == 1


def test_dead_run_is_recovered_before_changed_input_generation(local):
    store, root, _, _ = local
    row = bind(local)['plan']
    store.begin_validation(row['plan_id'], 'regression', row['input_digest'], owner_pid=987654321)
    (root / 'src.txt').write_text('fixed')
    publish(local)
    outcome = run(local)
    assert outcome['run']['status'] == 'pass'
    assert outcome['run']['plan_id'] != row['plan_id']
    assert store.validation_runs(row['plan_id'])[0]['status'] == 'unknown'


def test_functions_exec_nested_repo_test_is_guarded(local):
    store, root, _, _ = local
    event = dict(hook_event_name='PreToolUse', session_id='s', cwd=str(root.parent),
                 tool_name='functions.exec', tool_input={'code':
                     'await tools.exec_command({cmd:"pytest -q",workdir:' + json.dumps(str(root)) + '})'})
    denied = hook.handle(event, store)
    assert denied['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_clearing_ci_failure_does_not_reset_successful_local_validation(local):
    store, root, snap, _ = local
    (root / 'src.txt').write_text('fixed')
    publish(local)
    snap['checks'] = [dict(id=10, kind='check', key='check:CI', name='CI', head_sha=snap['head_sha'], status='completed', conclusion='failure', run_attempt=1)]
    store.set_snapshot('Owner/Repo', 1, snap, evaluate_pr(snap, []))
    row = bind(local)['plan']
    assert run(local)['run']['status'] == 'pass'
    snap['checks'][0]['conclusion'] = 'success'
    snap['collected_at'] = time.time()
    store.set_snapshot('Owner/Repo', 1, snap, evaluate_pr(snap, []))
    with pytest.raises(ValueError, match='COMPLETE'):
        run(local)
    assert store.validation_plan('s', 'Owner/Repo', 1, root)['plan_id'] == row['plan_id']
    assert len(store.validation_runs(row['plan_id'])) == 1


def test_background_check_returns_before_process_exit_and_records_actual_completion(local, monkeypatch):
    store, root, snap, _ = local
    (root / 'src.txt').write_text('fixed')
    (root / 'check.py').write_text("import time\ntime.sleep(1)\nfrom pathlib import Path\nassert Path('src.txt').read_text() == 'fixed'\n")
    publish(local)
    bound = bind(local)['plan']
    # The real detached runner gets a fixture collector; tests never contact GitHub.
    original_popen = subprocess.Popen
    bootstrap = ('import json\nfrom caller_pr_gates import github, cli\n'
                 f'remote = json.loads({json.dumps(snap)!r})\n'
                 'async def collect(*args):\n    return remote\n'
                 'github.collect_pr = collect\ngithub.collect_validation = collect\ncli.main()\n')
    def fixture_worker(argv, **kwargs):
        if argv[1:4] == ['-P', '-m', 'caller_pr_gates.cli']:
            argv = [argv[0], '-c', bootstrap, *argv[4:]]
        return original_popen(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', fixture_worker)
    launched = workflow.launch_check(store, 's', 'Owner/Repo', 1, root, 'regression')
    assert launched['completion_verified'] is False
    assert launched['started'] is True
    assert Path(launched['log']).is_file()
    deadline = time.time() + 10
    while time.time() < deadline:
        runs = store.validation_runs(bound['plan_id'])
        if runs and runs[-1]['status'] == 'pass':
            break
        time.sleep(0.1)
    assert runs[-1]['status'] == 'pass'
    assert runs[-1]['exit_code'] == 0
    with pytest.raises(ValueError, match='COMPLETE'):
        workflow.launch_check(store, 's', 'Owner/Repo', 1, root, 'regression')


def test_background_launch_cannot_delay_or_precede_remote_publication(local):
    store, root, _, _ = local
    (root / 'src.txt').write_text('fixed')
    bind(local)
    real = subprocess.Popen
    def only_git(argv, *args, **kwargs):
        if argv[0] != 'git':
            raise AssertionError('cannot launch before publication')
        return real(argv, *args, **kwargs)
    with patch('caller_pr_gates.workflow.subprocess.Popen', side_effect=only_git):
        with pytest.raises(ValueError, match='PUBLISH_REQUIRED'):
            workflow.launch_check(store, 's', 'Owner/Repo', 1, root, 'regression')
