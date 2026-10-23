"""Dispatch the existing release workflow for the owned merged 5.46.2 candidate."""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib

REPOSITORY = 'Vaquum/Limen'
VERSION = '5.46.2'
WORKFLOW = '.github/workflows/pr_post_release.yml'
ENDPOINT = 'repos/Vaquum/Limen/actions/workflows/pr_post_release.yml/dispatches'
TRUSTED = {WORKFLOW: 'e48ac931e71ffd8c57f2f488caf0f12b46e1783d',
           'scripts/create_release.py': '3a6f1fe04aad960ab914140eb18b160281652db6'}
API_VERSION = '2026-03-10'


def metadata(root):
    if tomllib.loads((root / 'pyproject.toml').read_text())['project']['version'] != VERSION:
        raise ValueError('Only the reviewed Limen 5.46.2 release is admitted')
    if re.findall(r'^version: "([^"]+)"$', (root / 'CITATION.cff').read_text(), re.M) != [VERSION]:
        raise ValueError('Release citation version differs')
    if not re.search(r'^## \[5\.46\.2\] - \d{4}-\d{2}-\d{2}$', (root / 'CHANGELOG.md').read_text(), re.M):
        raise ValueError('Release changelog entry is absent')
    templates = list((root / 'limen/yaml/templates').glob('*.yaml'))
    if not templates:
        raise ValueError('Release templates are absent')
    for path in [*templates, root / 'docs/examples/factorized_shuffle.yaml', root / 'docs/examples/logreg_return.yaml']:
        if re.findall(r'^\s*limen_version: "([^"]+)"$', path.read_text(), re.M) != [VERSION]:
            raise ValueError('Release template/public example version differs')


def dispatch(args, store, sid, repository, number, issue, endpoint, method, argv, run):
    if repository != REPOSITORY or issue or endpoint != ENDPOINT:
        return None
    if (method != 'POST' or len(argv) != 6
            or argv != ['api', ENDPOINT, '--method', 'POST', '--input', argv[-1]] or argv[-1] == '-'):
        raise ValueError('Limen release dispatch requires the exact saved-file API request')
    request = Path(argv[-1])
    if not request.is_absolute(): request = Path(args.cwd) / request
    payload = json.loads(request.read_text())
    if payload != {'ref': 'main'}:
        raise ValueError('Limen release dispatch permits only ref=main and no additional inputs')
    body = json.dumps(payload)
    from .github import collect_pr
    from .policy import evaluate_pr
    from .publication import source, exact_source

    def owner():
        retained = store.closed_review_owner(sid, REPOSITORY, number, args.cwd)
        watch = retained['identity']['watch']
        if watch['state'] != 'merged' or watch['client'] != args.client:
            raise ValueError('Limen release requires the retained merged-PR owner and client')
        return retained

    retained = owner()
    local = source(args.cwd, stable=True)
    if not exact_source(retained, local):
        raise ValueError('Limen release requires the clean original owned checkout')
    metadata(Path(args.cwd))

    def verified_candidate():
        value = asyncio.run(collect_pr(REPOSITORY, number))
        head = retained['identity']['retirement']['head_sha']
        if (value.get('state') != 'closed' or value.get('merged') is not True
                or value.get('head_sha') != head or value.get('head_sha_after') != head
                or value.get('merge_commit_sha') != local['head']
                or value.get('head_repository') != REPOSITORY or value.get('base_repository') != REPOSITORY
                or value.get('base_ref') != 'main' or type(value.get('head_repository_id')) is not int
                or value['head_repository_id'] <= 0 or value.get('base_repository_id') != value['head_repository_id']
                or value.get('review_decision') != 'APPROVED' or not value.get('expected_checks')):
            raise ValueError('Limen release requires the exact approved merged candidate at the checked-out merge commit')
        if not any(review.get('state') == 'APPROVED' and review.get('commit_sha') == head
                   and review.get('author_login') not in (None, value.get('author_login'))
                   for review in value.get('submitted_reviews', [])):
            raise ValueError('Limen release requires independent approval on the actual candidate head')
        # A merged PR has no merge button. Its source checks, review receipts and
        # closure still apply; independent approval is verified above.
        if any(gate['status'] != 'PASS' for gate in evaluate_pr(value, store.dispositions(REPOSITORY, number))
               if gate['gate'] != 'MERGE_READY'):
            raise ValueError('Limen release requires every current candidate gate and review disposition')
        return head

    head = verified_candidate()

    def read(path):
        result = run(['gh', 'api', path], runner=subprocess.run, cwd=args.cwd,
                     capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('Limen release prerequisite is unavailable: ' + path)
        return json.loads(result.stdout)

    def current_main():
        if read('repos/Vaquum/Limen/git/ref/heads/main').get('object', {}).get('sha') != local['head']:
            raise ValueError('Live main differs from the reviewed release merge commit')

    def unstarted():
        current_main()
        if read('repos/Vaquum/Limen/releases/latest').get('tag_name') != 'v5.46.1':
            raise ValueError('The predecessor release changed; reconcile before dispatch')
        tags = read('repos/Vaquum/Limen/git/matching-refs/tags/v5.46.2')
        if not isinstance(tags, list) or any(tag.get('ref') == 'refs/tags/v5.46.2' for tag in tags):
            raise ValueError('Release tag already exists or its absence is unverified')
        runs = read('repos/Vaquum/Limen/actions/workflows/pr_post_release.yml/runs?event=workflow_dispatch&head_sha=' + local['head'])
        if not isinstance(runs.get('workflow_runs'), list) or any(item.get('head_sha') == local['head'] for item in runs['workflow_runs']):
            raise ValueError('This release already has a dispatch; inspect its outcome before any retry')

    unstarted()
    for path, digest in TRUSTED.items():
        blob = read(f'repos/Vaquum/Limen/contents/{path}?ref={local["head"]}')
        if blob.get('path') != path or blob.get('sha') != digest or blob.get('type') != 'file':
            raise ValueError('Release workflow or release script differs from the reviewed trusted blob')
    if verified_candidate() != head or owner() != retained or source(args.cwd, stable=True) != local:
        raise ValueError('Release source or ownership changed before dispatch')
    unstarted()
    receipt = {'session_id': sid, 'repository': REPOSITORY, 'number': number, 'version': VERSION,
               'candidate_head': head, 'merge_commit': local['head'], 'workflow': WORKFLOW,
               'trusted_blobs': TRUSTED, 'payload_sha256': hashlib.sha256(body.encode()).hexdigest()}
    store.audit('github_limen_release_dispatch_attempt', receipt)
    try:
        result = run(['gh', 'api', ENDPOINT, '--method', 'POST', '--input', '-',
                      '--header', 'X-GitHub-Api-Version: ' + API_VERSION], runner=subprocess.run,
                     cwd=args.cwd, input=body, text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError('GitHub did not confirm release dispatch')
        response = json.loads(result.stdout)
        run_id = response.get('workflow_run_id')
        if type(run_id) is not int or run_id <= 0:
            raise RuntimeError('Release dispatch returned no workflow run identity')
        api_url = f'https://api.github.com/repos/Vaquum/Limen/actions/runs/{run_id}'
        url = f'https://github.com/Vaquum/Limen/actions/runs/{run_id}'
        if response.get('run_url') != api_url or response.get('html_url') != url:
            raise RuntimeError('Release dispatch returned inconsistent run URLs')
        receipt.update(run_id=run_id, run_url=url)
        actual = read(f'repos/Vaquum/Limen/actions/runs/{run_id}')
        if (actual.get('id') != run_id or actual.get('head_sha') != local['head']
                or actual.get('head_branch') != 'main' or actual.get('path') != WORKFLOW
                or actual.get('event') != 'workflow_dispatch' or actual.get('html_url') != url
                or actual.get('head_repository', {}).get('full_name') != REPOSITORY
                or owner() != retained or source(args.cwd, stable=True) != local):
            raise RuntimeError('Actual release run differs from the frozen candidate or owner')
    except Exception:
        store.audit('github_limen_release_dispatch_incomplete', receipt)
        raise RuntimeError('Release dispatch outcome is unverified; inspect runs/tags/releases before any retry') from None
    store.audit('github_limen_release_dispatch_verified', receipt)
    return receipt
