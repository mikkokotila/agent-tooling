"""One evidenced retry of the two blocked immutable Limen 5.46.2 runs."""
from __future__ import annotations
import asyncio
import json
import subprocess

from .limen_release import REPOSITORY, metadata

MERGE = '616d270e6f69ca723456104c66207c55ee808021'
QUALIFIED_HEAD = '2dc05c462be182052d5bf179042d2ae98644675a'
QUALIFIED_RUN = 39958832443
FIXED_TEST = 'f93636b2581308921099e0e1d8c7c14eb87c7475'
RUNS = {39955803473: ('.github/workflows/pr_checks_docs_site.yml', 'push'),
        39957123635: ('.github/workflows/pr_publish_pypi.yml', 'workflow_run')}


def retry(args, store, sid, repository, number, issue, endpoint, method, argv, run):
    selected = next((key for key in RUNS if endpoint == f'repos/{REPOSITORY}/actions/runs/{key}/rerun'), None)
    if selected is None:
        return None
    if (repository != REPOSITORY or issue or method != 'POST'
            or argv != ['api', endpoint, '--method', 'POST']):
        raise ValueError('Release recovery requires the exact owned run request')
    from pathlib import Path
    from .github import collect_pr
    from .policy import evaluate_pr
    from .publication import source, exact_source

    retained = store.closed_review_owner(sid, REPOSITORY, number, args.cwd)
    local = source(args.cwd, stable=True)
    if (retained['identity']['watch']['state'] != 'merged'
            or retained['identity']['watch']['client'] != args.client
            or not exact_source(retained, local) or local['head'] != MERGE):
        raise ValueError('Release recovery requires the original clean merged-release owner checkout')
    metadata(Path(args.cwd))

    def read(path):
        result = run(['gh', 'api', path], runner=subprocess.run, cwd=args.cwd,
                     capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('Release recovery prerequisite is unavailable: ' + path)
        return json.loads(result.stdout)

    def authority():
        snap = asyncio.run(collect_pr(REPOSITORY, number))
        head = retained['identity']['retirement']['head_sha']
        if (snap.get('state') != 'closed' or snap.get('merged') is not True
                or snap.get('head_sha') != head or snap.get('head_sha_after') != head
                or snap.get('merge_commit_sha') != MERGE or snap.get('base_ref') != 'main'
                or snap.get('head_repository') != REPOSITORY or snap.get('base_repository') != REPOSITORY
                or snap.get('review_decision') != 'APPROVED' or not snap.get('expected_checks')
                or not any(r.get('state') == 'APPROVED' and r.get('commit_sha') == head
                           and r.get('author_login') not in (None, snap.get('author_login'))
                           for r in snap.get('submitted_reviews', []))
                or any(g['status'] != 'PASS' for g in evaluate_pr(snap, store.dispositions(REPOSITORY, number))
                       if g['gate'] != 'MERGE_READY')):
            raise ValueError('Release recovery requires the exact independently approved and qualified merged PR')
        tag = read(f'repos/{REPOSITORY}/git/ref/tags/v5.46.2')
        if tag.get('object', {}).get('sha') != MERGE or tag['object'].get('type') != 'commit':
            raise ValueError('Immutable release tag differs')
        qualified = read(f'repos/{REPOSITORY}/actions/runs/{QUALIFIED_RUN}')
        if (qualified.get('id') != QUALIFIED_RUN or qualified.get('head_sha') != QUALIFIED_HEAD
                or qualified.get('head_repository', {}).get('full_name') != REPOSITORY
                or qualified.get('path') != RUNS[39955803473][0] or qualified.get('event') != 'pull_request'
                or qualified.get('status') != 'completed' or qualified.get('conclusion') != 'success'):
            raise ValueError('The actual browser-test correction must pass the required Docs Site workflow first')
        blob = read(f'repos/{REPOSITORY}/contents/docs-site/tests/book.spec.mjs?ref={QUALIFIED_HEAD}')
        if blob.get('sha') != FIXED_TEST or blob.get('type') != 'file':
            raise ValueError('Qualified browser-test correction differs')
        original = read(f'repos/{REPOSITORY}/actions/runs/{selected}')
        if (original.get('id') != selected or original.get('head_sha') != MERGE
                or original.get('head_repository', {}).get('full_name') != REPOSITORY
                or original.get('path') != RUNS[selected][0] or original.get('event') != RUNS[selected][1]
                or original.get('status') != 'completed' or original.get('conclusion') != 'failure'
                or original.get('run_attempt') != 1):
            raise ValueError('Only the original diagnosed failed release run can be retried once')
        if selected == 39957123635:
            validation = read(f'repos/{REPOSITORY}/actions/runs/39955803473')
            if validation.get('head_sha') != MERGE or validation.get('conclusion') != 'success':
                raise ValueError('The immutable release commit must pass its actual validation before publication resumes')
            jobs = read(f'repos/{REPOSITORY}/actions/runs/{selected}/jobs?filter=latest')['jobs']
            if (not jobs or [j.get('name') for j in jobs if j.get('conclusion') == 'failure'] != ['Resolve Release Tag']
                    or any(j.get('conclusion') not in {'failure', 'skipped'} for j in jobs)
                    or not {'Build Distribution', 'Publish Distribution to PyPI', 'Publish Release Assets'} <= {j.get('name') for j in jobs}):
                raise ValueError('Publication recovery requires the evidenced pre-build failure with no publication side effects')
        if (store.closed_review_owner(sid, REPOSITORY, number, args.cwd) != retained
                or source(args.cwd, stable=True) != local):
            raise ValueError('Release recovery owner or checkout changed')
        return head

    head = authority()
    receipt = dict(session_id=sid, repository=REPOSITORY, number=number, run_id=selected,
                   merge_commit=MERGE, candidate_head=head, qualified_fix_head=QUALIFIED_HEAD,
                   qualified_fix_run=QUALIFIED_RUN)
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM audit WHERE event='github_limen_release_retry_attempt' AND json_extract(fields,'$.run_id')=?", (selected,)).fetchone():
            raise ValueError('This release run already had its bounded recovery; inspect its outcome')
        store._audit(db, 'github_limen_release_retry_attempt', receipt)
    result = run(['gh', *argv], runner=subprocess.run, cwd=args.cwd,
                 capture_output=True, text=True, timeout=30)
    if result.returncode:
        store.audit('github_limen_release_retry_incomplete', receipt)
        raise RuntimeError('Release retry outcome is unverified; inspect the actual run before further action')
    store.audit('github_limen_release_retry_accepted', receipt)
    return dict(receipt, accepted=True, completion_verified=False)
