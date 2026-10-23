"""One retained Origo owner may retry the exact latest native-only preflight failure."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import tomllib

import httpx

from .origo_secret_retirement import _git, source_proof

REPOSITORY = 'Vaquum/Origo'
PR = 'https://github.com/Vaquum/Origo/pull/608'
ORIGIN_MERGE = 'aeeed96c07eddb75e7945d47d37560a816179905'
MERGE = '9629e4e26a555c007e2ee4c91c4885020b17d696'
RUN = 39931006450
JOB = 120338985274
WORKFLOW_ID = 261234032
WORKFLOW = '.github/workflows/deploy_on_merge.yml'
WORKFLOW_BLOB = '76ca4471877c6f8842b98997a42c42a6d843c9d6'
PROJECT_BLOB = '4e220ce63e86a857cbc07785b99e3d9e2315c253'
VERSION = '3.34.57'
WORK_ITEM = 'origo-608-latest-deployment-continuation'
GRANT = 'origo_608_deployment_continuation'
BOUNDARIES = {WORKFLOW: WORKFLOW_BLOB,
              'docker-compose.deploy.yml': '280ec3f4efc5c1d514126b8eaa2ea4eaf4362de2',
              'tools/retire_cryptohftdata.py': 'f6fd41ddca73e3db357940544fd10457c7ef5b2b',
              'tools/book_deploy_preflight.py': 'b899027c1b48f244009ca389859fec6ef7889414',
              'Dockerfile': 'c2985ae345e4705a1326dd57d9954267fc17be8e'}
ENDPOINT = f'repos/{REPOSITORY}/actions/runs/{RUN}/rerun'
EVENT = 'github_origo_608_deploy_retry_'
FAILURE = 'RuntimeError: Retired book metadata remains: binance_spot_book/source_activation_log'
PREFIX = ('Set up job', 'Checkout repository', 'Validate deploy configuration', 'Set image names',
          'Set up Docker Buildx', 'Log in to GitHub Container Registry',
          'Build and push Dagster image', 'Build and push ClickHouse image',
          'Validate deploy compose file', 'Start SSH agent',
          'Configure pinned deployment host key', 'Upload deploy compose file', 'Deploy updated images')


def continuation(store, sid):
    scope = store.get_scope(sid, work_item=WORK_ITEM)
    grant = (scope or {}).get(GRANT)
    fixed = {'repository': REPOSITORY, 'authorizing_pr': PR, 'source_merge': ORIGIN_MERGE,
             'deployment_sha': MERGE, 'run_id': RUN, 'version': VERSION}
    if (not isinstance(grant, dict) or set(grant) != {*fixed, 'approval_evidence'}
            or any(type(grant.get(key)) is not type(value) or grant[key] != value for key, value in fixed.items())
            or not isinstance(grant['approval_evidence'], str) or not grant['approval_evidence'].strip()):
        raise ValueError('The exact immutable latest-deployment continuation grant is required')
    return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def release_proof(cwd, read):
    if read(f'repos/{REPOSITORY}/git/ref/heads/main').get('object', {}).get('sha') != MERGE:
        raise ValueError('Only the exact latest reviewed descendant may deploy')
    comparison = read(f'repos/{REPOSITORY}/compare/{ORIGIN_MERGE}...{MERGE}')
    if comparison.get('status') != 'ahead' or comparison.get('merge_base_commit', {}).get('sha') != ORIGIN_MERGE:
        raise ValueError('The latest release must retain the original retirement merge')
    _git(cwd, 'merge-base', '--is-ancestor', ORIGIN_MERGE, MERGE)
    for path, expected in {**BOUNDARIES, 'pyproject.toml': PROJECT_BLOB}.items():
        remote = read(f'repos/{REPOSITORY}/contents/{path}?ref={MERGE}')
        if (remote.get('path') != path or remote.get('type') != 'file' or remote.get('sha') != expected
                or _git(cwd, 'rev-parse', f'{MERGE}:{path}').decode().strip() != expected
                or path in BOUNDARIES and _git(cwd, 'rev-parse', f'{ORIGIN_MERGE}:{path}').decode().strip() != expected):
            raise ValueError('Reviewed native-only deployment boundaries differ')
    project = tomllib.loads(_git(cwd, 'show', f'{MERGE}:pyproject.toml').decode())
    if (project['project']['version'] != VERSION
            or project['tool']['origo']['release']['native_jobs_only_version'] != VERSION):
        raise ValueError('The exact native-only release marker is required')


def retry(args, store, sid, argv, run):
    if args.target != PR or argv != ['api', ENDPOINT, '--method', 'POST']:
        raise ValueError('Only the exact bodyless latest Origo608 continuation retry is admitted')

    def owner():
        session = store.session(sid)
        retained = store.closed_review_owner(sid, REPOSITORY, 608, args.cwd)
        if (not session or session['root_session'] != sid or session['client'] != args.client
                or retained['identity']['watch']['state'] != 'merged'
                or retained['identity']['registration']['client'] != args.client):
            raise ValueError('Deployment retry requires the exact retained merged owner and client')
        origin = _git(args.cwd, 'remote', 'get-url', 'origin').decode().strip().removesuffix('.git').lower()
        if origin not in {'https://github.com/vaquum/origo', 'git@github.com:vaquum/origo',
                          'ssh://git@github.com/vaquum/origo'}:
            raise ValueError('The recorded deployment checkout origin differs')
        return {**retained, 'continuation_scope_sha256': continuation(store, sid)}

    frozen_owner = owner()

    def request(path, *, upstream=None):
        command = ['gh', 'api', path, '--include']
        if upstream is not None:
            command += ['--method', 'POST']
        result = run(command, runner=subprocess.run, cwd=args.cwd, text=True,
                     capture_output=True, timeout=30, **({'upstream': upstream} if upstream else {}))
        if not isinstance(result.stdout, str) or len(result.stdout) > 4 * 1024**2:
            raise ValueError('Deployment response is absent or exceeds its bound')
        header, separator, body = result.stdout.replace('\r\n', '\n').partition('\n\n')
        status = re.fullmatch(r'HTTP/\S+ ([0-9]{3})(?: .*)?', header.split('\n')[0])
        if not separator or not status or (int(status[1]) < 400) != (result.returncode == 0):
            raise ValueError('Deployment HTTP status is unverified')
        return int(status[1]), json.loads(body) if body.strip() else {}

    def read(path):
        code, value = request(path)
        if code != 200 or not isinstance(value, dict):
            raise ValueError('Required deployment evidence is unavailable')
        return value

    def identity(value):
        return (type(value.get('id')) is int and value['id'] == RUN
                and value.get('head_sha') == MERGE and value.get('head_branch') == 'main'
                and value.get('event') == 'push' and value.get('workflow_id') == WORKFLOW_ID
                and value.get('path') == WORKFLOW
                and (value.get('repository') or {}).get('full_name') == REPOSITORY)

    def inventory(*, after=False):
        first = read(f'repos/{REPOSITORY}/actions/workflows/deploy_on_merge.yml/runs?per_page=100&page=1')
        total, values = first.get('total_count'), first.get('workflow_runs')
        if type(total) is not int or not 0 < total <= 200 or not isinstance(values, list):
            raise ValueError('Complete deployment inventory exceeds its bound or is absent')
        if total > 100:
            second = read(f'repos/{REPOSITORY}/actions/workflows/deploy_on_merge.yml/runs?per_page=100&page=2')
            if second.get('total_count') != total or not isinstance(second.get('workflow_runs'), list):
                raise ValueError('Deployment inventory changed while paged')
            values = values + second['workflow_runs']
        if (len(values) != total or any(not isinstance(item, dict) or type(item.get('id')) is not int
                                       or item['id'] < 1 for item in values)
                or len({item['id'] for item in values}) != total
                or sum(item['id'] == RUN for item in values) != 1
                or any(item['id'] > RUN or (item['id'] != RUN or not after)
                       and item.get('status') != 'completed' for item in values)):
            raise ValueError('A newer, active, missing or ambiguous deployment prevents retry')

    def prerequisites():
        pr = read(f'repos/{REPOSITORY}/pulls/608')
        head, base = pr.get('head') or {}, pr.get('base') or {}
        repository = base.get('repo') or {}
        if (pr.get('number') != 608 or pr.get('state') != 'closed' or pr.get('merged') is not True
                or pr.get('draft') is not False or pr.get('merge_commit_sha') != ORIGIN_MERGE
                or head.get('sha') != frozen_owner['identity']['retirement']['head_sha']
                or base.get('ref') != 'main' or repository.get('full_name') != REPOSITORY
                or type(repository.get('id')) is not int or repository['id'] < 1
                or (head.get('repo') or {}).get('id') != repository['id']
                or (head.get('repo') or {}).get('full_name') != REPOSITORY
                or read(f'repos/{REPOSITORY}/git/ref/heads/main').get('object', {}).get('sha') != MERGE):
            raise ValueError('The exact retained source merge must still be live main')
        workflow = read(f'repos/{REPOSITORY}/actions/workflows/deploy_on_merge.yml')
        if (workflow.get('id') != WORKFLOW_ID or workflow.get('path') != WORKFLOW
                or workflow.get('name') != 'Deploy On Merge' or workflow.get('state') != 'active'):
            raise ValueError('The reviewed deployment workflow is not active')
        release_proof(args.cwd, read)
        value = read(f'repos/{REPOSITORY}/actions/runs/{RUN}')
        if (not identity(value) or value.get('status') != 'completed'
                or value.get('conclusion') != 'failure' or type(value.get('run_attempt')) is not int
                or value['run_attempt'] != 1 or (value.get('repository') or {}).get('id') != repository['id']):
            raise ValueError('Only the original failed push deployment attempt is retryable')
        inventory()

    prerequisites()
    sources = source_proof(args.cwd, MERGE)
    jobs = read(f'repos/{REPOSITORY}/actions/runs/{RUN}/attempts/1/jobs?per_page=100')
    values = jobs.get('jobs')
    if jobs.get('total_count') != 1 or not isinstance(values, list) or len(values) != 1:
        raise ValueError('Original deployment job evidence is incomplete')
    job = values[0]
    steps = job.get('steps') or []
    if (job.get('id') != JOB or job.get('run_id') != RUN or job.get('name') != 'Build Images And Deploy'
            or job.get('status') != 'completed' or job.get('conclusion') != 'failure'
            or [item.get('name') for item in steps[:13]] != list(PREFIX)
            or any(item.get('status') != 'completed' or item.get('conclusion') != 'success' for item in steps[:12])
            or steps[12].get('status') != 'completed' or steps[12].get('conclusion') != 'failure'):
        raise ValueError('The original failure is not the exact preflight deployment boundary')
    from .transport import GitHubClient
    log = asyncio.run(GitHubClient().logs(f'/repos/{REPOSITORY}/actions/jobs/{JOB}/logs'))
    if (not isinstance(log, str) or len(log) > 4 * 1024**2
            or f'2026-10-23T09:21:58.4363392Z {FAILURE}' not in log
            or 'ORIGO_NATIVE_JOBS_ONLY: true' not in log
            or re.search(r'(?m)^\S+Z post-up: deployment verified\r?$', log)):
        raise ValueError('The original native-only retired-history preflight failure is unverified')
    receipt = {'session_id': sid, 'client': args.client, 'repository': REPOSITORY, 'number': 608,
               'cwd': str(Path(args.cwd).resolve()), 'merge': MERGE, 'run_id': RUN,
               'from_attempt': 1, 'to_attempt': 2, 'workflow_blob': WORKFLOW_BLOB,
               'project_blob': PROJECT_BLOB, 'source': sources,
               'continuation_scope_sha256': frozen_owner['continuation_scope_sha256'],
               'original_job_id': JOB, 'failure_log_sha256': hashlib.sha256(log.encode()).hexdigest()}
    prerequisites()
    if owner() != frozen_owner:
        raise ValueError('Retained deployment owner changed before retry')
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM audit WHERE event=? AND json_extract(fields,'$.run_id')=?",
                      (EVENT + 'attempt', RUN)).fetchone():
            raise ValueError('This deployment retry was already attempted; reconcile read-only')
        if store._closed_review_owner(db, sid, REPOSITORY, 608) != frozen_owner['identity']:
            raise ValueError('Retained deployment owner changed before reservation')
        scope = store._scope_row(db, sid, WORK_ITEM)
        if (not scope or hashlib.sha256(json.dumps(json.loads(scope['scope']), sort_keys=True,
                separators=(',', ':')).encode()).hexdigest() != frozen_owner['continuation_scope_sha256']):
            raise ValueError('Deployment continuation grant changed before reservation')
        store._audit(db, EVENT + 'attempt', receipt)
    lock, writes = threading.Lock(), 0

    def one_post(method, url, **kwargs):
        nonlocal writes
        with lock:
            if (writes or method != 'POST' or url != 'https://api.github.com/' + ENDPOINT
                    or kwargs.get('content') != b'' or kwargs.get('follow_redirects') is not False):
                raise ValueError('Only one exact bodyless deployment retry request is admitted')
            writes += 1
        return httpx.request(method, url, **kwargs)

    try:
        code, _body = request(ENDPOINT, upstream=one_post)
        receipt.update(http_status=code, write_requests=writes)
        if code != 201 or writes != 1:
            raise ValueError('GitHub did not confirm the one deployment retry')
        for attempt in range(6):
            actual = read(f'repos/{REPOSITORY}/actions/runs/{RUN}')
            if not identity(actual) or type(actual.get('run_attempt')) is not int or actual['run_attempt'] > 2:
                raise ValueError('Deployment retry readback differs from the fixed run identity')
            if actual['run_attempt'] == 2:
                break
            if attempt < 5:
                time.sleep(1)
        else:
            raise ValueError('Deployment attempt2 was not observed; outcome remains unknown')
        if (owner() != frozen_owner
                or read(f'repos/{REPOSITORY}/git/ref/heads/main').get('object', {}).get('sha') != MERGE):
            raise ValueError('Deployment source or owner changed during retry')
        inventory(after=True)
    except BaseException as error:
        store.audit(EVENT + 'incomplete', {**receipt, 'write_requests': writes,
                                          'error_type': type(error).__name__})
        raise RuntimeError('Deployment retry outcome is unverified; inspect attempt2 read-only, never resend') from None
    receipt.update(status='retry_requested_verified', deployment_success=False)
    store.audit(EVENT + 'verified', receipt)
    return receipt
