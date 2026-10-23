"""One merged Origo task may retire its one obsolete Actions secret, once."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from datetime import datetime, timezone

import httpx

REPOSITORY = 'Vaquum/Origo'
PR = 'https://github.com/Vaquum/Origo/pull/608'
ENDPOINT = 'repos/Vaquum/Origo/actions/secrets/CRYPTOHFTDATA_API_KEY'
WORK_ITEM = 'origo-608-retire-cryptohftdata-actions-secret'
GRANT = 'origo_cryptohftdata_secret_retirement'
EVENT = 'github_origo_cryptohftdata_secret_delete_'
WORKFLOW = '.github/workflows/deploy_on_merge.yml'
FIXED = {'docker-compose.yml', 'docker-compose.deploy.yml', WORKFLOW, 'Dockerfile'}
OPTIONAL = {'dagster.yaml', 'workspace.yaml'}
REMOVED = {'origo/sources/adapters/book_vendor.py', 'origo/sources/adapters/book_columnar.py',
           'origo/sources/adapters/book_replay.cpp', 'tools/benchmark_book_replay.py'}


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40}', value):
        raise ValueError('Required immutable Git identity is unavailable')
    return value


def _object(value):
    if not isinstance(value, dict):
        raise ValueError('Required GitHub evidence is not an object')
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _timestamp(value):
    if not isinstance(value, str) or datetime.fromisoformat(value.replace('Z', '+00:00')).utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError('Required UTC evidence timestamp is unavailable')
    return value


def _git(cwd, *arguments, data=None):
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(cwd), *arguments],
                            input=data, capture_output=True, timeout=15,
                            env={**os.environ, 'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0',
                                 'GIT_OPTIONAL_LOCKS': '0'})
    if result.returncode:
        raise ValueError('Exact local Git source proof is unavailable; no fetch was attempted')
    return result.stdout


def source_proof(cwd, revision):
    """Inspect bounded immutable blobs, without checkout reads or replacement objects."""
    resolved = _git(cwd, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    if resolved != revision:
        raise ValueError('Local source revision differs from the merged deployment')
    entries = {}
    for raw in _git(cwd, 'ls-tree', '-r', '-l', '-z', revision).split(b'\0'):
        if raw:
            metadata, filename = raw.split(b'\t', 1)
            mode, kind, identity, size = metadata.decode().split()
            entries[filename.decode()] = (mode, kind, identity, size)
    if not FIXED <= entries.keys() or REMOVED.intersection(entries):
        raise ValueError('Required deployment source or provider-removal proof is absent')
    selected = {path: item for path, item in entries.items()
                if path in FIXED | OPTIONAL or path.startswith('origo/') and path.endswith('.py')}
    if (not any(path.startswith('origo/') for path in selected) or len(selected) > 512
            or any(mode not in {'100644', '100755'} or kind != 'blob'
                   or not size.isdigit() or int(size) > 2 * 1024**2
                   for mode, kind, _identity, size in selected.values())
            or sum(int(item[3]) for item in selected.values()) > 16 * 1024**2):
        raise ValueError('Deployment source exceeds its bounded regular-blob proof')
    ordered = sorted(selected.items())
    payload = _git(cwd, 'cat-file', '--batch',
                   data=''.join(item[2] + '\n' for _path, item in ordered).encode())
    stream, inventory = io.BytesIO(payload), {}
    for path, (_mode, _kind, identity, length) in ordered:
        if stream.readline() != f'{identity} blob {length}\n'.encode():
            raise ValueError('Immutable source blob framing differs')
        value = stream.read(int(length))
        if len(value) != int(length) or stream.read(1) != b'\n':
            raise ValueError('Immutable source blob is incomplete')
        if b'cryptohftdata_api_key' in value.lower():
            raise ValueError('Deployment source still references the retired credential')
        inventory[path] = {'blob': identity, 'sha256': hashlib.sha256(value).hexdigest()}
    if stream.read(1):
        raise ValueError('Unexpected extra immutable source data')
    return {'revision': revision, 'files': len(inventory), 'inventory_sha256': _digest(inventory),
            'absent_optional': sorted(OPTIONAL - selected.keys())}


def retire(args, store, sid, argv, github_run):
    if (args.target != PR or argv != ['api', ENDPOINT, '--method', 'DELETE']):
        raise ValueError('Only PR608 and the exact bodyless CryptoHFTData secret DELETE are permitted')

    def owner():
        scope = store.get_scope(sid, work_item=WORK_ITEM)
        grant = (scope or {}).get(GRANT)
        session = store.session(sid)
        if (not isinstance(grant, dict)
                or set(grant) != {'repository', 'authorizing_pr', 'approval_evidence'}
                or grant['repository'] != REPOSITORY or grant['authorizing_pr'] != PR
                or not isinstance(grant['approval_evidence'], str) or not grant['approval_evidence'].strip()
                or not session or session['root_session'] != sid or session['client'] != args.client):
            raise ValueError('The exact immutable human-approved retirement work-item is required')
        retained = store.closed_review_owner(sid, REPOSITORY, 608, args.cwd)
        if (retained['identity']['watch']['state'] != 'merged'
                or retained['identity']['registration']['client'] != args.client):
            raise ValueError('Only the retained merged source-PR owner may retire this secret')
        origin = _git(args.cwd, 'remote', 'get-url', 'origin').decode().strip().removesuffix('.git').lower()
        if origin not in {'https://github.com/vaquum/origo', 'git@github.com:vaquum/origo',
                          'ssh://git@github.com/vaquum/origo'}:
            raise ValueError('The recorded Origo checkout origin differs')
        return {'retained': retained['identity'], 'scope_sha256': _digest(scope)}

    def request(path, method='GET', upstream=None):
        command = ['gh', 'api', path, '--include']
        if method != 'GET':
            command += ['--method', method]
        options = {'upstream': upstream} if upstream is not None else {}
        result = github_run(command, runner=subprocess.run, cwd=args.cwd, text=True,
                            capture_output=True, timeout=90 if method == 'DELETE' else 30, **options)
        raw = result.stdout
        if not isinstance(raw, str) or len(raw) > 4 * 1024**2:
            raise ValueError('GitHub response is absent or exceeds its bound')
        headers, separator, body = raw.replace('\r\n', '\n').partition('\n\n')
        status = re.fullmatch(r'HTTP/\S+ ([0-9]{3})(?: .*)?', headers.split('\n')[0])
        if not separator or status is None:
            raise ValueError('GitHub HTTP status is unverified')
        code = int(status[1])
        if (code < 400) != (result.returncode == 0):
            raise ValueError('GitHub HTTP status and child outcome disagree')
        return code, _object(json.loads(body)) if body.strip() else {}

    def read(path):
        code, value = request(path)
        if code != 200:
            raise ValueError('Required GitHub evidence is unavailable: ' + path)
        return value

    frozen_owner = owner()

    def deployment():
        pr = read('repos/Vaquum/Origo/pulls/608')
        head, base = _object(pr.get('head')), _object(pr.get('base'))
        head_repo, base_repo = _object(head.get('repo')), _object(base.get('repo'))
        if (pr.get('number') != 608 or pr.get('url') != 'https://api.github.com/repos/Vaquum/Origo/pulls/608'
                or pr.get('state') != 'closed' or pr.get('merged') is not True or pr.get('draft') is not False
                or not pr.get('merged_at') or base.get('ref') != 'main'
                or head_repo.get('full_name') != REPOSITORY or base_repo.get('full_name') != REPOSITORY
                or type(base_repo.get('id')) is not int or base_repo['id'] < 1
                or head_repo.get('id') != base_repo['id']
                or _sha(head.get('sha')) != frozen_owner['retained']['retirement']['head_sha']):
            raise ValueError('PR608 is not the exact retained merged main-target source change')
        merge = _sha(pr.get('merge_commit_sha'))
        main = _sha(_object(read('repos/Vaquum/Origo/git/ref/heads/main').get('object')).get('sha'))
        from .origo_deploy_retry import (MERGE, ORIGIN_MERGE, RUN, WORKFLOW_ID,
                                         continuation, deployment_inventory, release_proof)
        if merge != ORIGIN_MERGE or main != MERGE:
            raise ValueError('Only the exact reviewed latest retirement descendant may qualify')
        continuation_sha256 = continuation(store, sid)
        release_proof(args.cwd, read)
        deployment_inventory(read)
        workflow = read('repos/Vaquum/Origo/actions/workflows/deploy_on_merge.yml')
        if (type(workflow.get('id')) is not int or workflow['id'] != WORKFLOW_ID or workflow.get('path') != WORKFLOW
                or workflow.get('name') != 'Deploy On Merge' or workflow.get('state') != 'active'):
            raise ValueError('The exact active deployment workflow is unavailable')
        runs = read('repos/Vaquum/Origo/actions/workflows/deploy_on_merge.yml/runs'
                    f'?branch=main&event=push&head_sha={main}&per_page=100')
        candidates = runs.get('workflow_runs')
        if (not isinstance(candidates, list) or not candidates or len(candidates) > 100
                or runs.get('total_count') != len(candidates)
                or any(not isinstance(item, dict) or type(item.get('id')) is not int
                       or item['id'] != RUN or item.get('head_sha') != main for item in candidates)):
            raise ValueError('The exact merge deployment run inventory is incomplete')
        if len(candidates) != 1 or len({item['id'] for item in candidates}) != len(candidates):
            raise ValueError('Deployment run identities are ambiguous')
        run_id = max(item['id'] for item in candidates)
        run = read('repos/Vaquum/Origo/actions/runs/' + str(run_id))
        repository = _object(run.get('repository'))
        if (type(run.get('id')) is not int or run['id'] != run_id or run.get('head_sha') != main or run.get('head_branch') != 'main' or run.get('event') != 'push'
                or run.get('workflow_id') != workflow['id'] or run.get('path') != WORKFLOW
                or repository.get('full_name') != REPOSITORY or repository.get('id') != base_repo['id']
                or run.get('status') != 'completed' or run.get('conclusion') != 'success'
                or type(run.get('run_attempt')) is not int or run['run_attempt'] < 1):
            raise ValueError('The exact merge deployment has not completed successfully')
        jobs = read(f'repos/Vaquum/Origo/actions/runs/{run["id"]}/attempts/{run["run_attempt"]}/jobs?per_page=100')
        values = jobs.get('jobs')
        if not isinstance(values, list) or jobs.get('total_count') != len(values) or len(values) > 100:
            raise ValueError('Deployment jobs are incomplete')
        matches = [job for job in values if isinstance(job, dict) and job.get('name') == 'Build Images And Deploy']
        if len(matches) != 1:
            raise ValueError('The actual deployment job is absent or ambiguous')
        job = matches[0]
        if (type(job.get('id')) is not int or job['id'] < 1 or job.get('run_id') != run['id']
                or job.get('status') != 'completed' or job.get('conclusion') != 'success'
                or not job.get('completed_at') or not isinstance(job.get('steps'), list)):
            raise ValueError('The deployment job did not complete successfully')
        steps = [step for step in job['steps'] if isinstance(step, dict) and step.get('name') == 'Deploy updated images']
        if (len(steps) != 1 or type(steps[0].get('number')) is not int or steps[0]['number'] < 1
                or steps[0].get('status') != 'completed' or steps[0].get('conclusion') != 'success'
                or not steps[0].get('completed_at')):
            raise ValueError('The actual image deployment step did not complete successfully')
        return {'head': head['sha'], 'base': _sha(base.get('sha')), 'merge': merge, 'main': main,
                'repository_id': base_repo['id'], 'merged_at': _timestamp(pr['merged_at']),
                'workflow_id': workflow['id'], 'run_id': run['id'], 'run_attempt': run['run_attempt'],
                'continuation_scope_sha256': continuation_sha256,
                'job_id': job['id'], 'job_completed_at': _timestamp(job['completed_at']),
                'step_number': steps[0]['number'], 'step_completed_at': _timestamp(steps[0]['completed_at'])}

    def metadata():
        code, value = request(ENDPOINT)
        if code == 200:
            if (value.get('name') != 'CRYPTOHFTDATA_API_KEY'
                    or not all(isinstance(value.get(key), str) and value[key] for key in ('created_at', 'updated_at'))):
                raise ValueError('Exact secret metadata is invalid')
            return code, {'name': value['name'], 'created_at': _timestamp(value['created_at']),
                          'updated_at': _timestamp(value['updated_at'])}
        return code, None

    proof = deployment()
    sources = [source_proof(args.cwd, revision) for revision in dict.fromkeys((proof['merge'], proof['main']))]
    before_code, before = metadata()
    if before_code != 200:
        raise ValueError('Secret metadata must exist before retirement; a preflight404 is not absence proof')
    if deployment() != proof or owner() != frozen_owner or metadata() != (before_code, before):
        raise ValueError('Retirement identity or metadata changed before the write')
    receipt = {'session_id': sid, 'client': args.client, 'cwd': str(Path(args.cwd).resolve()),
               'repository': REPOSITORY, 'number': 608, 'work_item': WORK_ITEM,
               'endpoint': ENDPOINT, 'method': 'DELETE', 'owner_sha256': _digest(frozen_owner),
               'scope_sha256': frozen_owner['scope_sha256'], 'deployment': proof,
               'sources': sources, 'before_secret': before, 'stage': 'reserved'}
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('''SELECT id FROM audit WHERE event=?
            AND json_extract(fields,'$.repository')=? AND json_extract(fields,'$.number')=608
            AND json_extract(fields,'$.work_item')=? LIMIT 1''', (EVENT + 'attempt', REPOSITORY, WORK_ITEM)).fetchone()
        scope = store._scope_row(db, sid, WORK_ITEM)
        if previous:
            raise ValueError('This one-shot retirement was already attempted; investigate read-only, never resend')
        if (store._closed_review_owner(db, sid, REPOSITORY, 608) != frozen_owner['retained']
                or not scope or _digest(json.loads(scope['scope'])) != frozen_owner['scope_sha256']):
            raise ValueError('Retirement owner or grant changed before reservation')
        from .origo_deploy_retry import WORK_ITEM as CONTINUATION_WORK_ITEM
        continuation_scope = store._scope_row(db, sid, CONTINUATION_WORK_ITEM)
        if (not continuation_scope
                or _digest(json.loads(continuation_scope['scope'])) != proof['continuation_scope_sha256']):
            raise ValueError('Deployment continuation grant changed before secret reservation')
        store._audit(db, EVENT + 'attempt', receipt)

    lock, writes = threading.Lock(), 0

    def one_delete(method, url, **kwargs):
        nonlocal writes
        with lock:
            if (writes or method != 'DELETE' or url != 'https://api.github.com/' + ENDPOINT
                    or kwargs.get('content') != b'' or kwargs.get('follow_redirects') is not False):
                raise ValueError('Only one exact bodyless DELETE HTTP request is admitted')
            writes += 1
        return httpx.request(method, url, **kwargs)

    try:
        receipt['stage'] = 'write'
        code, _body = request(ENDPOINT, 'DELETE', upstream=one_delete)
        receipt.update(delete_http_status=code, write_requests=writes)
        if code != 204 or writes != 1:
            raise ValueError('GitHub did not confirm the one exact deletion')
        receipt['stage'] = 'readback'
        after_code, _after = metadata()
        receipt['after_secret_http_status'] = after_code
        if after_code != 404 or deployment() != proof or owner() != frozen_owner:
            raise ValueError('Deletion readback or frozen identity is unverified')
    except BaseException as error:
        store.audit(EVENT + 'incomplete', {**receipt, 'write_requests': writes,
                                          'error_type': type(error).__name__})
        raise RuntimeError('Secret retirement is incomplete; investigate metadata read-only; no automatic retry or override') from None
    receipt['stage'] = 'verified'
    store.audit(EVENT + 'verified', receipt)
    return receipt
