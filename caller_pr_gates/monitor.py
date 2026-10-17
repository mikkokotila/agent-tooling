"""Quiet PR polling. Only changed, actionable work reaches its exact owner."""
from __future__ import annotations
from .prompts import render_prompt

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import uuid

SINGLE_INTERVAL = 300
MULTI_INTERVAL = 600
MARKER = '<!-- pr-work-event:'
RULE = (render_prompt('pr_rules'))


def schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS pr_watch (
            repository TEXT NOT NULL, number INTEGER NOT NULL,
            owner_session TEXT NOT NULL, client TEXT NOT NULL, cwd TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'open', armed INTEGER NOT NULL DEFAULT 1,
            next_check_at REAL NOT NULL DEFAULT 0, last_check_at REAL,
            fingerprint TEXT, delivered_fingerprint TEXT, event_id TEXT,
            payload TEXT, delivery_state TEXT, error TEXT, updated_at REAL NOT NULL,
            retry_at REAL NOT NULL DEFAULT 0, worker_pid INTEGER, lease_until REAL NOT NULL DEFAULT 0,
            terminal_notice INTEGER NOT NULL DEFAULT 0,
            branch TEXT, branch_identity TEXT, closure_identity TEXT,
            PRIMARY KEY(repository, number)
        );
        CREATE TABLE IF NOT EXISTS watch_sessions (
            session_id TEXT PRIMARY KEY, busy INTEGER NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS unpublished_work (
            session_id TEXT NOT NULL, repository TEXT NOT NULL, branch TEXT NOT NULL,
            cwd TEXT NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(session_id,repository,branch,cwd)
        );
    ''')
    columns = {r['name'] for r in db.execute('PRAGMA table_info(pr_watch)')}
    for name, definition in [('retry_at','REAL NOT NULL DEFAULT 0'), ('worker_pid','INTEGER'), ('lease_until','REAL NOT NULL DEFAULT 0'), ('terminal_notice','INTEGER NOT NULL DEFAULT 0'), ('branch','TEXT'), ('branch_identity','TEXT'), ('closure_identity','TEXT')]:
        if name not in columns:
            db.execute(f'ALTER TABLE pr_watch ADD COLUMN {name} {definition}')
    from .turns import _schema
    _schema(db)
    from .closure import schema as closure_schema
    closure_schema(db)


def checkout(cwd):
    """A checkout includes its subdirectories, but never another worktree."""
    try:
        result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'], capture_output=True, text=True, timeout=3)
        if result.returncode == 0 and isinstance(result.stdout, str) and result.stdout.strip():
            return str(Path(result.stdout.strip()).resolve())
    except (OSError, subprocess.SubprocessError):
        pass
    return str(Path(cwd).resolve())


def reject_queued(store, sid, token, reason):
    with store._connect() as db:
        db.execute("UPDATE pr_watch SET delivery_state='failed',error=?,lease_until=CASE WHEN client='codex' THEN 0 ELSE lease_until END WHERE owner_session=? AND event_id=? AND delivery_state='queued'",
                   (reason, sid, token))
        db.execute('UPDATE watch_sessions SET busy=0 WHERE session_id=?', (sid,))


def arm(db, sid, client, repository, number, cwd, *, armed=True):
    now = time.time()
    previous = db.execute('SELECT owner_session,state FROM pr_watch WHERE repository=? AND number=?',
                          (repository, number)).fetchone()
    if previous and previous['state'] == 'open':
        # Registration receipts are retained, but another root chat cannot steal
        # the canonical owner or replace the first recorded source checkout.
        if previous['owner_session'] == sid:
            db.execute("UPDATE pr_watch SET armed=MAX(armed,?),updated_at=? WHERE repository=? AND number=?",
                       (int(armed), now, repository, number))
        return
    db.execute('''INSERT INTO pr_watch(repository,number,owner_session,client,cwd,armed,updated_at)
                  VALUES(?,?,?,?,?,?,?) ON CONFLICT(repository,number) DO UPDATE SET
                  owner_session=excluded.owner_session,client=excluded.client,cwd=excluded.cwd,
                  armed=excluded.armed,state='open',next_check_at=0,fingerprint=NULL,
                  delivered_fingerprint=NULL,event_id=NULL,payload=NULL,delivery_state=NULL,error=NULL,
                  retry_at=0,worker_pid=NULL,lease_until=0,terminal_notice=0,branch=NULL,branch_identity=NULL,closure_identity=NULL,
                  updated_at=excluded.updated_at''', (repository, number, sid, client, str(cwd), int(armed), now))


def bootstrap(store):
    """Existing receipts stay owned. Historical failures establish a silent baseline."""
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        parents = store._parents(db)
        rows = db.execute("SELECT * FROM prs WHERE status='active' ORDER BY updated_at DESC").fetchall()
        for row in rows:
            if db.execute('SELECT 1 FROM pr_watch WHERE repository=? AND number=?',
                          (row['repository'], row['number'])).fetchone():
                continue
            sid = store._root(row['session_id'], parents)
            owner = db.execute('SELECT * FROM sessions WHERE session_id=?', (sid,)).fetchone()
            if owner:
                arm(db, sid, owner['client'], row['repository'], row['number'], row['cwd'], armed=False)


async def probe(repository, number):
    from .transport import GitHubClient
    return await GitHubClient().get(f'/repos/{repository}/pulls/{number}')


async def probe_branch(repository, ref):
    from .transport import GitHubClient
    from urllib.parse import quote
    return await GitHubClient().get(f'/repos/{repository}/branches/{quote(ref, safe="")}')


def lifecycle(raw):
    if not isinstance(raw, dict) or raw.get('state') not in {'open', 'closed'}:
        raise ValueError('PR lifecycle is unavailable')
    if not isinstance(raw.get('merged'), bool):
        raise ValueError('PR merge state is unavailable')
    if not (raw.get('head') or {}).get('sha'):
        raise ValueError('PR head is unavailable')
    if raw['merged'] and raw['state'] != 'closed':
        raise ValueError('PR lifecycle is inconsistent')
    return 'merged' if raw['merged'] else raw['state']


def retire(store, repository, number, raw, *, checklist=False):
    state = lifecycle(raw)
    if state == 'open':
        return False
    from .publication import reconcile
    reconcile(store,repository,number,raw)
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        target = db.execute('SELECT owner_session FROM pr_watch WHERE repository=? AND number=?', (repository, number)).fetchone()
        if target and state == 'merged' and not checklist:
            cached = db.execute('SELECT snapshot FROM snapshots WHERE repository=? AND number=?', (repository, number)).fetchone()
            if cached:
                from .closure import record
                record(db, target['owner_session'], repository, number, json.loads(cached['snapshot']))
        busy = db.execute('SELECT busy FROM watch_sessions WHERE session_id=?', (target['owner_session'],)).fetchone() if target else None
        mode = db.execute('SELECT mode FROM turn_modes WHERE session_id=?', (target['owner_session'],)).fetchone() if target else None
        notice = int(not checklist and bool(busy and busy['busy'] and mode and mode['mode'] == 'engineering'))
        branch = raw['head'].get('ref')
        db.execute("UPDATE pr_watch SET state=?,branch=COALESCE(?,branch),event_id=NULL,payload=NULL,delivery_state=NULL,error=NULL,lease_until=0,terminal_notice=?,updated_at=? WHERE repository=? AND number=?",
                   (state, branch, notice, time.time(), repository, number))
        db.execute("UPDATE prs SET status=?,updated_at=? WHERE repository=? AND number=? AND status='active'",
                   ('retired_' + state, time.time(), repository, number))
        # Evidence and dispositions remain; retirement is never a passing receipt.
        store._audit(db, 'pr_monitor_retired', {'repository': repository, 'number': number,
                     'state': state, 'head_sha': raw['head']['sha'], 'gates_waived': False})
    return True


def actionable(snapshot, gates, *, checklist=False):
    errors = snapshot.get('component_errors') or {}
    unavailable = [error for component, error in errors.items() if not (
        component == 'rules' and isinstance(error, dict) and error.get('kind') == 'feature_unavailable'
        and (snapshot.get('component_status') or {}).get('rules') == 'not_available_on_plan'
        and isinstance(snapshot.get('expected_checks'), list))]
    if checklist and unavailable:
        return []  # Partial API outages cannot authorize a chat continuation.
    failures = {g['gate']: g for g in gates if g.get('status') == 'FAIL'}
    items = []
    if 'CI_FAILED' in failures:
        from .policy import _latest_checks, _outcome
        from .copilot import is_service_check
        names = sorted({str(c.get('name') or c.get('key')) for c in _latest_checks(snapshot.get('checks') or [])
                        if not is_service_check(c) and _outcome(c) == 'failed' and (not checklist or genuine_failure(c))})
        if names:
            items.append(render_prompt('monitor_ci_failed', check_names=', '.join(names[:5])))
    if {'REVIEW_HANDLED', 'REVIEW_CONCLUDED'} & failures.keys():
        obligations = (failures.get('REVIEW_HANDLED') or failures.get('REVIEW_CONCLUDED'))['evidence'].get('obligations', [])
        outstanding = [o for o in obligations if not o.get('handled') or not o.get('concluded')]
        items.append(render_prompt('monitor_review_comments', comment_count=len(outstanding)))
    merge_state = (failures.get('MERGE_READY', {}).get('evidence') or {}).get('merge_state_status')
    if snapshot.get('mergeable') is False or merge_state == 'DIRTY':
        items.append(render_prompt('monitor_conflict'))
    if 'BRANCH_UP_TO_DATE' in failures or merge_state == 'BEHIND':
        ref = snapshot.get('base_ref') or 'target branch'
        items.append(render_prompt('monitor_branch_behind', target_branch=ref))
    if 'PR_NOT_DRAFT' in failures or merge_state == 'DRAFT':
        items.append(render_prompt('monitor_draft'))
    if 'ISSUE_CLOSURE' in failures and not checklist:
        items.append(render_prompt('monitor_issue_closure'))
    if snapshot.get('local_failures'):
        names = sorted({r['check_id'] for r in snapshot['local_failures']})
        items.append(render_prompt('monitor_local_failures', check_names=', '.join(names[:5])))
    return items


def local_failures(store, sid, snapshot, *, full=True):
    """Notify actual failed managed runs; obsolete plans/inputs never authorize work."""
    from .workflow import current_phase
    result = []
    for plan in store.validation_plans(sid):
        if (plan['repository'], plan['number']) != (snapshot['repository'], snapshot['number']):
            continue
        runs = [r for r in store.validation_runs(plan['plan_id'])
                if r['status'] == 'fail' and r['input_digest'] == plan['input_digest']]
        if not runs:
            continue
        if full:
            try:
                phase = current_phase(store, plan)
                if phase['phase'] != 'VALIDATE_FAILED':
                    continue
                runs = [r for r in runs if r['check_id'] in phase['failed']]
            except Exception:
                continue  # Missing local evidence remains unknown, never passing.
        result.extend({'plan_id': plan['plan_id'], 'run_id': r['run_id'], 'check_id': r['check_id']} for r in runs)
    return result


def quiet_hold(store, sid, snapshot, gates):
    from .external import current_hold
    # GitHub computes mergeability asynchronously. An unknown conflict state
    # cannot invalidate independently proved, unchanged account-quota evidence.
    usable = [g for g in gates if not (g['gate'] in {'MERGE_CONFLICT', 'BRANCH_UP_TO_DATE', 'ISSUE_CLOSURE', 'MERGE_READY'} and g['status'] == 'UNKNOWN')]
    return current_hold(store, sid, snapshot, usable)


def genuine_failure(check):
    return str(check.get('conclusion') or check.get('status') or '').lower() in {'failure', 'error', 'failed'}


def identity(snapshot, gates, previous_branch=None, previous_closure=None, *, checklist=False):
    from .handoff import evidence_fingerprint
    local = json.dumps(snapshot.get('local_failures') or [], sort_keys=True)
    if checklist:
        # Availability, pending checks and interrupted-CI reruns belong outside
        # chat repair. They must not replay an already-delivered source failure.
        snapshot = dict(snapshot, checks=[c for c in snapshot.get('checks') or [] if genuine_failure(c)], component_errors={})
        gates = [g for g in gates if g['status'] == 'FAIL' and g['gate'] != 'ISSUE_CLOSURE'
                 and (g['gate'] != 'CI_FAILED' or snapshot['checks'])]
    return hashlib.sha256((evidence_fingerprint(snapshot, gates, previous_branch, previous_closure) + str(snapshot.get('mergeable')) + local).encode()).hexdigest()


def work_payload(store, row, snapshot, items):
    scope = store.pr_scope(row['owner_session'], row['repository'], row['number'])
    previous = json.loads(row.get('payload') or '{}')
    return json.dumps({'head_sha': snapshot['head_sha'], 'items': items,
                       'agreed_scope': (scope or {}).get('agreed_scope'),
                       **({'queued_body_hash': previous['queued_body_hash']} if previous.get('queued_body_hash') else {})})


async def tick(store, *, execute=False, now=None, reader=probe, collector=None, spawn=None, checklist=False):
    from .github import collect_pr
    from .policy import evaluate_pr
    collector = collector or collect_pr
    now = time.time() if now is None else now
    bootstrap(store)
    result = {'checked': 0, 'retired': 0, 'events': 0, 'errors': 0, 'workers': 0}
    with store._connect() as db:
        db.execute("UPDATE pr_watch SET delivery_state='failed',lease_until=0,error='Delivery worker expired; explicit retry required' WHERE delivery_state IN ('reserved','queued') AND lease_until>0 AND lease_until<=?", (now,))
        rows = [dict(r) for r in db.execute("SELECT * FROM pr_watch WHERE state='open'")]
    counts = {}
    for row in rows:
        counts[row['owner_session']] = counts.get(row['owner_session'], 0) + 1
    semaphore = asyncio.Semaphore(4)

    async def check(row):
        if row['next_check_at'] > now:
            return
        repository, number = row['repository'], row['number']
        interval = MULTI_INTERVAL if counts[row['owner_session']] > 1 else SINGLE_INTERVAL
        # Reserve the next read before networking; failures also obey the cadence.
        with store._connect() as db:
            db.execute('UPDATE pr_watch SET next_check_at=?,last_check_at=? WHERE repository=? AND number=?',
                       (now + interval, now, repository, number))
        try:
            async with semaphore:
                raw = await asyncio.wait_for(reader(repository, number), 25)
                if retire(store, repository, number, raw, checklist=checklist):
                    result['retired'] += 1
                    return
                snapshot = await asyncio.wait_for(collector(repository, number), 65)
            if snapshot.get('state') != 'open' or snapshot.get('merged') is not False:
                raise ValueError('PR changed lifecycle during collection; reread next cycle')
            snapshot = dict(snapshot, mergeable=snapshot.get('mergeable', raw.get('mergeable')))
            if snapshot.get('head_sha') != raw['head']['sha']:
                raise ValueError('PR head changed during polling')
            gates = evaluate_pr(snapshot, store.dispositions(repository, number))
            store.set_snapshot(repository, number, snapshot, gates)
            snapshot['local_failures'] = local_failures(store, row['owner_session'], snapshot)
            from .handoff import branch_identity, closure_identity
            previous_branch = json.loads(row['branch_identity']) if row.get('branch_identity') else None
            branch_proof = branch_identity(snapshot, gates, previous_branch)
            previous_closure = json.loads(row['closure_identity']) if row.get('closure_identity') else None
            link_proof = closure_identity(snapshot, gates, previous_closure)
            active_gates = [g for g in gates if g['gate'] != 'ISSUE_CLOSURE'] if checklist else gates
            fingerprint = identity(snapshot, active_gates, previous_branch, None if checklist else previous_closure, checklist=checklist)
            items = actionable(snapshot, gates, checklist=checklist)
            if quiet_hold(store, row['owner_session'], snapshot, gates):
                items = [i for i in items if i.startswith('Background local')]
            with store._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=?',
                                     (repository, number)).fetchone()
                if current['owner_session'] != row['owner_session'] or current['state'] != 'open':
                    return
                event = current['event_id']
                payload = current['payload']
                delivery = current['delivery_state']
                # A changed passing/pending state cancels stale work. A first historical read is silent.
                if not items:
                    event, payload, delivery = None, None, None
                elif fingerprint != current['delivered_fingerprint'] and (current['armed'] or current['fingerprint'] is not None):
                    if current['fingerprint'] != fingerprint or not event:
                        if delivery not in {'reserved', 'queued'} or not event:
                            event = str(uuid.uuid4())
                        payload = work_payload(store, row, snapshot, items)
                        if delivery not in {'reserved', 'queued'}:
                            delivery = 'pending'
                        result['events'] += 1
                db.execute('''UPDATE pr_watch SET fingerprint=?,event_id=?,payload=?,delivery_state=?,
                              branch_identity=?,closure_identity=?,error=NULL,updated_at=? WHERE repository=? AND number=?''',
                           (fingerprint, event, payload, delivery, json.dumps(branch_proof) if branch_proof else None, json.dumps(link_proof) if link_proof else None, now, repository, number))
                saved = db.execute('SELECT fields FROM recovery WHERE repository=? AND number=? AND session_id=?',
                                   (repository, number, row['owner_session'])).fetchone()
                waiting = bool(saved and json.loads(saved['fields']).get('awaiting_validation'))
                busy = db.execute('SELECT busy FROM watch_sessions WHERE session_id=?', (row['owner_session'],)).fetchone()
            if not checklist and waiting and delivery == 'consumed' and not (busy and busy['busy']):
                _idle_handoff(store, row['owner_session'], {}, None, retry_at=now)
            result['checked'] += 1
        except Exception as exc:
            with store._connect() as db:
                db.execute('UPDATE pr_watch SET error=? WHERE repository=? AND number=?',
                           (f'{type(exc).__name__}: {str(exc)[:300]}', repository, number))
            result['errors'] += 1  # Health data remains local; never queue an API-error prompt.

    await asyncio.gather(*(check(row) for row in rows))
    if not checklist:
        from .closure import refresh as refresh_closeouts
        await refresh_closeouts(store, now=now)
    if execute:
        with store._connect() as db:
            owners = [r[0] for r in db.execute("SELECT DISTINCT owner_session FROM pr_watch WHERE state='open' AND delivery_state='pending'")]
        for sid in owners:
            with store._connect() as db:
                if db.execute("SELECT 1 FROM pr_watch WHERE owner_session=? AND (delivery_state IN ('reserved','queued') OR lease_until>?)", (sid, now)).fetchone() or db.execute('SELECT 1 FROM issue_closeouts WHERE owner_session=? AND lease_until>?', (sid, now)).fetchone():
                    continue
                busy = db.execute('SELECT busy FROM watch_sessions WHERE session_id=?', (sid,)).fetchone()
                if busy and busy['busy']:
                    continue  # Its next native tool callback consumes the inbox.
                claimed = db.execute("UPDATE pr_watch SET delivery_state='reserved',lease_until=? WHERE owner_session=? AND state='open' AND delivery_state='pending' AND retry_at<=?", (now + 3000, sid, now)).rowcount
            if not claimed:
                continue
            try:
                args = [sys.executable, '-P', '-m', 'caller_pr_gates.monitor', 'deliver', '--session', sid]
                if spawn:
                    worker = spawn(args)
                else:
                    worker = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, start_new_session=True)
                with store._connect() as db:
                    db.execute("UPDATE pr_watch SET worker_pid=? WHERE owner_session=? AND delivery_state IN ('reserved','queued') AND worker_pid IS NULL", (getattr(worker, 'pid', None), sid))
                result['workers'] += 1
            except Exception as exc:
                with store._connect() as db:
                    db.execute("UPDATE pr_watch SET delivery_state='failed',lease_until=0,error=? WHERE owner_session=? AND delivery_state='reserved'", (str(exc)[:300], sid))
                result['errors'] += 1
    if execute and not checklist:
        from .closure import dispatch
        result['closeout_workers'] = dispatch(store, now=now, spawn=spawn)
    return result


def pending(store, sid, states=('pending', 'reserved', 'queued')):
    with store._connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM pr_watch WHERE owner_session=? AND state=\'open\' AND delivery_state IN (' + ','.join('?' for _ in states) + ')', (sid, *states))]


def message(rows, *, checklist=False):
    if checklist:
        from .checklist import message as checklist_message
        return checklist_message(rows)
    lines = []
    for row in rows:
        payload = json.loads(row['payload'])
        lines.append(render_prompt('monitor_pr_line', repository=row['repository'], number=row['number'], items='; '.join(payload['items'])))
        if payload.get('agreed_scope'):
            lines.append(render_prompt('monitor_scope', approved_scope=payload['agreed_scope']))
    return render_prompt('monitor_message', alert_lines='\n'.join(lines))


async def revalidate(store, rows, reader=probe, collector=None, *, full=True, branch_reader=None, checklist=False):
    from .github import collect_pr
    from .policy import evaluate_pr
    collector = collector or collect_pr
    branch_reader = branch_reader or probe_branch
    valid = []
    for row in rows:
        try:
            if full:
                raw = await asyncio.wait_for(reader(row['repository'], row['number']), 25)
            else:
                snapshot = (store.snapshot(row['repository'], row['number']) or {}).get('snapshot') or {}
                if time.time() - float(snapshot.get('collected_at') or 0) > MULTI_INTERVAL:
                    continue
                ref = snapshot.get('base_ref')
                if not isinstance(ref, str) or not ref:
                    continue
                # The PR object's embedded base SHA can lag the live branch tip.
                # Parallel bounded reads fit the native hook's ten-second budget.
                raw, branch = await asyncio.gather(
                    asyncio.wait_for(reader(row['repository'], row['number']), 5),
                    asyncio.wait_for(branch_reader(row['repository'], ref), 5),
                    return_exceptions=True)
                if isinstance(raw, Exception): raise raw
            if retire(store, row['repository'], row['number'], raw, checklist=checklist):
                continue
            if raw['head']['sha'] != json.loads(row['payload'])['head_sha']:
                with store._connect() as db:
                    db.execute("UPDATE pr_watch SET event_id=NULL,payload=NULL,delivery_state=NULL,next_check_at=0 WHERE repository=? AND number=?", (row['repository'], row['number']))
                continue
            if full:
                snapshot = await asyncio.wait_for(collector(row['repository'], row['number']), 65)
            else:
                if isinstance(branch, Exception): raise branch
                base = raw.get('base') or {}
                current_tip = (branch.get('commit') or {}).get('sha')
                if not isinstance(current_tip, str) or not re.fullmatch(r'[a-fA-F0-9]{40}', current_tip):
                    continue
                if (base.get('ref') != ref or current_tip != snapshot.get('base_sha')):
                    with store._connect() as db:
                        db.execute('UPDATE pr_watch SET event_id=NULL,payload=NULL,delivery_state=NULL,next_check_at=0 WHERE repository=? AND number=? AND event_id=?',
                                   (row['repository'], row['number'], row['event_id']))
                    continue
            if snapshot.get('head_sha') != raw['head']['sha'] or snapshot.get('state') != 'open' or snapshot.get('merged') is not False:
                continue
            snapshot = dict(snapshot, mergeable=snapshot.get('mergeable', raw.get('mergeable')))
            gates = evaluate_pr(snapshot, store.dispositions(row['repository'], row['number']))
            if full:
                store.set_snapshot(row['repository'], row['number'], snapshot, gates)
            snapshot['local_failures'] = local_failures(store, row['owner_session'], snapshot, full=full)
            items = actionable(snapshot, gates, checklist=checklist)
            if quiet_hold(store, row['owner_session'], snapshot, gates):
                items = [i for i in items if i.startswith('Background local')]
            if not items:
                with store._connect() as db:
                    db.execute('UPDATE pr_watch SET event_id=NULL,payload=NULL,delivery_state=NULL WHERE repository=? AND number=? AND event_id=?', (row['repository'], row['number'], row['event_id']))
                continue
            with store._connect() as db:
                latest = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=?', (row['repository'], row['number'])).fetchone()
                if latest['owner_session'] == row['owner_session'] and latest['event_id'] == row['event_id']:
                    valid.append(dict(row, payload=work_payload(store, row, snapshot, items)))
        except Exception as exc:
            from .errors import redact
            store.audit('pr_revalidation_deferred', {'session_id': row['owner_session'],
                'repository': row['repository'], 'number': row['number'], 'event_id': row['event_id'],
                'full': full, 'error': type(exc).__name__ + ': ' + redact(str(exc))[:200]})
    return valid


def consume(store, sid, *, event_id=None, reader=probe, collector=None, full=True, branch_reader=None, checklist=False, cwd=None):
    rows = [row for row in pending(store, sid)
            if row['delivery_state'] != 'pending' or row['retry_at'] <= time.time()]
    if event_id and not any(r['event_id'] == event_id for r in rows):
        return None
    if checklist:
        contexts = set(map(checkout, cwd if isinstance(cwd, (list, tuple)) else [cwd])) if cwd is not None else None
        rows = [r for r in rows if (not event_id or r['event_id'] == event_id)
                and (contexts is None or checkout(r['cwd']) in contexts)][:1]
    if not rows:
        return None
    if not full:
        rows.sort(key=lambda row: row['event_id'] != event_id)
        rows = rows[:3]  # Native prompt hooks have a ten-second budget.
    rows = asyncio.run(revalidate(store, rows, reader, collector, full=full, branch_reader=branch_reader, checklist=checklist))
    if not rows:
        return None
    claimed = []
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in rows:
            changed = db.execute("UPDATE pr_watch SET delivery_state='consumed',delivered_fingerprint=fingerprint,lease_until=CASE WHEN client='codex' THEN 0 ELSE lease_until END WHERE repository=? AND number=? AND owner_session=? AND event_id=? AND state='open' AND delivery_state IN ('pending','reserved','queued')",
                       (row['repository'], row['number'], sid, row['event_id'])).rowcount
            if changed:
                claimed.append(row)
        if claimed:
            store._audit(db, 'pr_monitor_consumed', {'session_id': sid, 'events': [r['event_id'] for r in claimed]})
    return message(claimed, checklist=checklist) if claimed else None


def retry(store, sid, repository, number):
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM pr_watch WHERE owner_session=? AND repository=? AND number=?', (sid, repository, number)).fetchone()
        if not row or row['state'] != 'open':
            return False
        if row['lease_until'] > time.time():
            raise ValueError('Monitor still has a live worker lease')
        db.execute('UPDATE pr_watch SET delivered_fingerprint=NULL,delivery_state=NULL,event_id=NULL,payload=NULL,error=NULL,retry_at=0,next_check_at=0,worker_pid=NULL WHERE repository=? AND number=?', (repository, number))
        _receipt(db, store, row, {'idle_retry_fingerprint': None, 'idle_retry_outcome': None, 'awaiting_user_turn': None, 'awaiting_validation': False})
        store._audit(db, 'pr_monitor_explicit_retry', {'session_id': sid, 'repository': repository, 'number': number})
    return True


def _receipt(db, store, row, updates):
    """Extend existing recovery evidence atomically; preserve leases and ownership."""
    key = (row['repository'], row['number'], row['owner_session'])
    owned = store._task_sessions(db, key[2])
    if not any(p[0] in owned for p in db.execute("SELECT session_id FROM prs WHERE repository=? AND number=? AND status='active'", key[:2])):
        raise ValueError('PR is no longer owned by this task')
    previous = db.execute('SELECT fields FROM recovery WHERE repository=? AND number=? AND session_id=?', key).fetchone()
    fields = json.loads(previous['fields']) if previous else {}
    fields.update(updates)
    now = time.time()
    db.execute('''INSERT INTO recovery VALUES(?,?,?,?,?,?) ON CONFLICT(repository,number,session_id)
                  DO UPDATE SET fields=excluded.fields,updated_at=excluded.updated_at''',
               (*key, json.dumps(fields), now, now))
    store._audit(db, 'pr_continuation_outcome', dict(repository=key[0], number=key[1], session_id=key[2], **updates))
    return fields


def _idle_handoff(store, sid, event, result, *, retry_at=None):
    """One recovery for a new idle transition, using evidence already collected."""
    if result and result.get('decision') == 'block':
        return result
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_modes'").fetchone():
            return result
        mode = db.execute('SELECT * FROM turn_modes WHERE session_id=?', (sid,)).fetchone()
        if not mode or mode['mode'] != 'engineering' or event.get('turn_id') and event['turn_id'] != mode['turn_id']:
            return result
        rows = db.execute("SELECT * FROM pr_watch WHERE owner_session=? AND state='open' AND armed=1 AND delivery_state='consumed' AND lease_until<=?", (sid, time.time())).fetchall()
        stalled = False
        for row in rows:
            cached = db.execute('SELECT snapshot,gates FROM snapshots WHERE repository=? AND number=?', (row['repository'], row['number'])).fetchone()
            if not cached or not row['payload']:
                continue
            snapshot, gates = json.loads(cached['snapshot']), json.loads(cached['gates'])
            if (snapshot.get('state') != 'open' or snapshot.get('merged') is not False
                    or time.time() - float(snapshot.get('collected_at') or 0) > MULTI_INTERVAL
                    or snapshot.get('head_sha') != json.loads(row['payload'])['head_sha']
                    or any(g.get('status') == 'UNKNOWN' for g in gates)):
                continue
            snapshot['local_failures'] = local_failures(store, sid, snapshot, full=False)
            if not actionable(snapshot, gates) or quiet_hold(store, sid, snapshot, gates):
                continue
            old = db.execute('SELECT fields FROM recovery WHERE repository=? AND number=? AND session_id=?', (row['repository'], row['number'], sid)).fetchone()
            fields = json.loads(old['fields']) if old else {}
            if fields.get('awaiting_user_turn') == mode['turn_id'] or float(fields.get('lease_until') or 0) > time.time():
                continue
            running = db.execute('''SELECT r.pid FROM validation_runs r JOIN validation_plans p ON p.plan_id=r.plan_id
                                    WHERE p.repository=? AND p.number=? AND r.status='running' ''',
                                 (row['repository'], row['number'])).fetchall()
            if any(_worker_alive(r['pid']) for r in running):
                if not fields.get('awaiting_validation'):
                    _receipt(db, store, row, {'awaiting_validation': True})
                continue
            fingerprint = row['fingerprint']
            if not fingerprint or fingerprint != row['delivered_fingerprint']:
                continue
            if fields.get('idle_retry_fingerprint') == fingerprint:
                if fields.get('idle_retry_outcome') != 'stalled':
                    _receipt(db, store, row, {'idle_retry_outcome': 'stalled', 'awaiting_validation': False})
                    db.execute('UPDATE pr_watch SET error=? WHERE repository=? AND number=?',
                               ('Agent yielded with actionable work after bounded recovery; explicit retry required', row['repository'], row['number']))
                    stalled = True
                continue
            _receipt(db, store, row, {'idle_retry_fingerprint': fingerprint, 'idle_retry_outcome': 'pending', 'awaiting_validation': False})
            db.execute("UPDATE pr_watch SET delivery_state='pending',event_id=?,retry_at=? WHERE repository=? AND number=?",
                       (str(uuid.uuid4()), row['next_check_at'] if retry_at is None else retry_at, row['repository'], row['number']))
        if stalled:
            return dict(result or {}, systemMessage=render_prompt('incomplete_handoff'))
    return result


def _worker_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return True  # Uncertain process evidence never authorizes recovery.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def native(event, store, result):
    name = event.get('hook_event_name')
    sid = str(event.get('session_id') or '')
    if not sid:
        return result
    owner = store.session(sid)
    if not owner or owner['root_session'] != sid:
        return result
    if name == 'PostToolUse' and re.split(r'__|[.:/]', str(event.get('tool_name') or ''))[-1] == 'request_user_input_async':
        from .hook import _succeeded
        if _succeeded(event) and isinstance(event.get('tool_input'), dict) and event['tool_input'].get('questions'):
            with store._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                mode = db.execute('SELECT turn_id FROM turn_modes WHERE session_id=?', (sid,)).fetchone()
                rows = db.execute("SELECT * FROM pr_watch WHERE owner_session=? AND state='open'", (sid,)).fetchall()
                text = json.dumps(event['tool_input'])
                for row in rows:
                    if mode and (len(rows) == 1 or f"https://github.com/{row['repository']}/pull/{row['number']}" in text):
                        _receipt(db, store, row, {'awaiting_user_turn': mode['turn_id']})
    busy = name in {'UserPromptSubmit', 'PreToolUse', 'PostToolUse'}
    if busy or name in {'Stop', 'Interrupt', 'SessionEnd', 'StopFailure'}:
        if name == 'Stop' and result and result.get('decision') == 'block':
            busy = True
        with store._connect() as db:
            db.execute('INSERT INTO watch_sessions VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET busy=excluded.busy,updated_at=excluded.updated_at', (sid, int(busy), time.time()))
    prompt = event.get('prompt') or ''
    from . import closure
    if name == 'UserPromptSubmit' and prompt.startswith(closure.MARKER):
        token = prompt[len(closure.MARKER):].split('-->', 1)[0].strip()
        text = closure.consume(store, sid, event_id=token)
        if text:
            return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': text}}
        with store._connect() as db:
            db.execute('UPDATE watch_sessions SET busy=0 WHERE session_id=?', (sid,))
        return {'continue': False, 'stopReason': render_prompt('closeout_stale')}
    if name == 'UserPromptSubmit' and prompt.startswith(MARKER):
        token = prompt[len(MARKER):].split('-->', 1)[0].strip()
        text = consume(store, sid, event_id=token, full=False)
        if text:
            return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': text + '\n' + render_prompt('pr_rules')}}
        # A rejected attempt is terminal; only a genuinely new event or explicit retry can rearm it.
        with store._connect() as db:
            db.execute("UPDATE pr_watch SET delivery_state='failed',lease_until=0,error='Queued event was stale or unverified; not automatically replayed' WHERE owner_session=? AND event_id=? AND delivery_state='queued'", (sid, token))
        # An old queued delivery never restarts work or produces a stale status report.
        with store._connect() as db:
            db.execute('UPDATE watch_sessions SET busy=0 WHERE session_id=?', (sid,))
        return {'continue': False, 'stopReason': render_prompt('pr_update_stale')}
    if name == 'PostToolUse':
        with store._connect() as db:
            mode = db.execute('SELECT mode FROM turn_modes WHERE session_id=?', (sid,)).fetchone()
        if mode and mode['mode'] == 'informational':
            return result
        with store._connect() as db:
            ended = [dict(r) for r in db.execute('SELECT repository,number,state FROM pr_watch WHERE owner_session=? AND terminal_notice=1', (sid,))]
            db.execute('UPDATE pr_watch SET terminal_notice=0 WHERE owner_session=?', (sid,))
        if ended:
            text = '\n'.join(render_prompt('pr_terminal', repository=r['repository'], number=r['number'], state=r['state']) for r in ended)
            return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': text}}
        closeout = closure.consume(store, sid)
        if closeout:
            return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': closeout}}
        text = consume(store, sid, full=False)
        if text:
            return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': text + '\n' + render_prompt('pr_rules')}}
    return _idle_handoff(store, sid, event, result) if name == 'Stop' else result


async def deliver(store, sid, *, reader=probe, collector=None, runner=None, availability=None, checklist=False):
    from .supervisor import resume_command, run_process, claude_availability
    reserved = pending(store, sid, ('reserved',))
    owner = store.session(sid)
    if not owner or owner['root_session'] != sid:
        raise ValueError('Exact owner metadata is unavailable')
    uuid.UUID(sid)
    if checklist and owner['client'] == 'claude':
        eligible = next((r for r in reserved if (availability or claude_availability)(sid, r['cwd']) == 'idle'), None)
        if eligible:
            reserved = [eligible] + [r for r in reserved if r['event_id'] != eligible['event_id']]
        else:
            with store._connect() as db:
                db.execute("UPDATE pr_watch SET delivery_state='pending',lease_until=0,retry_at=next_check_at WHERE owner_session=? AND delivery_state='reserved'", (sid,))
            return False
    if checklist and len(reserved) > 1:
        # Each queued message has one PR, one receipt and one source checkout.
        with store._connect() as db:
            for other in reserved[1:]:
                db.execute("UPDATE pr_watch SET delivery_state='pending',lease_until=0,worker_pid=NULL WHERE owner_session=? AND event_id=? AND delivery_state='reserved'", (sid, other['event_id']))
        reserved = reserved[:1]
    rows = await revalidate(store, reserved, reader, collector, checklist=checklist)
    if not rows:
        with store._connect() as db:
            db.execute("UPDATE pr_watch SET delivery_state='pending',retry_at=next_check_at,lease_until=0 WHERE owner_session=? AND state='open' AND delivery_state='reserved'", (sid,))
        return False
    cwd = rows[0]['cwd']
    if not Path(cwd).is_dir():
        raise ValueError('Owner working directory is unavailable; no alternate chat is allowed')
    if owner['client'] == 'claude':
        state = (availability or claude_availability)(sid, cwd)
        if state != 'idle':
            with store._connect() as db:
                db.execute("UPDATE pr_watch SET delivery_state='pending',lease_until=0 WHERE owner_session=? AND delivery_state='reserved'", (sid,))
            return False
    body = message(rows, checklist=checklist)
    with store._connect() as db:
        busy = db.execute('SELECT busy FROM watch_sessions WHERE session_id=?', (sid,)).fetchone()
        if busy and busy['busy']:
            db.execute("UPDATE pr_watch SET delivery_state='pending',lease_until=0 WHERE owner_session=? AND delivery_state='reserved'", (sid,))
            return False
        for row in rows:
            payload = json.loads(row['payload'])
            if checklist:
                payload['queued_body_hash'] = hashlib.sha256(body.encode()).hexdigest()
            db.execute("UPDATE pr_watch SET delivery_state='queued',delivered_fingerprint=fingerprint,payload=?,worker_pid=COALESCE(worker_pid,?) WHERE owner_session=? AND event_id=? AND delivery_state='reserved'", (json.dumps(payload), os.getpid(), sid, row['event_id']))
    # The human-visible message contains actual work, never API JSON or a polling reminder.
    prompt = MARKER + rows[0]['event_id'] + '-->\n' + body
    args, stdin = resume_command(owner['client'], sid, prompt)
    result = await asyncio.to_thread(runner or run_process, args, cwd=cwd, input_text=stdin,
                                    timeout=20 if owner['client'] == 'codex' else 2700)
    execution_error = False
    if owner['client'] == 'claude':
        try:
            response = json.loads(result.get('stdout') or '')
            execution_error = isinstance(response, dict) and response.get('is_error') is True
        except (ValueError, TypeError):
            pass
    if result.get('returncode') != 0 or execution_error:
        raise RuntimeError('Owner delivery failed; explicit retry is required')
    with store._connect() as db:
        db.execute('UPDATE pr_watch SET lease_until=0 WHERE owner_session=? AND worker_pid=?', (sid, os.getpid()))
    return True


def write_health(store, result):
    path = store.state_dir / 'monitor-health.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(result, at=time.time())))
    temporary.replace(path)


async def serve(store, *, execute=True, sleeper=asyncio.sleep, clock=time.monotonic, checklist=False):
    """A live supervised process avoids macOS deferring interval job launches."""
    while True:
        started = clock()
        prompt_error = None
        try:
            from .prompts import refresh
            refresh(**({'checklist': True} if checklist else {}))
        except Exception as exc:
            prompt_error = f'{type(exc).__name__}: {str(exc)[:300]}'
        try:
            result = await tick(store, execute=execute, **({'checklist': True} if checklist else {}))
        except Exception as exc:
            result = {'errors': 1, 'service_error': f'{type(exc).__name__}: {str(exc)[:300]}'}
        if prompt_error:
            result['prompt_refresh_error'] = prompt_error
        write_health(store, result)
        await sleeper(max(1, 60 - (clock() - started)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['tick', 'serve', 'deliver', 'status'])
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--session')
    args = parser.parse_args()
    from .store import Store
    store = Store()
    if args.command == 'status':
        with store._connect() as db:
            print(json.dumps([dict(r) for r in db.execute('SELECT repository,number,owner_session,state,next_check_at,last_check_at,delivery_state,error FROM pr_watch')], indent=2))
        return
    lock = (store.state_dir / 'monitor.lock').open('a+')
    if args.command in {'tick', 'serve'}:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if args.command == 'serve':
            asyncio.run(serve(store, execute=args.execute, checklist=True))
        else:
            write_health(store, asyncio.run(tick(store, execute=args.execute, checklist=True)))
    else:
        try:
            asyncio.run(deliver(store, args.session, checklist=True))
        except Exception as exc:
            with store._connect() as db:
                db.execute("UPDATE pr_watch SET delivery_state='failed',lease_until=0,error=? WHERE owner_session=? AND ((worker_pid=? AND delivery_state IN ('reserved','queued','consumed')) OR (worker_pid IS NULL AND delivery_state IN ('reserved','queued')))", (str(exc)[:300], args.session, os.getpid()))
        finally:
            with store._connect() as db:
                db.execute('UPDATE pr_watch SET lease_until=0,worker_pid=NULL WHERE owner_session=? AND worker_pid=?', (args.session, os.getpid()))


if __name__ == '__main__':
    main()
