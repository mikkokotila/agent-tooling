"""Execute necessary PR checks once, then require publication and conclusion."""
from __future__ import annotations
from .prompts import render_prompt

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from . import progress
from .policy import evaluate_pr, evaluate_review


def _run_process(argv, cwd):
    child = subprocess.Popen(argv, cwd=cwd, shell=False)
    try:
        return subprocess.CompletedProcess(argv, child.wait())
    except BaseException:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        raise


def _recover_runs(store, row):
    if row:
        for old in store.validation_runs(row['plan_id']):
            if old['status'] == 'running':
                store.recover_validation_run(old['run_id'])


def owned_pr(store, sid, repository, number):
    row = next((p for p in store.prs(sid) if p['repository'].lower() == repository.lower()
                and p['number'] == number), None)
    if row is None:
        raise ValueError('PR is not registered to this task')
    return row


def evidence_digest(snapshot, dispositions, previous=None):
    """Only changed review requests or failed CI justify a new check generation."""
    reviews = evaluate_review(snapshot, dispositions)
    exempt = set(reviews[0].get('evidence', {}).get('verified_conclusion_replies', []))
    comments = sorted((str(c.get('id')), str(c.get('updated_at')),
                       hashlib.sha256(str(c.get('body', '')).encode()).hexdigest())
                      for c in snapshot.get('comments', []) if c.get('url') not in exempt and c.get('body'))
    failures = []
    for check in snapshot.get('checks', []):
        state = str(check.get('status', '')).lower()
        conclusion = str(check.get('conclusion', '')).lower()
        if state in {'failure', 'error', 'failed'} or conclusion in {'failure', 'cancelled', 'timed_out', 'action_required', 'startup_failure', 'stale'}:
            failures.append({k: check.get(k) for k in ('name', 'id', 'url', 'status', 'conclusion', 'run_attempt', 'completed_at')})
    keys = {hashlib.sha256(json.dumps({'review': c}, sort_keys=True).encode()).hexdigest() for c in comments}
    keys.update(hashlib.sha256(json.dumps({'failed_check': c}, sort_keys=True).encode()).hexdigest() for c in failures)
    # Keep observed identities. A failure clearing, comment disappearing or a
    # refresh timestamp changing does not justify rerunning unchanged checks.
    if previous:
        observed = json.loads(previous)
        if not isinstance(observed, list) or any(not isinstance(item, str) for item in observed):
            raise ValueError('Stored validation evidence identities are malformed')
        keys.update(observed)
    return json.dumps(sorted(keys), separators=(',', ':'))


def current_phase(store, row):
    cached = store.snapshot(row['repository'], row['number']) or {}
    snapshot = cached.get('snapshot') or {}
    dispositions = store.dispositions(row['repository'], row['number'])
    gates = evaluate_pr(snapshot, dispositions)
    return progress.evaluate_progress(row, store.validation_evidence(row['plan_id']),
        progress.fingerprint(row['cwd'], row['plan']), snapshot, gates,
        progress.working_state(row['cwd']))


def save_plan(store, sid, repository, number, cwd, payload):
    owned_pr(store, sid, repository, number)
    root = progress.repo_root(cwd)
    cwd = str(Path(cwd).expanduser().resolve())
    if cwd != root:
        raise ValueError('Use the repository root for --cwd; put required subdirectories in each check cwd')
    cached = store.snapshot(repository, number) or {}
    snapshot = cached.get('snapshot') or {}
    if snapshot.get('state') != 'open' or snapshot.get('merged') is not False:
        raise ValueError('A validation plan requires verified live evidence of an open PR; completed work is not reopened')
    plan = progress.normalize_plan(payload, cwd)
    previous = store.validation_plan(sid, repository, number, cwd)
    _recover_runs(store, previous)
    row = store.save_validation_plan(sid, repository, number, cwd, plan,
        progress.fingerprint(cwd, plan), evidence_digest(snapshot, store.dispositions(repository, number),
                                                       (previous or {}).get('evidence_digest')))
    store.audit('pr_validation_plan_bound', {'session_id': sid, 'plan_id': row['plan_id'],
                'repository': repository, 'number': number, 'root': root})
    return {'plan': row, 'progress': current_phase(store, row)}


def run_check(store, sid, repository, number, cwd, check_id, *, manage_process_group=False):
    owned_pr(store, sid, repository, number)
    cwd = progress.repo_root(cwd)
    row = store.validation_plan(sid, repository, number, cwd)
    if row is None:
        raise ValueError('Declare the necessary checks with validation-plan before running local PR verification')
    _recover_runs(store, row)
    cached = store.snapshot(repository, number) or {}
    snapshot = cached.get('snapshot') or {}
    digest = progress.fingerprint(row['cwd'], row['plan'])
    evidence = evidence_digest(snapshot, store.dispositions(repository, number), row['evidence_digest'])
    if digest != row['input_digest'] or evidence != row['evidence_digest']:
        row = store.save_validation_plan(sid, repository, number, row['cwd'], row['plan'], digest, evidence)
    phase = current_phase(store, row)
    if phase['phase'] not in {'VALIDATE_REQUIRED', 'VALIDATE_FAILED'}:
        raise ValueError(f"PR_PROGRESS {phase['phase']}: {phase.get('next_action') or phase.get('reason')}")
    check = next((c for c in row['plan']['checks'] if c['id'] == check_id), None)
    if check is None:
        raise ValueError('Check is absent from the frozen necessary-check plan')
    group = None
    if manage_process_group:
        if os.getpgrp() != os.getpid():
            os.setpgid(0, 0)
        group = os.getpgrp()
    run = store.begin_validation(row['plan_id'], check_id, digest, owner_pid=os.getpid(), owner_group=group, session_id=sid)
    exit_code = None
    try:
        completed = _run_process(check['argv'], cwd=Path(row['cwd']) / check.get('cwd', '.'))
        exit_code = completed.returncode
    finally:
        try:
            after = progress.fingerprint(row['cwd'], row['plan'])
        except Exception:
            after = 'unverifiable'
        result = store.finish_validation(run['run_id'], exit_code, after, session_id=sid)
    return {'run': result, 'progress': current_phase(store, row)}


def task_progress(store, sid):
    result = []
    for row in store.validation_plans(sid):
        # Historical validation evidence stays recorded after source work ends.
        # A vanished temporary checkout must never reopen a merged PR.
        with store._connect() as db:
            watch = db.execute('SELECT state FROM pr_watch WHERE repository=? AND number=?',
                               (row['repository'], row['number'])).fetchone()
        cached = store.snapshot(row['repository'], row['number']) or {}
        snapshot = cached.get('snapshot') or {}
        if (watch and watch['state'] in {'closed', 'merged'}) or snapshot.get('state') == 'closed' or snapshot.get('merged') is True:
            continue
        try:
            phase = current_phase(store, row)
            state = 'PASS' if phase['phase'] == 'COMPLETE' else 'WAIT'
            details = [phase.get('next_action') or phase.get('reason') or phase['phase']]
        except Exception as exc:
            phase = {'phase': 'UNKNOWN'}
            state, details = 'UNKNOWN', [str(exc)]
        result.append({'gate': 'PR_PROGRESS', 'status': state, 'repository': row['repository'],
                       'number': row['number'], 'details': details, 'evidence': {'plan_id': row['plan_id'], **phase}})
    return result


def launch_check(store, sid, repository, number, cwd, check_id):
    """Detach the managed runner; only its actual eventual exit records a result."""
    owned_pr(store, sid, repository, number)
    cwd = progress.repo_root(cwd)
    row = store.validation_plan(sid, repository, number, cwd)
    if row is None:
        raise ValueError('Declare the necessary checks before launching validation')
    _recover_runs(store, row)
    digest = progress.fingerprint(row['cwd'], row['plan'])
    cached = store.snapshot(repository, number) or {}
    evidence = evidence_digest(cached.get('snapshot') or {}, store.dispositions(repository, number), row['evidence_digest'])
    if digest != row['input_digest'] or evidence != row['evidence_digest']:
        row = store.save_validation_plan(sid, repository, number, cwd, row['plan'], digest, evidence)
    phase = current_phase(store, row)
    if phase['phase'] not in {'VALIDATE_REQUIRED', 'VALIDATE_FAILED'}:
        raise ValueError(f"PR_PROGRESS {phase['phase']}: {phase.get('next_action')}")
    if check_id not in {c['id'] for c in row['plan']['checks']}:
        raise ValueError('Check is absent from the declared plan')
    if any(r['check_id'] == check_id and r['input_digest'] == digest and r['status'] in {'running','pass','fail'} for r in store.validation_evidence(row['plan_id'])):
        raise ValueError('This check is already running or completed for these inputs')
    logs = store.state_dir / 'validation-logs'
    logs.mkdir(mode=0o700, exist_ok=True)
    log = logs / (str(uuid.uuid4()) + '.log')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent),
               CALLER_PR_GATES_STATE_DIR=str(store.state_dir))
    args = [sys.executable, '-m', 'caller_pr_gates.cli', 'validate', '--worker', '--session', sid,
            '--pr', f'https://github.com/{repository}/pull/{number}', '--cwd', cwd, '--check', check_id]
    with log.open('w') as output:
        log.chmod(0o600)
        child = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=output, start_new_session=True)
    store.audit('validation_background_started', {'session_id': sid, 'repository': repository,
                'number': number, 'check': check_id, 'pid': child.pid, 'log': str(log)})
    return {'started': True, 'pid': child.pid, 'log': str(log), 'completion_verified': False,
            'next_action': render_prompt('validation_background')}
