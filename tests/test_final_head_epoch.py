"""A pre-merge source sync must not erase old evidence or import old passes."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from caller_pr_gates import cli, github, progress, workflow
from test_post_merge_qualification import prepare, merge
from test_workflow import local, git, publish


def final_source(local, *, fix=False):
    store, root, snap, _ = local
    if fix:
        (root / 'src.txt').write_text('fixed')
    (root / 'final-source.txt').write_text('Published synchronization before merge\n')
    publish(local)
    proof = merge(local)
    proof['merged_at'] = datetime.now(timezone.utc).isoformat()
    store.set_preflight('Owner/Repo', 1, proof)
    return proof


def epoch(local, previous_head, payload=None, **kwargs):
    store, root, snap, plan = local
    return workflow.save_plan(store, 's', 'Owner/Repo', 1, root, payload or plan,
                              merged_head=snap['head_sha'], from_head=previous_head, **kwargs)


def execute(local, check):
    store, root, snap, _ = local
    return workflow.run_check(store, 's', 'Owner/Repo', 1, root, check, merged_head=snap['head_sha'])


@pytest.mark.parametrize('failed', [False, True])
def test_final_head_requires_fresh_execution_and_keeps_original_history(local, failed):
    store, root, snap, plan = local
    old = prepare(local, inputs=True, failing=failed)
    before = snap['head_sha']
    first = workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'native-live-crash')['run']
    assert first['status'] == ('fail' if failed else 'pass')
    history = store.validation_runs(old['plan_id'])
    proof = final_source(local, fix=failed)
    with pytest.raises(ValueError, match='Post-merge validation inputs changed'):
        workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan, merged_head=snap['head_sha'])
    saved = epoch(local, before)
    new = saved['plan']
    assert new['plan_id'] != old['plan_id'] and new['input_digest'] != old['input_digest']
    assert progress.plan_declaration(new['plan']) == old['plan']
    assert new['plan']['source_epoch']['previous_plan_id'] == old['plan_id']
    assert new['plan']['source_epoch']['head_sha'] == snap['head_sha']
    assert saved['progress']['passed'] == []
    assert set(saved['progress']['pending']) == {'native-live-crash', 'remaining'}
    assert store.validation_evidence(new['plan_id']) == []
    assert store.validation_runs(old['plan_id']) == history
    with store._connect() as db:
        historical = store._validation_plan_row(db.execute('SELECT * FROM validation_plans WHERE plan_id=?', (old['plan_id'],)).fetchone())
        audit = json.loads(db.execute("SELECT fields FROM audit WHERE event='validation_final_head_epoch'").fetchone()[0])
        watch = dict(db.execute('SELECT * FROM pr_watch').fetchone())
    assert historical['active'] == 0 and historical['plan'] == old['plan']
    assert historical['input_digest'] == old['input_digest']
    assert audit['previous_input_digest'] == old['input_digest'] and audit['input_digest'] == new['input_digest']
    assert audit['from_head'] == before and audit['merge_commit_sha'] == proof['merge_commit_sha']
    assert watch['state'] == 'merged' and watch['event_id'] is None
    for identifier in ('native-live-crash', 'remaining'):
        result = execute(local, identifier)
        actual = result['run']
        assert actual['status'] == 'pass' and actual['exit_code'] == 0
        assert actual['before_digest'] == actual['after_digest'] == new['input_digest']
        assert actual['run_id'] != first['run_id']
    assert result['progress']['phase'] == 'WAIT_REMOTE'
    assert result['progress']['local_checks_passed'] is True
    assert result['progress']['completion_verified'] is False
    assert epoch(local, before)['plan']['plan_id'] == new['plan_id']
    assert store.validation_runs(old['plan_id']) == history
    with pytest.raises(ValueError):
        execute(local, 'native-live-crash')
    assert progress.working_state(root) == dict(head=snap['head_sha'], clean=True, root=str(root))
    assert store.prs('s') == []


def test_real_final_head_failure_cannot_be_reset(local):
    store, root, snap, _ = local
    old = prepare(local, inputs=True, failing=True)
    before = snap['head_sha']
    final_source(local)
    new = epoch(local, before)['plan']
    failed = execute(local, 'native-live-crash')
    assert failed['run']['status'] == 'fail' and failed['run']['exit_code'] > 0
    assert failed['progress']['phase'] == 'VALIDATE_FAILED'
    assert epoch(local, before)['plan'] == new
    assert store.validation_runs(new['plan_id']) == [failed['run']]
    assert store.validation_runs(old['plan_id']) == []
    with pytest.raises(ValueError, match='already completed'):
        execute(local, 'native-live-crash')


def test_older_identical_final_source_pass_does_not_supply_epoch_credit(local):
    store, root, snap, plan = local
    prepare(local, inputs=True)
    previous = snap['head_sha']
    (root / 'final-source.txt').write_text('Published synchronization before merge\n')
    publish(local)
    earlier = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    actual = workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'native-live-crash')['run']
    assert actual['status'] == 'pass'
    (root / 'final-source.txt').unlink()
    publish(local)
    old = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    previous = snap['head_sha']
    final_source(local)
    new = epoch(local, previous)['plan']
    assert new['input_digest'] == earlier['input_digest'] != old['input_digest']
    assert store.validation_evidence(new['plan_id']) == []
    assert store.validation_runs(earlier['plan_id']) == [actual]
    fresh = execute(local, 'native-live-crash')['run']
    assert fresh['status'] == 'pass' and fresh['run_id'] != actual['run_id']


@pytest.mark.parametrize('change', ['argv', 'reason', 'cwd', 'id', 'remove', 'add', 'input_paths', 'env_names'])
def test_final_epoch_refuses_changed_necessary_declarations(local, change):
    store, root, snap, plan = local
    old = prepare(local, inputs=True)
    before = snap['head_sha']
    final_source(local)
    altered = deepcopy(plan)
    if change == 'argv':
        altered['checks'][0]['argv'].append('different')
    elif change == 'reason':
        altered['checks'][0]['reason'] = 'Different acceptance'
    elif change == 'cwd':
        (root / 'subdir').mkdir()
        altered['checks'][0]['cwd'] = 'subdir'
        altered['checks'][0]['argv'] = [sys.executable, '../check.py']
    elif change == 'id':
        altered['checks'][0]['id'] = 'replacement'
    elif change == 'remove':
        altered['checks'].pop()
    elif change == 'add':
        altered['checks'].append(dict(id='additional', argv=[sys.executable, 'diagnose.py'], reason='New control'))
    elif change == 'input_paths':
        altered['input_paths'] = []
    else:
        altered['env_names'] = ['NEW_EPOCH_ENV']
    with pytest.raises(ValueError, match='preserve every necessary check'):
        epoch(local, before, altered)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == old


@pytest.mark.parametrize('change', ['private-file', 'private-mode', 'private-env', 'private-added', 'private-deleted'])
def test_final_epoch_refuses_changed_private_pins(local, monkeypatch, change):
    store, root, snap, plan = local
    monkeypatch.setenv('EPOCH_PRIVATE_VALUE', 'original')
    plan['env_names'] = ['EPOCH_PRIVATE_VALUE']
    with (root / '.git/info/exclude').open('a') as target:
        target.write('.private/\n')
    (root / '.private').mkdir()
    (root / '.private/driver.py').write_text('assert 1 + 1 == 2\n')
    plan['input_paths'] = ['.private']
    old = prepare(local)
    before = snap['head_sha']
    final_source(local)
    if change == 'private-file':
        (root / '.private/driver.py').write_text('assert 2 + 2 == 4\n')
    elif change == 'private-mode':
        (root / '.private/driver.py').chmod(0o755)
    elif change == 'private-env':
        monkeypatch.setenv('EPOCH_PRIVATE_VALUE', 'changed')
    elif change == 'private-added':
        (root / '.private/extra').write_text('new dependency')
    else:
        (root / '.private/driver.py').unlink()
    with pytest.raises(ValueError, match='unchanged private inputs'):
        epoch(local, before)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == old


@pytest.mark.parametrize('change', ['dirty', 'postmerge-commit', 'origin', 'owner', 'checkout', 'unknown-head', 'nonancestor', 'same-head'])
def test_epoch_refuses_source_and_ownership_escape(local, tmp_path, change):
    store, root, snap, plan = local
    old = prepare(local, inputs=True)
    before = snap['head_sha']
    final_source(local)
    sid, cwd, head = 's', root, snap['head_sha']
    if change == 'dirty':
        (root / 'src.txt').write_text('unpublished')
    elif change == 'postmerge-commit':
        (root / 'src.txt').write_text('post-merge source edit')
        git(root, 'add', 'src.txt')
        git(root, 'commit', '-qm', 'Not the merged source')
        head = git(root, 'rev-parse', 'HEAD')
    elif change == 'origin':
        git(root, 'remote', 'set-url', 'origin', 'https://github.com/Other/Repo.git')
    elif change == 'owner':
        sid = 'other'
    elif change == 'checkout':
        cwd = tmp_path / 'clone'
        subprocess.run(['git', 'clone', '-q', str(root), str(cwd)], check=True)
    elif change == 'unknown-head':
        before = 'a' * 40
    elif change == 'nonancestor':
        before = git(root, 'commit-tree', f'{before}^{{tree}}', '-m', 'Unrelated root')
    else:
        before = head
    with pytest.raises(ValueError):
        workflow.save_plan(store, sid, 'Owner/Repo', 1, cwd, plan, merged_head=head, from_head=before)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == old


@pytest.mark.parametrize('change', ['missing', 'old-merge', 'open', 'wrong-final-head', 'stale'])
def test_epoch_requires_fresh_proven_final_merge_after_plan(local, change):
    store, root, snap, _ = local
    old = prepare(local, inputs=True)
    before = snap['head_sha']
    proof = final_source(local)
    if change == 'missing':
        proof.pop('merged_at')
    elif change == 'old-merge':
        proof['merged_at'] = datetime.fromtimestamp(old['created_at'] - 1, timezone.utc).isoformat()
    elif change == 'open':
        proof.update(state='open', merged=False)
    elif change == 'wrong-final-head':
        proof['head_sha_after'] = before
    else:
        proof['collected_at'] = time.time() - 1000
    store.set_preflight('Owner/Repo', 1, proof)
    with pytest.raises(ValueError):
        epoch(local, before)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == old


def test_metadata_only_commit_cannot_reset_unchanged_inputs(local):
    store, root, snap, _ = local
    old = prepare(local, inputs=True)
    before = snap['head_sha']
    git(root, 'commit', '--allow-empty', '-qm', 'Metadata only')
    head = git(root, 'rev-parse', 'HEAD')
    snap.update(head_sha=head, head_sha_after=head)
    proof = merge(local)
    proof['merged_at'] = datetime.now(timezone.utc).isoformat()
    store.set_preflight('Owner/Repo', 1, proof)
    with pytest.raises(ValueError, match='Unchanged source inputs'):
        epoch(local, before)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == old


def test_active_original_process_blocks_epoch_then_keeps_actual_result(local):
    store, root, snap, _ = local
    old = prepare(local, inputs=True, live=True)
    before = snap['head_sha']
    outcomes, errors = [], []
    def launch():
        try:
            outcomes.append(workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'native-live-crash'))
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=launch)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not (root / '.started').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (root / '.started').exists()
        final_source(local)
        with pytest.raises(ValueError, match='active run'):
            epoch(local, before)
        assert store.validation_plan('s', 'Owner/Repo', 1, root) == old
    finally:
        (root / '.release').touch()
        worker.join(12)
    assert not worker.is_alive() and not errors
    actual = outcomes[0]['run']
    assert actual['exit_code'] == 0 and actual['status'] == 'unknown'
    assert actual['before_digest'] != actual['after_digest']
    new = epoch(local, before)['plan']
    assert store.validation_runs(old['plan_id']) == [actual]
    assert store.validation_evidence(new['plan_id']) == []


def test_cli_uses_unchanged_plan_and_worker_executes_only_fresh_final_source(local, tmp_path, monkeypatch, capsys):
    store, root, snap, plan = local
    old = prepare(local, inputs=True)
    before = snap['head_sha']
    proof = final_source(local)
    file = tmp_path / 'necessary-plan.json'
    file.write_text(json.dumps(plan))
    original_bytes = file.read_bytes()
    monkeypatch.setenv('CALLER_PR_GATES_STATE_DIR', str(store.state_dir))
    reads = []
    async def collect(*args):
        reads.append(args)
        return dict(proof, collected_at=time.time())
    monkeypatch.setattr(github, 'collect_validation', collect)
    monkeypatch.setattr(sys, 'argv', ['gate', 'validation-plan', '--session', 's', '--pr',
        'https://github.com/Owner/Repo/pull/1', '--cwd', str(root), '--file', str(file),
        '--merged-head', snap['head_sha'], '--from-head', before])
    cli.main()
    new = json.loads(capsys.readouterr().out)['plan']
    assert new['plan_id'] != old['plan_id'] and reads == [('Owner/Repo', 1)]
    assert file.read_bytes() == original_bytes
    original_popen = subprocess.Popen
    workers = []
    bootstrap = ('import json, time\nfrom caller_pr_gates import github, cli\n'
                 f'remote = json.loads({json.dumps(proof)!r})\n'
                 'async def collect(*args):\n    return dict(remote, collected_at=time.time())\n'
                 'github.collect_validation = collect\ncli.main()\n')
    def start(argv, **kwargs):
        worker = argv[1:4] == ['-P', '-m', 'caller_pr_gates.cli']
        if worker:
            assert '--from-head' not in argv and argv[-2:] == ['--merged-head', snap['head_sha']]
            argv = [argv[0], '-c', bootstrap, *argv[4:]]
        child = original_popen(argv, **kwargs)
        if worker:
            workers.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', start)
    launched = workflow.launch_check(store, 's', 'Owner/Repo', 1, root, 'remaining', merged_head=snap['head_sha'])
    assert launched['completion_verified'] is False
    assert workers[0].wait(timeout=10) == 0, Path(launched['log']).read_text()
    runs = store.validation_runs(new['plan_id'])
    assert len(runs) == 1 and runs[0]['status'] == 'pass' and runs[0]['exit_code'] == 0
    assert runs[0]['before_digest'] == runs[0]['after_digest'] == new['input_digest']
    assert store.validation_runs(old['plan_id']) == []


def test_open_pr_cannot_use_final_head_epoch(local):
    store, root, snap, plan = local
    prepare(local)
    before = snap['head_sha']
    with pytest.raises(ValueError, match='requires --merged-head'):
        workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan, from_head=before)
    with pytest.raises(ValueError, match='fresh stable proof'):
        epoch(local, before)
    assert workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'remaining')['run']['status'] == 'pass'


def test_caller_cannot_inject_epoch_provenance(local):
    _, root, _, plan = local
    altered = deepcopy(plan)
    altered['source_epoch'] = {'head_sha': 'a' * 40}
    with pytest.raises(ValueError, match='validation plan must contain'):
        progress.normalize_plan(altered, root)


@pytest.mark.parametrize('change', ['delete', 'mode', 'binary', 'rename'])
def test_historical_ordinary_source_is_reconstructed_without_checkout(local, change):
    store, root, snap, _ = local
    original = root / 'published.dat'
    original.write_bytes(b'original\x00source\xff')
    old = prepare(local, inputs=True)
    before = snap['head_sha']
    if change == 'delete':
        original.unlink()
    elif change == 'mode':
        original.chmod(0o755)
    elif change == 'binary':
        original.write_bytes(b'new\x00source\xff\n')
    else:
        original.rename(root / 'renamed.dat')
    final_source(local)
    final = snap['head_sha']
    assert epoch(local, before)['plan']['input_digest'] != old['input_digest']
    assert git(root, 'rev-parse', 'HEAD') == final
    assert git(root, 'status', '--porcelain') == ''
    assert store.validation_runs(old['plan_id']) == []


def test_changed_source_symlink_is_not_reconstructed_as_ordinary_file(local):
    _, root, snap, _ = local
    prepare(local, inputs=True)
    before = snap['head_sha']
    (root / 'link').symlink_to('src.txt')
    final_source(local)
    with pytest.raises(ValueError, match='ordinary Git source files'):
        epoch(local, before)


@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
@pytest.mark.parametrize('change', ['bytes', 'delete', 'mode'])
def test_index_hints_cannot_admit_unpublished_final_source(local, flag, change):
    store, root, snap, _ = local
    old = prepare(local, inputs=True)
    before = snap['head_sha']
    final_source(local)
    target = root / 'final-source.txt'
    git(root, 'update-index', flag, target.name)
    if change == 'bytes':
        target.write_text('Unpublished source hidden by an index hint\n')
    elif change == 'delete':
        target.unlink()
    else:
        target.chmod(0o755)
    assert git(root, 'status', '--porcelain') == ''
    assert git(root, '--no-replace-objects', 'diff', '--name-only', snap['head_sha'], '--') == ''
    workflow.validation_owner(store, 's', 'Owner/Repo', 1, root, snap['head_sha'], preflight=True)
    with pytest.raises(ValueError, match='exact published final tree'):
        epoch(local, before)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == old
    assert store.validation_runs(old['plan_id']) == []
    assert store.validation_evidence(old['plan_id']) == []


def test_unpublished_unchanged_tree_file_cannot_reconstruct_a_prior_pin(local):
    store, root, snap, plan = local
    prepare(local, inputs=True)
    before = snap['head_sha']
    git(root, 'update-index', '--assume-unchanged', 'src.txt')
    (root / 'src.txt').write_text('Unpublished bytes already in an older local pin')
    old = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    final_source(local)
    assert git(root, 'status', '--porcelain') == ''
    with pytest.raises(ValueError, match='do not reproduce the retained fingerprint'):
        epoch(local, before)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == old
    assert store.validation_runs(old['plan_id']) == []


def test_hidden_source_edit_after_admission_cannot_execute(local):
    store, root, snap, _ = local
    old = prepare(local, inputs=True)
    before = snap['head_sha']
    final_source(local)
    new = epoch(local, before)['plan']
    git(root, 'update-index', '--skip-worktree', 'final-source.txt')
    (root / 'final-source.txt').write_text('Unpublished edit after admission')
    assert git(root, 'status', '--porcelain') == ''
    with pytest.raises(ValueError, match='Post-merge validation inputs changed'):
        execute(local, 'remaining')
    assert store.validation_runs(old['plan_id']) == store.validation_runs(new['plan_id']) == []


@pytest.mark.parametrize('change', ['delete', 'rename', 'directory-file', 'file-directory', 'missing-directory'])
def test_declared_source_directory_types_are_reconstructed(local, change):
    store, root, snap, plan = local
    target = root / 'declared-source'
    if change == 'file-directory':
        target.write_bytes(b'original file\x00bytes')
    elif change != 'missing-directory':
        target.mkdir()
        (target / 'source.dat').write_bytes(b'original directory\x00bytes')
    prepare(local, inputs=True)
    plan['input_paths'].append(target.name)
    old = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    before = snap['head_sha']
    if change == 'rename':
        target.rename(root / 'renamed-source')
    elif change in {'delete', 'directory-file'}:
        (target / 'source.dat').unlink()
        target.rmdir()
        if change == 'directory-file':
            target.write_bytes(b'published replacement file')
    else:
        if change == 'file-directory':
            target.unlink()
        target.mkdir()
        (target / 'source.dat').write_bytes(b'published new directory')
    final_source(local)
    new = epoch(local, before)['plan']
    assert new['plan_id'] != old['plan_id'] and new['input_digest'] != old['input_digest']
    assert progress.plan_declaration(new['plan']) == old['plan']
    assert store.validation_evidence(new['plan_id']) == []
    actual = execute(local, 'remaining')['run']
    assert actual['status'] == 'pass' and actual['exit_code'] == 0
    assert actual['before_digest'] == actual['after_digest'] == new['input_digest']
    assert store.validation_runs(old['plan_id']) == []
    assert git(root, 'status', '--porcelain') == ''


def test_unchanged_published_symlink_retains_its_actual_target_pin(local):
    store, root, snap, _ = local
    (root / 'source-link').symlink_to('src.txt')
    old = prepare(local, inputs=True)
    before = snap['head_sha']
    final_source(local)
    new = epoch(local, before)['plan']
    assert store.validation_evidence(new['plan_id']) == []
    assert execute(local, 'remaining')['run']['status'] == 'pass'
    assert store.validation_runs(old['plan_id']) == []


def test_removed_published_file_cannot_remain_as_hidden_source(local):
    store, root, snap, _ = local
    target = root / 'removed-source.py'
    target.write_text('original published source')
    old = prepare(local, inputs=True)
    before = snap['head_sha']
    target.unlink()
    with (root / '.gitignore').open('a') as ignored:
        ignored.write(target.name + '\n')
    final_source(local)
    target.write_text('original published source')
    assert git(root, 'status', '--porcelain') == ''
    with pytest.raises(ValueError, match='Removed published source is still present'):
        epoch(local, before)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == old
    assert store.validation_runs(old['plan_id']) == []


def test_replace_ref_cannot_impersonate_the_published_source(local):
    _, root, snap, _ = local
    prepare(local, inputs=True)
    before = snap['head_sha']
    final_source(local)
    final = snap['head_sha']
    (root / 'src.txt').write_text('counterfeit final source')
    git(root, 'add', 'src.txt')
    git(root, 'commit', '-qm', 'Not published')
    counterfeit = git(root, 'rev-parse', 'HEAD')
    git(root, 'replace', final, counterfeit)
    git(root, 'update-ref', 'HEAD', final)
    assert git(root, 'status', '--porcelain') == ''
    with pytest.raises(ValueError):
        epoch(local, before)


def test_transformed_historical_worktree_bytes_refuse_without_repinning(local):
    store, root, snap, plan = local
    (root / '.gitattributes').write_text('transformed.txt text eol=crlf\n')
    (root / 'transformed.txt').write_bytes(b'original\r\n')
    old = prepare(local, inputs=True)
    before = snap['head_sha']
    assert subprocess.check_output(['git', '-C', str(root), 'show', f'{before}:transformed.txt']) == b'original\n'
    assert (root / 'transformed.txt').read_bytes() == b'original\r\n'
    (root / 'transformed.txt').write_bytes(b'published final\r\n')
    final_source(local)
    assert git(root, 'status', '--porcelain') == ''
    with pytest.raises(ValueError, match='do not reproduce the retained fingerprint'):
        epoch(local, before)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == old
    assert store.validation_runs(old['plan_id']) == []
    assert progress.plan_declaration(old['plan']) == progress.normalize_plan(plan, root)
    assert (root / 'transformed.txt').read_bytes() == b'published final\r\n'


@pytest.mark.parametrize('explicit_pin', [False, True])
def test_changed_ignore_membership_requires_an_existing_explicit_private_pin(local, explicit_pin):
    store, root, snap, plan = local
    prepare(local, inputs=True)
    before = snap['head_sha']
    artifact = root / 'local-artifact.txt'
    artifact.write_text('unchanged local input\n')
    if explicit_pin:
        plan['input_paths'].append('local-artifact.txt')
    old = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    assert git(root, 'status', '--porcelain') == '?? local-artifact.txt'
    with (root / '.gitignore').open('a') as target:
        target.write('local-artifact.txt\n')
    final_source(local)
    assert git(root, 'status', '--porcelain') == ''
    if explicit_pin:
        new = epoch(local, before)['plan']
        assert new['plan_id'] != old['plan_id']
        assert store.validation_evidence(new['plan_id']) == []
        assert execute(local, 'remaining')['run']['status'] == 'pass'
    else:
        with pytest.raises(ValueError, match='do not reproduce the retained fingerprint'):
            epoch(local, before)
        assert store.validation_plan('s', 'Owner/Repo', 1, root) == old
    assert artifact.read_text() == 'unchanged local input\n'
    assert store.validation_runs(old['plan_id']) == []
