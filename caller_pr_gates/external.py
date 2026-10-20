"""Verified external failures can yield, but never become successful checks."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable

from .policy import _latest_checks, _outcome
from .copilot import is_service_review

JOB_URL = re.compile(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/(\d+)/job/(\d+)(?:\?.*)?\Z')


def gate_fingerprint(snapshot: dict, gates: list[dict]) -> str:
    # No collection timestamps: identical observations must stay identical.
    checks = [{key: check.get(key) for key in ('id', 'kind', 'name', 'url', 'status', 'conclusion', 'run_attempt')}
              for check in _latest_checks(snapshot.get('checks') or []) if _outcome(check) == 'failed']
    value = {'head': snapshot.get('head_sha'),
             'comments': sorted([{'id': c.get('id'), 'updated_at': c.get('updated_at'),
                                   'resolved': c.get('is_resolved')} for c in snapshot.get('comments') or []
                                  if not is_service_review(c)], key=lambda c: str(c['id'])),
             'gates': sorted([{'gate': gate.get('gate'), 'status': gate.get('status'),
                               'details': sorted(gate.get('details') or [], key=str)} for gate in gates
                              if gate.get('gate') not in {'MERGE_CONFLICT', 'BRANCH_UP_TO_DATE', 'ISSUE_CLOSURE', 'MERGE_READY'} or gate.get('status') == 'FAIL'], key=lambda g: str(g['gate'])),
             'failed_checks': sorted(checks, key=lambda c: (str(c['kind']), str(c['id'])))}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def eligible(gates: list[dict]) -> bool:
    expected = {'PR_NOT_DRAFT', 'CI_FAILED', 'CI_PENDING', 'REVIEW_HANDLED', 'REVIEW_CONCLUDED'}
    return (expected.issubset({g.get('gate') for g in gates})
            and {g.get('gate') for g in gates}.issubset(expected | {'MERGE_CONFLICT', 'BRANCH_UP_TO_DATE', 'ISSUE_CLOSURE', 'MERGE_READY'})
            and all(g.get('status') in ({'PASS','WAIT'} if g.get('gate') == 'MERGE_READY'
                    else {'FAIL'} if g.get('gate') == 'CI_FAILED' else {'PASS'}) for g in gates))


def current_hold(store, sid: str, snapshot: dict, gates: list[dict]) -> dict | None:
    repo, num = snapshot.get('repository'), snapshot.get('number')
    if not repo or not num or not hasattr(store, 'external_blocker'): return None
    held = store.external_blocker(sid, repo, num)
    if not held: return None
    if not snapshot.get('head_sha') or not gates or any(g.get('status') == 'UNKNOWN' for g in gates):
        return None  # Unavailable evidence cannot establish a change or success.
    fingerprint = gate_fingerprint(snapshot, gates)
    if held['head_sha'] != snapshot['head_sha'] or held['fingerprint'] != fingerprint:
        store.release_external_blocker(sid, repo, num, reason='new_authoritative_evidence',
                                      head_sha=snapshot['head_sha'], fingerprint=fingerprint)
        return None
    return held if eligible(gates) else None


async def _job_proof(repository: str, run: str, job: str) -> dict:
    from .transport import GitHubClient
    import httpx
    client = GitHubClient()
    metadata = await client.get(f'/repos/{repository}/actions/jobs/{job}')
    workflow = await client.get(f'/repos/{repository}/actions/runs/{run}')
    log = await client.logs(f'/repos/{repository}/actions/jobs/{job}/logs')
    return {'job': metadata, 'workflow': workflow, 'log': log}


async def hold_verified_failure(store, sid: str, snapshot: dict, gates: list[dict], *,
                                evidence_url: str | None = None, reason: str | None = None,
                                proof_reader: Callable | None = None) -> dict | None:
    held = current_hold(store, sid, snapshot, gates)
    if held: return held
    if not eligible(gates): return None
    repo, num, head = snapshot.get('repository'), snapshot.get('number'), snapshot.get('head_sha')
    if not repo or not num or not head: return None
    failures = [c for c in _latest_checks(snapshot.get('checks') or []) if _outcome(c) == 'failed']
    jobs = [c for c in failures if c.get('kind') == 'job']
    if not jobs or any('copilot' not in str(c.get('name', '')).lower() for c in jobs): return None
    proven_jobs, proven_runs, proofs = set(), set(), []
    for check in jobs:
        match = JOB_URL.fullmatch(str(check.get('url') or ''))
        if not match or match.group(1).lower() != repo.lower(): return None
        _, run, job = match.groups()
        data = await (proof_reader or _job_proof)(repo, run, job)
        metadata, workflow, log = data.get('job') or {}, data.get('workflow') or {}, str(data.get('log') or '')
        if (str(metadata.get('id')) != job or str(metadata.get('run_id')) != run
                or metadata.get('head_sha') != head or str(workflow.get('id')) != run
                or workflow.get('head_sha') != head or metadata.get('conclusion') != 'failure'
                or 'copilot' not in str(metadata.get('name', '')).lower()): return None
        # A test printing "quota" is not an external account failure. Require
        # Copilot's actual structured rejection plus its explicit quota message.
        if not (re.search(r'errorType\s*[:=]\s*[\"\']quota[\"\']', log, re.I)
                and re.search(r'statusCode\s*[:=]\s*402\b', log)
                and re.search(r'exceeded your monthly quota', log, re.I)): return None
        proven_jobs.add(job); proven_runs.add(run)
        proofs.append({'job_id': job, 'run_id': run, 'log_sha256': hashlib.sha256(log.encode()).hexdigest()})
    # Workflow/check duplicates must refer to the proved job or workflow;
    # unrelated failed build/tests can never be covered by this handoff.
    for check in failures:
        identity = str(check.get('id'))
        if check.get('kind') == 'workflow' and identity in proven_runs: continue
        if check.get('kind') == 'job' and identity in proven_jobs: continue
        if check.get('kind') == 'check' and 'copilot' in str(check.get('name', '')).lower():
            match = JOB_URL.fullmatch(str(check.get('url') or ''))
            if match and match.group(1).lower() == repo.lower() and match.group(2) in proven_runs and match.group(3) in proven_jobs: continue
        return None
    urls = {str(c['url']) for c in jobs}
    if evidence_url is not None and evidence_url not in urls: raise ValueError('Evidence URL is not a current failed job')
    explanation = 'Copilot rejected the current review job because its account monthly quota is exhausted (HTTP 402).'
    if reason: explanation += ' Reported context: ' + reason
    held = store.record_external_blocker(sid, repo, num, head, gate_fingerprint(snapshot, gates),
                                        explanation, evidence_url or sorted(urls)[0])
    store.audit('external_failure_verified', {'session_id': sid, 'repository': repo, 'number': num,
                'head_sha': head, 'provider_cause': 'copilot_monthly_quota', 'proofs': proofs, 'obligation_complete': False})
    return held


def blocked_report(text: str) -> bool:
    return (isinstance(text, str) and bool(re.search(r'\b(?:blocked|pending|cannot|quota|account recovery|awaiting)\b', text, re.I))
            and not re.search(r'\b(?:all (?:CI|checks|gates) (?:pass|green)|PR\s*#?\d*\s*(?:is )?(?:complete|done|ready)|task (?:is )?complete)\b', text, re.I))


def publication_ready(store, sid: str, repository: str, number: int) -> bool:
    if not hasattr(store, 'validation_plans'): return True
    from .workflow import task_progress
    return all(g.get('evidence', {}).get('phase') in {'COMPLETE', 'WAIT_REMOTE'}
               for g in task_progress(store, sid) if g['repository'] == repository and g['number'] == number)


def held_followup(store, sid: str, job: dict) -> bool:
    if job.get('issue_number'): return False
    cached = store.snapshot(job['repository'], job['number']) or {}
    snapshot, gates = cached.get('snapshot') or {}, cached.get('gates') or []
    if not current_hold(store, sid, snapshot, gates): return False
    from .supervisor import _verified, recorded_job_scope
    # Check the actual due reread/scope/review facts without claiming the failed
    # CI succeeded. The complete job remains unresolved in storage.
    review_gates = [g for g in gates if g.get('gate') in {'REVIEW_HANDLED', 'REVIEW_CONCLUDED'}]
    return _verified(job, snapshot, review_gates, recorded_job_scope(store, job))[0]
