"""Real gateway registration cycles retain closed receipt ownership, not execution credit."""
from copy import deepcopy
import json
import subprocess
from types import SimpleNamespace

import pytest

from caller_pr_gates import cli, monitor, workflow
import test_closed_review_receipts as closed
from test_workflow import local, git


def gateway_comment(local, monkeypatch, *, sid='s', cwd=None, client='codex', operation='comment'):
    store, root, _, _ = local
    calls = []

    def upstream(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=closed.URL + '#issuecomment-12\n', stderr='')

    monkeypatch.setattr(cli, 'github_run', upstream)
    args = SimpleNamespace(session=sid, client=client, cwd=str(cwd or root), kind=None,
                           slice_id=None, target=None,
                           arguments=['pr', operation, closed.URL, '--body', closed.REASON])
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


def test_new_registration_audit_anchors_receipts_without_a_validation_plan(local, monkeypatch, capsys):
    store, root, _, _ = local
    review = closed.review_snapshot(local)
    gateway_comment(local, monkeypatch)
    retire(local)
    owner = store.closed_review_owner('s', 'Owner/Repo', 1, root)
    assert owner['identity']['original_cwd'] == str(root)
    assert owner['identity']['registration_history'][0]['client'] == 'codex'
    assert not store.validation_plans('s')
    assert closed.address(local, monkeypatch, capsys, review)[0]['receipt']['closed_head'] == review['head_sha']


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
    assert 'immutable original checkout' in capsys.readouterr().err
    assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('change', ['plain_register', 'wrong_origin', 'wrong_operation', 'wrong_target',
                                   'wrong_actor', 'duplicate_proof', 'proof_after_retirement', 'unretired'])
def test_only_completed_original_owner_gateway_comment_cycles_are_admitted(
        local, monkeypatch, capsys, change):
    store, root, _, _ = local
    review = closed.review_snapshot(local)
    if change in {'plain_register', 'proof_after_retirement'}:
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
    elif change == 'proof_after_retirement':
        store.audit('ownership_evidence', dict(session_id='s', target=closed.URL,
                    origin='gateway_write', operation=['pr', 'comment']))
    with pytest.raises(SystemExit):
        closed.address(local, monkeypatch, capsys, review)
    assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('change', ['owner', 'child', 'client', 'checkout', 'head', 'lifecycle'])
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
    elif change == 'checkout':
        other = tmp_path / 'other'
        git(root, 'clone', '-q', str(root), str(other))
        git(other, 'remote', 'set-url', 'origin', 'https://github.com/Owner/Repo.git')
        gateway_comment(local, monkeypatch, cwd=other)
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


def test_legacy_checkout_roundtrip_cannot_hide_an_intervening_checkout_change(
        local, tmp_path, monkeypatch, capsys):
    store, root, _, plan = local
    workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)
    review = closed.review_snapshot(local)
    other = tmp_path / 'other'
    git(root, 'clone', '-q', str(root), str(other))
    git(other, 'remote', 'set-url', 'origin', 'https://github.com/Owner/Repo.git')
    gateway_comment(local, monkeypatch, cwd=other)
    retire(local)
    gateway_comment(local, monkeypatch)
    retire(local)
    legacy_registration_audits(store)
    # The current mutable rows have returned to the original root, but the
    # first plan alone cannot establish each later legacy registration's cwd.
    with pytest.raises(SystemExit):
        closed.address(local, monkeypatch, capsys, review)
    assert store.dispositions('Owner/Repo', 1) == []


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
