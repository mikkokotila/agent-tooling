"""Execute necessary PR checks once, then require publication and conclusion."""
from __future__ import annotations
from .prompts import render_prompt

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from . import progress
from .policy import evaluate_pr, evaluate_review
from .copilot import is_service_review


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


def review_owner(store, sid, repository, number, cwd, closed_head=None, *, snapshot=None):
    if closed_head is None:
        return owned_pr(store, sid, repository, number)
    if not isinstance(closed_head, str) or not re.fullmatch(r'[0-9a-f]{40}', closed_head):
        raise ValueError('--closed-head requires the full closed PR head SHA')
    row = store.closed_review_owner(sid, repository, number, cwd)
    from .publication import source, exact_source
    local = source(cwd, stable=True)
    if (not exact_source(row, local) or local['head'] != closed_head
            or row['identity']['retirement'].get('head_sha') != closed_head
            or row['identity']['retirement'].get('state') != row['identity']['watch']['state']):
        raise ValueError('Closed review receipts require the clean recorded checkout at the pinned PR head')
    if snapshot is not None:
        stamp = snapshot.get('collected_at')
        if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                or not 0 <= time.time() - stamp <= progress.REMOTE_EVIDENCE_TTL
                or snapshot.get('profile') != 'review' or snapshot.get('state') != 'closed'
                or snapshot.get('merged') is not (row['identity']['watch']['state'] == 'merged')
                or snapshot.get('repository') != repository or snapshot.get('number') != number
                or snapshot.get('head_sha') != closed_head or snapshot.get('head_sha_after') != closed_head
                or snapshot.get('head_repository') != repository or snapshot.get('base_repository') != repository
                or type(snapshot.get('head_repository_id')) is not int or snapshot['head_repository_id'] <= 0
                or type(snapshot.get('base_repository_id')) is not int
                or snapshot.get('base_repository_id') != snapshot['head_repository_id']
                or not isinstance(snapshot.get('head_ref'), str) or not snapshot['head_ref']
                or closed_head not in snapshot.get('commit_shas', [])):
            raise ValueError('Closed review receipts require fresh coherent proof of the exact closed PR source')
    return dict(row, source=local)


def validation_owner(store, sid, repository, number, cwd, merged_head=None, *, preflight=False):
    if merged_head is None:
        return owned_pr(store, sid, repository, number)
    if not isinstance(merged_head, str) or not re.fullmatch(r'[0-9a-f]{40}', merged_head):
        raise ValueError('--merged-head requires the full original merged PR head SHA')
    row = store.retained_validation_plan(sid, repository, number, cwd)
    from .publication import source, exact_source
    local = source(cwd)
    if not exact_source(row, local) or local['head'] != merged_head:
        raise ValueError('Post-merge validation requires the clean recorded checkout at the pinned merged PR head')
    if preflight:
        proof = store.preflight(repository, number) or {}
        stamp = proof.get('collected_at')
        if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                or not 0 <= time.time() - stamp <= progress.REMOTE_EVIDENCE_TTL
                or proof.get('state') != 'closed' or proof.get('merged') is not True
                or proof.get('repository') != repository or proof.get('number') != number
                or proof.get('head_sha') != merged_head or proof.get('head_sha_after') != merged_head
                or not proof.get('base_ref') or proof.get('base_ref_after') != proof.get('base_ref')
                or proof.get('head_repository') != repository or proof.get('base_repository') != repository
                or not re.fullmatch(r'[0-9a-f]{40}', str(proof.get('merge_commit_sha')))):
            raise ValueError('Post-merge validation requires fresh stable proof of the exact merged PR source')
    return row


def evidence_digest(snapshot, dispositions, previous=None):
    """Only changed review requests or failed CI justify a new check generation."""
    reviews = evaluate_review(snapshot, dispositions)
    exempt = set(reviews[0].get('evidence', {}).get('verified_conclusion_replies', []))
    comments = sorted((str(c.get('id')), str(c.get('updated_at')),
                       hashlib.sha256(str(c.get('body', '')).encode()).hexdigest())
                      for c in snapshot.get('comments', []) if c.get('url') not in exempt
                      and c.get('body') and not is_service_review(c))
    failures = []
    from .copilot import is_service_check
    for check in snapshot.get('checks', []):
        if is_service_check(check):
            continue
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
    with store._connect() as db:
        watch = db.execute('SELECT state FROM pr_watch WHERE repository=? AND number=?',
                           (row['repository'], row['number'])).fetchone()
    return progress.evaluate_progress(row, store.validation_evidence(row['plan_id']),
        progress.fingerprint(row['cwd'], row['plan']), snapshot, gates,
        progress.working_state(row['cwd']),store.preflight(row['repository'],row['number']),
        source_closed=bool(watch and watch['state'] in {'closed', 'merged'}),
        check_digests={check['id']: progress.check_fingerprint(row['cwd'], row['plan'], check)
                       for check in row['plan']['checks'] if 'input_digest' in check})


def _final_coverage_local(context, cwd):
    from .publication import source, exact_source
    row = context['final']['row']
    local = source(cwd, stable=True)
    if (not exact_source(row, local) or local['head'] != context['final']['identity']['retirement']['head_sha']
            or progress.fingerprint(cwd, row['plan']) != row['input_digest'] or source(cwd, stable=True) != local):
        raise ValueError('Final coverage requires the exact clean successor checkout, source and current frozen inputs')
    files, directories = progress.source_tree(cwd, local['head'])
    if (any((Path(cwd) / path).is_symlink() for path in directories)
            or progress.fingerprint(cwd, row['plan'], source_snapshot=(files, None)) != row['input_digest']
            or source(cwd, stable=True) != local):
        raise ValueError('Final qualification inputs do not match the exact published final tree')
    return local


def _final_coverage_evidence(context, proof, cwd):
    from .closure import gate as closure_gate
    from .publication import _merged_at
    for role in ('original', 'final'):
        snapshot, recorded = proof[role], context[role]
        stamp = snapshot.get('collected_at')
        if (not isinstance(stamp, (float, int)) or isinstance(stamp, bool) or not 0 <= time.time() - stamp <= progress.REMOTE_EVIDENCE_TTL
                or snapshot.get('repository') != context['repository'] or snapshot.get('number') != recorded['row']['number']
                or snapshot.get('state') != 'closed' or snapshot.get('merged') is not True
                or snapshot.get('head_sha') != recorded['identity']['retirement']['head_sha']
                or snapshot.get('head_sha_after') != snapshot.get('head_sha')
                or not re.fullmatch(r'[0-9a-f]{40}', str(snapshot.get('head_sha')))
                or snapshot.get('head_repository') != context['repository'] or snapshot.get('base_repository') != context['repository']
                or type(snapshot.get('head_repository_id')) is not int or snapshot['head_repository_id'] <= 0
                or type(snapshot.get('base_repository_id')) is not int
                or snapshot.get('base_repository_id') != snapshot['head_repository_id']
                or not snapshot.get('head_ref') or not snapshot.get('base_ref')
                or not re.fullmatch(r'[0-9a-f]{40}', str(snapshot.get('merge_commit_sha')))
                or _merged_at(snapshot.get('merged_at')) is None):
            raise ValueError('Final coverage requires fresh coherent merged source identities for both recorded PRs')
        if any(g['status'] != 'PASS' for g in evaluate_review(snapshot, recorded['dispositions'])):
            raise ValueError('Final coverage requires actual handling and public conclusions for both PRs')
    original = {**proof['original'], **proof['original_closure']}
    if (proof['original_closure'].get('closure_error') or closure_gate(original)['status'] != 'PASS'
            or original.get('closure_merged') is not True or original.get('closure_state') != 'MERGED'):
        raise ValueError('Original issue closure remains unverified')
    final = proof['final']
    if final['head_repository_id'] != proof['original']['head_repository_id'] or not final.get('checks'):
        raise ValueError('Final qualification requires the same repository identity and actual CI evidence')
    gates = evaluate_pr(final, context['final']['dispositions'])
    if any(g['status'] != 'PASS' for g in gates if g['gate'] != 'MERGE_READY'):
        raise ValueError('Final qualification requires every actual CI, review and issue-closure gate')
    real_checks = next(g for g in gates if g['gate'] == 'CI_FAILED')['evidence'].get('checks', [])
    if not any(check.get('head_sha') == final['head_sha'] and check.get('status') == 'completed'
               and check.get('conclusion') == 'success' for check in real_checks):
        raise ValueError('Final qualification requires successful non-service CI execution on the exact final head')
    decisions = {}
    for review in sorted(final.get('submitted_reviews', []), key=lambda r: str(r.get('submitted_at') or '')):
        if review.get('state') in {'APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'}:
            decisions[review.get('author_login')] = review
    approvals = [review for review in decisions.values() if review.get('state') == 'APPROVED'
                 and review.get('commit_sha') == final['head_sha'] and review.get('author_type') == 'User'
                 and review.get('author_login') not in {final.get('author_login'), final.get('actor_login')}
                 and review.get('id') and review.get('url') and _merged_at(review.get('submitted_at')) is not None]
    if not approvals or any(review.get('state') == 'CHANGES_REQUESTED' for review in decisions.values()):
        raise ValueError('Final qualification requires an actual independent approval of the exact final head')
    comparison, merge = proof['ancestry'], proof['merge']
    original_merge = proof['original']['merge_commit_sha']
    if (comparison.get('base_commit', {}).get('sha') != original_merge
            or comparison.get('merge_base_commit', {}).get('sha') != original_merge
            or type(comparison.get('behind_by')) is not int or comparison['behind_by'] != 0
            or type(comparison.get('ahead_by')) is not int or comparison['ahead_by'] < 0
            or comparison.get('status') != ('ahead' if comparison['ahead_by'] else 'identical')
            or (comparison['ahead_by'] == 0) != (original_merge == final['head_sha'])):
        raise ValueError('The delivered predecessor merge is not proven in the final source ancestry')
    tree = progress._git(Path(cwd), '--no-replace-objects', 'rev-parse', final['head_sha'] + '^{tree}').decode().strip()
    if merge.get('sha') != final['merge_commit_sha'] or merge.get('tree', {}).get('sha') != tree:
        raise ValueError('The qualified final head and actual merge must have identical source trees')
    manifests = []
    for snapshot in (proof['original'], final):
        raw = progress._git(Path(cwd), '--no-replace-objects', 'show', snapshot['head_sha'] + ':package.json')
        manifests.append(json.loads(raw).get('scripts'))
    if not isinstance(manifests[0], dict) or manifests[0] != manifests[1]:
        raise ValueError('The full repository script definitions changed; this bounded coverage route cannot establish equivalence')
    row, runs = context['final']['row'], context['final']['runs']
    checks = {check['id']: check for check in row['plan']['checks']}
    if len(runs) != len(checks) or {run['check_id'] for run in runs} != set(checks):
        raise ValueError('Every final necessary check needs its own actual execution in the exact final plan')
    for run in runs:
        stamps = [run.get('started_at'), run.get('finished_at')]
        if (run['plan_id'] != row['plan_id'] or run['status'] != 'pass' or type(run['exit_code']) is not int or run['exit_code'] != 0
                or any(run.get(key) != row['input_digest'] for key in ('input_digest', 'before_digest', 'after_digest'))
                or any(type(stamp) not in {float, int} or not math.isfinite(stamp) for stamp in stamps)
                or not row['created_at'] <= stamps[0] <= stamps[1] <= time.time()):
            raise ValueError('Final coverage cannot promote missing, failed, unknown, stale or fabricated execution evidence')
    coverage = progress.final_coverage(context['original']['row'], row, context['original']['scope'], proof['declaration'])
    for value in coverage['replaced_private_configs']:
        path = Path(value)
        # Establish only that this was a private/untracked declaration, not a
        # tracked source config or a path through a tracked link/submodule.
        # Missing historical private bytes remain explicitly unverified.
        for candidate in (path, *(parent for parent in path.parents if parent != Path('.'))):
            entries = progress._git(Path(cwd), '--no-replace-objects', 'ls-tree', '-z', proof['original']['head_sha'],
                                    '--', candidate.as_posix())
            for entry in entries.split(b'\0'):
                if entry and (candidate == path or entry.split(b' ', 1)[0] != b'040000'):
                    raise ValueError('Final coverage cannot replace a tracked browser configuration or linked source path')
    if any(not isinstance(manifests[1].get(script), str) or not manifests[1][script].strip() for script in coverage['scripts'].values()):
        raise ValueError('A declared full gate is absent from the published package scripts')
    local = _final_coverage_local(context, cwd)
    return {'phase': 'QUALIFIED_SUCCESSOR', 'blocked': False, 'qualification_verified': True,
            'completion_verified': False, 'source_closed': True,
            'reason': 'Actual final-source full gates cover the original declared qualification; historical results remain unchanged',
            'next_action': 'Report the final coverage and remaining task acceptance; do not rerun obsolete source or copy passing receipts.',
            'original_plan_id': context['original']['row']['plan_id'], 'final_plan_id': row['plan_id'],
            'final_pr': f"https://github.com/{context['repository']}/pull/{row['number']}",
            'final_head': local['head'], 'final_tree': tree, 'final_input_digest': row['input_digest'],
            'final_runs': runs, 'independent_approvals': approvals, 'coverage': coverage,
            'historical_runs': context['original']['runs']}


async def qualify_successor(store, sid, repository, original, final, cwd, payload, *, client=None):
    """One explicit collection and append-only coverage receipt, without execution."""
    import asyncio
    from .github import collect_pr, collect_review
    from .closure import read_live
    from .transport import GitHubClient
    context = store.final_coverage_context(sid, repository, original, final)
    progress.final_coverage(context['original']['row'], context['final']['row'], context['original']['scope'], payload)
    _final_coverage_local(context, cwd)
    # Supersede any earlier success before remote reads, including failed or
    # interrupted refreshes. Only this latest attempt may append a conclusion.
    attempt = store.record_final_coverage(context, None, lambda: _final_coverage_local(context, cwd))
    previous, successor, closure = await asyncio.gather(collect_review(repository, original), collect_pr(repository, final),
                                                       read_live(repository, original))
    client = client or GitHubClient()
    prefix = '/repos/' + repository
    old_merge, head, merge_sha = previous.get('merge_commit_sha'), successor.get('head_sha'), successor.get('merge_commit_sha')
    if any(not re.fullmatch(r'[0-9a-f]{40}', str(sha)) for sha in (old_merge, head, merge_sha)):
        raise ValueError('Merged source identities are unavailable; no final coverage was recorded')
    ancestry, merge = await asyncio.gather(client.get(f'{prefix}/compare/{old_merge}...{head}'),
                                         client.get(f'{prefix}/git/commits/{merge_sha}'))
    proof = {'original': previous, 'final': successor, 'original_closure': closure,
             'ancestry': ancestry, 'merge': merge, 'declaration': payload}
    result = _final_coverage_evidence(context, proof, cwd)
    receipt = store.record_final_coverage(context, proof, lambda: _final_coverage_evidence(context, proof, cwd),
                                          attempt_id=attempt['id'])
    return {'receipt_id': receipt['id'], **result}


def successor_progress(store, row):
    receipt = store.final_coverage_receipt(row['session_id'], row['plan_id'])
    if receipt is None:
        return None
    context = receipt['context']
    try:
        if receipt['proof'] is None:
            raise ValueError('Final coverage refresh is pending or failed; prior successful coverage cannot be reused')
        current = store.final_coverage_context(context['session_id'], context['repository'],
            context['original']['row']['number'], context['final']['row']['number'])
        if current != context:
            raise ValueError('Recorded final coverage context has changed')
        return {'receipt_id': receipt['id'], **_final_coverage_evidence(context, receipt['proof'], context['final']['row']['cwd'])}
    except Exception as exc:
        return {'phase': 'SUCCESSOR_INCOMPLETE', 'blocked': True, 'qualification_verified': False,
                'completion_verified': False, 'receipt_id': receipt['id'], 'reason': str(exc),
                'next_action': 'Reconcile the stated evidence gap and explicitly refresh qualify-successor; preserve all actual results.'}


def save_plan(store, sid, repository, number, cwd, payload, *, merged_head=None, from_head=None):
    if from_head is not None and merged_head is None:
        raise ValueError('--from-head requires --merged-head and the retained necessary plan')
    validation_owner(store, sid, repository, number, cwd, merged_head, preflight=True)
    root = progress.repo_root(cwd)
    cwd = str(Path(cwd).expanduser().resolve())
    if cwd != root:
        raise ValueError('Use the repository root for --cwd; put required subdirectories in each check cwd')
    cached = store.snapshot(repository, number) or {}
    snapshot = cached.get('snapshot') or {}
    if merged_head is None and (snapshot.get('state') != 'open' or snapshot.get('merged') is not False):
        raise ValueError('A validation plan requires verified live evidence of an open PR; completed work is not reopened')
    plan = progress.normalize_plan(payload, cwd)
    for check in plan['checks']:
        if 'input_paths' in check or 'env_names' in check:
            if merged_head is None:
                raise ValueError('Per-check qualification inputs require --merged-head and a retained plan')
            if not check.get('input_paths'):
                raise ValueError('An added qualification control requires explicit input_paths')
            if any(not (Path(cwd) / path).exists() for path in check['input_paths']):
                raise ValueError('Qualification control inputs must exist before they are pinned')
            check['input_digest'] = progress.check_fingerprint(cwd, plan, check)
    previous = store.validation_plan(sid, repository, number, cwd)
    _recover_runs(store, previous)
    source_epoch = None
    if previous and 'source_epoch' in previous['plan']:
        plan['source_epoch'] = previous['plan']['source_epoch']
    digest = progress.fingerprint(cwd, plan)
    if from_head is not None:
        prior_epoch = (previous or {}).get('plan', {}).get('source_epoch')
        if prior_epoch:
            if prior_epoch['from_head'] != from_head or prior_epoch['head_sha'] != merged_head:
                raise ValueError('The final published source epoch is immutable')
        else:
            from .publication import _merged_at
            proof = store.preflight(repository, number) or {}
            merged_at = _merged_at(proof.get('merged_at'))
            if not previous or merged_at is None or previous['created_at'] > merged_at:
                raise ValueError('A final-head epoch requires a retained plan recorded before the proven merge')
            if progress.plan_declaration(plan) != progress.plan_declaration(previous['plan']):
                raise ValueError('A final-head epoch must preserve every necessary check and input/environment declaration')
            if any('correction' in check for check in plan['checks']):
                raise ValueError('Source-bound checker correction receipts cannot transfer to a new source epoch')
            historical, published = progress.source_snapshots(cwd, from_head, merged_head)
            canonical_fallback = False
            if progress.fingerprint(cwd, previous['plan'], source_snapshot=historical) != previous['input_digest']:
                # Preserve the earlier canonical candidate for unresolved historical
                # modes. Present matching files and private inputs keep current modes.
                canonical_fallback = True
                if progress.fingerprint(cwd, previous['plan'], source_snapshot=historical,
                                        canonical_fallback=True) != previous['input_digest']:
                    raise ValueError('Previous source and unchanged private inputs do not reproduce the retained fingerprint')
            old_pins = {check['id']: check['input_digest'] for check in previous['plan']['checks'] if 'input_digest' in check}
            if any(progress.check_fingerprint(cwd, previous['plan'], check, source_snapshot=historical,
                                             canonical_fallback=canonical_fallback) != old_pins[check['id']]
                   for check in previous['plan']['checks'] if check['id'] in old_pins):
                raise ValueError('Previous qualification control inputs no longer match their retained pins')
            if (progress.fingerprint(cwd, plan, source_snapshot=published) != digest
                    or any(progress.check_fingerprint(cwd, plan, check, source_snapshot=published) != check['input_digest']
                           for check in plan['checks'] if 'input_digest' in check)):
                raise ValueError('Current tracked source does not match the exact published final tree')
            if digest == previous['input_digest']:
                raise ValueError('Unchanged source inputs cannot create a new validation epoch')
            validation_owner(store, sid, repository, number, cwd, merged_head, preflight=True)
            if (progress.fingerprint(cwd, plan) != digest
                    or any(progress.check_fingerprint(cwd, plan, check) != check['input_digest']
                           for check in plan['checks'] if 'input_digest' in check)):
                raise ValueError('Validation inputs changed while proving the final source epoch')
            source_epoch = dict(previous_plan_id=previous['plan_id'], from_head=from_head, head_sha=merged_head,
                merge_commit_sha=proof['merge_commit_sha'], previous_input_digest=previous['input_digest'],
                previous_check_digests=old_pins)
    row = store.save_validation_plan(sid, repository, number, cwd, plan,
        digest, evidence_digest(snapshot, store.dispositions(repository, number),
                                                       (previous or {}).get('evidence_digest')),
        retained=merged_head is not None, source_epoch=source_epoch)
    store.audit('pr_validation_plan_bound', {'session_id': sid, 'plan_id': row['plan_id'],
                'repository': repository, 'number': number, 'root': root})
    return {'plan': row, 'progress': current_phase(store, row)}


def run_check(store, sid, repository, number, cwd, check_id, *, manage_process_group=False, merged_head=None):
    validation_owner(store, sid, repository, number, cwd, merged_head, preflight=True)
    cwd = progress.repo_root(cwd)
    row = store.validation_plan(sid, repository, number, cwd)
    if row is None:
        raise ValueError('Declare the necessary checks with validation-plan before running local PR verification')
    _recover_runs(store, row)
    cached = store.snapshot(repository, number) or {}
    snapshot = cached.get('snapshot') or {}
    digest = progress.fingerprint(row['cwd'], row['plan'])
    if merged_head is not None and digest != row['input_digest']:
        raise ValueError('Post-merge validation inputs changed; source repairs need their own scoped PR')
    evidence = evidence_digest(snapshot, store.dispositions(repository, number), row['evidence_digest'])
    if digest != row['input_digest'] or evidence != row['evidence_digest']:
        row = store.save_validation_plan(sid, repository, number, row['cwd'], row['plan'], digest, evidence,
                                        retained=merged_head is not None)
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
    if merged_head is not None:
        validation_owner(store, sid, repository, number, cwd, merged_head, preflight=True)
    before = progress.check_fingerprint(row['cwd'], row['plan'], check)
    run = store.begin_validation(row['plan_id'], check_id, before, owner_pid=os.getpid(), owner_group=group,
                                 session_id=sid, retained=merged_head is not None)
    exit_code = None
    try:
        completed = _run_process(check['argv'], cwd=Path(row['cwd']) / check.get('cwd', '.'))
        exit_code = completed.returncode
    finally:
        try:
            after = progress.check_fingerprint(row['cwd'], row['plan'], check)
        except Exception:
            after = 'unverifiable'
        source_valid = True
        if merged_head is not None:
            try:
                validation_owner(store, sid, repository, number, cwd, merged_head)
            except Exception:
                source_valid = False
        result = store.finish_validation(run['run_id'], exit_code, after, session_id=sid, source_valid=source_valid)
    return {'run': result, 'progress': current_phase(store, row)}


def task_progress(store, sid):
    result = []
    for row in store.validation_plans(sid):
        # Retirement cancels reminders, never the retained task's acceptance.
        try:
            phase = successor_progress(store, row) or current_phase(store, row)
            state = 'PASS' if phase['phase'] == 'COMPLETE' or phase.get('qualification_verified') is True else 'WAIT'
            details = [phase.get('next_action') or phase.get('reason') or phase['phase']]
        except Exception as exc:
            phase = {'phase': 'UNKNOWN'}
            state, details = 'UNKNOWN', [str(exc)]
        result.append({'gate': 'PR_PROGRESS', 'status': state, 'repository': row['repository'],
                       'number': row['number'], 'details': details, 'evidence': {'plan_id': row['plan_id'], **phase}})
    return result


def launch_check(store, sid, repository, number, cwd, check_id, *, merged_head=None):
    """Detach the managed runner; only its actual eventual exit records a result."""
    validation_owner(store, sid, repository, number, cwd, merged_head, preflight=True)
    cwd = progress.repo_root(cwd)
    row = store.validation_plan(sid, repository, number, cwd)
    if row is None:
        raise ValueError('Declare the necessary checks before launching validation')
    _recover_runs(store, row)
    digest = progress.fingerprint(row['cwd'], row['plan'])
    if merged_head is not None and digest != row['input_digest']:
        raise ValueError('Post-merge validation inputs changed; source repairs need their own scoped PR')
    cached = store.snapshot(repository, number) or {}
    evidence = evidence_digest(cached.get('snapshot') or {}, store.dispositions(repository, number), row['evidence_digest'])
    if digest != row['input_digest'] or evidence != row['evidence_digest']:
        row = store.save_validation_plan(sid, repository, number, cwd, row['plan'], digest, evidence,
                                        retained=merged_head is not None)
    phase = current_phase(store, row)
    if phase['phase'] not in {'VALIDATE_REQUIRED', 'VALIDATE_FAILED'}:
        raise ValueError(f"PR_PROGRESS {phase['phase']}: {phase.get('next_action')}")
    if check_id not in {c['id'] for c in row['plan']['checks']}:
        raise ValueError('Check is absent from the declared plan')
    if any(c.get('correction', {}).get('unexecuted') is True
           and c['correction']['check_id'] == check_id for c in row['plan']['checks']):
        raise ValueError('This unexecuted checker has an admitted correction; execute its fresh corrected check')
    check = next(c for c in row['plan']['checks'] if c['id'] == check_id)
    check_digest = check.get('input_digest', digest)
    if any(r['check_id'] == check_id and r['input_digest'] == check_digest and r['status'] in {'running','pass','fail'} for r in store.validation_evidence(row['plan_id'])):
        raise ValueError('This check is already running or completed for these inputs')
    logs = store.state_dir / 'validation-logs'
    logs.mkdir(mode=0o700, exist_ok=True)
    log = logs / (str(uuid.uuid4()) + '.log')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent),
               CALLER_PR_GATES_STATE_DIR=str(store.state_dir))
    args = [sys.executable, '-P', '-m', 'caller_pr_gates.cli', 'validate', '--worker', '--session', sid,
            '--pr', f'https://github.com/{repository}/pull/{number}', '--cwd', cwd, '--check', check_id]
    if merged_head is not None:
        validation_owner(store, sid, repository, number, cwd, merged_head, preflight=True)
        args.extend(['--merged-head', merged_head])
    with log.open('w') as output:
        log.chmod(0o600)
        child = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=output, start_new_session=True)
    store.audit('validation_background_started', {'session_id': sid, 'repository': repository,
                'number': number, 'check': check_id, 'pid': child.pid, 'log': str(log)})
    return {'started': True, 'pid': child.pid, 'log': str(log), 'completion_verified': False,
            'next_action': render_prompt('validation_background')}
