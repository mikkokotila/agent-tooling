"""Resume the owned, reviewed Limen publication through its existing CI credential."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import re
import subprocess

from .limen_release import REPOSITORY, metadata

PR = 1212
HEAD = '48af6a6028f42fd45aa6c9a295e683d460a45dba'
RELEASE = '616d270e6f69ca723456104c66207c55ee808021'
PACKAGE_RUN = 39957123635
FAILED_RUN = 39989784603
WORKFLOW = '.github/workflows/docs_publish.yml'
ENDPOINT = f'repos/{REPOSITORY}/actions/workflows/docs_publish.yml/dispatches'
TRUSTED = {WORKFLOW: '1ea91d43d635b7dac7edeb3a70f06fb3dbf11a42',
           'docs-site/publisher/prepare-assets.mjs': '8f1d151f22e2490a1ab58d17059da4381c67f768'}


def dispatch(args, store, sid, repository, number, issue, endpoint, method, argv, run):
    if endpoint != ENDPOINT:
        return None
    payload_path = argv[-1] if len(argv) == 6 else None
    if (repository != REPOSITORY or number != PR or issue or method != 'POST'
            or not payload_path or payload_path == '-'
            or argv != ['api', ENDPOINT, '--method', 'POST', '--input', payload_path]):
        raise ValueError('Documentation recovery requires the exact owned saved-file request')
    request = Path(payload_path)
    if not request.is_absolute():
        request = Path(args.cwd) / request
    payload = {'ref': 'main', 'inputs': {'package_publish_run': str(PACKAGE_RUN)}}
    if json.loads(request.read_text()) != payload:
        raise ValueError('Documentation recovery requires the exact canonical package run')
    from .github import collect_pr
    from .policy import evaluate_pr
    from .publication import source, exact_source
    retained = store.closed_review_owner(sid, REPOSITORY, number, args.cwd)
    local = source(args.cwd, stable=True)
    if (retained['identity']['watch']['state'] != 'merged'
            or retained['identity']['watch']['client'] != args.client
            or not exact_source(retained, local)):
        raise ValueError('Documentation recovery requires its clean retained owner checkout')
    metadata(Path(args.cwd))

    def read(path):
        result = run(['gh', 'api', path], runner=subprocess.run, cwd=args.cwd,
                     capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('Documentation recovery evidence is unavailable: ' + path)
        return json.loads(result.stdout)

    def authority():
        snap = asyncio.run(collect_pr(REPOSITORY, number))
        if (snap.get('merged') is not True or snap.get('state') != 'closed'
                or snap.get('head_sha') != HEAD or snap.get('head_sha_after') != HEAD
                or local['head'] not in {HEAD, snap.get('merge_commit_sha')}
                or snap.get('base_ref') != 'main' or snap.get('head_repository') != REPOSITORY
                or snap.get('base_repository') != REPOSITORY or not snap.get('expected_checks')
                or snap.get('review_decision') != 'APPROVED'
                or not any(r.get('state') == 'APPROVED' and r.get('commit_sha') == HEAD
                           and r.get('author_login') not in (None, snap.get('author_login'))
                           for r in snap.get('submitted_reviews', []))
                or any(g['status'] != 'PASS' for g in evaluate_pr(snap, store.dispositions(REPOSITORY, number))
                       if g['gate'] != 'MERGE_READY')):
            raise ValueError('Documentation recovery requires the independently approved qualified merged repair')
        main = read(f'repos/{REPOSITORY}/git/ref/heads/main')['object']
        if main.get('type') != 'commit' or not re.fullmatch(r'[a-f0-9]{40}', str(main.get('sha'))):
            raise ValueError('Live main identity is unavailable')
        for name, digest in TRUSTED.items():
            blob = read(f'repos/{REPOSITORY}/contents/{name}?ref={main["sha"]}')
            if blob.get('type') != 'file' or blob.get('sha') != digest:
                raise ValueError('The trusted publication adapter or workflow changed')
        target = read(f'repos/{REPOSITORY}/git/ref/tags/v5.46.2')['object']
        if target.get('type') == 'tag':
            annotation = read(f'repos/{REPOSITORY}/git/tags/{target["sha"]}')
            if annotation.get('sha') != target['sha'] or annotation.get('tag') != 'v5.46.2':
                raise ValueError('Annotated release identity changed')
            target = annotation.get('object', {})
        if target != {'sha': RELEASE, 'type': 'commit', 'url': f'https://api.github.com/repos/{REPOSITORY}/git/commits/{RELEASE}'}:
            raise ValueError('Immutable released source changed')
        for run_id, workflow, outcome in ((PACKAGE_RUN, '.github/workflows/pr_publish_pypi.yml', 'success'), (FAILED_RUN, WORKFLOW, 'failure')):
            actual = read(f'repos/{REPOSITORY}/actions/runs/{run_id}')
            if (actual.get('id') != run_id or actual.get('path') != workflow or actual.get('head_sha') != RELEASE
                    or actual.get('head_repository', {}).get('full_name') != REPOSITORY
                    or actual.get('event') != 'workflow_run' or actual.get('status') != 'completed'
                    or actual.get('conclusion') != outcome):
                raise ValueError('Canonical package success or diagnosed publication failure changed')
        if (store.closed_review_owner(sid, REPOSITORY, number, args.cwd) != retained
                or source(args.cwd, stable=True) != local):
            raise ValueError('Documentation recovery owner or checkout changed')
        return main['sha']

    main = authority()
    if authority() != main:
        raise ValueError('Live main changed before documentation recovery')
    receipt = dict(session_id=sid, repository=REPOSITORY, number=number,
                   candidate_head=HEAD, main=main, package_run=PACKAGE_RUN, failed_run=FAILED_RUN)
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM audit WHERE event='github_limen_docs_recovery_attempt'").fetchone():
            raise ValueError('This publication already had its bounded recovery; inspect the actual outcome')
        store._audit(db, 'github_limen_docs_recovery_attempt', receipt)
    try:
        result = run(['gh', 'api', ENDPOINT, '--method', 'POST', '--input', '-',
                      '--header', 'X-GitHub-Api-Version: 2026-03-10'], input=json.dumps(payload),
                     runner=subprocess.run, cwd=args.cwd, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError('Dispatch did not confirm a run')
        response = json.loads(result.stdout)
        run_id = response.get('workflow_run_id')
        if type(run_id) is not int or run_id <= 0:
            raise RuntimeError('Dispatch returned no run identity')
        url = f'https://github.com/{REPOSITORY}/actions/runs/{run_id}'
        if response.get('html_url') != url or response.get('run_url') != f'https://api.github.com/repos/{REPOSITORY}/actions/runs/{run_id}':
            raise RuntimeError('Dispatch response identities differ')
        actual = read(f'repos/{REPOSITORY}/actions/runs/{run_id}')
        if (actual.get('id') != run_id or actual.get('path') != WORKFLOW or actual.get('head_sha') != main
                or actual.get('head_branch') != 'main' or actual.get('event') != 'workflow_dispatch'
                or actual.get('head_repository', {}).get('full_name') != REPOSITORY or actual.get('html_url') != url):
            raise RuntimeError('Dispatched run does not match the admitted workflow and main')
    except Exception as error:
        store.audit('github_limen_docs_recovery_incomplete', receipt)
        raise RuntimeError('Documentation dispatch outcome is unverified; inspect runs before further action') from error
    receipt.update(run_id=run_id, url=url, accepted=True, completion_verified=False)
    store.audit('github_limen_docs_recovery_verified', receipt)
    return receipt
