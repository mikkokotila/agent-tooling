"""Published bytes preserve frozen permissions; no old result is manufactured."""
from copy import deepcopy
import os
import stat

import pytest

from caller_pr_gates import progress, workflow
from test_workflow import git, local as original_local, publish
from test_post_merge_qualification import prepare
from test_final_head_epoch import epoch, final_source
from test_final_coverage import (
    delivered as original_delivered,
    test_absent_original_checkout_unknown_private_bytes_and_divergent_heads_allow_current_full_coverage
    as assert_original_final_coverage,
)


@pytest.fixture
def restrictive_local(tmp_path):
    previous = os.umask(0o077)
    try:
        yield original_local.__wrapped__(tmp_path)
    finally:
        os.umask(previous)


@pytest.fixture
def restrictive_delivered(tmp_path, monkeypatch, request):
    previous = os.umask(0o077)
    try:
        yield original_delivered.__wrapped__(tmp_path, monkeypatch, request)
    finally:
        os.umask(previous)


def permissions(root):
    files, _ = progress.source_tree(root, git(root, 'rev-parse', 'HEAD'))
    return {name: stat.S_IMODE((root / name).lstat().st_mode) for name in files}


@pytest.mark.parametrize('mode', [0o600, 0o640, 0o664, 0o700, 0o740, 0o775])
def test_published_bytes_keep_each_actual_full_permission_pin(restrictive_local, mode):
    _, root, _, plan = restrictive_local
    path = root / 'permissions.dat'
    path.write_bytes(b'exact published bytes')
    path.chmod(mode)  # This case declares this mode; the comparison must not change it.
    publish(restrictive_local)
    files, _ = progress.source_tree(root, git(root, 'rev-parse', 'HEAD'))
    assert files[path.name][0] == (0o100755 if mode & stat.S_IXUSR else 0o100644)
    before = permissions(root)
    pin = progress.fingerprint(root, plan)
    assert progress.fingerprint(root, plan, source_snapshot=(files, None)) == pin
    assert progress.fingerprint(root, plan) == pin
    assert permissions(root) == before and before[path.name] == mode


def test_reported_final_coverage_failure_passes_with_original_assertions_and_0600_files(restrictive_delivered):
    data = restrictive_delivered
    before = permissions(data['final'])
    assert set(before.values()) == {0o600}
    assert_original_final_coverage(data)
    assert permissions(data['final']) == before


@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
@pytest.mark.parametrize('change', ['bytes', 'executable', 'symlink'])
def test_even_a_frozen_hidden_source_change_cannot_match_published_source(restrictive_local, flag, change):
    store, root, snap, plan = restrictive_local
    target = root / 'src.txt'
    git(root, 'update-index', flag, target.name)
    if change == 'bytes':
        target.write_text('unpublished bytes')
    elif change == 'executable':
        target.chmod(0o700)
    else:
        target.unlink()
        target.symlink_to('check.py')
    assert git(root, 'status', '--porcelain') == ''
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    assert progress.fingerprint(root, row['plan']) == row['input_digest']
    context = {'final': {'row': row, 'identity': {'retirement': {'head_sha': snap['head_sha']}}}}
    with pytest.raises(ValueError, match='exact published final tree'):
        workflow._final_coverage_local(context, root)
    assert store.validation_runs(row['plan_id']) == []


@pytest.mark.parametrize('target', ['tracked', 'private'])
def test_changed_permission_bits_remain_changed_frozen_inputs(restrictive_local, target):
    store, root, snap, plan = restrictive_local
    row = prepare(restrictive_local, inputs=True)
    context = {'final': {'row': row, 'identity': {'retirement': {'head_sha': snap['head_sha']}}}}
    assert workflow._final_coverage_local(context, root)
    path = root / ('src.txt' if target == 'tracked' else '.inputs')
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o640)
    assert git(root, 'status', '--porcelain') == ''
    assert progress.fingerprint(root, row['plan']) != row['input_digest']
    with pytest.raises(ValueError, match='current frozen inputs'):
        workflow._final_coverage_local(context, root)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == row
    assert store.validation_runs(row['plan_id']) == []


@pytest.mark.parametrize('change', ['delete', 'mode', 'rename', 'directory'])
def test_historical_candidate_must_reproduce_full_old_pin_and_keeps_real_failure(restrictive_local, change):
    store, root, snap, _ = restrictive_local
    path = root / 'published.dat'
    path.write_bytes(b'original published bytes')
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    old = prepare(restrictive_local, inputs=True, failing=True)
    before = snap['head_sha']
    failed = workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'native-live-crash')['run']
    assert failed['status'] == 'fail' and failed['exit_code'] == 1
    history = deepcopy(store.validation_runs(old['plan_id']))
    if change == 'delete':
        path.unlink()
    elif change == 'mode':
        path.chmod(0o755)
    elif change == 'rename':
        path.rename(root / 'renamed.dat')
    else:
        path.unlink()
        path.mkdir()
        (path / 'child.dat').write_bytes(b'new published child')
    final_source(restrictive_local, fix=True)
    historical, current = progress.source_snapshots(root, before, snap['head_sha'])
    assert progress.fingerprint(root, old['plan'], source_snapshot=historical) == old['input_digest']
    saved = epoch(restrictive_local, before)
    new = saved['plan']
    assert new['plan_id'] != old['plan_id'] and new['input_digest'] != old['input_digest']
    assert progress.fingerprint(root, new['plan'], source_snapshot=current) == new['input_digest']
    assert store.validation_runs(old['plan_id']) == history
    assert store.validation_evidence(new['plan_id']) == []
    assert saved['progress']['passed'] == []
    assert set(saved['progress']['pending']) == {'native-live-crash', 'remaining'}
    with store._connect() as db:
        original = store._validation_plan_row(db.execute(
            'SELECT * FROM validation_plans WHERE plan_id=?', (old['plan_id'],)).fetchone())
    assert original['input_digest'] == old['input_digest'] and original['plan'] == old['plan']


@pytest.mark.parametrize('unknown', ['different_removed_mode', 'ambiguous_modes'])
def test_unknown_historical_permissions_never_replace_or_bypass_old_pin(restrictive_local, unknown):
    store, root, snap, _ = restrictive_local
    path = root / 'removed.dat'
    path.write_bytes(b'historical bytes')
    if unknown == 'different_removed_mode':
        path.chmod(0o640)
    else:
        (root / 'src.txt').chmod(0o640)
    old = prepare(restrictive_local, inputs=True)
    before = snap['head_sha']
    path.unlink()
    final_source(restrictive_local)
    with pytest.raises(ValueError, match='do not reproduce the retained fingerprint'):
        epoch(restrictive_local, before)
    assert store.validation_plan('s', 'Owner/Repo', 1, root) == old
    assert store.validation_runs(old['plan_id']) == []
    assert store.validation_evidence(old['plan_id']) == []
