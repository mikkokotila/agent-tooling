"""Explicit withdrawal retires an owned PR without certifying its unfinished work."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import time
import uuid

from .github import collect_pr
from .policy import evaluate_pr
from .publication import exact_source, source
from .transport import GitHubClient


def _owner(store, db, sid, repository, number, client, directory, *, retired=False):
    requester = os.environ.get('CODEX_THREAD_ID')
    if not requester:
        raise ValueError('Withdrawal requires the actual native Codex requester')
    uuid.UUID(requester)
    session = db.execute('SELECT * FROM sessions WHERE session_id=?', (requester,)).fetchone()
    root_session = db.execute('SELECT client FROM sessions WHERE session_id=?', (sid,)).fetchone()
    parent = store._parents(db)
    watch = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=?', (repository, number)).fetchone()
    registered = db.execute('SELECT * FROM prs WHERE repository=? AND number=? AND session_id=?',
                            (repository, number, sid)).fetchone()
    if (client != 'codex' or not session or session['client'] != client
            or not root_session or root_session['client'] != client
            or store._root(requester, parent) != sid or store._root(sid, parent) != sid
            or not watch or watch['state'] != ('closed' if retired else 'open') or watch['owner_session'] != sid
            or watch['client'] != client or watch['cwd'] != directory
            or not registered or registered['status'] != ('retired_closed' if retired else 'active')
            or registered['client'] != client or registered['cwd'] != directory):
        raise ValueError('Withdrawal requires the actual canonical root owner, client and exact checkout')
    return {key: watch[key] for key in ('repository', 'number', 'owner_session', 'client', 'cwd', 'state')} | {
        'native_requester': requester}


def _identity(pull, repository, number, head, *, closed=False):
    expected_state = 'closed' if closed else 'open'
    if (not isinstance(pull, dict) or pull.get('number') != number
            or pull.get('url') != f'https://api.github.com/repos/{repository}/pulls/{number}'
            or pull.get('state') != expected_state or pull.get('merged') is not False
            or pull.get('draft') is not False or not re.fullmatch(r'[0-9a-f]{40}', head)):
        raise ValueError('Withdrawal requires the exact open/unmerged PR or verified closed/unmerged readback')
    before, after = pull.get('head') or {}, pull.get('base') or {}
    source_repo, target_repo = before.get('repo') or {}, after.get('repo') or {}
    if (before.get('sha') != head or source_repo.get('full_name') != repository
            or target_repo.get('full_name') != repository or type(source_repo.get('id')) is not int
            or source_repo['id'] < 1 or source_repo['id'] != target_repo.get('id')
            or any(not isinstance(ref.get('ref'), str) or not ref['ref'] for ref in (before, after))):
        raise ValueError('Withdrawal PR head, repository or ref identity changed')
    return dict(repository_id=source_repo['id'], head_sha=head, head_ref=before['ref'], base_ref=after['ref'])


async def _proof(client, repository, number, head):
    snapshot = await collect_pr(repository, number)
    pull = await client.get(f'/repos/{repository}/pulls/{number}')
    identity = _identity(pull, repository, number, head)
    stamp = snapshot.get('collected_at')
    if (snapshot.get('repository') != repository or snapshot.get('number') != number
            or snapshot.get('state') != 'open' or snapshot.get('merged') is not False
            or snapshot.get('head_sha') != head or snapshot.get('head_sha_after') != head
            or snapshot.get('base_ref') != identity['base_ref']
            or not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not 0 <= time.time() - stamp <= 90):
        raise ValueError('Withdrawal requires fresh evidence of the exact open PR head')
    return snapshot, identity


def _claim(store, sid, pin, receipt):
    from .monitor import _worker_alive
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if _owner(store, db, sid, pin['repository'], pin['number'], pin['client'], pin['cwd']) != pin:
            raise ValueError('Withdrawal canonical owner changed during collection')
        row = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=?',
                         (pin['repository'], pin['number'])).fetchone()
        now = time.time()
        if (row['lease_until'] > now or row['delivery_state'] in {'reserved', 'queued'}
                or row['worker_pid'] is not None and _worker_alive(row['worker_pid'])
                or db.execute('''SELECT 1 FROM validation_runs r JOIN validation_plans p ON p.plan_id=r.plan_id
                    WHERE p.repository=? AND p.number=? AND r.status='running' LIMIT 1''',
                    (pin['repository'], pin['number'])).fetchone()
                or any(json.loads(r['fields']).get('lease_until', 0) > now for r in db.execute(
                    'SELECT fields FROM recovery WHERE repository=? AND number=?', (pin['repository'], pin['number'])))
                or any(db.execute(f'SELECT 1 FROM {table} WHERE repository=? AND number=? AND lease_until>? LIMIT 1',
                    (pin['repository'], pin['number'], now)).fetchone() for table in ('followups', 'issue_closeouts'))):
            raise ValueError('Withdrawal refuses active PR workers, validation or recovery leases')
        if db.execute('''SELECT 1 FROM audit WHERE event='pr_withdrawal_attempt'
                AND json_extract(fields,'$.repository')=? AND json_extract(fields,'$.number')=?
                AND json_extract(fields,'$.head_sha')=?''', (pin['repository'], pin['number'], receipt['head_sha'])).fetchone():
            raise ValueError('Withdrawal already attempted for this head; investigate its outcome without replay')
        deadline = now + 180
        db.execute('UPDATE pr_watch SET lease_until=?,worker_pid=? WHERE repository=? AND number=?',
                   (deadline, os.getpid(), pin['repository'], pin['number']))
        store._audit(db, 'pr_withdrawal_attempt', receipt)
        return deadline, db.execute('SELECT last_insert_rowid()').fetchone()[0]


def _held(store, db, sid, pin, deadline, *, attempt=None, head=None):
    row = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=?',
                     (pin['repository'], pin['number'])).fetchone()
    retired = attempt is not None and row is not None and row['state'] == 'closed'
    expected = dict(pin, state='closed') if retired else pin
    if _owner(store, db, sid, pin['repository'], pin['number'], pin['client'], pin['cwd'], retired=retired) != expected:
        raise ValueError('Withdrawal lost its exact canonical owner')
    if row['worker_pid'] != os.getpid() or row['lease_until'] != (0 if retired else deadline) or deadline <= time.time():
        raise ValueError('Withdrawal lost its reserved owner lease')
    if not retired:
        return []
    # Normal monitoring may observe our successful close before readback. It
    # clears the lease deadline but retains the worker PID. Accept only that
    # causally proved retirement, never a rearmed/replaced watch or lost lease.
    history = [dict(event) for event in db.execute('''SELECT id,event,fields FROM audit WHERE id>?
        AND event IN ('pr_monitor_retired','pr_registered','pr_owner_transferred')
        AND json_extract(fields,'$.repository')=? AND json_extract(fields,'$.number')=? ORDER BY id''',
        (attempt, pin['repository'], pin['number']))]
    if not history or any(event['event'] != 'pr_monitor_retired'
            or (fields := json.loads(event['fields'])).get('state') != 'closed'
            or fields.get('head_sha') != head or fields.get('gates_waived') is not False for event in history):
        raise ValueError('Withdrawal lacks matching causal monitor retirement evidence')
    return [event['id'] for event in history]


def close(args, store, sid, argv, run):
    from .cli import reference
    reason = getattr(args, 'withdrawn_reason', None)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000 or '\x00' in reason:
        raise ValueError('Withdrawal needs a nonempty public reason of at most 2000 characters')
    if len(argv) != 3 or argv[:2] != ['pr', 'close']:
        raise ValueError('Withdrawal accepts only pr close with one explicit PR URL and no extra flags')
    if any(getattr(args, option, None) is not None for option in ('kind', 'slice_id', 'nonrequired_quota_approval')):
        raise ValueError('Withdrawal does not accept document, slice or merge-approval options')
    target = argv[2]
    repository, number, issue = reference(target)
    if issue or getattr(args, 'target', None) not in (None, target):
        raise ValueError('Withdrawal requires one matching explicit PR URL')
    directory = str(Path(args.cwd).expanduser().resolve())
    with store._connect() as db:
        pin = _owner(store, db, sid, repository, number, args.client, directory)
    local = source(directory, stable=True)
    if not exact_source({'repository': repository, 'cwd': directory}, local):
        raise ValueError('Withdrawal requires the clean exact recorded repository checkout')
    client = GitHubClient()
    snapshot, identity = asyncio.run(asyncio.wait_for(_proof(client, repository, number, local['head']), 85))
    gates = evaluate_pr(snapshot, store.dispositions(repository, number))
    store.set_snapshot(repository, number, snapshot, gates)
    if source(directory, stable=True) != local:
        raise ValueError('Withdrawal checkout changed during collection')
    receipt = dict(pin, **identity, reason=reason.strip(), observed_gates=gates,
                   unresolved_gates=[g for g in gates if g['status'] != 'PASS'],
                   evidence_collected_at=snapshot['collected_at'], completion_verified=False, gates_waived=False)
    deadline, attempt = _claim(store, sid, pin, receipt)
    receipt['attempt_audit_id'] = attempt
    try:
        before = asyncio.run(asyncio.wait_for(client.get(f'/repos/{repository}/pulls/{number}'), 20))
        if _identity(before, repository, number, local['head']) != identity:
            raise ValueError('Withdrawal remote identity changed before close')
        with store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _held(store, db, sid, pin, deadline)
            if source(directory, stable=True) != local:
                raise ValueError('Withdrawal checkout changed before close')
        result = run(['gh', 'pr', 'close', target, '--comment', 'Withdrawn without merging: ' + reason.strip()],
                     cwd=directory, text=True, capture_output=True, timeout=90)
        if result.returncode:
            raise RuntimeError('Withdrawal close failed; investigate live state without replay')
        closed = asyncio.run(asyncio.wait_for(client.get(f'/repos/{repository}/pulls/{number}'), 30))
        if _identity(closed, repository, number, local['head'], closed=True) != identity:
            raise ValueError('Withdrawal readback identity changed; closure is unverified')
        with store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            retired = _held(store, db, sid, pin, deadline, attempt=attempt, head=local['head'])
            if source(directory, stable=True) != local:
                raise ValueError('Withdrawal checkout changed before retirement')
            # Commit ordinary unmerged retirement and its ownership proof in
            # one transaction; a replaced watch must never be retired here.
            if retired:
                db.execute('UPDATE pr_watch SET worker_pid=NULL,terminal_notice=0 WHERE repository=? AND number=?',
                           (repository, number))
            else:
                db.execute('''UPDATE pr_watch SET state='closed',branch=?,event_id=NULL,payload=NULL,
                    delivery_state=NULL,error=NULL,lease_until=0,worker_pid=NULL,terminal_notice=0,updated_at=?
                    WHERE repository=? AND number=?''', (identity['head_ref'], time.time(), repository, number))
                db.execute("UPDATE prs SET status='retired_closed',updated_at=? WHERE repository=? AND number=? AND status='active'",
                           (time.time(), repository, number))
                store._audit(db, 'pr_monitor_retired', dict(repository=repository, number=number,
                             state='closed', head_sha=local['head'], gates_waived=False))
                retired = [db.execute('SELECT last_insert_rowid()').fetchone()[0]]
            receipt['retirement_audit_ids'] = retired
            store._audit(db, 'pr_withdrawn', receipt)
        return dict(closed=True, withdrawn=True, **receipt)
    except BaseException:
        store.audit('pr_withdrawal_unverified', receipt)
        raise
    finally:
        with store._connect() as db:
            db.execute('''UPDATE pr_watch SET lease_until=0,worker_pid=NULL WHERE repository=? AND number=?
                AND owner_session=? AND client=? AND cwd=? AND worker_pid=? AND lease_until=?''',
                (repository, number, sid, pin['client'], directory, os.getpid(), deadline))
