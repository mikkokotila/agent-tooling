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


def _cancelled_readiness_rerun_allowed(store, sid, repo, number, issue, endpoint, method, argv):
    """Retry only this owned PR's cancelled readiness gate; never a review."""
    match = re.fullmatch(re.escape(f'repos/{repo}/actions/') + r'(jobs|runs)/([1-9][0-9]*)/rerun', endpoint)
    if issue or not match or method != 'POST' or argv != ['api', endpoint, '--method', 'POST']:
        return False
    from .workflow import owned_pr
    from .github import collect_pr
    from .policy import branch_freshness_gate
    owned_pr(store, sid, repo, number)
    snapshot = asyncio.run(collect_pr(repo, number))
    head = snapshot.get('head_sha')
    if (snapshot.get('state') != 'open' or snapshot.get('merged') is not False
            or snapshot.get('head_sha_after') != head or branch_freshness_gate(snapshot)['status'] != 'PASS'):
        raise ValueError('Readiness retry requires fresh open-PR head and target ancestry')
    def read(path):
        result = github_run(['gh', 'api', path], runner=subprocess.run, text=True, capture_output=True, check=True, timeout=30)
        return json.loads(result.stdout)
    item = read(endpoint.removesuffix('/rerun'))
    if (item.get('name') != 'pr_merge_readiness' or item.get('status') != 'completed'
            or item.get('conclusion') != 'cancelled' or item.get('head_sha') != head):
        return False
    run = read(f'repos/{repo}/actions/runs/{item["run_id"]}') if match[1] == 'jobs' else item
    if (run.get('path') != '.github/workflows/pr_merge_readiness.yml' or run.get('head_sha') != head
            or run.get('status') != 'completed' or run.get('conclusion') != 'cancelled'
            or run.get('run_attempt') != item.get('run_attempt')
            or not any(pr.get('url') == f'https://api.github.com/repos/{repo}/pulls/{number}'
                       for pr in run.get('pull_requests', []))):
        return False
    after = read(f'repos/{repo}/pulls/{number}')
    base = read(f'repos/{repo}/branches/{snapshot["base_ref"]}')
    if (after.get('state') != 'open' or after.get('merged') is not False
            or (after.get('head') or {}).get('sha') != head
            or (after.get('base') or {}).get('ref') != snapshot['base_ref']
            or (base.get('commit') or {}).get('sha') != snapshot['base_sha']):
        raise ValueError('PR head or target branch changed before readiness retry')
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


async def inspect(store, sid=None, url=None):
    from .github import collect_pr
    from .policy import evaluate_pr
    rows = store.prs(sid)
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
            result.append({'repository': row['repository'], 'number': row['number'], 'gates': [{'gate': 'GITHUB_DATA', 'status': 'UNKNOWN', 'details': [str(exc)[:900]], 'evidence': {}}]})
    return result


def gateway(args, store):
    sid = session_id(args)
    argv = args.arguments
    if argv and argv[0] == '--': argv = argv[1:]
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
        if api_write:
            if not args.target: raise ValueError('GitHub API writes need --target with the owned PR/document URL')
            repo, num, issue = reference(args.target)
            allowed_reply = _api_reply_allowed(repo, num, issue, endpoint, method or 'POST', field_values, input_file)
            allowed_readiness_rerun = _cancelled_readiness_rerun_allowed(
                store, sid, repo, num, issue, endpoint, method, argv)
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
            if not (allowed_reply or allowed_runner_registration or allowed_release_email_config or allowed_readiness_rerun):
                raise ValueError('API gateway permits review replies and owned Origo runner registration only; use resolve for thread resolution, gh pr/issue for other writes')
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
            raise ValueError('ISSUE_CLOSURE FAIL: the PR body must close its actual existing issue, for example Closes #123; do not create an issue to satisfy this check')
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
            followup = store.register_followup(sid, repository, number, _posted_at(snapshot), scope, kind=kind, issue_number=number if typ == 'issues' else None)
            if args.slice_id: store.update_followup(followup['id'], followup['status'], {'slice_id': args.slice_id})
        store.audit('github_mutation', {'session_id': sid, 'operation': argv[:2], 'repository': repository, 'number': number})
        return None
    result = github_run(['gh', *argv], runner=subprocess.run, cwd=args.cwd, timeout=90)
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
    p = owned('scope'); p.add_argument('--file', required=True); p.add_argument('--work-item', help='Stable ID for a separately authorized work item; existing baselines remain immutable')
    p = owned('resolve'); p.add_argument('--pr', required=True); p.add_argument('--thread', required=True)
    p = owned('address'); p.add_argument('--pr', required=True); p.add_argument('--comment', required=True); p.add_argument('--disposition', choices=['fixed','nonfix'], required=True); p.add_argument('--commit'); p.add_argument('--reason', required=True); p.add_argument('--reply-url')
    p = owned('post-review'); p.add_argument('url'); p.add_argument('--kind', choices=['prd','slice'], default='prd'); p.add_argument('--slice-id')
    p = sub.add_parser('review-read'); p.add_argument('--followup', required=True)
    p = owned('status'); p.add_argument('--fresh', action='store_true'); p.add_argument('--pr')
    p = owned('github'); p.add_argument('--client', choices=['codex','claude'], default='codex'); p.add_argument('--cwd', default=os.getcwd()); p.add_argument('--kind', choices=['prd','slice']); p.add_argument('--slice-id'); p.add_argument('--target'); p.add_argument('arguments', nargs=argparse.REMAINDER)
    p = owned('retry'); p.add_argument('--followup'); p.add_argument('--pr')
    p = owned('blocked'); p.add_argument('--pr', required=True); p.add_argument('--evidence-url', required=True); p.add_argument('--reason', required=True)
    p = owned('validation-plan'); p.add_argument('--pr', required=True); p.add_argument('--file', required=True); p.add_argument('--cwd', default=os.getcwd())
    p = owned('validate'); p.add_argument('--pr', required=True); p.add_argument('--check', required=True); p.add_argument('--cwd', default=os.getcwd())
    p.add_argument('--background', action='store_true')
    p.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    p = sub.add_parser('supervise'); p.add_argument('--execute', action='store_true'); p.add_argument('--verbose', action='store_true')
    sub.add_parser('hook')
    args = parser.parse_args()
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
        elif args.command == 'resolve':
            from .github import collect_pr
            from .policy import evaluate_review
            repo,num,issue=reference(args.pr)
            if issue: raise ValueError('GitHub issues have no resolvable review threads; use a reply')
            sid=session_id(args)
            if not any(p['repository']==repo and p['number']==num for p in store.prs(sid)): raise ValueError('PR is not owned by this task')
            snap=asyncio.run(asyncio.wait_for(collect_pr(repo,num),65))
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
            from .github import collect_issue, collect_pr
            repo, num, issue = reference(args.pr)
            snap = asyncio.run(asyncio.wait_for((collect_issue if issue else collect_pr)(repo, num), 65))
            comments = [c for c in snap['comments'] if str(c['id']) == args.comment]
            if len(comments) != 1: raise ValueError('Comment ID not found in the complete live review inventory')
            receipt = {'comment_id': args.comment, 'comment_updated_at': comments[0]['updated_at'], 'disposition': args.disposition, 'commit_sha': args.commit, 'reason': args.reason, 'reply_url': args.reply_url}
            if issue and args.disposition == 'fixed':
                receipt.update(document_revision=snap['document_edited_at'], content_sha256=hashlib.sha256(snap['body'].encode()).hexdigest())
                receipt['commit_sha'] = 'document:' + receipt['content_sha256']
            store.disposition(session_id(args), repo, num, receipt)
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
            from .github import collect_issue, collect_pr
            matches = [f for f in store.followups() if f['id'] == args.followup]
            if len(matches) != 1: raise ValueError('Followup ID not found')
            job = matches[0]
            if time.time() < job['due_at']: raise ValueError('The 30-minute review check is not due yet')
            snap = asyncio.run(asyncio.wait_for((collect_issue if job.get('issue_number') else collect_pr)(job['repository'], job['number']), 65))
            scope = store.followup_scope(job)
            if scope != job['scope']: raise ValueError('SLICE_SCOPE FAIL: scope differs from original proposal')
            receipt = {'read_at': time.time(), 'head_sha': snap.get('head_sha') or snap.get('updated_at'), 'comment_ids': [str(c['id']) for c in snap['comments']], 'scope_digest': scope_digest(scope)}
            fields = {'review_read': receipt}
            if job.get('lease_token'): fields['lease_token'] = job['lease_token']
            store.update_followup(job['id'], job['status'], fields)
            output = {'followup_id': job['id'], 'review_read': receipt, 'comments': snap['comments'], 'scope': scope}
        elif args.command == 'status':
            sid = args.session or os.getenv('CODEX_THREAD_ID')
            if args.fresh:
                from .closure import refresh
                asyncio.run(refresh(store, sid, force=True))
                output = {'prs': asyncio.run(inspect(store, sid, args.pr)), 'task_gates': task_gates(store, sid) if sid else []}
            else: output = {'task_gates': task_gates(store, sid) if sid else [], 'prs': store.prs(sid), 'snapshots': [store.snapshot(p['repository'], p['number']) for p in store.prs(sid)], 'followups': store.followups(sid), 'scope': store.get_scope(sid) if sid else None}
            if sid:
                output['external_blockers'] = [hold for p in store.prs(sid) if (hold := store.external_blocker(sid, p['repository'], p['number']))]
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
            from .workflow import owned_pr, run_check, save_plan, launch_check
            repo, num, issue = reference(args.pr)
            if issue: raise ValueError('Local validation plans apply to PRs, not review documents')
            sid = session_id(args)
            owned_pr(store, sid, repo, num)
            if args.command == 'validation-plan':
                asyncio.run(inspect(store, sid, args.pr))
                output = save_plan(store, sid, repo, num, args.cwd, json.loads(Path(args.file).read_text()))
            else:
                refreshed = asyncio.run(inspect(store, sid, args.pr))
                if (not refreshed or any(g.get('gate') == 'GITHUB_DATA' and g.get('status') == 'UNKNOWN'
                                         for row in refreshed for g in row.get('gates') or [])):
                    raise ValueError('Fresh remote PR and target-branch evidence is unavailable; local validation cannot use an earlier passing snapshot')
                if not args.worker and (args.background or len(store.prs(sid)) > 1):
                    output = launch_check(store, sid, repo, num, args.cwd, args.check)
                else:
                    output = run_check(store, sid, repo, num, args.cwd, args.check, manage_process_group=True)
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
