"""A real transfer retains destination authority without moving prior evidence."""
import asyncio
from copy import deepcopy
import json
import sqlite3
import subprocess
import sys
import time

import pytest

from caller_pr_gates import monitor, ownership, progress, publication, workflow
from test_ownership_transfer import OLD, NEW, REPO, baseline, context, transfer

THIRD = '33333333-3333-4333-8333-333333333333'


def declare(context, sid, cwd):
    plan = progress.normalize_plan(dict(checks=[
        dict(id='passing', argv=[sys.executable, 'check_source.py', 'original'],
             reason='Verify the retained published source'),
        dict(id='failing', argv=[sys.executable, 'check_source.py', 'fixed'],
             reason='Keep the original failed behavior visible'),
        dict(id='pending', argv=[sys.executable, 'check_source.py', 'exists'],
             reason='Complete the still unexecuted declaration')]), cwd)
    return context['store'].save_validation_plan(sid, REPO, 1, cwd, plan,
                                                progress.fingerprint(cwd, plan), 'original-evidence')


def execute(store, row, check_id, *, retained=False):
    check = next(check for check in row['plan']['checks'] if check['id'] == check_id)
    run = store.begin_validation(row['plan_id'], check_id, row['input_digest'],
                                 session_id=row['session_id'], retained=retained)
    result = subprocess.run(check['argv'], cwd=row['cwd'], capture_output=True)
    return store.finish_validation(run['run_id'], result.returncode,
                                   progress.fingerprint(row['cwd'], row['plan']), session_id=row['session_id'])


def retire(context):
    store, head = context['store'], context['grant']['head_sha']
    monitor.retire(store, REPO, 1, dict(state='closed', merged=True, head={'sha': head}), checklist=True)
    store.set_preflight(REPO, 1, dict(repository=REPO, number=1, profile='validation',
        state='closed', merged=True, collected_at=time.time(), head_sha=head, head_sha_after=head,
        merge_commit_sha='b' * 40, base_ref='main', base_ref_after='main',
        head_repository=REPO, base_repository=REPO))


def rows(store, tables=('prs', 'validation_plans', 'validation_runs', 'dispositions')):
    with store._connect() as db:
        return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)] for table in tables}


def test_real_transfer_retains_own_plan_and_new_review_without_importing_old_credit(context, monkeypatch):
    store = context['store']
    old = declare(context, OLD, context['old'])
    assert execute(store, old, 'passing')['exit_code'] == 0
    assert execute(store, old, 'failing')['exit_code'] == 1
    store.disposition(OLD, REPO, 1, dict(comment_id='old-review', comment_updated_at='original',
                      disposition='nonfix', reason='Original owner public conclusion'))
    destination = declare(context, NEW, context['new'])
    transfer(context)
    retire(context)
    before = rows(store)
    old_runs = store.validation_runs(old['plan_id'])
    # A historical owner may now be archived. Retained authority is read from
    # the immutable transfer, never by reopening its native chat or checkout.
    with sqlite3.connect(context['home'] / 'state_5.sqlite') as db:
        db.execute('UPDATE threads SET archived=1 WHERE id=?', (OLD,))
    original_checkout = monitor.checkout
    def recipient_checkout(cwd):
        assert str(cwd) != str(context['old'])
        return original_checkout(cwd)
    monkeypatch.setattr(monitor, 'checkout', recipient_checkout)
    monkeypatch.setattr(ownership, '_native', lambda *_: pytest.fail('Historical native chat must not be reopened'))
    retained = workflow.validation_owner(store, NEW, REPO, 1, context['new'],
                                         context['grant']['head_sha'], preflight=True)
    assert retained['plan_id'] == destination['plan_id']
    assert store.validation_evidence(destination['plan_id']) == []
    assert rows(store) == before
    assert execute(store, retained, 'pending', retained=True)['exit_code'] == 0
    assert store.validation_runs(old['plan_id']) == old_runs
    assert {run['status'] for run in store.validation_runs(old['plan_id'])} == {'pass', 'fail'}
    owner = store.closed_review_owner(NEW, REPO, 1, context['new'])
    assert owner['identity']['registration']['session_id'] == NEW
    assert owner['identity']['original_registration']['session_id'] == OLD
    assert owner['identity']['checkout_anchor']['kind'] == 'ownership_transfer'
    owner['source'] = publication.source(context['new'], stable=True)
    receipt = dict(comment_id='late-review', comment_updated_at='later', disposition='nonfix',
                   reason='Recipient verified the later public conclusion', closed_head=context['grant']['head_sha'])
    store.disposition(NEW, REPO, 1, receipt, closed_owner=owner)
    with store._connect() as db:
        owners = {row['comment_id']: row['session_id'] for row in db.execute('SELECT * FROM dispositions')}
    assert owners == {'old-review': OLD, 'late-review': NEW}
    after = rows(store)
    assert after['prs'] == before['prs'] and after['validation_plans'] == before['validation_plans']
    assert all(run in after['validation_runs'] for run in before['validation_runs'])


def test_transfer_chain_retains_only_latest_recipient(context, monkeypatch, tmp_path):
    store = context['store']
    declare(context, OLD, context['old'])
    declare(context, NEW, context['new'])
    transfer(context)
    third = tmp_path / 'third'
    subprocess.run(['git', '-C', str(context['old']), 'worktree', 'add', '--detach', str(third)],
                   check=True, capture_output=True)
    transcript = context['home'] / 'sessions' / (THIRD + '.jsonl')
    transcript.write_bytes((context['home'] / 'sessions' / (NEW + '.jsonl')).read_bytes())
    with sqlite3.connect(context['home'] / 'state_5.sqlite') as db:
        db.execute('INSERT INTO threads VALUES(?,?,0,?)', (THIRD, str(third), str(transcript)))
    store.record_session(THIRD, 'codex', str(third))
    store.record_scope(THIRD, baseline())
    store.register(THIRD, 'codex', REPO, 1, str(third))
    plan = declare(context, THIRD, third)
    grant = deepcopy(context['grant'])
    grant.update({'from': context['grant']['to'], 'to': dict(session_id=THIRD, client='codex', cwd=str(third))})
    store.record_scope(THIRD, dict(baseline(context['scope']['approval_evidence']), pr_ownership_transfer=grant),
                       work_item='second-transfer')
    monkeypatch.setenv('CODEX_THREAD_ID', THIRD)
    asyncio.run(ownership.transfer(store, THIRD, REPO, 1, 'codex', third, 'second-transfer', reader=context['reader']))
    retire(context)
    assert store.retained_validation_plan(THIRD, REPO, 1, third)['plan_id'] == plan['plan_id']
    assert len(store.closed_review_owner(THIRD, REPO, 1, third)['identity']['transfer_history']) == 2
    for sid, cwd in ((OLD, context['old']), (NEW, context['new'])):
        with pytest.raises(ValueError, match='audited transfer recipient'):
            store.retained_validation_plan(sid, REPO, 1, cwd)
        with pytest.raises(ValueError, match='audited transfer recipient'):
            store.closed_review_owner(sid, REPO, 1, cwd)


def test_former_first_owner_cannot_reclaim_retained_authority_by_registering_after_merge(context):
    store = context['store']
    declare(context, OLD, context['old'])
    declare(context, NEW, context['new'])
    transfer(context)
    retire(context)
    store.register(OLD, 'codex', REPO, 1, context['old'])
    retire(context)
    for sid, cwd in ((OLD, context['old']), (NEW, context['new'])):
        with pytest.raises(ValueError, match='audited transfer recipient'):
            store.retained_validation_plan(sid, REPO, 1, cwd)
        with pytest.raises(ValueError, match='audited transfer recipient'):
            store.closed_review_owner(sid, REPO, 1, cwd)


def test_recipient_late_gateway_acknowledgement_preserves_transfer_anchor(context):
    store = context['store']
    declare(context, NEW, context['new'])
    transfer(context)
    retire(context)
    store.register(NEW, 'codex', REPO, 1, context['new'])
    store.audit('ownership_evidence', dict(session_id=NEW, target=f'https://github.com/{REPO}/pull/1',
                origin='gateway_write', operation=['pr', 'comment']))
    retire(context)
    identity = store.closed_review_owner(NEW, REPO, 1, context['new'])['identity']
    assert len(identity['acknowledgements']) == 1
    assert identity['checkout_anchor']['kind'] == 'ownership_transfer'
    assert identity['original_registration']['session_id'] == OLD


@pytest.mark.parametrize('change', ['missing-requester', 'foreign-requester', 'grant', 'previous-watch', 'binding', 'registration', 'client', 'cwd'])
def test_incoherent_transfer_cannot_grant_retained_authority(context, change):
    store = context['store']
    declare(context, NEW, context['new'])
    transfer(context)
    retire(context)
    with store._connect() as db:
        if change in {'missing-requester', 'foreign-requester', 'grant', 'previous-watch'}:
            row = db.execute("SELECT id,fields FROM audit WHERE event='pr_owner_transferred'").fetchone()
            receipt = json.loads(row['fields'])
            if change == 'missing-requester':
                receipt.pop('native_requester')
            elif change == 'foreign-requester':
                receipt['native_requester'] = OLD
            elif change == 'grant':
                receipt['grant']['head_sha'] = 'c' * 40
            else:
                receipt['previous_watch']['cwd'] = '/other/checkout'
            db.execute('UPDATE audit SET fields=? WHERE id=?', (json.dumps(receipt), row['id']))
        elif change == 'binding':
            db.execute('UPDATE pr_scope_bindings SET scope=? WHERE session_id=?', (json.dumps(baseline('Changed')), NEW))
        elif change == 'registration':
            db.execute('UPDATE prs SET cwd=? WHERE session_id=?', ('/other/checkout', NEW))
        else:
            db.execute('UPDATE pr_watch SET ' + change + '=?', ('other' if change == 'client' else '/other/checkout',))
    with pytest.raises(ValueError, match='audited transfer recipient'):
        store.retained_validation_plan(NEW, REPO, 1, context['new'])
    with pytest.raises(ValueError, match='audited transfer recipient'):
        store.closed_review_owner(NEW, REPO, 1, context['new'])


def test_transfer_does_not_invent_a_recipient_plan(context):
    store = context['store']
    declare(context, OLD, context['old'])
    transfer(context)
    retire(context)
    with pytest.raises(ValueError, match='retained plan'):
        store.retained_validation_plan(NEW, REPO, 1, context['new'])
    assert store.validation_plans(NEW) == []


def test_reregistration_cannot_create_a_first_recipient_plan_after_retirement(context):
    store = context['store']
    transfer(context)
    retire(context)
    store.register(NEW, 'codex', REPO, 1, context['new'])
    declare(context, NEW, context['new'])
    retire(context)
    with pytest.raises(ValueError, match='pre-retirement plan'):
        store.retained_validation_plan(NEW, REPO, 1, context['new'])


def test_transfer_does_not_replace_source_or_checkout_proof(context):
    store = context['store']
    declare(context, NEW, context['new'])
    transfer(context)
    retire(context)
    with pytest.raises(ValueError, match='exact recorded owner'):
        store.retained_validation_plan(NEW, REPO, 1, context['old'])
    (context['new'] / 'source.txt').write_text('unpublished change\n')
    with pytest.raises(ValueError, match='clean recorded checkout'):
        workflow.validation_owner(store, NEW, REPO, 1, context['new'], context['grant']['head_sha'])


def test_registration_and_grant_without_transfer_never_create_retained_authority(context):
    store = context['store']
    declare(context, NEW, context['new'])
    retire(context)
    with pytest.raises(ValueError, match='exact recorded owner'):
        store.retained_validation_plan(NEW, REPO, 1, context['new'])
    with pytest.raises(ValueError, match='exact retained owner'):
        store.closed_review_owner(NEW, REPO, 1, context['new'])
