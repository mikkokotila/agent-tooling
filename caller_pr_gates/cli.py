from __future__ import annotations
import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from .store import Store
from .ghrelay import run as github_run

REF = re.compile(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(pull|issues)/(\d+)')
DOC = re.compile(r'\b(?:PRD|product requirements document|slice(?:\s+\d+)?)\b', re.I)
DOCS_ENVIRONMENT = 'repos/Vaquum/Limen/environments/documentation'
DOCS_SECRET = DOCS_ENVIRONMENT + '/secrets/CLOUDFLARE_DOCS_API_TOKEN'
DOCS_PROTECTION = 'repos/Vaquum/Limen/branches/main/protection'
DOCS_CONTEXTS = DOCS_PROTECTION + '/required_status_checks/contexts'
DOCS_RETIRED_CONTEXT = 'Workers Builds: limen'


def reference(value):
    match = REF.fullmatch(value.rstrip('/'))
    if not match: raise ValueError('Use an explicit https://github.com/OWNER/REPO/pull/N or /issues/N URL')
    return match.group(1), int(match.group(3)), match.group(2) == 'issues'


def session_id(args):
    value = getattr(args, 'session', None) or os.getenv('CODEX_THREAD_ID') or os.getenv('CLAUDE_SESSION_ID') or os.getenv('CALLER_GATE_SESSION')
    if not value: raise ValueError('--session is required; the hook context supplies the exact session ID')
    return value


def scope_digest(scope):
    from .supervisor import scope_digest as digest
    return digest(scope)


def _posted_at(snapshot):
    value = snapshot.get('created_at')
    if isinstance(value, (int, float)): return value
    if isinstance(value, str): return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    raise ValueError('GitHub did not provide posting time')


def _options(argv):
    result = {}
    for i, value in enumerate(argv):
        if value.startswith('--') and i + 1 < len(argv) and not argv[i+1].startswith('--'): result[value] = argv[i+1]
        elif value.startswith('-') and i + 1 < len(argv) and not argv[i+1].startswith('-'): result[value] = argv[i+1]
    return result


def _api_request(argv):
    """Locate the real endpoint with flags before or after it."""
    endpoints, fields, method, input_file = [], [], None, None
    valued = {'--method', '-X', '--field', '-F', '--raw-field', '-f', '--input',
              '--header', '-H', '--hostname', '--jq', '-q', '--template', '-t', '--cache'}
    index = 1
    while index < len(argv):
        token = argv[index]
        flag, value = token, None
        if token.startswith('--') and '=' in token:
            flag, value = token.split('=', 1)
        elif len(token) > 2 and token[:2] in {'-X', '-F', '-f', '-H', '-q', '-t'}:
            flag, value = token[:2], token[2:].removeprefix('=')
        if flag in valued:
            if value is None:
                index += 1
                if index >= len(argv): raise ValueError('GitHub API option needs a value')
                value = argv[index]
            if flag in {'--method', '-X'}: method = value
            elif flag in {'--field', '-F', '--raw-field', '-f'}: fields.append(value)
            elif flag == '--input': input_file = value
        elif not token.startswith('-'):
            endpoints.append(token.lstrip('/'))
        index += 1
    if len(endpoints) != 1: raise ValueError('GitHub API needs one explicit endpoint')
    return endpoints[0], method, fields, input_file


def _api_reply_allowed(repo, number, issue, endpoint, method, fields, input_file):
    if method.upper() != 'POST': return False
    prefix = f'repos/{repo}'
    if endpoint == f'{prefix}/issues/{number}/comments': return True
    if issue: return False
    if re.fullmatch(re.escape(f'{prefix}/pulls/{number}/comments/') + r'[1-9][0-9]*/replies', endpoint):
        return True
    if endpoint != f'{prefix}/pulls/{number}/comments': return False
    replies = [value.split('=', 1)[1] for value in fields if value.startswith('in_reply_to=')]
    if input_file:
        # gh puts field flags in the query when --input supplies the body.
        if replies or input_file == '-': return False
        try:
            payload = json.loads(Path(input_file).read_text())
        except (OSError, ValueError): return False
        if not isinstance(payload, dict): return False
        if 'in_reply_to' in payload:
            value = payload['in_reply_to']
            if not isinstance(value, int) or isinstance(value, bool): return False
            replies.append(str(value))
    return len(replies) == 1 and re.fullmatch(r'[1-9][0-9]*', replies[0]) is not None


def _update_release_body(args, store, sid, argv, endpoint, repository, release_id):
    """Update only body text in the recorded session's repository, without PR adoption."""
    target = 'https://api.github.com/' + endpoint
    if (getattr(args, 'target', None) != target or len(argv) != 6
            or argv != ['api', endpoint, '--method', 'PATCH', '--input', argv[-1]]
            or argv[-1] == '-' or any(part in {'.', '..'} for part in repository.split('/'))):
        raise ValueError('Release body update needs its exact API --target and api ENDPOINT --method PATCH --input FILE')
    session = store.session(sid)
    if (not session or session['client'] != args.client
            or Path(session['cwd']).resolve() != Path(args.cwd).resolve()):
        raise ValueError('Release body update requires the recorded session, client and working directory')
    source = Path(argv[-1])
    if not source.is_absolute(): source = Path(args.cwd) / source
    try:
        payload = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Release body update needs a saved JSON body object') from exc
    if not isinstance(payload, dict) or set(payload) != {'body'} or not isinstance(payload['body'], str):
        raise ValueError('Release body update accepts only a JSON object with one string body field')
    try:
        origin = subprocess.run(['git', '-C', args.cwd, 'remote', 'get-url', 'origin'],
                                capture_output=True, text=True, timeout=3, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('Release body update requires a matching GitHub origin') from exc
    origin = origin.removesuffix('.git').casefold()
    if origin not in {f'https://github.com/{repository}'.casefold(),
                      f'git@github.com:{repository}'.casefold(),
                      f'ssh://git@github.com/{repository}'.casefold()}:
        raise ValueError('Release body target repository differs from the recorded checkout origin')
    # Freeze admitted content; gh must not reopen the caller's mutable file.
    result = github_run(['gh', 'api', endpoint, '--method', 'PATCH', '--input', '-'],
                        runner=subprocess.run, cwd=args.cwd, input=json.dumps(payload),
                        text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f'GitHub release body update exited {result.returncode}; read the release before retrying')
    try:
        updated = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError('Release body update response is unverified; read the release before retrying') from exc
    if (not isinstance(updated, dict) or type(updated.get('id')) is not int
            or updated['id'] != int(release_id) or not isinstance(updated.get('url'), str)
            or updated['url'].casefold() != target.casefold()
            or updated.get('body') != payload['body']):
        raise RuntimeError('Release body update response is unverified; read the release before retrying')
    receipt = {'session_id': sid, 'repository': repository, 'release_id': int(release_id),
               'url': target, 'body_sha256': hashlib.sha256(payload['body'].encode()).hexdigest()}
    store.audit('github_release_body_updated', receipt)
    return receipt


def _documentation_configuration(args, store, sid, argv, endpoint, repo, number, issue):
    """Approved provisioning and one retired name; never adopt a PR, retry or replace lists."""
    retire = endpoint == DOCS_CONTEXTS
    method = 'DELETE' if retire else 'PUT'
    if (endpoint not in {DOCS_ENVIRONMENT, DOCS_SECRET, DOCS_CONTEXTS}
            or repo != 'Vaquum/Limen' or issue or len(argv) != 6
            or argv != ['api', endpoint, '--method', method, '--input', argv[-1]]
            or argv[-1] == '-'):
        raise ValueError('Documentation configuration requires the exact owned PR and api ENDPOINT --method METHOD --input FILE')

    def owner():
        from .workflow import owned_pr
        owned_pr(store, sid, repo, number)
        session = store.session(sid)
        with store._connect() as db:
            watch = db.execute("SELECT * FROM pr_watch WHERE repository=? AND number=? AND owner_session=? AND state='open'",
                               (repo, number, sid)).fetchone()
        cwd = Path(args.cwd).resolve()
        if (not session or session['root_session'] != sid or session['client'] != args.client
                or Path(session['cwd']).resolve() != cwd or not watch
                or watch['client'] != args.client or Path(watch['cwd']).resolve() != cwd):
            raise ValueError('Documentation configuration requires the recorded open-PR owner, client and checkout')
        try:
            origin = subprocess.run(['git', '-C', args.cwd, 'remote', 'get-url', 'origin'],
                                    capture_output=True, text=True, timeout=3, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError('Documentation configuration requires the matching GitHub origin') from exc
        if origin.removesuffix('.git').casefold() not in {
                'https://github.com/vaquum/limen', 'git@github.com:vaquum/limen',
                'ssh://git@github.com/vaquum/limen'}:
            raise ValueError('Documentation configuration repository differs from the recorded checkout origin')

    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)

    def read(path):
        result = github_run(['gh', 'api', path], runner=subprocess.run, cwd=args.cwd,
                            text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError('Documentation configuration read unavailable: ' + path)
        try:
            value = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise ValueError('Documentation configuration read is not verified JSON: ' + path) from exc
        if not isinstance(value, dict):
            raise ValueError('Documentation configuration read is not an object: ' + path)
        return value

    def live_pr():
        path = f'repos/{repo}/pulls/{number}'
        pr = read(path)
        if (type(pr.get('number')) is not int or pr['number'] != number
                or pr.get('url') != 'https://api.github.com/' + path
                or pr.get('state') != 'open' or pr.get('merged') is not False
                or pr.get('draft') is not False):
            raise ValueError('Documentation configuration requires a live open non-draft PR')
        identity = {}
        for side in ('head', 'base'):
            branch = pr.get(side) or {}
            repository = branch.get('repo') or {}
            if (repository.get('full_name') != repo or type(repository.get('id')) is not int
                    or repository['id'] < 1 or not isinstance(branch.get('ref'), str)
                    or not branch['ref'] or not re.fullmatch(r'[0-9a-f]{40}', str(branch.get('sha')))):
                raise ValueError('Documentation configuration PR repository/ref identity is unavailable')
            identity[side] = {key: branch[key] for key in ('ref', 'sha')}
            identity[side]['repository_id'] = repository['id']
        if identity['base']['ref'] != 'main' or identity['head']['repository_id'] != identity['base']['repository_id']:
            raise ValueError('Documentation configuration requires a same-repository PR targeting main')
        return identity

    policy = {'protected_branches': True, 'custom_branch_policies': False}

    def environment():
        value = read(DOCS_ENVIRONMENT)
        if (value.get('name') != 'documentation'
                or value.get('url') != 'https://api.github.com/' + DOCS_ENVIRONMENT
                or encoded(value.get('deployment_branch_policy')) != encoded(policy)):
            raise ValueError('Provision the protected-branches-only documentation environment first')
        return {'name': value['name'], 'deployment_branch_policy': value['deployment_branch_policy']}

    def secret_metadata():
        value = read(DOCS_SECRET)
        if (value.get('name') != 'CLOUDFLARE_DOCS_API_TOKEN'
                or not all(isinstance(value.get(key), str) and value[key] for key in ('created_at', 'updated_at'))):
            raise ValueError('Documentation environment secret metadata is unavailable')
        # Secret GET returns metadata only. Never retain a caller payload or unexpected value field.
        return {key: value[key] for key in ('name', 'created_at', 'updated_at')}

    rules_path = f'repos/{repo}/rulesets/5406995'

    def authorities(snapshot):
        # Retain each actual response, even when the other authority is unavailable.
        unavailable = []
        for name, path in (('classic', DOCS_PROTECTION), ('ruleset', rules_path)):
            try:
                snapshot[name] = read(path)
            except Exception:
                unavailable.append(name)
        if unavailable:
            snapshot['unavailable'] = unavailable
            raise ValueError('Full classic protection and independent ruleset reads are required')

    owner()
    source = Path(argv[-1])
    if not source.is_absolute(): source = Path(args.cwd) / source
    try:
        payload = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Documentation configuration needs a saved JSON object') from exc
    if not isinstance(payload, dict):
        raise ValueError('Documentation configuration needs a saved JSON object')
    if endpoint == DOCS_ENVIRONMENT:
        if encoded(payload) != encoded({'deployment_branch_policy': policy}):
            raise ValueError('Only the protected-branches-only deployment_branch_policy is admitted')
    elif endpoint == DOCS_SECRET:
        if (set(payload) != {'key_id', 'encrypted_value'} or not isinstance(payload['key_id'], str)
                or not payload['key_id'].strip() or not isinstance(payload['encrypted_value'], str)):
            raise ValueError('Only encrypted_value and its environment key_id are admitted')
        try:
            valid = len(base64.b64decode(payload['encrypted_value'], validate=True)) > 48
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError('Documentation secret requires a nonempty base64 sealed-box ciphertext')
    elif encoded(payload) != encoded({'contexts': [DOCS_RETIRED_CONTEXT]}):
        raise ValueError('Only the exact retired documentation context name is admitted')
    # Read and serialize once. The child receives exactly these admitted bytes, never the file.
    body = encoded(payload)
    receipt = {'session_id': sid, 'repository': repo, 'number': number, 'endpoint': endpoint,
               'method': method, 'payload_sha256': hashlib.sha256(body.encode()).hexdigest()}
    if endpoint == DOCS_SECRET:
        receipt['environment'] = environment()
        if read(DOCS_ENVIRONMENT + '/secrets/public-key').get('key_id') != payload['key_id']:
            raise ValueError('Documentation secret key_id differs from the current environment public key')
    elif retire:
        receipt['environment'] = environment()
        receipt['secret'] = secret_metadata()
    # Bounded PR reads, not full gate/thread collection.
    pr = live_pr()
    if retire:
        receipt['stage'] = 'preflight'
        receipt['retired_context'] = DOCS_RETIRED_CONTEXT
        receipt['before'] = {}
        try:
            authorities(receipt['before'])
            before, rules = receipt['before']['classic'], receipt['before']['ruleset']
            required = before.get('required_status_checks') or {}
            reviews = before.get('required_pull_request_reviews') or {}
            if not all(isinstance(value, dict) for value in (
                    required, reviews, before.get('enforce_admins'), before.get('required_conversation_resolution'))):
                raise ValueError('Complete classic protection controls are required')
            existing, contexts = required.get('checks'), required.get('contexts')
            if (before.get('url') != 'https://api.github.com/' + DOCS_PROTECTION
                    or required.get('strict') is not True
                    or (before.get('enforce_admins') or {}).get('enabled') is not True
                    or (before.get('required_conversation_resolution') or {}).get('enabled') is not True
                    or reviews.get('dismiss_stale_reviews') is not True
                    or type(reviews.get('required_approving_review_count')) is not int
                    or reviews['required_approving_review_count'] < 1
                    or not isinstance(existing, list) or not existing
                    or any(not isinstance(check, dict) or set(check) != {'context', 'app_id'}
                           or not isinstance(check['context'], str) or not check['context']
                           or (check['app_id'] is not None and
                               (type(check['app_id']) is not int or check['app_id'] == 0 or check['app_id'] < -1))
                           for check in existing)
                    or not isinstance(contexts, list) or not all(isinstance(name, str) for name in contexts)
                    or set(contexts) != {check['context'] for check in existing}
                    or DOCS_RETIRED_CONTEXT not in contexts):
                raise ValueError('Complete strict classic protection with the retired name is required')
            if (rules.get('id') != 5406995 or rules.get('name') != 'Protect-Main'
                    or rules.get('source') != repo or rules.get('source_type') != 'Repository'
                    or rules.get('target') != 'branch' or rules.get('enforcement') != 'active'
                    or rules.get('bypass_actors') != [] or not isinstance(rules.get('conditions'), dict)
                    or not isinstance(rules.get('rules'), list) or not rules['rules']):
                raise ValueError('The complete independent Protect-Main ruleset is required')
            # Expected readback only. Never send this snapshot or the unrelated list to GitHub.
            expected = json.loads(encoded(before))
            expected['required_status_checks']['checks'] = [check for check in existing
                if check['context'] != DOCS_RETIRED_CONTEXT]
            expected['required_status_checks']['contexts'] = [name for name in contexts
                if name != DOCS_RETIRED_CONTEXT]
            if live_pr() != pr:
                raise ValueError('PR identity changed during documentation configuration admission')
        except Exception:
            store.audit('github_documentation_configuration_incomplete', receipt)
            raise
    owner()
    store.audit('github_documentation_configuration_attempt', receipt)
    receipt['stage'] = 'write'
    try:
        try:
            result = github_run(['gh', 'api', endpoint, '--method', method, '--input', '-'],
                                runner=subprocess.run, cwd=args.cwd, input=body,
                                text=True, capture_output=True, timeout=90)
            receipt['exit_code'] = result.returncode
            if not result.returncode:
                receipt['stage'] = 'readback'
        finally:
            if retire:
                receipt['after'] = {}
                authorities(receipt['after'])
        if result.returncode:
            raise ValueError('GitHub did not confirm the configuration write')
        receipt['stage'] = 'readback'
        if retire and (encoded(receipt['after']['classic']) != encoded(expected)
                       or encoded(receipt['after']['ruleset']) != encoded(rules)):
            raise ValueError('Protection readback differs beyond retirement of the authorized name')
        receipt['environment'] = environment()
        if endpoint == DOCS_SECRET or retire:
            after_secret = secret_metadata()
            if retire and after_secret != receipt['secret']:
                raise ValueError('Documentation secret metadata changed during name retirement')
            receipt['secret'] = after_secret
        if live_pr() != pr:
            raise ValueError('PR identity changed during documentation configuration')
        owner()
    except Exception:
        store.audit('github_documentation_configuration_incomplete', receipt)
        if retire:
            raise RuntimeError('Documentation configuration is incomplete; read back both protection authorities and provisioning metadata before any new operation; no automatic retry or rollback') from None
        raise RuntimeError('Documentation configuration is incomplete; read back the environment and secret metadata before retrying') from None
    receipt['stage'] = 'verified'
    store.audit('github_documentation_configuration_verified', receipt)
    return receipt


def _owned_slice_sweep_allowed(store, sid, repo, number, issue, endpoint, method, argv, cwd):
    """Refresh the existing trusted-main Limen slice verdict; never run candidate code."""
    expected = 'repos/Vaquum/Limen/actions/workflows/pr_checks_slice_sweep.yml/dispatches'
    if issue or repo != 'Vaquum/Limen' or endpoint != expected or method != 'POST':
        return False
    if len(argv) != 6 or argv != ['api', endpoint, '--method', 'POST', '--input', argv[-1]] or argv[-1] == '-':
        return False
    path = Path(argv[-1])
    if not path.is_absolute(): path = Path(cwd) / path
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if payload != {'ref': 'main'}: return False
    from .workflow import owned_pr
    from .github import collect_validation
    from .policy import branch_freshness_gate
    owned_pr(store, sid, repo, number)

    def current_owner():
        with store._connect() as db:
            root = store._root(sid, store._parents(db))
            row = db.execute("SELECT cwd FROM pr_watch WHERE repository=? AND number=? AND owner_session=? AND state='open'", (repo, number, root)).fetchone()
            return row is not None and Path(row['cwd']).resolve() == Path(cwd).resolve()

    if not current_owner(): return False
    snapshot = asyncio.run(collect_validation(repo, number))
    if (snapshot.get('state') != 'open' or snapshot.get('merged') is not False
            or snapshot.get('base_ref') != 'main'
            or not snapshot.get('head_sha') or snapshot.get('head_sha_after') != snapshot['head_sha']
            or branch_freshness_gate(snapshot)['status'] != 'PASS'):
        raise ValueError('Slice sweep requires an owned open PR with fresh head and target ancestry')
    if not current_owner():
        raise ValueError('Slice sweep owner or checkout changed during validation')
    return True


def _owned_origo_ci_dispatch(args, store, sid, repo, number, issue, endpoint, method, argv):
    """Run only the reviewed hosted source suite for the exact owned Origo head."""
    workflow = '.github/workflows/pr_checks_tests.yml'
    trusted_blob = '6bf211fb8bbc65040bd5848040e36f2c2d463134'
    expected = 'repos/Vaquum/Origo/actions/workflows/pr_checks_tests.yml/dispatches'
    if repo != 'Vaquum/Origo' or issue or endpoint != expected:
        return None
    if (method != 'POST' or len(argv) != 6
            or argv != ['api', expected, '--method', 'POST', '--input', argv[-1]]
            or argv[-1] == '-'):
        raise ValueError('Origo CI dispatch requires the exact saved-file API request')
    path = Path(argv[-1])
    if not path.is_absolute(): path = Path(args.cwd) / path
    payload = json.loads(path.read_text())
    from .workflow import owned_pr
    from .github import collect_validation
    from .policy import branch_freshness_gate
    from .publication import source, exact_source
    from urllib.parse import quote
    owner = owned_pr(store, sid, repo, number)

    def current_owner():
        with store._connect() as db:
            root = store._root(sid, store._parents(db))
            row = db.execute("SELECT cwd,client FROM pr_watch WHERE repository=? AND number=? AND owner_session=? AND state='open'", (repo, number, root)).fetchone()
            return (row is not None and row['client'] == args.client
                    and Path(row['cwd']).resolve() == Path(args.cwd).resolve())

    if not current_owner():
        raise ValueError('Origo CI dispatch requires the current owner and exact checkout')
    local = source(args.cwd, stable=True)
    if not exact_source(owner, local):
        raise ValueError('Origo CI dispatch requires the clean owned repository checkout')
    snapshot = asyncio.run(collect_validation(repo, number))
    head, branch = snapshot.get('head_sha'), local['branch']
    if (snapshot.get('state') != 'open' or snapshot.get('merged') is not False
            or snapshot.get('head_sha_after') != head or head != local['head']
            or snapshot.get('head_repository') != repo or snapshot.get('base_repository') != repo
            or snapshot.get('base_ref') != 'main'
            or branch_freshness_gate(snapshot)['status'] != 'PASS'):
        raise ValueError('Origo CI dispatch requires an owned same-repository fresh stable PR head')
    frozen = {'ref': branch, 'inputs': {'runner': 'github'}}
    if not branch or payload != frozen:
        raise ValueError('Origo CI dispatch permits only its current head branch and runner=github')

    def read(target, timeout=15):
        result = github_run(['gh', 'api', target], runner=subprocess.run, cwd=args.cwd,
                            capture_output=True, text=True, check=True, timeout=timeout)
        return json.loads(result.stdout)

    def same_pr(pull):
        source_repo = (pull.get('head') or {}).get('repo') or {}
        target_repo = (pull.get('base') or {}).get('repo') or {}
        return (pull.get('state') == 'open' and pull.get('merged') is False
                and pull.get('draft') is False
                and (pull.get('head') or {}).get('ref') == branch
                and (pull.get('head') or {}).get('sha') == head
                and (pull.get('base') or {}).get('ref') == 'main'
                and (pull.get('base') or {}).get('sha') == snapshot['base_sha']
                and source_repo.get('full_name') == target_repo.get('full_name') == repo
                and type(source_repo.get('id')) is int and source_repo['id'] > 0
                and source_repo.get('id') == target_repo.get('id'))

    for ref in (snapshot['base_sha'], head):
        blob = read(f'repos/{repo}/contents/{workflow}?ref={ref}')
        if blob.get('path') != workflow or blob.get('sha') != trusted_blob or blob.get('type') != 'file':
            raise ValueError('Origo source-suite workflow differs from the reviewed trusted blob')
    runs_path = (f'repos/{repo}/actions/workflows/pr_checks_tests.yml/runs'
                 f'?event=workflow_dispatch&branch={quote(branch, safe="")}&per_page=100')

    def runs(timeout=15):
        rows = read(runs_path, timeout).get('workflow_runs')
        if not isinstance(rows, list) or any(not isinstance(row, dict) or type(row.get('id')) is not int for row in rows):
            raise ValueError('Origo CI run inventory is incomplete')
        return rows

    before = {row['id'] for row in runs()}
    if (not same_pr(read(f'repos/{repo}/pulls/{number}'))
            or not current_owner() or source(args.cwd, stable=True) != local):
        raise ValueError('Origo CI owner, checkout, PR head or target changed before dispatch')
    body = json.dumps(frozen)
    receipt = {'session_id': sid, 'repository': repo, 'number': number,
               'head_sha': head, 'head_ref': branch, 'workflow': workflow,
               'workflow_blob': trusted_blob, 'payload_sha256': hashlib.sha256(body.encode()).hexdigest()}
    store.audit('github_origo_ci_dispatch_attempt', receipt)
    try:
        result = github_run(['gh', 'api', expected, '--method', 'POST', '--input', '-'],
                            runner=subprocess.run, cwd=args.cwd, input=body,
                            text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError('GitHub did not confirm workflow dispatch')
        deadline = time.monotonic() + 30
        while True:
            found = [row for row in runs(min(10, max(.1, deadline - time.monotonic())))
                     if row['id'] not in before]
            if found:
                if len(found) != 1:
                    raise RuntimeError('Workflow dispatch has ambiguous new run identities')
                run = read(f'repos/{repo}/actions/runs/{found[0]["id"]}', timeout=10)
                url = f'https://github.com/{repo}/actions/runs/{found[0]["id"]}'
                if (run.get('id') != found[0]['id'] or run.get('head_sha') != head
                        or run.get('head_branch') != branch or run.get('path') != workflow
                        or run.get('event') != 'workflow_dispatch' or run.get('html_url') != url
                        or (run.get('head_repository') or {}).get('full_name') != repo):
                    raise RuntimeError('Dispatched CI run does not match the frozen owned head')
                receipt.update(run_id=run['id'], run_url=url)
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Dispatched CI run did not become observable within30seconds')
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        if (not same_pr(read(f'repos/{repo}/pulls/{number}'))
                or not current_owner() or source(args.cwd, stable=True) != local):
            raise RuntimeError('Origo CI owner, checkout, PR head or target changed during dispatch')
    except Exception:
        store.audit('github_origo_ci_dispatch_incomplete', receipt)
        raise RuntimeError('Origo CI dispatch outcome is unverified; inspect workflow runs before any retry') from None
    store.audit('github_origo_ci_dispatch_verified', receipt)
    return receipt


def _owned_ci_rerun_allowed(store, sid, repo, number, issue, endpoint, method, argv):
    """One retry of an owned current-head failed/cancelled CI run; never a review."""
    match = re.fullmatch(re.escape(f'repos/{repo}/actions/') + r'(jobs|runs)/([1-9][0-9]*)/rerun', endpoint)
    if issue or not match or method != 'POST' or argv != ['api', endpoint, '--method', 'POST']:
        return False
    from .workflow import owned_pr
    from .github import collect_validation
    from .policy import branch_freshness_gate
    owned_pr(store, sid, repo, number)
    prior = (store.recovery(repo, number, sid) or {}).get('ci_reruns') or {}
    if match[1] == 'runs' and match[2] in prior.get('run_ids', []):
        raise ValueError('This CI run already had its bounded retry; fix its cause before retrying')
    snapshot = asyncio.run(collect_validation(repo, number))
    head = snapshot.get('head_sha')
    if (snapshot.get('state') != 'open' or snapshot.get('merged') is not False
            or snapshot.get('head_sha_after') != head or branch_freshness_gate(snapshot)['status'] != 'PASS'):
        raise ValueError('CI retry requires fresh open-PR head and target ancestry')
    def read(path):
        result = github_run(['gh', 'api', path], runner=subprocess.run, text=True, capture_output=True, check=True, timeout=30)
        return json.loads(result.stdout)
    item = read(endpoint.removesuffix('/rerun'))
    if (str(item.get('id')) != match[2] or item.get('status') != 'completed'
            or item.get('conclusion') not in {'failure', 'cancelled'} or item.get('head_sha') != head):
        return False
    run = read(f'repos/{repo}/actions/runs/{item["run_id"]}') if match[1] == 'jobs' else item
    path = run.get('path') or ''
    readiness = path == '.github/workflows/pr_merge_readiness.yml'
    if (not path.startswith('.github/workflows/') or run.get('head_sha') != head
            or (not readiness and re.search(r'copilot|review', path + ' ' + str(run.get('name') or '') + ' ' + str(item.get('name') or ''), re.I))
            or run.get('event') not in ({'pull_request', 'pull_request_review'} if readiness else {'pull_request'})
            or run.get('status') != 'completed' or run.get('conclusion') not in {'failure', 'cancelled'}
            or run.get('run_attempt') != item.get('run_attempt')
            or not any(pr.get('url') == f'https://api.github.com/repos/{repo}/pulls/{number}'
                       for pr in run.get('pull_requests', []))):
        return False
    after = read(f'repos/{repo}/pulls/{number}')
    from urllib.parse import quote
    base = read(f'repos/{repo}/branches/{quote(snapshot["base_ref"], safe="")}')
    if (after.get('state') != 'open' or after.get('merged') is not False
            or (after.get('head') or {}).get('sha') != head
            or (after.get('base') or {}).get('ref') != snapshot['base_ref']
            or (base.get('commit') or {}).get('sha') != snapshot['base_sha']):
        raise ValueError('PR head or target branch changed before CI retry')
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        root = store._root(sid, store._parents(db))
        row = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=? AND owner_session=? AND state=\'open\'', (repo, number, root)).fetchone()
        if not row:
            return False
        saved = db.execute('SELECT fields FROM recovery WHERE repository=? AND number=? AND session_id=?', (repo, number, root)).fetchone()
        fields = json.loads(saved['fields']) if saved else {}
        prior = fields.get('ci_reruns') or {}
        ids = prior.get('run_ids', []) if prior.get('head_sha') == head else []
        run_id = str(run['id'])
        if run_id in ids:
            raise ValueError('This CI run already had its bounded retry; fix its cause before retrying')
        from .monitor import _receipt
        _receipt(db, store, row, {'ci_reruns': {'head_sha': head, 'run_ids': [*ids, run_id]}})
    return True


def _gateway_ownership(store, args, sid, repository, number, origin, operation):
    target = f'https://github.com/{repository}/pull/{number}'
    store.register(sid, args.client, repository, number, args.cwd)
    store.audit('ownership_evidence', {'session_id': sid, 'target': target,
        'origin': origin, 'operation': operation,
        'tool_call_id': os.getenv('CALLER_GATE_TOOL_CALL_ID'),
        'turn_id': os.getenv('CALLER_GATE_TURN_ID') or os.getenv('CODEX_TURN_ID') or os.getenv('CLAUDE_TURN_ID')})
    from .turns import note_mutation
    note_mutation(store, {'turn_id': os.getenv('CALLER_GATE_TURN_ID') or os.getenv('CODEX_TURN_ID') or os.getenv('CLAUDE_TURN_ID')}, sid)


def _gateway_target(args, argv):
    positional = argv[2] if len(argv) > 2 and REF.fullmatch(argv[2].rstrip('/')) else None
    if len(argv) > 2 and not argv[2].startswith('-') and not positional:
        raise ValueError('Mutation target must be an explicit positional PR/issue URL')
    explicit = getattr(args, 'target', None)
    if positional and explicit and reference(positional) != reference(explicit):
        raise ValueError('Mutation positional target and --target disagree')
    target = positional or explicit
    if not target: raise ValueError('GitHub mutations require an explicit positional PR/issue URL or --target')
    repository, number, issue = reference(target)
    if argv[0] in {'pr', 'issue'} and issue != (argv[0] == 'issue'):
        raise ValueError('GitHub mutation target kind does not match the command')
    return target, repository, number, issue


def session_prs(store, sid=None):
    """Legacy registration receipts never override the canonical chat owner."""
    rows = store.prs(sid)
    if sid is None:
        return rows
    session = store.session(sid)
    if not session:
        return []
    with store._connect() as db:
        owned = {(r['repository'], r['number']) for r in db.execute(
            "SELECT repository,number FROM pr_watch WHERE owner_session=? AND state='open'", (session['root_session'],))}
    return [r for r in rows if (r['repository'], r['number']) in owned]


async def inspect(store, sid=None, url=None):
    from .github import collect_pr
    from .policy import evaluate_pr
    rows = session_prs(store, sid)
    if url:
        repository, number, issue = reference(url)
        if issue: raise ValueError('inspect --pr needs a PR URL')
        rows = [{'repository': repository, 'number': number}]
    result = []
    for row in rows:
        try:
            snap = await asyncio.wait_for(collect_pr(row['repository'], row['number']), 65)
            gates = evaluate_pr(snap, store.dispositions(row['repository'], row['number']))
            store.set_snapshot(row['repository'], row['number'], snap, gates)
            result.append({'repository': row['repository'], 'number': row['number'], 'head_sha': snap['head_sha'], 'gates': gates,
                           'scope': store.pr_scope(sid, row['repository'], row['number']) if sid else None})
        except Exception as exc:
            from .errors import diagnostic
            error=diagnostic(exc,f"/repos/{row['repository']}/pulls/{row['number']}")
            gates=[{'gate':'GITHUB_DATA','status':'UNKNOWN','details':[error['message'][:900]],'evidence':{'error':error}}]
            store.set_snapshot(row['repository'],row['number'],{'repository':row['repository'],'number':row['number'],'component_errors':{'collection':error}},gates)
            result.append({'repository':row['repository'],'number':row['number'],'gates':gates})
    return result


def _nonrequired_quota_merge_allowed(store, sid, target, argv, rows, approval):
    if not isinstance(approval, str) or not approval.strip() or len(rows) != 1:
        return False
    row = rows[0]
    head = row.get('head_sha')
    if argv != ['pr', 'merge', target, '--merge', '--match-head-commit', head]:
        return False
    from .workflow import owned_pr
    from .external import current_hold, publication_ready
    from .policy import _latest_checks, _outcome
    repo, num, issue = reference(target)
    if issue: return False
    owned_pr(store, sid, repo, num)
    snapshot = (store.snapshot(repo, num) or {}).get('snapshot') or {}
    gates = row['gates']
    held = current_hold(store, sid, snapshot, gates)
    if any(g.get('status')!='PASS' for g in gates if g.get('gate')!='CI_FAILED'):
        return False
    if {g.get('gate') for g in gates}!={'PR_NOT_DRAFT','CI_FAILED','CI_PENDING','REVIEW_HANDLED','REVIEW_CONCLUDED','MERGE_CONFLICT','BRANCH_UP_TO_DATE','ISSUE_CLOSURE'}:
        return False
    required = snapshot.get('expected_checks')
    if (not held or snapshot.get('head_sha') != head or not isinstance(required, list) or not required
            or not publication_ready(store, sid, repo, num)
            or any(check.get('name') in required for check in _latest_checks(snapshot.get('checks') or [])
                   if _outcome(check) == 'failed')):
        return False
    store.audit('operator_nonrequired_quota_merge', {'session_id': sid, 'target': target,
        'head_sha': head, 'approval_evidence': approval, 'failure_evidence_url': held['evidence_url'],
        'failure_fingerprint': held['fingerprint'], 'ci_failure_preserved': True,
        'normal_github_merge': True, 'gates': gates})
    return True


def create_scoped_repository(args, store, argv):
    """Create only the exact private personal repository recorded in approved scope."""
    sid = session_id(args)
    scope = store.get_scope(sid) or {}
    approved = scope.get('repository_creation') or {}
    if not isinstance(approved, dict):
        raise ValueError('Repository creation authorization must be a mapping')
    name = argv[2] if len(argv) > 2 else ''
    valid = (len(argv) in {4, 6} and argv[:2] == ['repo', 'create']
             and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', name)
             and argv[3] == '--private'
             and (len(argv) == 4 or argv[4] == '--description')
             and approved.get('full_name') == name
             and approved.get('visibility') == 'private'
             and isinstance(approved.get('approval_evidence'), str)
             and bool(approved['approval_evidence'].strip()))
    if not valid:
        raise ValueError('Repository creation requires the exact approved private repository; clone, push and other repository operations are not admitted')
    actor = github_run(['gh', 'api', 'user'], runner=subprocess.run, cwd=args.cwd,
                       text=True, capture_output=True, timeout=30)
    if actor.returncode:
        raise RuntimeError('Authenticated account lookup failed: ' + actor.stderr.strip())
    if json.loads(actor.stdout).get('login', '').lower() != name.split('/')[0].lower():
        raise ValueError('Repository owner must match the authenticated personal account')
    result = github_run(['gh', *argv], runner=subprocess.run, cwd=args.cwd,
                        text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    verified = github_run(['gh', 'api', 'repos/' + name], runner=subprocess.run,
                          cwd=args.cwd, text=True, capture_output=True, timeout=30)
    if verified.returncode:
        raise RuntimeError('Repository creation outcome requires readback; do not repeat creation')
    repo = json.loads(verified.stdout)
    canonical = repo.get('full_name')
    url = 'https://github.com/' + (canonical if isinstance(canonical, str) else '')
    if not isinstance(canonical, str) or canonical.casefold() != name.casefold() or repo.get('private') is not True or repo.get('html_url') != url:
        raise RuntimeError('Created repository identity/visibility could not be verified')
    store.audit('repository_created', {'session_id': sid, 'repository': name, 'url': url, 'private': True})
    return {'repository': name, 'url': url, 'private': True}


def gateway(args, store):
    sid = session_id(args)
    argv = args.arguments
    admitted_api_body = None
    if argv and argv[0] == '--': argv = argv[1:]
    if argv[:2] == ['repo', 'create']:
        return create_scoped_repository(args, store, argv)
    if len(argv) < 2 or argv[0] not in {'pr', 'issue', 'api'}:
        raise ValueError('Gateway accepts gh pr, gh issue, or gh api operations')
    if any(a == '-d' or a.startswith('--draft') for a in argv) or '--undo' in argv or 'convertPullRequestToDraft' in ' '.join(argv):
        raise ValueError('PR_NOT_DRAFT FAIL: drafts are forbidden')
    if re.search(r'[\"\'](?:draft|isDraft)[\"\']\s*:\s*true', ' '.join(argv), re.I):
        raise ValueError('PR_NOT_DRAFT FAIL: drafts are forbidden')
    options = _options(argv)
    creation = argv[0] in {'pr', 'issue'} and argv[1] == 'create'
    mutation = argv[0] in {'pr', 'issue'} and argv[1] in {'edit', 'ready', 'merge', 'close', 'reopen', 'review', 'comment', 'delete', 'lock', 'unlock', 'transfer', 'pin', 'unpin'}
    target_info = _gateway_target(args, argv) if mutation else None
    if target_info and (len(argv) < 3 or not REF.fullmatch(argv[2].rstrip('/'))):
        argv = [*argv[:2], target_info[0], *argv[2:]]
    if argv[0] == 'api':
        endpoint, method, field_values, input_file = _api_request(argv)
        fields = bool(field_values) or input_file is not None
        api_write = method.upper() not in {'GET', 'HEAD'} if method else fields
        google_grant = (store.get_scope(sid) or {}).get('google_actions_configuration')
        valid_google_grant = (isinstance(google_grant, dict)
            and set(google_grant) == {'repository', 'authorizing_pr', 'approval_evidence'}
            and isinstance(google_grant['repository'], str)
            and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', google_grant['repository']) is not None
            and all(part not in {'.', '..'} for part in google_grant['repository'].split('/'))
            and isinstance(google_grant['authorizing_pr'], str)
            and REF.fullmatch(google_grant['authorizing_pr']) is not None
            and '/pull/' in google_grant['authorizing_pr']
            and isinstance(google_grant['approval_evidence'], str)
            and bool(google_grant['approval_evidence'].strip()))
        google_prefix = f"repos/{google_grant['repository']}/actions/" if valid_google_grant else ''
        google_secret = bool(google_prefix) and endpoint == google_prefix + 'secrets/POISE_GOOGLE_CLIENT_SECRET'
        google_variable = bool(google_prefix) and endpoint in (google_prefix + 'variables',
                                      google_prefix + 'variables/POISE_GOOGLE_CLIENT_ID')
        if fields and (endpoint == DOCS_CONTEXTS or google_secret or google_variable):
            # A read-method spelling cannot bypass this operation's frozen-body admission.
            api_write = True
        if api_write:
            release = re.fullmatch(r'repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/releases/([1-9][0-9]*)', endpoint)
            if release:
                return _update_release_body(args, store, sid, argv, endpoint, *release.groups())
            if not args.target: raise ValueError('GitHub API writes need --target with the owned PR/document URL')
            repo, num, issue = reference(args.target)
            dispatch = _owned_origo_ci_dispatch(args, store, sid, repo, num, issue, endpoint, method, argv)
            if dispatch is not None:
                print(json.dumps(dispatch))
                return None
            if endpoint in {DOCS_ENVIRONMENT, DOCS_SECRET, DOCS_CONTEXTS}:
                return _documentation_configuration(args, store, sid, argv, endpoint, repo, num, issue)
            allowed_reply = _api_reply_allowed(repo, num, issue, endpoint, method or 'POST', field_values, input_file)
            allowed_ci_rerun = _owned_ci_rerun_allowed(
                store, sid, repo, num, issue, endpoint, method, argv)
            allowed_slice_sweep = _owned_slice_sweep_allowed(
                store, sid, repo, num, issue, endpoint, method, argv, args.cwd)
            if allowed_slice_sweep:
                admitted_api_body = json.dumps({'ref': 'main'})
                argv = ['api', endpoint, '--method', 'POST', '--input', '-']
            allowed_runner_registration = (
                repo == 'Vaquum/Origo'
                and issue
                and endpoint == f'repos/{repo}/actions/runners/registration-token'
                and method is not None
                and method.upper() == 'POST'
                and not fields
                and argv == ['api', endpoint, '--method', 'POST']
            )
            # User-authorized maintenance, 2026-10-09: only this owned PR's
            # two encrypted release-email secrets and verified sender variable.
            # No generic repository-setting or arbitrary API-write permission.
            allowed_release_email_config = False
            if (repo == 'autonomio/resolvent' and not issue
                    and any(p['repository'] == repo and p['number'] == num for p in store.prs(sid))):
                secret_endpoint = re.fullmatch(r'repos/autonomio/resolvent/actions/secrets/(RESEND_API_KEY|RESOLVENT_NOTIFY_TO)', endpoint)
                variable_endpoint = endpoint in ('repos/autonomio/resolvent/actions/variables',
                                                'repos/autonomio/resolvent/actions/variables/RESOLVENT_NOTIFY_FROM')
                verb = (method or '').upper()
                input_path = options.get('--input')
                exact_args = (input_path and argv == ['api', endpoint, '--method', verb, '--input', input_path])
                if exact_args and ((secret_endpoint and verb == 'PUT') or
                                   (variable_endpoint and verb == ('POST' if endpoint.endswith('/variables') else 'PATCH'))):
                    payload = json.loads(Path(input_path).read_text())
                    if secret_endpoint and isinstance(payload, dict) and set(payload) == {'key_id', 'encrypted_value'}:
                        try:
                            ciphertext = base64.b64decode(payload['encrypted_value'], validate=True)
                            allowed_release_email_config = (len(ciphertext) > 48 and isinstance(payload['key_id'], str) and bool(payload['key_id']))
                        except (ValueError, TypeError):
                            pass
                    elif variable_endpoint:
                        allowed_release_email_config = payload == {'name': 'RESOLVENT_NOTIFY_FROM', 'value': 'Resolvent <resolvent@autonom.io>'}
            # The immutable approved work-item names both repositories and the exact
            # authorizing PR. Only the two integration keys are admitted by this grant.
            allowed_google_config = False
            google_owner = None
            if (not issue and (google_secret or google_variable)
                    and reference(google_grant['authorizing_pr']) == (repo, num, False)):
                google_owner = next((p for p in store.prs(sid)
                    if p['repository'] == repo and p['number'] == num), None)
                if google_owner is None:
                    try:
                        retained = store.closed_review_owner(sid, repo, num, args.cwd)
                        if retained['identity']['watch']['state'] == 'merged':
                            google_owner = retained['identity']['registration']
                    except ValueError:
                        pass
            if (google_owner and google_owner['client'] == args.client
                    and Path(google_owner['cwd']).resolve() == Path(args.cwd).resolve()):
                verb = (method or '').upper()
                input_path = options.get('--input')
                exact_args = (input_path and input_path != '-'
                              and argv == ['api', endpoint, '--method', verb, '--input', input_path])
                if exact_args and ((google_secret and verb == 'PUT') or
                                   (google_variable and verb == ('POST' if endpoint.endswith('/variables') else 'PATCH'))):
                    source = Path(input_path)
                    if not source.is_absolute(): source = Path(args.cwd) / source
                    payload = json.loads(source.read_text())
                    if google_secret and isinstance(payload, dict) and set(payload) == {'key_id', 'encrypted_value'}:
                        try:
                            ciphertext = base64.b64decode(payload['encrypted_value'], validate=True)
                            allowed_google_config = (len(ciphertext) > 48 and isinstance(payload['key_id'], str) and bool(payload['key_id']))
                        except (ValueError, TypeError):
                            pass
                    elif google_variable and isinstance(payload, dict) and set(payload) == {'name', 'value'}:
                        allowed_google_config = (payload['name'] == 'POISE_GOOGLE_CLIENT_ID'
                            and isinstance(payload['value'], str)
                            and re.fullmatch(r'[0-9]+-[a-z0-9-]+\.apps\.googleusercontent\.com', payload['value']) is not None)
                    if allowed_google_config:
                        admitted_api_body = json.dumps(payload)
                        argv = ['api', endpoint, '--method', verb, '--input', '-']
                        receipt = {'session_id': sid, 'repository': google_grant['repository'],
                            'upstream_pr': args.target, 'endpoint': endpoint, 'method': verb,
                            'payload_sha256': hashlib.sha256(admitted_api_body.encode()).hexdigest()}
                        store.audit('github_google_configuration_attempt', receipt)
                        try:
                            result = github_run(['gh', *argv], runner=subprocess.run, cwd=args.cwd,
                                input=admitted_api_body, text=True, capture_output=True, timeout=90)
                        except Exception:
                            store.audit('github_google_configuration_incomplete', receipt)
                            raise RuntimeError('Google configuration is incomplete; read back fork metadata before retrying') from None
                        if result.returncode:
                            store.audit('github_google_configuration_incomplete', receipt)
                            raise RuntimeError('Google configuration is incomplete; read back fork metadata before retrying')
                        store.audit('github_google_configuration_written', receipt)
                        print(json.dumps(receipt))
                        # This is configuration under existing ownership, never PR adoption.
                        # In particular a retired merged PR must remain retired and silent.
                        return None
            # Restore the existing LinkedIn integration for the authorized site-stream repair.
            # The exact repository, owned PR, endpoints, verbs and payloads are bounded here;
            # branch protection, workflow approval requirements and arbitrary secrets stay denied.
            allowed_stream_config = False
            if (repo == 'mikkokotila/mikkokotila-com' and not issue
                    and any(p['repository'] == repo and p['number'] == num for p in store.prs(sid))):
                verb = (method or '').upper()
                input_path = options.get('--input')
                exact_args = (input_path and input_path != '-' and argv == ['api', endpoint, '--method', verb, '--input', input_path])
                secret_endpoint = endpoint == f'repos/{repo}/actions/secrets/LINKEDIN_TOKEN'
                workflow_endpoint = endpoint == f'repos/{repo}/actions/permissions/workflow'
                auto_merge_endpoint = endpoint == f'repos/{repo}'
                if exact_args and ((verb == 'PUT' and (secret_endpoint or workflow_endpoint)) or (verb == 'PATCH' and auto_merge_endpoint)):
                    source = Path(input_path)
                    if not source.is_absolute(): source = Path(args.cwd) / source
                    payload = json.loads(source.read_text())
                    if secret_endpoint and isinstance(payload, dict) and set(payload) == {'key_id', 'encrypted_value'}:
                        try:
                            ciphertext = base64.b64decode(payload['encrypted_value'], validate=True)
                            allowed_stream_config = (len(ciphertext) > 48 and isinstance(payload['key_id'], str) and bool(payload['key_id']))
                        except (ValueError, TypeError):
                            pass
                    elif workflow_endpoint:
                        allowed_stream_config = (payload == {'default_workflow_permissions': 'read', 'can_approve_pull_request_reviews': True}
                                                 and payload['can_approve_pull_request_reviews'] is True)
                    elif auto_merge_endpoint:
                        allowed_stream_config = payload == {'allow_auto_merge': True} and payload['allow_auto_merge'] is True
                    if allowed_stream_config:
                        # gh must receive these admitted bytes, never reopen a mutable caller file.
                        admitted_api_body = json.dumps(payload)
                        argv = ['api', endpoint, '--method', verb, '--input', '-']
            if not (allowed_reply or allowed_runner_registration or allowed_release_email_config or allowed_stream_config or allowed_ci_rerun or allowed_slice_sweep):
                raise ValueError('API gateway permits owned CI reruns, review replies and owned Origo runner registration; use resolve for thread resolution, gh pr/issue for other writes')
            if issue:
                if not any(j['repository']==repo and j['number']==num for j in store.followups(sid)): raise ValueError('Document is not owned by this task; register post-review first')
            else: target_info = (args.target, repo, num, False)
            mutation = True
    body = options.get('--body', options.get('-b', ''))
    body_file = options.get('--body-file', options.get('-F')) if argv[0] in {'pr','issue'} else None
    if body_file:
        if body_file == '-': raise ValueError('Use a saved body file; stdin cannot be independently checked')
        body = Path(body_file).read_text()
    title = options.get('--title', options.get('-t', ''))
    if argv[0] == 'pr' and (creation or argv[1] == 'edit' and ('--body' in options or '-b' in options or body_file)):
        from .closure import has_closing_reference
        if not has_closing_reference(body):
            raise ValueError('ISSUE_CLOSURE FAIL: add a closing reference to the genuine issue for this authorized work, for example Closes #123. Find and reuse a matching issue; if none exists, create an issue recording the authorized task through the GitHub gateway. Do not create placeholders, duplicates or unrelated issues.')
    scope = store.get_scope(sid)
    kind = args.kind
    if not kind and DOC.search(title): kind = 'slice' if re.search(r'\bslice\b', title, re.I) else 'prd'
    if creation and kind:
        if not scope: raise ValueError('SLICE_SCOPE UNKNOWN: record the user-agreed scope before publishing')
        if kind == 'slice':
            if not args.slice_id or args.slice_id not in scope['slice_ids']:
                raise ValueError('SLICE_SCOPE FAIL: --slice-id must be one of the originally approved slice IDs')
            same_scope = [f for f in store.followups(sid) if scope_digest(f.get('scope')) == scope_digest(scope)]
            previous = [f for f in same_scope if (f.get('fields') or {}).get('slice_id') == args.slice_id]
            if previous: raise ValueError('SLICE_SCOPE FAIL: this slice already has a published document; update it instead of creating another')
            existing = [f for f in same_scope if f.get('kind') == 'slice']
            if len(existing) >= scope['slice_count']: raise ValueError('SLICE_SCOPE FAIL: original slice count reached')
    if argv[0] == 'pr' and argv[1] in {'merge', 'close'}:
        target = target_info[0]
        gates = asyncio.run(inspect(store, sid, target))
        if any(g['status'] != 'PASS' for row in gates for g in row['gates']):
            if not _nonrequired_quota_merge_allowed(store,sid,target,argv,gates,getattr(args,'nonrequired_quota_approval',None)):
                raise ValueError('PR_GATE FAIL: merge/close requires every separate PR gate to pass')
        if argv[1] == 'merge':
            expected_head = gates[0]['head_sha']
            supplied = options.get('--match-head-commit')
            if supplied and supplied != expected_head: raise ValueError('Merge candidate differs from freshly verified head')
            if not supplied: argv += ['--match-head-commit', expected_head]
    if creation:
        # Posting without a registered scope is allowed only for ordinary engineering PRs/issues.
        result = github_run(['gh', *argv], runner=subprocess.run, cwd=args.cwd, text=True, capture_output=True, timeout=90)
        if result.returncode: raise RuntimeError(result.stderr.strip())
        print(result.stdout, end='')
        # gh create prints its actual target as a URL line; body/reference URLs
        # and diagnostic prose cannot establish ownership.
        matches = [REF.fullmatch(line.strip().rstrip('/')) for line in result.stdout.splitlines()]
        matches = [match for match in matches if match]
        if len(matches) != 1: raise RuntimeError('GITHUB_DATA UNKNOWN: creation succeeded but returned no unique target URL; reconcile before completion')
        repository, typ, number = matches[0].groups()
        if typ != ('pull' if argv[0] == 'pr' else 'issues'):
            raise RuntimeError('GITHUB_DATA UNKNOWN: returned creation target kind differs from the actual command')
        number = int(number)
        if typ == 'pull': _gateway_ownership(store, args, sid, repository, number, 'gateway_creation', argv[:2])
        if kind:
            from .github import collect_issue, collect_pr
            snapshot = asyncio.run(asyncio.wait_for((collect_issue if typ == 'issues' else collect_pr)(repository, number), 65))
            followup = store.register_followup(sid, repository, number, _posted_at(snapshot), scope, kind=kind,
                issue_number=number if typ == 'issues' else None, publication_notice=args.kind == 'prd')
            if args.slice_id: store.update_followup(followup['id'], followup['status'], {'slice_id': args.slice_id})
        store.audit('github_mutation', {'session_id': sid, 'operation': argv[:2], 'repository': repository, 'number': number})
        return None
    run_options = {'cwd': args.cwd, 'timeout': 90}
    if admitted_api_body is not None:
        run_options.update(input=admitted_api_body, text=True)
    result = github_run(['gh', *argv], runner=subprocess.run, **run_options)
    if result.returncode: raise RuntimeError(f'GitHub operation exited {result.returncode}')
    # Reads and URLs mentioned in comment/body/diagnostic data do not own PRs.
    if mutation and target_info and not target_info[3]:
        _gateway_ownership(store, args, sid, target_info[1], target_info[2], 'gateway_write', argv[:2])
        if argv[0:2] == ['pr', 'merge']:
            from .monitor import retire
            from .closure import refresh as refresh_closeouts, task_gates as closeout_gates
            from .github import collect_pr
            # Capture the actual pre-merge links before replacing their snapshot.
            cached = store.snapshot(target_info[1], target_info[2]) or {}
            premerge = cached.get('snapshot') or {}
            snapshot = asyncio.run(asyncio.wait_for(collect_pr(target_info[1], target_info[2]), 65))
            from .policy import evaluate_pr
            if snapshot.get('merged') is not True:
                raise ValueError('Merge command did not establish a merged PR; completion is unverified')
            from .closure import record
            with store._connect() as db:
                owner = db.execute('SELECT owner_session FROM pr_watch WHERE repository=? AND number=?', (target_info[1], target_info[2])).fetchone()
                if owner:
                    record(db, owner['owner_session'], target_info[1], target_info[2], premerge)
            store.set_snapshot(target_info[1], target_info[2], snapshot, evaluate_pr(snapshot, store.dispositions(target_info[1], target_info[2])))
            retire(store, target_info[1], target_info[2], dict(state='closed', merged=True, head={'sha': snapshot['head_sha']}))
            owner_sid = store.session(sid)['root_session']
            asyncio.run(refresh_closeouts(store, owner_sid, force=True))
            remaining = [g for g in closeout_gates(store, owner_sid) if (g['evidence']['repository'], g['evidence']['number']) == (target_info[1], target_info[2])]
            if remaining: raise ValueError('ISSUE_CLOSED incomplete: ' + '; '.join(g['details'][0] for g in remaining))
        elif argv[0:2] == ['issue', 'close']:
            from .closure import refresh
            asyncio.run(refresh(store, store.session(sid)['root_session'], force=True))
    store.audit('github_operation', {'session_id': sid, 'operation': argv[:2]})
    return None


def task_gates(store, sid):
    from .hook import _scope_gate
    jobs=store.followups(sid)
    due=[j for j in jobs if j['status']!='complete' and j['due_at']<=time.time()]
    future=[j for j in jobs if j['status']!='complete' and j['due_at']>time.time()]
    errors=_scope_gate(store,sid)
    result = [
        {'gate':'PRD_FOLLOWUP','status':'WAIT' if due else ('SCHEDULED' if future else 'PASS'),'details':[{'id':j['id'],'due_at':j['due_at'],'status':j['status']} for j in due+future]},
        {'gate':'SLICE_SCOPE','status':'FAIL' if errors else 'PASS','details':errors,'evidence':store.get_scope(sid)}
    ]
    if sid:
        from .workflow import task_progress
        result += task_progress(store, sid)
        from .closure import task_gates as closeout_gates
        result += closeout_gates(store, sid)
    return result


def main():
    parser = argparse.ArgumentParser(prog='caller-pr-gates')
    sub = parser.add_subparsers(dest='command', required=True)
    def owned(name):
        p = sub.add_parser(name)
        p.add_argument('--session')
        return p
    p = owned('register'); p.add_argument('url'); p.add_argument('--client', choices=['codex','claude'], default='codex'); p.add_argument('--cwd', default=os.getcwd())
    p = owned('link-issues'); p.add_argument('parent', help='Same-repository parent issue URL'); p.add_argument('child', help='Owned PRD/slice issue URL; existing parents are never replaced')
    p = owned('scope'); p.add_argument('--file', required=True); p.add_argument('--work-item', help='Stable ID for a separately authorized work item; existing baselines remain immutable')
    p = owned('resolve'); p.add_argument('--pr', required=True); p.add_argument('--thread', required=True)
    p = owned('address'); p.add_argument('--pr', required=True); p.add_argument('--comment', required=True); p.add_argument('--disposition', choices=['fixed','nonfix'], required=True); p.add_argument('--commit'); p.add_argument('--reason', required=True); p.add_argument('--reply-url')
    p.add_argument('--cwd', default=os.getcwd())
    p.add_argument('--closed-head', help='Record a review receipt for the retained owner at this exact closed PR head')
    p = owned('post-review'); p.add_argument('url'); p.add_argument('--kind', choices=['prd','slice'], default='prd'); p.add_argument('--slice-id')
    p = sub.add_parser('review-read'); p.add_argument('--followup', required=True)
    p = owned('status'); p.add_argument('--fresh', action='store_true'); p.add_argument('--pr')
    p = owned('publication-reconcile'); p.add_argument('--cwd', required=True)
    p = owned('qualify-successor'); p.add_argument('--pr', required=True); p.add_argument('--successor', required=True)
    p.add_argument('--cwd', required=True, help='Exact recorded final qualification checkout')
    p.add_argument('--file', required=True, help='Explicit coverage of the original scope and declaration differences')
    p = owned('github'); p.add_argument('--client', choices=['codex','claude'], default='codex'); p.add_argument('--cwd', default=os.getcwd()); p.add_argument('--kind', choices=['prd','slice']); p.add_argument('--slice-id'); p.add_argument('--target'); p.add_argument('--nonrequired-quota-approval',help='Actual explicit human authorization for normal merge with a verified non-required account failure'); p.add_argument('arguments', nargs=argparse.REMAINDER)
    p = owned('retry'); p.add_argument('--followup'); p.add_argument('--pr')
    p = owned('blocked'); p.add_argument('--pr', required=True); p.add_argument('--evidence-url', required=True); p.add_argument('--reason', required=True)
    p = owned('validation-plan'); p.add_argument('--pr', required=True); p.add_argument('--file', required=True); p.add_argument('--cwd', default=os.getcwd())
    p.add_argument('--merged-head', help='Continue the retained plan on this exact original merged PR head')
    p.add_argument('--from-head', help='Append a final merged-head epoch from this proven earlier source, preserving the necessary plan')
    p = owned('validate'); p.add_argument('--pr', required=True); p.add_argument('--check', required=True); p.add_argument('--cwd', default=os.getcwd())
    p.add_argument('--merged-head', help='Continue the retained plan on this exact original merged PR head')
    p.add_argument('--background', action='store_true')
    p.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    p = sub.add_parser('supervise'); p.add_argument('--execute', action='store_true'); p.add_argument('--verbose', action='store_true')
    sub.add_parser('hook')
    args = parser.parse_args()
    if getattr(args,'session',None):os.environ['CALLER_GATE_SESSION']=args.session
    os.environ['CALLER_GATE_OPERATION']=args.command+(':worker' if getattr(args,'worker',False) else '')
    store = Store()
    output = None
    try:
        if args.command == 'hook':
            from .hook import main as hook_main
            return hook_main()
        if args.command == 'register':
            repo, num, issue = reference(args.url)
            if issue: raise ValueError('Register PR URLs; use post-review for PRD issues')
            output = store.register(session_id(args), args.client, repo, num, args.cwd)
        elif args.command == 'scope':
            scope = json.loads(Path(args.file).read_text())
            if not scope.get('approval_evidence'): raise ValueError('scope needs approval_evidence quoting the user-agreed proposal')
            previous = store.get_scope(session_id(args), work_item=args.work_item)
            scope.setdefault('approved_at', previous['approved_at'] if previous else time.time())
            store.record_scope(session_id(args), scope, work_item=args.work_item)
            output = {'scope': store.get_scope(session_id(args)), 'immutable': True, 'work_item': args.work_item or 'selected'}
        elif args.command == 'link-issues':
            from .hierarchy import link
            output = asyncio.run(link(store, session_id(args), args.parent, args.child))
        elif args.command == 'resolve':
            from .github import collect_review
            from .policy import evaluate_review
            repo,num,issue=reference(args.pr)
            if issue: raise ValueError('GitHub issues have no resolvable review threads; use a reply')
            sid=session_id(args)
            if not any(p['repository']==repo and p['number']==num for p in store.prs(sid)): raise ValueError('PR is not owned by this task')
            snap=asyncio.run(asyncio.wait_for(collect_review(repo,num),65))
            members=[c for c in snap['comments'] if c.get('thread_id')==args.thread]
            if not members: raise ValueError('Thread is absent from the complete live PR inventory')
            # Require evidenced handling for this thread, including same-account human comments.
            handling=next(g for g in evaluate_review(snap,store.dispositions(repo,num)) if g['gate']=='REVIEW_HANDLED')
            if handling['status']=='UNKNOWN': raise ValueError('REVIEW_HANDLED UNKNOWN: live handling evidence cannot be verified')
            member_ids={str(c['id']) for c in members}
            if any(str(o['id']) in member_ids and not o['handled'] for o in handling['evidence'].get('obligations',[])):
                raise ValueError('REVIEW_HANDLED FAIL: record a real fix/nonfix for every comment in this thread before resolving')
            query='mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}'
            result=github_run(['gh','api','graphql','-f','query='+query,'-f','id='+args.thread],runner=subprocess.run,text=True,capture_output=True,timeout=30)
            if result.returncode: raise RuntimeError(result.stderr.strip())
            output=json.loads(result.stdout)
            if output.get('errors'): raise RuntimeError('GitHub refused review resolution: '+str(output['errors']))
            store.audit('review_thread_resolved',{'session_id':sid,'repository':repo,'number':num,'thread_id':args.thread})
        elif args.command == 'address':
            from .github import collect_issue, collect_review
            repo, num, issue = reference(args.pr)
            sid=session_id(args)
            owner = None
            if issue:
                if args.closed_head is not None: raise ValueError('--closed-head applies only to retained PR review receipts')
                if not any(j['repository']==repo and j['number']==num for j in store.followups(sid)):raise ValueError('Document is not owned by this task')
            else:
                from .workflow import review_owner
                owner = review_owner(store,sid,repo,num,args.cwd,args.closed_head)
            snap = asyncio.run(asyncio.wait_for((collect_issue if issue else collect_review)(repo, num), 65))
            comments = [c for c in snap['comments'] if str(c['id']) == args.comment]
            if len(comments) != 1: raise ValueError('Comment ID not found in the complete live review inventory')
            receipt = {'comment_id': args.comment, 'comment_updated_at': comments[0]['updated_at'], 'disposition': args.disposition, 'commit_sha': args.commit, 'reason': args.reason, 'reply_url': args.reply_url}
            if issue and args.disposition == 'fixed':
                receipt.update(document_revision=snap['document_edited_at'], content_sha256=hashlib.sha256(snap['body'].encode()).hexdigest())
                receipt['commit_sha'] = 'document:' + receipt['content_sha256']
            if args.closed_head is not None:
                from .copilot import is_service_review
                from .policy import _handled, _concluded, evaluate_review
                if review_owner(store,sid,repo,num,args.cwd,args.closed_head,snapshot=snap) != owner:
                    raise ValueError('Retained review ownership changed during collection')
                gates = evaluate_review(snap, [*store.dispositions(repo,num), receipt])
                obligations = gates[0]['evidence'].get('obligations', [])
                target = next((item for item in obligations if str(item['id']) == args.comment), None)
                verified = bool(target and target['handled'] and target['concluded'])
                if not verified and is_service_review(comments[0]):
                    # A previously posted acknowledgement still needs its own
                    # verifiable parent receipt; the service notice adds no duty.
                    verified = (_handled(snap, comments[0], receipt, None)[0]
                                and _concluded(snap, comments[0], receipt, reply_only=True)[0])
                if not verified:
                    raise ValueError('Closed review receipt needs a verified current fix/nonfix and public conclusion: '
                                     + str(target or 'No independent review obligation'))
                receipt.update(closed_head=args.closed_head,cwd=owner['cwd'],review_collected_at=snap['collected_at'],
                               retirement_id=owner['identity']['retirement']['id'],
                               checkout_anchor=owner['identity']['checkout_anchor'])
                store.disposition(sid, repo, num, receipt, closed_owner=owner)
                output = {'receipt': receipt, 'review_gates': gates, 'completion_verified': False}
            else:
                store.disposition(sid, repo, num, receipt)
                output = receipt
        elif args.command == 'post-review':
            from .hook import _register_document
            repo, num, issue = reference(args.url)
            sid = session_id(args)
            scope = store.get_scope(sid)
            existing = [j for j in store.followups(sid) if j['repository'] == repo and j['number'] == num
                        and j['kind'] == args.kind and bool(j.get('issue_number')) == bool(issue)]
            if existing:
                scope = store.followup_scope(existing[-1])
            if args.kind == 'slice' and (not scope or args.slice_id not in scope['slice_ids']): raise ValueError('Slice ID must be in the original approved proposal')
            _register_document(store, sid, repo, num, issue, kind=args.kind)
            jobs = [j for j in store.followups(sid) if j['repository'] == repo and j['number'] == num]
            if args.slice_id and jobs: store.update_followup(jobs[-1]['id'], jobs[-1]['status'], {'slice_id': args.slice_id})
            output = store.followups(sid)
        elif args.command == 'review-read':
            from .github import collect_issue, collect_review
            matches = [f for f in store.followups() if f['id'] == args.followup]
            if len(matches) != 1: raise ValueError('Followup ID not found')
            job = matches[0]
            if time.time() < job['due_at']: raise ValueError('The 30-minute review check is not due yet')
            snap = asyncio.run(asyncio.wait_for((collect_issue if job.get('issue_number') else collect_review)(job['repository'], job['number']), 65))
            scope = store.followup_scope(job)
            if scope != job['scope']: raise ValueError('SLICE_SCOPE FAIL: scope differs from original proposal')
            receipt = {'read_at': time.time(), 'head_sha': snap.get('head_sha') or snap.get('updated_at'), 'comment_ids': [str(c['id']) for c in snap['comments']], 'scope_digest': scope_digest(scope)}
            fields = {'review_read': receipt}
            if job.get('lease_token'): fields['lease_token'] = job['lease_token']
            store.update_followup(job['id'], job['status'], fields)
            output = {'followup_id': job['id'], 'review_read': receipt, 'comments': snap['comments'], 'scope': scope}
        elif args.command == 'status':
            sid = args.session or os.getenv('CODEX_THREAD_ID')
            rows = session_prs(store, sid)
            if args.fresh:
                from .closure import refresh
                asyncio.run(refresh(store, sid, force=True))
                output = {'prs': asyncio.run(inspect(store, sid, args.pr)), 'task_gates': task_gates(store, sid) if sid else []}
            else: output = {'task_gates': task_gates(store, sid) if sid else [], 'prs': rows, 'snapshots': [store.snapshot(p['repository'], p['number']) for p in rows], 'followups': store.followups(sid), 'scope': store.get_scope(sid) if sid else None}
            if sid:
                output['external_blockers'] = [hold for p in rows if (hold := store.external_blocker(sid, p['repository'], p['number']))]
                output['continuations'] = [{k: receipt.get(k) for k in ('repository','number','idle_retry_outcome','awaiting_user_turn','awaiting_validation')}
                                           for p in rows if (receipt := store.recovery(p['repository'], p['number'], sid))
                                           and (receipt.get('idle_retry_outcome') or receipt.get('awaiting_user_turn') or receipt.get('awaiting_validation'))]
                if args.fresh:
                    from .publication import refresh as refresh_publication
                    output['publication'] = asyncio.run(asyncio.wait_for(refresh_publication(store, sid), timeout=35))
        elif args.command == 'publication-reconcile':
            from .publication import refresh as refresh_publication
            output = asyncio.run(asyncio.wait_for(refresh_publication(store, session_id(args), cwd=args.cwd), timeout=35))
        elif args.command == 'qualify-successor':
            from .workflow import qualify_successor
            repo, number, issue = reference(args.pr)
            final_repo, final, final_issue = reference(args.successor)
            if issue or final_issue or repo != final_repo:
                raise ValueError('Final coverage requires two recorded PRs in the same repository')
            payload = json.loads(Path(args.file).read_text())
            output = asyncio.run(asyncio.wait_for(qualify_successor(store, session_id(args), repo, number,
                                final, args.cwd, payload), timeout=90))
        elif args.command == 'blocked':
            from .github import collect_pr
            from .policy import evaluate_pr
            from .external import hold_verified_failure
            sid = session_id(args)
            repo, num, issue = reference(args.pr)
            if issue or not any(p['repository'] == repo and p['number'] == num for p in store.prs(sid)):
                raise ValueError('PR is not owned by this task')
            snap = asyncio.run(asyncio.wait_for(collect_pr(repo, num), 65))
            gates = evaluate_pr(snap, store.dispositions(repo, num))
            store.set_snapshot(repo, num, snap, gates)
            output = asyncio.run(asyncio.wait_for(hold_verified_failure(store, sid, snap, gates,
                evidence_url=args.evidence_url, reason=args.reason), 35))
            if not output: raise ValueError('No independently verified external account failure covers the remaining blockers')
            output = {'handoff': output, 'obligation_complete': False, 'gates': gates}
        elif args.command in {'validation-plan', 'validate'}:
            from .workflow import validation_owner, run_check, save_plan, launch_check
            from . import progress
            repo, num, issue = reference(args.pr)
            if issue: raise ValueError('Local validation plans apply to PRs, not review documents')
            sid = session_id(args)
            if args.command == 'validation-plan' and args.from_head is not None and args.merged_head is None:
                raise ValueError('--from-head requires --merged-head and the retained necessary plan')
            validation_owner(store,sid,repo,num,args.cwd,args.merged_head)
            os.environ['CALLER_GATE_SESSION']=sid
            if args.command=='validation-plan':
                payload=progress.normalize_plan(json.loads(Path(args.file).read_text()),args.cwd)
            else:
                row=store.validation_plan(sid,repo,num,args.cwd)
                if row is None:raise ValueError('Declare necessary checks before remote validation preflight')
                if args.check not in {c['id'] for c in row['plan']['checks']}:raise ValueError('Check is absent from declared plan')
                local=progress.working_state(args.cwd)
                if not local['clean']:raise ValueError('PUBLISH_REQUIRED: commit and push before local validation')
                digest=progress.fingerprint(args.cwd,row['plan'])
                if args.merged_head is not None and digest!=row['input_digest']:
                    raise ValueError('Post-merge validation inputs changed; source repairs need their own scoped PR')
                check=next(c for c in row['plan']['checks'] if c['id']==args.check)
                if 'input_digest' in check:
                    digest=progress.check_fingerprint(args.cwd,row['plan'],check)
                    if digest!=check['input_digest']:
                        raise ValueError('Frozen qualification control inputs changed; retain their original pins and results')
                if any(r['check_id']==args.check and r['input_digest']==digest and r['status'] in {'running','pass','fail'} for r in store.validation_evidence(row['plan_id'])):
                    raise ValueError('This check already ran on unchanged inputs; inspect its recorded result')
            from .github import collect_validation
            try:
                preflight=asyncio.run(asyncio.wait_for(collect_validation(repo,num),65))
                store.set_preflight(repo,num,preflight)
                from .policy import branch_freshness_gate
                freshness=branch_freshness_gate(preflight)
                if args.merged_head is not None:
                    validation_owner(store,sid,repo,num,args.cwd,args.merged_head,preflight=True)
                else:
                    if preflight.get('state')!='open' or preflight.get('merged') is not False:raise ValueError('REMOTE_READ_REQUIRED: owned PR is no longer open')
                    if freshness['status']!='PASS':
                        phase='SYNC_BASE_REQUIRED' if freshness['status']=='FAIL' else 'REMOTE_READ_REQUIRED'
                        raise ValueError(phase+': '+'; '.join(freshness['details']))
                    cached=(store.snapshot(repo,num) or {}).get('snapshot') or {}
                    if cached.get('head_sha')!=preflight.get('head_sha') or not cached.get('actor_login'):
                        refreshed=asyncio.run(inspect(store,sid,args.pr))
                        if not refreshed or any(g['gate']=='GITHUB_DATA' for r in refreshed for g in r['gates']):
                            raise ValueError('Initial complete remote evidence is unavailable')
            except Exception:
                store.set_preflight(repo,num,{'profile':'validation','collected_at':None})
                raise
            continuation={'merged_head':args.merged_head} if args.merged_head is not None else {}
            if args.command == 'validation-plan' and args.from_head is not None:
                continuation['from_head'] = args.from_head
            if args.command=='validation-plan':output=save_plan(store,sid,repo,num,args.cwd,payload,**continuation)
            elif not args.worker and (args.background or len(store.prs(sid))>1):
                output=launch_check(store,sid,repo,num,args.cwd,args.check,**continuation)
            else:output=run_check(store,sid,repo,num,args.cwd,args.check,manage_process_group=True,**continuation)
        elif args.command == 'retry':
            if bool(args.followup) == bool(args.pr): raise ValueError('Choose exactly one --followup ID or --pr URL')
            sid = session_id(args)
            if args.followup:
                if not any(j['id']==args.followup for j in store.followups(sid)): raise ValueError('Followup is not owned by this task')
                output = {'retried': store.retry_followup(args.followup)}
                job = next(j for j in store.followups(sid) if j['id'] == args.followup)
                if not job.get('issue_number'):
                    store.release_external_blocker(sid, job['repository'], job['number'], explicit_retry=True)
            else:
                repo,num,issue=reference(args.pr)
                if issue: raise ValueError('PR URL required')
                active = any(p['repository']==repo and p['number']==num for p in store.prs(sid))
                with store._connect() as db:
                    closeout = db.execute("SELECT 1 FROM issue_closeouts WHERE owner_session=? AND repository=? AND number=? AND state!='complete'", ((store.session(sid) or {}).get('root_session', sid), repo, num)).fetchone()
                if not active and not closeout: raise ValueError('PR is not owned by this task')
                session=store.session(sid);sid=session['root_session'] if session else sid
                recovery=store.recovery(repo,num,sid) or {}
                if float(recovery.get('lease_until') or 0)>time.time(): raise ValueError('Recovery still has a live worker lease')
                from .monitor import retry
                retry(store, sid, repo, num)
                from .closure import retry as retry_closeout
                retry_closeout(store, sid, repo, num)
                store.release_external_blocker(sid, repo, num, explicit_retry=True)
                output=store.record_recovery(repo,num,sid,{'attempts':0,'lease_until':0,'next_attempt_at':0,'state':'pending','error':None})
                store.audit('recovery_explicit_retry',{'repository':repo,'number':num,'session_id':sid})
        elif args.command == 'github': output = gateway(args, store)
        elif args.command == 'supervise':
            from .supervisor import tick
            output = tick(store=store, execute=args.execute)
            if asyncio.iscoroutine(output): output = asyncio.run(output)
            if not args.verbose:
                output['refreshed']=[dict(repository=row['repository'],number=row['number'],issue=row.get('issue',False),gates=[{k:g[k] for k in ('gate','status','details')} for g in row['gates']]) for row in output['refreshed']]
                output['actions']=[{k:action[k] for k in ('id','repository','number','session_id','client','dispatched','delivery') if k in action} for action in output['actions']]
        if output is not None: print(json.dumps(output, indent=2))
        if args.command == 'validate' and 'run' in output and output['run']['status'] != 'pass':
            raise SystemExit(1)
    except Exception as exc:
        print(f'caller-pr-gates: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)

if __name__ == '__main__': main()


