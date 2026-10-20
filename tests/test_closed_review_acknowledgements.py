"""Real gateway registration cycles retain closed receipt ownership, not execution credit."""
from copy import deepcopy
import json
import subprocess
from types import SimpleNamespace

import pytest

from caller_pr_gates import cli, monitor, workflow
from caller_pr_gates.policy import evaluate_pr
from caller_pr_gates.store import Store
import test_closed_review_receipts as closed
from test_workflow import local, git


def gateway_comment(local, monkeypatch, *, sid='s', cwd=None, client='codex', operation='comment', body_file=None):
    store, root, _, _ = local
    calls = []

    def upstream(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=closed.URL + '#issuecomment-12\n', stderr='')

    monkeypatch.setattr(cli, 'github_run', upstream)
    body = ['--body', closed.REASON]
    if body_file is not None:
        body_file.write_text(closed.REASON)
        body = ['--body-file', str(body_file)]
    args = SimpleNamespace(session=sid, client=client, cwd=str(cwd or root), kind=None,
                           slice_id=None, target=None,
                           arguments=['pr', operation, closed.URL, *body])
    cli.gateway(args, store)
    assert calls == [['gh', *args.arguments]]


def retire(local, *, merged=True, head=None):
    store, _, snap, _ = local
    monitor.retire(store, 'Owner/Repo', 1, dict(state='closed', merged=merged,
                   head={'sha': head or snap['head_sha'], 'ref': 'feature'}), checklist=True)


def legacy_registration_audits(store):
    # Reconstruct pre-0.1.54 fixture records only; production never edits history.
    with store._connect() as db:
        db.execute("UPDATE audit SET fields=json_remove(fields,'$.cwd','$.client') WHERE event='pr_registered'")


def retained_state(store):
    with store._connect() as db:
        return {table: [dict(row) for row in db.execute('SELECT * FROM ' + table)]
                for table in ('prs', 'pr_watch', 'validation_plans', 'validation_runs')}


@pytest.mark.parametrize('legacy,merged,cycles', [(True, True, 1), (True, True, 2), (False, True, 1), (False, False, 1)])
def test_completed_gateway_acknowledgements_allow_both_parent_receipts_without_rearming(
        local, monkeypatch, capsys, legacy, merged, cycles):
    store, root, _, plan = local
    payload = deepcopy(plan)
    payload['checks'].append(dict(payload['checks'][0], id='pending',
                                  argv=[*payload['checks'][0]['argv'], 'pending']))
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, payload)['plan']
    assert workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'regression')['run']['exit_code'] == 1
    review = closed.review_snapshot(local, merged=merged)
    for _ in range(cycles):
        gateway_comment(local, monkeypatch)
        retire(local, merged=merged)
    if legacy:
        legacy_registration_audits(store)
    before = retained_state(store)
    with store._connect() as db:
        first_retirement = db.execute("SELECT MIN(id) FROM audit WHERE event='pr_monitor_retired'").fetchone()[0]
        last_registration = db.execute("SELECT MAX(id) FROM audit WHERE event='pr_registered'").fetchone()[0]
        last_retirement = db.execute("SELECT MAX(id) FROM audit WHERE event='pr_monitor_retired'").fetchone()[0]
    # This exact valid history was rejected by the original first-retirement guard.
    assert first_retirement < last_registration < last_retirement
    first, calls = closed.address(local, monkeypatch, capsys, review)
    assert calls == [('Owner/Repo', 1)] and first['receipt']['retirement_id'] == last_retirement
    final, _ = closed.address(local, monkeypatch, capsys, review, comment='review:11')
    assert {gate['status'] for gate in final['review_gates']} == {'PASS'}
    assert final['completion_verified'] is False
    assert retained_state(store) == before
    assert not store.prs('s')
    assert store.validation_runs(row['plan_id'])[0]['status'] == 'fail'
    assert not any(run['check_id'] == 'pending' for run in store.validation_runs(row['plan_id']))
    assert workflow.task_progress(store, 's')[0]['status'] != 'PASS'


def test_legacy_comment_then_body_edits_keep_both_receipts_on_the_retained_checkout(
        local, tmp_path, monkeypatch, capsys):
    store, root, _, plan = local
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    review = closed.review_snapshot(local)
    gateway_comment(local, monkeypatch)
    gateway_comment(local, monkeypatch, operation='edit', body_file=tmp_path / 'first-body.md')
    retire(local)
    gateway_comment(local, monkeypatch, operation='edit', body_file=tmp_path / 'second-body.md')
    retire(local)
    legacy_registration_audits(store)
    before = retained_state(store)
    first, _ = closed.address(local, monkeypatch, capsys, review)
    final, _ = closed.address(local, monkeypatch, capsys, review, comment='review:11')
    assert first['receipt']['checkout_anchor'] == {'kind': 'validation_plan', 'id': row['plan_id'], 'cwd': str(root)}
    assert {gate['status'] for gate in final['review_gates']} == {'PASS'}
    assert final['completion_verified'] is False and retained_state(store) == before
    assert not store.prs('s') and not store.validation_runs(row['plan_id'])
    assert workflow.task_progress(store, 's')[0]['status'] != 'PASS'


def test_new_registration_audit_anchors_receipts_without_a_validation_plan(local, monkeypatch, capsys):
    store, root, _, _ = local
    review = closed.review_snapshot(local)
    gateway_comment(local, monkeypatch)
    retire(local)
    owner = store.closed_review_owner('s', 'Owner/Repo', 1, root)
    assert owner['identity']['checkout_anchor']['cwd'] == str(root)
    assert owner['identity']['checkout_anchor']['kind'] == 'registration'
    assert owner['identity']['registration_history'][0]['client'] == 'codex'
    assert not store.validation_plans('s')
    assert closed.address(local, monkeypatch, capsys, review)[0]['receipt']['closed_head'] == review['head_sha']


def test_legacy_original_alias_and_current_canonical_path_use_the_same_checkout(
        local, tmp_path, monkeypatch, capsys):
    _, root, snap, plan = local
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    store = Store(tmp_path / 'alias-state')
    store.register('s', 'codex', 'Owner/Repo', 1, alias)
    store.set_snapshot('Owner/Repo', 1, snap, evaluate_pr(snap, []))
    context = (store, root, snap, plan)
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    review = closed.review_snapshot(context)
    gateway_comment(context, monkeypatch)
    retire(context)
    legacy_registration_audits(store)
    before = retained_state(store)
    first, _ = closed.address(context, monkeypatch, capsys, review, cwd=alias)
    final, _ = closed.address(context, monkeypatch, capsys, review, comment='review:11')
    assert first['receipt']['checkout_anchor'] == {'kind': 'validation_plan', 'id': row['plan_id'], 'cwd': str(root)}
    assert final['receipt']['cwd'] == str(root)
    assert {gate['status'] for gate in final['review_gates']} == {'PASS'}
    assert retained_state(store) == before


@pytest.mark.parametrize('change', ['missing', 'late'])
def test_legacy_repeated_registration_needs_a_pre_retirement_checkout_anchor(
        local, monkeypatch, capsys, change):
    store, root, _, plan = local
    if change == 'late':
        row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    review = closed.review_snapshot(local)
    gateway_comment(local, monkeypatch)
    retire(local)
    legacy_registration_audits(store)
    if change == 'late':
        with store._connect() as db:
            stamp = db.execute("SELECT MIN(recorded_at) FROM audit WHERE event='pr_monitor_retired'").fetchone()[0]
            db.execute('UPDATE validation_plans SET created_at=? WHERE plan_id=?', (stamp + 1, row['plan_id']))
    with pytest.raises(SystemExit):
        closed.address(local, monkeypatch, capsys, review)
    assert 'pre-retirement checkout anchor' in capsys.readouterr().err
    assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('change', ['plain_register', 'wrong_origin', 'wrong_operation', 'wrong_target',
                                   'wrong_actor', 'duplicate_proof', 'unretired'])
def test_only_completed_original_owner_gateway_acknowledgement_cycles_are_admitted(
        local, monkeypatch, capsys, change):
    store, root, _, _ = local
    review = closed.review_snapshot(local)
    if change == 'plain_register':
        store.register('s', 'codex', 'Owner/Repo', 1, root)
    else:
        gateway_comment(local, monkeypatch)
    if change == 'duplicate_proof':
        store.audit('ownership_evidence', dict(session_id='s', target=closed.URL,
                    origin='gateway_write', operation=['pr', 'comment']))
    if change != 'unretired':
        retire(local)
    if change in {'wrong_origin', 'wrong_operation', 'wrong_target', 'wrong_actor'}:
        with store._connect() as db:
            row = db.execute("SELECT id, fields FROM audit WHERE event='ownership_evidence' ORDER BY id DESC LIMIT 1").fetchone()
            fields = json.loads(row['fields'])
            key, value = {'wrong_origin': ('origin', 'local_mutation'),
                          'wrong_operation': ('operation', ['pr', 'reopen']),
                          'wrong_target': ('target', 'https://github.com/Other/Repo/pull/1'),
                          'wrong_actor': ('session_id', 'other')}[change]
            fields[key] = value
            db.execute('UPDATE audit SET fields=? WHERE id=?', (json.dumps(fields), row['id']))
    with pytest.raises(SystemExit):
        closed.address(local, monkeypatch, capsys, review)
    assert store.dispositions('Owner/Repo', 1) == []


def test_retirement_between_gateway_registration_and_its_evidence_preserves_receipt_access(
        local, monkeypatch, capsys):
    store, _, _, _ = local
    review = closed.review_snapshot(local)
    original = store.audit

    def interleaved(event, fields):
        if event == 'ownership_evidence':
            retire(local)
        return original(event, fields)

    monkeypatch.setattr(store, 'audit', interleaved)
    gateway_comment(local, monkeypatch)
    before = retained_state(store)
    with store._connect() as db:
        latest_retirement = db.execute("SELECT MAX(id) FROM audit WHERE event='pr_monitor_retired'").fetchone()[0]
        proof = db.execute("SELECT MAX(id) FROM audit WHERE event='ownership_evidence'").fetchone()[0]
    assert latest_retirement < proof
    result, _ = closed.address(local, monkeypatch, capsys, review)
    assert result['receipt']['retirement_id'] == latest_retirement
    assert retained_state(store) == before and not store.prs('s')


def test_original_root_receipt_survives_registration_by_a_different_client_child(
        local, tmp_path, monkeypatch, capsys):
    _, root, snap, plan = local
    store = Store(tmp_path / 'child-state')
    store.record_session('s', 'codex', root)
    store.register('child', 'claude', 'Owner/Repo', 1, root, parent_session='s')
    context = (store, root, snap, plan)
    review = closed.review_snapshot(context)
    with store._connect() as db:
        assert db.execute('SELECT client FROM prs').fetchone()[0] == 'claude'
        assert db.execute('SELECT client FROM pr_watch').fetchone()[0] == 'codex'
    assert closed.address(context, monkeypatch, capsys, review)[0]['receipt']['closed_head'] == snap['head_sha']
    with pytest.raises(SystemExit):
        closed.address(context, monkeypatch, capsys, review, sid='child', comment='review:11')
    assert {receipt['comment_id'] for receipt in store.dispositions('Owner/Repo', 1)} == {'review:10'}


@pytest.mark.parametrize('legacy', [True, False])
def test_plan_checkout_is_an_explicit_legacy_anchor_not_registration_history_proof(
        local, tmp_path, monkeypatch, capsys, legacy):
    store, root, _, plan = local
    other = tmp_path / 'other'
    git(root, 'clone', '-q', str(root), str(other))
    git(other, 'remote', 'set-url', 'origin', 'https://github.com/Owner/Repo.git')
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, other, plan)['plan']
    assert row['cwd'] == str(other)
    review = closed.review_snapshot(local)
    gateway_comment(local, monkeypatch, cwd=other)
    retire(local)
    if legacy:
        legacy_registration_audits(store)
        before = retained_state(store)
        result, _ = closed.address(local, monkeypatch, capsys, review, cwd=other)
        assert result['receipt']['checkout_anchor'] == {'kind': 'validation_plan', 'id': row['plan_id'], 'cwd': str(other)}
        assert result['completion_verified'] is False and retained_state(store) == before
        assert not store.validation_runs(row['plan_id'])
        assert workflow.task_progress(store, 's')[0]['status'] != 'PASS'
    else:
        # A recorded original checkout cannot be replaced by a plan elsewhere.
        with pytest.raises(SystemExit):
            closed.address(local, monkeypatch, capsys, review, cwd=other)
        assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('change', ['owner', 'child', 'client', 'head', 'lifecycle'])
def test_restoring_current_identity_cannot_hide_a_changed_registration_or_retirement(
        local, tmp_path, monkeypatch, capsys, change):
    store, root, snap, _ = local
    review = closed.review_snapshot(local)
    if change in {'owner', 'child'}:
        sid = 'other' if change == 'owner' else 'child'
        store.record_session(sid, 'codex', root, parent_session='s' if change == 'child' else None)
        gateway_comment(local, monkeypatch, sid=sid)
    elif change == 'client':
        gateway_comment(local, monkeypatch, client='claude')
    else:
        gateway_comment(local, monkeypatch)
    retire(local, head='b' * 40 if change == 'head' else None, merged=change != 'lifecycle')
    gateway_comment(local, monkeypatch)
    retire(local)
    with pytest.raises(SystemExit):
        closed.address(local, monkeypatch, capsys, review)
    assert store.dispositions('Owner/Repo', 1) == []


def test_legacy_plan_anchor_rejects_a_new_checkout_with_the_same_head(local, tmp_path, monkeypatch, capsys):
    store, root, _, plan = local
    workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)
    review = closed.review_snapshot(local)
    other = tmp_path / 'other'
    git(root, 'clone', '-q', str(root), str(other))
    git(other, 'remote', 'set-url', 'origin', 'https://github.com/Owner/Repo.git')
    gateway_comment(local, monkeypatch, cwd=other)
    retire(local)
    legacy_registration_audits(store)
    with pytest.raises(SystemExit):
        closed.address(local, monkeypatch, capsys, review, cwd=other)
    assert 'original recorded checkout' in capsys.readouterr().err
    assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('legacy', [True, False])
def test_checkout_roundtrip_preserves_history_but_requires_the_current_retained_root(
        local, tmp_path, monkeypatch, capsys, legacy):
    store, root, _, plan = local
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    assert workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'regression')['run']['exit_code'] == 1
    review = closed.review_snapshot(local)
    other = tmp_path / 'other'
    git(root, 'clone', '-q', str(root), str(other))
    git(other, 'remote', 'set-url', 'origin', 'https://github.com/Owner/Repo.git')
    gateway_comment(local, monkeypatch, cwd=other)
    retire(local)
    gateway_comment(local, monkeypatch)
    retire(local)
    if legacy:
        legacy_registration_audits(store)
    before = retained_state(store)
    # The receipt proves current source/review identity, not that earlier
    # comments were executed in this checkout. The other root is still refused.
    with pytest.raises(SystemExit):
        closed.address(local, monkeypatch, capsys, review, cwd=other)
    assert store.dispositions('Owner/Repo', 1) == []
    result, _ = closed.address(local, monkeypatch, capsys, review)
    assert result['receipt']['cwd'] == str(root)
    assert result['receipt']['checkout_anchor']['cwd'] == str(root)
    assert result['completion_verified'] is False and retained_state(store) == before
    assert [run['status'] for run in store.validation_runs(row['plan_id'])] == ['fail']
    assert workflow.task_progress(store, 's')[0]['status'] != 'PASS'


@pytest.mark.parametrize('change', ['dirty', 'head', 'origin', 'reply', 'lifecycle'])
def test_legacy_body_edit_history_cannot_bypass_current_source_or_review_proof(
        local, tmp_path, monkeypatch, capsys, change):
    store, root, _, plan = local
    workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)
    review = closed.review_snapshot(local)
    gateway_comment(local, monkeypatch, operation='edit', body_file=tmp_path / 'body.md')
    retire(local)
    legacy_registration_audits(store)
    if change == 'dirty':
        (root / 'src.txt').write_text('unpublished')
    elif change == 'head':
        git(root, 'commit', '--allow-empty', '-qm', 'Different source identity')
    elif change == 'origin':
        git(root, 'remote', 'set-url', 'origin', 'https://github.com/Other/Repo.git')
    elif change == 'reply':
        review['comments'][-1]['body'] = 'An unrelated acknowledgement.'
    else:
        review.update(state='open', merged=False)
    before = retained_state(store)
    with pytest.raises(SystemExit):
        closed.address(local, monkeypatch, capsys, review)
    assert store.dispositions('Owner/Repo', 1) == []
    assert retained_state(store) == before and not store.prs('s')


@pytest.mark.parametrize('phase', ['collection', 'transaction'])
def test_another_valid_cycle_during_admission_still_requires_fresh_receipt_collection(
        local, monkeypatch, capsys, phase):
    store, _, _, _ = local
    review = closed.review_snapshot(local)
    gateway_comment(local, monkeypatch)
    retire(local)

    def cycle():
        gateway_comment(local, monkeypatch)
        retire(local)

    if phase == 'collection':
        original = closed.inventory

        def collect(snapshot):
            cycle()
            return original(snapshot)

        monkeypatch.setattr(closed, 'inventory', collect)
    else:
        original = store.disposition

        def record(*args, **kwargs):
            cycle()
            return original(*args, **kwargs)

        monkeypatch.setattr(store, 'disposition', record)
    with pytest.raises(SystemExit):
        closed.address(local, monkeypatch, capsys, review)
    assert 'ownership changed' in capsys.readouterr().err.lower()
    assert store.dispositions('Owner/Repo', 1) == []
