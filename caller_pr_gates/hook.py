from __future__ import annotations
from .prompts import render_prompt
import asyncio
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from .store import Store

CLI = str(Path.home() / '.local/bin/caller-pr-gates')
OLD_INTERFACE = str(Path.home() / '.local/share/caller-pr-stop-gate/bin/github-interface')
PR_URL = re.compile(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)')
ISSUE_URL = re.compile(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)')
DOC = re.compile(r'\b(?:PRD|product requirements document|slice(?:\s+\d+)?)\b', re.I)
GUARDED_GH = re.compile(r'\bgh\s+(?:pr\s+(?:create|edit|ready|merge|close|reopen|review|comment)|issue\s+(?:create|edit|close|reopen|delete|comment))\b')
GUARDED_API = re.compile(r'(?:/repos/[^\s\"\']+/(?:pulls|issues)|api\.github\.com|resolveReviewThread|createPullRequest|convertPullRequestToDraft)')
DURABLE_FOLLOWUP = re.compile(r'\ADurable (prd|slice) review followup ([0-9a-f-]{36})\. The 30-minute check is due\.\n')


def _remote_write_tool(tool):
    method=re.split(r'__|[.:/]',tool.lower())[-1]
    return ('github' in tool.lower() or 'pull_request' in tool.lower()) and bool(re.match(r'^(?:create|add|edit|update|merge|close|comment|reply|resolve|delete|submit|convert)',method))


def _deny(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                  'permissionDecisionReason': reason}, 'systemMessage': reason}


def _block(reason):
    return {'decision': 'block', 'reason': reason}


def _context(event, text):
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}


def _operation_command(command):
    # Heredoc bodies are file data, not executed shell commands.
    lines = command.splitlines(); kept = []; delimiter = None
    for line in lines:
        if delimiter is not None:
            if line.strip() == delimiter: delimiter = None
            continue
        kept.append(line)
        match = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if match: delimiter = match.group(1)
    return '\n'.join(kept)


def _command(event):
    args = event.get('tool_input') or {}
    return str(args.get('command') or args.get('cmd') or args.get('code') or '') if isinstance(args, dict) else ''


def _draft_command_text(event):
    """Inspect executable commands, never the contents of file edits."""
    source = _command(event)
    tool = str(event.get('tool_name') or '').lower()
    if tool in {'edit', 'write', 'multiedit', 'notebookedit', 'apply_patch', 'functions.apply_patch'}:
        return ''
    if tool in {'functions.exec', 'exec'}:
        from .progress import literal_exec_commands
        return '\n'.join(_operation_command(row['command']) for row in literal_exec_commands(source))
    return _operation_command(source)


def _direct_github_write(source):
    """Inspect invocations independently; a gateway mention cannot exempt a sibling write."""
    from .progress import literal_exec_commands
    nested = literal_exec_commands(source)
    commands = [row['command'] for row in nested] if nested else [source]
    for command in commands:
        try:
            lexer = shlex.shlex(_operation_command(command), posix=True, punctuation_chars=';&|()\n')
            lexer.whitespace = ' \t\r'; lexer.whitespace_split = True
            group = []
            groups = []
            for token in lexer:
                if token and all(c in ';&|()\n' for c in token):
                    if group: groups.append(group); group = []
                else: group.append(token)
            if group: groups.append(group)
        except ValueError:
            continue
        for group in groups:
            while group and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', group[0]): group.pop(0)
            if group and Path(group[0]).name in {'env', 'command', 'exec', 'sudo'}:
                group.pop(0)
                while group and (group[0].startswith('-') or re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', group[0])): group.pop(0)
            if not group: continue
            executable = Path(group[0]).name
            if executable in {'sh','bash','zsh'} and '-c' in group:
                index = group.index('-c')
                if index + 1 < len(group) and _direct_github_write(group[index + 1]): return True
            if executable == 'curl' and any('api.github.com/' in arg for arg in group[1:]):
                text = ' '.join(group[1:])
                if re.search(r'(?:-X\s*|--request(?:=|\s+))(?:POST|PATCH|PUT|DELETE)\b', text, re.I) or any(re.match(r'^(?:-d|-F|--data(?:-raw|-binary|-urlencode)?|--form)(?:=|$)', arg) for arg in group[1:]):
                    return True
            if executable in {'python','python3','node'} and any(arg in {'-c','-e','--eval'} for arg in group[1:]):
                text = ' '.join(group[1:])
                if GUARDED_API.search(text) and re.search(r'\.post\(|\.patch\(|\.delete\(|\.put\(|mutation\b', text, re.I):
                    return True
            if executable != 'gh': continue
            args = group[1:]
            while args and args[0] in {'--repo','-R','--hostname'}: args = args[2:]
            if len(args) >= 2 and args[0] in {'pr','issue'} and args[1] in {'create','edit','ready','merge','close','reopen','review','comment','delete'}:
                return True
            if args and args[0] == 'api':
                text = ' '.join(args)
                if 'mutation' in text or re.search(r'(?:-X\s*|--method(?:=|\s+))(?:POST|PATCH|PUT|DELETE)', text, re.I):
                    return True
                if any(arg in {'-f','-F','--field','--raw-field','--input'} for arg in args) and not re.search(r'(?:-X\s*|--method(?:=|\s+))GET\b', text, re.I) and 'graphql' not in args:
                    return True
    return False


def _text(event):
    return json.dumps({'input': event.get('tool_input'), 'output': event.get('tool_response', event.get('tool_output'))}, ensure_ascii=False)


def _session(event):
    return str(event.get('session_id') or os.getenv('CODEX_THREAD_ID') or os.getenv('CLAUDE_SESSION_ID') or '')


def _client():
    return os.getenv('CALLER_HOOK_CLIENT', 'codex')


def _cwd(event):
    return str(event.get('cwd') or os.getcwd())


def _succeeded(event):
    if event.get('hook_event_name') == 'PostToolUseFailure': return False
    out = event.get('tool_response', event.get('tool_output'))
    if isinstance(out, dict):
        if out.get('isError') or out.get('is_error'): return False
        if 'exit_code' in out: return out['exit_code'] == 0
        if out.get('session_id') or out.get('sessionId'): return False
        return True
    return True


def _ownership_evidence(event, store, target, origin, operation):
    store.audit('ownership_evidence', {'session_id': _session(event), 'target': target,
        'origin': origin, 'operation': operation, 'tool_name': event.get('tool_name'),
        'tool_call_id': event.get('tool_call_id') or event.get('tool_use_id'),
        'turn_id': event.get('turn_id')})


def _local_mutation(event):
    """Recognize explicit successful writes, never infer writes from Python."""
    if event.get('hook_event_name') != 'PostToolUse' or not _succeeded(event): return None
    tool = str(event.get('tool_name') or '')
    if tool in {'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'apply_patch', 'functions.apply_patch'}:
        return 'local_edit'
    command = _operation_command(_command(event))
    from .progress import literal_exec_commands, _js_tokens
    tokens = _js_tokens(command)
    if any(tokens[i:i + 4] == [('identifier', 'tools'), ('punctuation', '.'), ('identifier', 'apply_patch'), ('punctuation', '(')]
           for i in range(max(0, len(tokens) - 3))):
        return 'local_edit'
    nested = literal_exec_commands(command)
    commands = [item['command'] for item in nested] if nested else [command]
    for source in commands:
        source = _operation_command(source)
        try:
            lexer = shlex.shlex(source, posix=True, punctuation_chars=';&|()\n>')
            lexer.whitespace = ' \t\r'; lexer.whitespace_split = True
            arguments, groups = [], []
            for value in lexer:
                if value and all(character in ';&|()\n' for character in value):
                    if arguments: groups.append(arguments); arguments = []
                else: arguments.append(value)
            if arguments: groups.append(arguments)
        except ValueError:
            continue
        for arguments in groups:
            while arguments and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', arguments[0]): arguments = arguments[1:]
            if not arguments: continue
            executable = Path(arguments[0]).name
            if executable == 'git':
                index = 1
                while index < len(arguments) and arguments[index].startswith('-'):
                    flag = arguments[index]
                    index += 2 if flag in {'-C', '-c', '--git-dir', '--work-tree'} else 1
                if index < len(arguments) and arguments[index] in {'add', 'commit', 'push', 'merge', 'rebase', 'cherry-pick', 'apply', 'am', 'rm', 'mv', 'restore', 'reset'}:
                    return 'git_' + arguments[index]
            if executable in {'rm', 'mv', 'cp', 'touch', 'tee', 'install'}:
                return 'filesystem_write'
            if executable in {'sed', 'perl'} and any(re.match(r'^-(?:i|[a-z]*i)', value) for value in arguments[1:]):
                return 'inplace_edit'
            # A real output redirection is outside quoted command data. Do not
            # interpret a quoted > in a diagnostic message as a file write.
            quote = None; escaped = False
            for position, character in enumerate(source):
                if escaped: escaped = False; continue
                if character == '\\' and quote != "'": escaped = True; continue
                if quote:
                    if character == quote: quote = None
                elif character in {'"', "'", '`'}: quote = character
                elif character == '>':
                    suffix = source[position + 1:].lstrip('> ')
                    if suffix.startswith('/dev/null') or suffix.startswith('&'): continue
                    return 'filesystem_write'
    return None


def _mutation_dirs(event):
    """Use the mutated tool's target; unrelated caller/read directories are not ownership."""
    args = event.get('tool_input') or {}
    cwd = _cwd(event)
    if isinstance(args, dict):
        for key in ('file_path', 'filePath', 'path', 'notebook_path'):
            value = args.get(key)
            if isinstance(value, str) and value:
                path = Path(value).expanduser()
                if not path.is_absolute(): path = Path(cwd) / path
                return [str(path if path.is_dir() else path.parent)]
        cwd = str(args.get('workdir') or args.get('cwd') or cwd)
    command = _command(event)
    from .progress import literal_exec_commands, _js_tokens
    nested = literal_exec_commands(_operation_command(command))
    if nested:
        result = []
        for item in nested:
            child = {**event, 'tool_name': 'Bash', 'cwd': item.get('cwd') or cwd,
                     'tool_input': {'command': item['command']}}
            if _local_mutation(child): result.extend(_mutation_dirs(child))
        return list(dict.fromkeys(result))
    patch_sources = []
    if str(event.get('tool_name') or '') in {'apply_patch', 'functions.apply_patch'}:
        if isinstance(args, str): patch_sources.append(args)
        elif isinstance(args, dict): patch_sources.extend(value for key, value in args.items() if key in {'patch', 'input', 'code'} and isinstance(value, str))
    tokens = _js_tokens(command)
    for index in range(max(0, len(tokens) - 4)):
        if tokens[index:index + 4] == [('identifier', 'tools'), ('punctuation', '.'), ('identifier', 'apply_patch'), ('punctuation', '(')] and tokens[index + 4][0] == 'literal':
            patch_sources.append(tokens[index + 4][1])
    paths = []
    for patch_source in patch_sources:
        for raw in re.findall(r'^\*\*\* (?:Add|Update|Delete) File: (.+)$', patch_source, re.M):
            path = Path(raw).expanduser()
            if not path.is_absolute(): path = Path(cwd) / path
            paths.append(str(path.parent))
    if paths: return list(dict.fromkeys(paths))
    # Shell file writes own their actual destinations, rather than whatever
    # repository happened to be the caller's cwd (for example > /tmp/log).
    def directory(raw, prefix=''):
        if not raw or '$' in raw: return None
        try: path = Path(raw).expanduser()
        except (OSError, RuntimeError): return None
        if path.is_absolute():
            return str(path if path.is_dir() else path.parent)
        base = Path(cwd)
        # Redirections run before their command. Only completed commands can
        # have changed its directory; wrappers are outside this literal subset.
        changes = [m for m in re.finditer(r'(?:^|[;&|()\n]+)\s*cd(?=\s|[;&|()\n]|$)\s*', prefix)
                   if m.start() in unquoted]
        wrappers = [m for m in re.finditer(r'(?:^|[;&|()\n]+)\s*(?:command|builtin|eval|source|\.)\s', prefix)
                    if m.start() in unquoted]
        if ambiguous_directory and (changes or wrappers): return None
        end = max((i + 2 for i in range(len(prefix) - 1)
                   if prefix[i:i + 2] == '&&' and i in unquoted and i + 1 in unquoted), default=0)
        if any(m.end() <= end for m in wrappers): return None
        prefix = prefix[:end]
        # A relative redirect belongs to the shell directory at that point,
        # not the chat's launch checkout. Never infer a dynamic cd target.
        for match in changes:
            if match.end() > end: continue  # This command's cd has not run yet.
            lexer = shlex.shlex(prefix[match.end():], posix=True, punctuation_chars=';&|()\n')
            lexer.whitespace_split = True
            def word():
                raw = prefix[match.end() + lexer.instream.tell():].lstrip()
                return next(lexer), raw
            physical = False
            try:
                target, raw = word()
                while target in {'-L', '-P'}:
                    physical = target == '-P'
                    target, raw = word()
                if target == '--': target, raw = word()
                elif target.startswith('-'): return None  # Unsupported cd syntax is not a path.
            except (ValueError, StopIteration): return None
            if '$' in target or '`' in target or target == '-' or all(c in ';&|()\n' for c in target): return None
            target = Path(target)
            if raw.startswith('~'):
                if not re.match(r'~[A-Za-z0-9_.-]*(?:/|(?=\s|[;&|()\n]|$))', raw): return None
                if target.parts[0] in {'~-', '~+'}: return None  # Shell environment, not a username.
                try: target = target.expanduser()
                except (OSError, RuntimeError): return None
            target = target if target.is_absolute() else base / target
            try: base = target.resolve() if physical else Path(os.path.normpath(target))
            except (OSError, RuntimeError): return None
        path = base / path
        return str(path if path.is_dir() else path.parent)
    write_paths = []
    source = _operation_command(command)
    unquoted = set()
    redirects = []
    quote = None; escaped = False
    for index, character in enumerate(source):
        if escaped: escaped = False; continue
        if character == '\\' and quote != "'": escaped = True; continue
        if quote:
            if character == quote: quote = None
            continue
        if character in {'"', "'", '`'}: quote = character; continue
        unquoted.add(index)
        if character == '>': redirects.append(index)
    # Infer relative destinations only along a straight && chain. An independent
    # command, alternative, pipeline or subshell can hide a skipped/failed cd.
    ambiguous_directory = any(source[i] in ';\n|()' or
        (source[i] == '&' and not ((i - 1 in unquoted and source[i - 1] == '&') or
                                  (i + 1 in unquoted and source[i + 1] == '&')))
        for i in unquoted)
    for index in redirects:
        suffix = source[index + 1:].lstrip('> \t')
        if suffix.startswith('&'): continue
        try:
            lexer = shlex.shlex(suffix, posix=True, punctuation_chars=';&|()<>\n')
            lexer.whitespace_split = True
            raw = next(iter(lexer), '')
        except ValueError: continue
        target = directory(raw, source[:index])
        if target: write_paths.append(target)
    try:
        lexer = shlex.shlex(source, posix=True, punctuation_chars=';&|()\n')
        lexer.whitespace = ' \t\r'; lexer.whitespace_split = True
        groups, group = [], []
        for value in lexer:
            if value and all(character in ';&|()\n' for character in value):
                if group: groups.append(group); group = []
            else: group.append(value)
        if group: groups.append(group)
    except ValueError: groups = []
    for group in groups:
        if not group: continue
        executable = Path(group[0]).name
        operands = [value for value in group[1:] if not value.startswith('-')]
        if executable in {'cp', 'install'}: operands = operands[-1:]
        elif executable in {'sed', 'perl'}: operands = operands[-1:]
        elif executable not in {'rm', 'mv', 'touch', 'tee'}: continue
        for operand in operands:
            target = directory(operand)
            if target: write_paths.append(target)
    if write_paths: return list(dict.fromkeys(write_paths))
    if _local_mutation(event) in {'filesystem_write', 'inplace_edit'}: return []
    # An explicit shell working-directory target supersedes the caller cwd.
    targeted = []
    for match in re.finditer(r'(?:\bcd\s+|\bgit\s+-C\s+)("[^"]+"|\'[^\']+\'|[^\s;&|]+)', command):
        try: raw = shlex.split(match.group(1))[0]
        except ValueError: continue
        path = Path(raw).expanduser()
        if not path.is_absolute(): path = Path(cwd) / path
        targeted.append(str(path))
    return list(dict.fromkeys(targeted or [cwd]))


def _candidate_dirs(event):
    result = [_cwd(event)]
    args = event.get('tool_input') or {}
    if isinstance(args, dict):
        for key in ('cwd', 'workdir', 'path', 'file_path', 'filePath'):
            value = args.get(key)
            if isinstance(value, str):
                p = Path(value).expanduser()
                if not p.is_absolute(): p = Path(_cwd(event)) / p
                result.append(str(p if p.is_dir() else p.parent))
    command = _command(event)
    from .progress import literal_exec_commands
    for nested in literal_exec_commands(_operation_command(command)):
        directory = nested.get('cwd')
        if isinstance(directory, str):
            p = Path(directory).expanduser()
            if not p.is_absolute(): p = Path(_cwd(event)) / p
            result.append(str(p))
    for match in re.finditer(r'(?:\bcd\s+|\bgit\s+-C\s+)("[^"]+"|\'[^\']+\'|[^\s;&|]+)', command):
        try: value = shlex.split(match.group(1))[0]
        except ValueError: continue
        p = Path(value).expanduser()
        if not p.is_absolute(): p = Path(_cwd(event)) / p
        result.append(str(p))
    return list(dict.fromkeys(result))[:32]


def _git_context(cwd):
    if not Path(cwd).is_dir(): return None
    r = subprocess.run(['git', '-C', cwd, 'rev-parse', '--show-toplevel'], capture_output=True, text=True, timeout=3)
    if r.returncode: return None
    root = r.stdout.strip()
    r = subprocess.run(['git', '-C', root, 'remote', 'get-url', 'origin'], capture_output=True, text=True, timeout=3)
    if r.returncode: return None
    remote = r.stdout.strip().removesuffix('.git')
    m = re.search(r'github\.com[:/]([^/]+/[^/]+)$', remote)
    if not m: return None
    r = subprocess.run(['git', '-C', root, 'branch', '--show-current'], capture_output=True, text=True, timeout=3)
    return root, m.group(1), r.stdout.strip()


def discover_current_pr(repository, branch):
    from .transport import GitHubClient
    from .github import _rest_pages, EvidenceError
    async def read():
        owner=repository.split('/')[0]
        pulls=await _rest_pages(GitHubClient(),f'/repos/{repository}/pulls',{'head':owner+':'+branch,'state':'open'})
        if len(pulls)>1:raise EvidenceError('Multiple open PRs for this exact branch')
        if not pulls:return {'found':False}
        pull=pulls[0]
        if (pull.get('head') or {}).get('ref')!=branch:raise EvidenceError('Discovery returned a different branch')
        return {'found':True,'pull_number':int(pull['number']),'head_sha':pull['head']['sha']}
    return asyncio.run(asyncio.wait_for(read(),22))


def discover(event, store):
    """Reconcile an explicit successful local mutation, never an observation."""
    operation = _local_mutation(event)
    if not operation: return
    sid = _session(event)
    known = {p['repository'].lower() + '/' + str(p['number']) for p in store.prs(sid)}
    errors = []
    for directory in _mutation_dirs(event):
        context = _git_context(directory)
        if not context: continue
        root, repository, branch = context
        if not branch: continue
        try:
            found = discover_current_pr(repository, branch)
            if found.get('found'):
                if hasattr(store, '_connect'):
                    with store._connect() as db:
                        owner = db.execute("SELECT owner_session FROM pr_watch WHERE repository=? AND number=? AND state='open'", (repository, int(found['pull_number']))).fetchone()
                    if owner and owner['owner_session'] != store.session(sid)['root_session']:
                        continue  # A shared checkout cannot adopt another chat's PR.
                key = repository.lower() + '/' + str(found['pull_number'])
                if key not in known:
                    store.register(sid, _client(), repository, int(found['pull_number']), root)
                    known.add(key)
                _ownership_evidence(event, store, f"https://github.com/{repository}/pull/{found['pull_number']}", 'local_mutation', operation)
            elif hasattr(store, '_connect'):
                with store._connect() as db:
                    terminal = db.execute("SELECT updated_at FROM pr_watch WHERE repository=? AND cwd=? AND branch=? AND state IN ('closed','merged')", (repository, root, branch)).fetchone()
                    turn = db.execute('SELECT started_at FROM turn_modes WHERE session_id=?', (sid,)).fetchone()
                    if terminal and (not turn or turn['started_at'] <= terminal['updated_at']):
                        continue  # Finishing an old closed branch must not manufacture a new PR obligation.
                    db.execute('INSERT INTO unpublished_work VALUES(?,?,?,?,?) ON CONFLICT(session_id,repository,branch,cwd) DO UPDATE SET updated_at=excluded.updated_at',
                               (sid, repository, branch, root, time.time()))
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append(f'{repository} discovery: {exc}')
    if errors: raise RuntimeError('; '.join(errors))


async def evaluate(store, sid):
    from .github import collect_pr
    from .policy import evaluate_pr
    rows = [row for row in store.prs(sid) if ((store.snapshot(row['repository'], row['number']) or {}).get('snapshot') or {}).get('state') != 'closed']
    outcomes = []
    async def one(row):
        snapshot = {'repository': row['repository'], 'number': int(row['number'])}
        try:
            snapshot = await asyncio.wait_for(collect_pr(row['repository'], int(row['number'])), timeout=65)
            gates = evaluate_pr(snapshot, store.dispositions(row['repository'], int(row['number'])))
            store.set_snapshot(row['repository'], int(row['number']), snapshot, gates)
        except Exception as exc:
            from .errors import diagnostic
            error = diagnostic(exc, f"/repos/{row['repository']}/pulls/{row['number']}")
            snapshot['component_errors'] = {'collection': error}
            gates = [{'gate': 'GITHUB_DATA', 'status': 'UNKNOWN', 'details': [error['message'][:600]], 'evidence': {'error': error}}]
            store.set_snapshot(row['repository'], int(row['number']), snapshot, gates)
        return dict(row, stop_snapshot=snapshot), gates
    if rows: outcomes = await asyncio.gather(*(one(row) for row in rows))
    return outcomes


async def _finish_due_followups(store, sid):
    from .github import collect_issue, collect_pr
    from .policy import evaluate_pr, evaluate_review
    from .supervisor import complete_verified_followup
    jobs = [j for j in store.followups(sid) if j['status'] != 'complete' and j['due_at'] <= time.time() and (j.get('fields') or {}).get('review_read')]
    async def one(job):
        issue = bool(job.get('issue_number'))
        snapshot = await asyncio.wait_for((collect_issue if issue else collect_pr)(job['repository'], job['number']), 65)
        gates = (evaluate_review if issue else evaluate_pr)(snapshot, store.dispositions(job['repository'], job['number']))
        store.set_snapshot(job['repository'], job['number'], snapshot, gates)
        complete_verified_followup(store, job, snapshot, gates)
    if jobs: await asyncio.gather(*(one(j) for j in jobs))


def _followup_gate(store, sid):
    jobs = store.followups(sid)
    blockers = []
    now = time.time()
    for job in jobs:
        if job.get('status') == 'complete': continue
        if not job.get('issue_number'):
            if hasattr(store, '_connect'):
                with store._connect() as db:
                    watch = db.execute('SELECT state FROM pr_watch WHERE repository=? AND number=?', (job['repository'], job['number'])).fetchone()
                if watch and watch['state'] in {'merged','closed'}: continue
            cached = ((store.snapshot(job['repository'], job['number']) or {}).get('snapshot') or {}) if hasattr(store, 'snapshot') else {}
            if cached.get('state') == 'closed' or cached.get('merged') is True: continue
        if job.get('due_at', 0) > now:
            continue  # Persisted scheduler owns the wait; it is not task completion.
        from .external import held_followup
        if hasattr(store, 'external_blocker') and held_followup(store, sid, job):
            continue  # Reviewed document is held unresolved by verified external CI.
        blockers.append(render_prompt('due_review', repository=job['repository'], number=job['number'], cli=CLI, followup_id=job['id']))
    return blockers


def _scope_gate(store, sid):
    scope = store.get_scope(sid)
    if not scope: return []
    from .supervisor import scope_digest
    baselines = store.scope_baselines(sid) if hasattr(store, 'scope_baselines') else [scope]
    groups = {scope_digest(b): (b, []) for b in baselines}
    errors = []
    for job in store.followups(sid):
        if job.get('kind') != 'slice': continue
        digest = scope_digest(job.get('scope'))
        if digest not in groups:
            errors.append(render_prompt('scope_missing_baseline'))
        else:
            groups[digest][1].append(job)
    for baseline, slices in groups.values():
        ids = [(j.get('fields') or {}).get('slice_id') for j in slices]
        if len(slices) > baseline.get('slice_count', 0) or any(i not in baseline.get('slice_ids', []) for i in ids) or len(set(ids)) != len(ids):
            errors.append(render_prompt('scope_exceeded'))
        if baseline.get('slice_count') != len(baseline.get('slice_ids', [])):
            errors.append(render_prompt('scope_invalid'))
    return errors


def _local_validation_guard(event, store, sid):
    from .progress import validation_command
    command = _draft_command_text(event)
    if not validation_command(command): return None
    rows = store.prs(sid)
    if hasattr(store, '_connect'):
        with store._connect() as db:
            unpublished = [dict(r) for r in db.execute('SELECT * FROM unpublished_work WHERE session_id=?', (sid,))]
        for directory in _candidate_dirs(event):
            context = _git_context(directory)
            if context and any(r['repository'].lower() == context[1].lower() and r['branch'] == context[2] and r['cwd'] == context[0] for r in unpublished):
                return _deny(render_prompt('publish_first'))
    if not rows: return None
    for directory in _candidate_dirs(event):
        context = _git_context(directory)
        if not context: continue
        root, repository, _branch = context
        for row in rows:
            if row['repository'].lower() != repository.lower(): continue
            if event.get('checklist_only'):
                from .monitor import checkout
                with store._connect() as db:
                    watch = db.execute("SELECT owner_session,cwd,state FROM pr_watch WHERE repository=? AND number=?", (row['repository'], row['number'])).fetchone()
                if (not watch or watch['state'] != 'open' or watch['owner_session'] != store.session(sid)['root_session']
                        or checkout(watch['cwd']) != checkout(root)):
                    continue
            cached = store.snapshot(row['repository'], row['number']) or {}
            snapshot = cached.get('snapshot') or {}
            if snapshot.get('state') == 'closed': continue
            if event.get('checklist_only') and snapshot.get('head_ref') and snapshot['head_ref'] != _branch: continue
            url = f"https://github.com/{row['repository']}/pull/{row['number']}"
            return _deny(render_prompt('local_validation_guard', cli=CLI, session_id=sid, pr_url=url, repo_root=root))
    return None


def _stop_feedback(event, store, sid, results, blocked, final_text):
    """Incomplete PR work is not a reason to recursively restart an answer."""
    from .handoff import evidence_fingerprint, hold_unresolved
    from .external import blocked_report
    claims_completion = bool(re.search(
        r'\b(?:all\s+(?:CI|checks|gates)\s+(?:pass(?:ed)?|green)|'
        r'(?:PR\s*#?\d*|task)\s+(?:is\s+)?(?:complete|done|ready)|'
        r'everything\s+(?:is\s+)?(?:complete|done))\b|^\s*(?:done|finished|complete)[.!]?\s*$',
        re.sub(r'[*_`]', '', final_text), re.I))
    truthful_handoff = bool(final_text.strip()) and not claims_completion and blocked_report(final_text)
    actionable = []
    identities = []
    for row, gates in results:
        snapshot = row.get('stop_snapshot')
        if snapshot is None:
            cached = store.snapshot(row['repository'], row['number']) if hasattr(store, 'snapshot') else None
            snapshot = (cached or {}).get('snapshot') or {}
        snapshot = dict(snapshot, repository=row['repository'], number=row['number'])
        failures = [g for g in gates if g.get('status') == 'FAIL']
        if snapshot.get('state') != 'closed' and snapshot.get('merged') is not True:
            actionable.extend(failures)
        identities.append((row, snapshot, gates, evidence_fingerprint(snapshot, gates)))
    # Scope/review-read/publication corrections remain actionable even when no PR
    # is registered. Unknown API evidence and elapsed CI time cannot be fixed locally.
    local_actions = [line for line in blocked if line.startswith(('SLICE_SCOPE FAIL:', 'PRD_FOLLOWUP WAIT:', 'PR_PROGRESS FAIL:', 'ISSUE_CLOSED FAIL:'))]
    current_turn = str(event.get('turn_id') or '')
    if not current_turn and hasattr(store, '_connect'):
        with store._connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_modes'").fetchone()
            turn = db.execute('SELECT turn_id FROM turn_modes WHERE session_id=?', (sid,)).fetchone() if exists else None
            current_turn = turn['turn_id'] if turn else ''
    digest = hashlib.sha256(json.dumps({'prs': [value[3] for value in identities], 'local': local_actions}, sort_keys=True).encode()).hexdigest()
    previously_blocked = bool(event.get('stop_hook_active'))
    if hasattr(store, '_connect'):
        with store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS stop_feedback (session_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, fingerprint TEXT NOT NULL)')
            old = db.execute('SELECT * FROM stop_feedback WHERE session_id=?', (sid,)).fetchone()
            previously_blocked = bool(old and old['fingerprint'] == digest) or (previously_blocked and not old)
            db.execute('INSERT INTO stop_feedback VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET turn_id=excluded.turn_id,fingerprint=excluded.fingerprint', (sid, current_turn, digest))
    if not previously_blocked and (claims_completion or actionable or local_actions):
        if claims_completion:
            instruction = render_prompt('completion_claim')
        else:
            instruction = render_prompt('actionable_stop')
        return _block(render_prompt('stop_feedback', blocked_details='\n'.join(blocked), instruction=instruction))
    for row, snapshot, gates, _digest in identities:
        if any(g.get('status') != 'PASS' for g in gates) and hasattr(store, 'record_recovery'):
            hold_unresolved(store, sid, row['repository'], row['number'], snapshot, gates,
                            turn_id=current_turn, reason='Incomplete work handed back without repeated continuation')
    store.audit('unresolved_stop_handoff', {'session_id': sid, 'fingerprint': digest,
                                         'obligation_complete': False, 'completion_claim_rejected': claims_completion})
    if previously_blocked and truthful_handoff:
        return None  # The assistant already reported this exact blocker.
    return {'systemMessage': render_prompt('incomplete_handoff')}


def _followup_delivery(event, store, sid):
    """Record known machine delivery; discard only its verified completed obligation."""
    prompt = event.get('prompt')
    if not isinstance(prompt, str): return None
    match = DURABLE_FOLLOWUP.match(prompt)
    if not match: return None
    kind, identifier = match.groups()
    job = next((j for j in store.followups(sid) if j.get('id') == identifier
                and j.get('kind') == kind and j.get('session_id') == sid), None)
    if not job: return None
    fields = job.get('fields') or {}
    known = fields.get('delivery') == 'queue_existing_thread' and fields.get('prompt') == prompt
    if not known:
        # Earlier attempts can still arrive after a newer dispatch replaced fields.prompt.
        with store._connect() as db:
            known = db.execute("""SELECT 1 FROM audit WHERE event='followup_updated'
                AND json_extract(fields, '$.id')=? AND json_extract(fields, '$.status')='running'
                AND json_extract(fields, '$.fields.delivery')='queue_existing_thread'
                AND json_extract(fields, '$.fields.prompt')=? LIMIT 1""", (identifier, prompt)).fetchone() is not None
    if not known: return None
    if job.get('status') != 'complete':
        store.note_followup_delivery(identifier, sid, event.get('turn_id') or '', hashlib.sha256(prompt.encode()).hexdigest())
        # Completion can win the race with consumption recording. Re-read once;
        # the earlier snapshot cannot authorize a completed queued obligation.
        job = next((j for j in store.followups(sid) if j.get('id') == identifier
                    and j.get('kind') == kind and j.get('session_id') == sid), None)
        if not job or job.get('status') != 'complete': return None
        fields = job.get('fields') or {}
    receipt = fields.get('review_read') or {}
    if not isinstance(receipt, dict): return None
    def timestamp(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
    verified_at = fields.get('verified_at')
    read_at = receipt.get('read_at')
    due_at = job.get('due_at')
    if (fields.get('verification') != 'live reread, separate gates and unchanged scope verified'
            or not timestamp(verified_at) or verified_at > time.time()
            or not timestamp(read_at) or read_at > time.time() or not timestamp(due_at) or read_at < due_at
            or not isinstance(receipt.get('head_sha'), str) or not receipt['head_sha']
            or not isinstance(receipt.get('comment_ids'), list)
            or any(not isinstance(i, str) or not i for i in receipt['comment_ids'])):
        return None
    from .supervisor import scope_digest, recorded_job_scope
    try:
        digest = scope_digest(job.get('scope'))
        if scope_digest(recorded_job_scope(store, job)) != digest or receipt.get('scope_digest') != digest:
            return None
    except (TypeError, ValueError):
        return None
    store.audit('stale_followup_delivery_blocked', {'session_id': sid, 'id': identifier, 'verified_at': verified_at})
    return _block(render_prompt('completed_followup_stale', followup_id=identifier))


def _register_urls(event, store):
    sid = _session(event)
    if not _succeeded(event): return
    command = _operation_command(_command(event))
    tool = str(event.get('tool_name') or '').lower()
    # Gateway registration is authoritative and already records its exact
    # mutation target. Its input/output can also contain read/reference URLs.
    if 'caller-pr-gates github ' in command: return
    method = re.split(r'__|[.:/]', tool)[-1]
    if not _remote_write_tool(tool) or not method.startswith('create'): return
    out = event.get('tool_response', event.get('tool_output'))
    candidates = [out] if isinstance(out, dict) else []
    if isinstance(out, dict):
        if isinstance(out.get('structuredContent'), dict): candidates.append(out['structuredContent'])
        for block in out.get('content') or []:
            if isinstance(block, dict) and block.get('type') == 'text':
                try: parsed = json.loads(block.get('text', ''))
                except (ValueError, TypeError): continue
                if isinstance(parsed, dict): candidates.append(parsed)
    urls = {item.get('html_url') for item in candidates if isinstance(item.get('html_url'), str)}
    args = event.get('tool_input') or {}
    if isinstance(args, dict) and DOC.search(str(args.get('title', '')) + ' ' + str(args.get('body', ''))):
        documentation = True
    else: documentation = bool(DOC.search(command))
    for url in urls:
        match = PR_URL.fullmatch(url.rstrip('/'))
        if not match: continue
        repository, raw_number = match.groups()
        store.register(sid, _client(), repository, int(raw_number), _cwd(event))
        _ownership_evidence(event, store, url, 'remote_creation', method)
        if documentation:
            _register_document(store, sid, repository, int(raw_number), False)
    if documentation:
        for url in urls:
            match = ISSUE_URL.fullmatch(url.rstrip('/'))
            if not match: continue
            repository, raw_number = match.groups()
            _register_document(store, sid, repository, int(raw_number), True)


def _register_document(store, sid, repository, number, issue, kind=None):
    from .github import collect_issue, collect_pr
    scope = store.get_scope(sid)
    if hasattr(store, 'followup_scope'):
        existing = [j for j in store.followups(sid) if j['repository'] == repository and j['number'] == number
                    and bool(j.get('issue_number')) == bool(issue) and (kind is None or j['kind'] == kind)]
        if existing:
            scope = store.followup_scope(existing[-1])
    if not scope: raise RuntimeError('SLICE_SCOPE UNKNOWN: capture the user-agreed scope before publishing a PRD/Slice.')
    snapshot = asyncio.run(asyncio.wait_for((collect_issue if issue else collect_pr)(repository, number), timeout=65))
    posted = snapshot.get('created_at')
    if isinstance(posted, str):
        from datetime import datetime
        posted = datetime.fromisoformat(posted.replace('Z', '+00:00')).timestamp()
    if not isinstance(posted, (int, float)): raise RuntimeError('PRD posting timestamp unavailable')
    store.register_followup(sid, repository, number, posted, scope, kind=kind or ('slice' if re.search(r'\bslice\b', snapshot.get('title', ''), re.I) else 'prd'), issue_number=number if issue else None)


def handle(event, store=None):
    store = store or Store()
    name = str(event.get('hook_event_name') or '')
    sid = _session(event)
    if not sid: raise RuntimeError('Hook session_id missing; ownership cannot be established')
    os.environ['CALLER_GATE_SESSION']=sid
    os.environ['CALLER_GATE_OPERATION']='hook:'+name
    store.record_session(sid, _client(), _cwd(event))
    if name == 'SubagentStart':
        agent = event.get('agent_id')
        if agent: store.record_session(str(agent), _client(), _cwd(event), parent_session=sid)
        return _context(name, render_prompt('subagent_start', cli=CLI, session_id=sid))
    if name in ('SessionStart', 'UserPromptSubmit'):
        if name == 'UserPromptSubmit':
            if hasattr(store, '_connect'):
                from .turns import start_turn
                start_turn(store, event, sid)
            completed = _followup_delivery(event, store, sid)
            if completed: return completed
        return _context(name, render_prompt('session_context', session_id=sid, cli=CLI, client=_client(), pr_rules=render_prompt('pr_rules')))
    if name == 'PreToolUse':
        command = _operation_command(_command(event))
        draft_command = _draft_command_text(event)
        args = event.get('tool_input') or {}
        tool = str(event.get('tool_name') or '').lower()
        if tool in {'edit', 'write', 'multiedit', 'notebookedit', 'apply_patch', 'functions.apply_patch'}:
            command = ''
        if re.search(r'\bgh\s+pr\s+(?:create|ready|edit)\b', draft_command) and re.search(r'(?:--draft\b|--undo\b|(?:^|\s)-d(?:\s|$))', draft_command):
            return _deny(render_prompt('draft_denied'))
        if ('pull' in tool or 'github' in tool or 'pr_' in tool) and isinstance(args, dict):
            if args.get('draft') is True or args.get('isDraft') is True or 'converttodraft' in tool:
                return _deny(render_prompt('draft_denied'))
        if re.search(r'convertPullRequestToDraft|[\"\'](?:draft|isDraft)[\"\']\s*:\s*true', draft_command, re.I):
            return _deny(render_prompt('draft_denied'))
        remote_write = _remote_write_tool(tool)
        if remote_write:
            return _deny(render_prompt('gateway_denied', cli=CLI))
        if _direct_github_write(command):
            return _deny(render_prompt('gateway_denied', cli=CLI))
        # The write gateway checks real publication targets and scope. Quoted
        # documentation/source code is data, never a publication operation.
        return _local_validation_guard(event, store, sid)
    if name in ('PostToolUse', 'PostToolUseFailure'):
        _register_urls(event, store)
        discover(event, store)
        if hasattr(store, '_connect') and (_local_mutation(event) or (_remote_write_tool(str(event.get('tool_name') or '')) and _succeeded(event))):
            from .turns import note_mutation
            note_mutation(store, event, sid)
        return None
    if name in ('Interrupt', 'SessionEnd', 'StopFailure'):
        store.audit(name, {'session_id': sid, 'obligations_retained': True})
        return None
    if name in ('Stop', 'SubagentStop'):
        if name == 'SubagentStop' and event.get('agent_id'): sid = str(event['agent_id'])
        if name == 'SubagentStop':
            session = store.session(sid) if hasattr(store, 'session') else None
            if session and session.get('root_session') != sid:
                store.audit('subagent_obligation_handoff', {'session_id': sid, 'root_session': session['root_session'], 'obligation_complete': False})
                return {'systemMessage': render_prompt('subagent_handoff')}
        final_text = event.get('last_assistant_message') or event.get('lastAssistantMessage') or ''
        if hasattr(store, '_connect'):
            from .turns import informational_yield
            if informational_yield(store, event, sid, final_text):
                return {'systemMessage': render_prompt('informational_handoff')}
        results = asyncio.run(asyncio.wait_for(evaluate(store, sid), timeout=75))
        blocked = []
        held = []
        for row, gates in results:
            if hasattr(store, 'external_blocker'):
                from .external import hold_verified_failure
                cached = store.snapshot(row['repository'], row['number']) or {}
                snapshot = cached.get('snapshot') or {}
                # A collector failure must not reuse an older successful snapshot.
                if gates and not any(g.get('status') == 'UNKNOWN' for g in gates):
                    try:
                        external = asyncio.run(asyncio.wait_for(hold_verified_failure(store, sid, snapshot, gates), timeout=35))
                    except Exception as exc:
                        external = None
                        store.audit('external_classification_unavailable', {'session_id': sid, 'repository': row['repository'], 'number': row['number'], 'error': str(exc)[:300]})
                    if external:
                        held.append(external)
                        continue
            for gate in gates:
                if gate['status'] != 'PASS':
                    blocked.append(f"{gate['gate']} {gate['status']}: {row['repository']}#{row['number']}: " + '; '.join(gate.get('details') or []))
        asyncio.run(asyncio.wait_for(_finish_due_followups(store, sid), timeout=70))
        blocked += _scope_gate(store, sid) + _followup_gate(store, sid)
        from .closure import task_gates as closeout_gates, refresh as refresh_closeouts
        if hasattr(store, '_connect'):
            asyncio.run(refresh_closeouts(store, sid, force=True))
        blocked += [f"{g['gate']} {g['status']}: " + '; '.join(g['details']) for g in closeout_gates(store, sid)]
        if hasattr(store, '_connect'):
            from .publication import refresh as refresh_publication
            publication = asyncio.run(asyncio.wait_for(refresh_publication(store, sid), timeout=35))
            if publication['errors']:
                store.audit('publication_reconciliation_unavailable', {'session_id': sid, 'errors': publication['errors']})
            with store._connect() as db:
                unpublished = [dict(r) for r in db.execute('SELECT * FROM unpublished_work WHERE session_id=?', (sid,))]
            for row in unpublished:
                blocked.append(render_prompt('unpublished_stop', repository=row['repository'], branch=row['branch']))
        if hasattr(store, 'validation_plans'):
            from .workflow import task_progress
            for gate in task_progress(store, sid):
                if (gate.get('evidence', {}).get('phase') == 'WAIT_REMOTE'
                        and any(h['repository'] == gate['repository'] and h['number'] == gate['number'] for h in held)):
                    continue
                if gate['status'] != 'PASS':
                    blocked.append(f"PR_PROGRESS {gate['status']}: {gate['repository']}#{gate['number']}: " + '; '.join(gate['details']))
        store.audit('gate_decision', {'session_id': sid, 'blocked': blocked, 'pr_count': len(results)})
        if blocked:
            return _stop_feedback(event, store, sid, results, blocked, final_text)
        if held:
            from .external import blocked_report
            if not blocked_report(final_text):
                reasons = [render_prompt('external_account_blocked')]
                return _stop_feedback(event, store, sid, results, reasons, final_text)
            return None  # Already reported; the failed CI and due reviews remain held.
        jobs = [j for j in store.followups(sid) if j.get('status') != 'complete']
        if jobs:
            return {'systemMessage': render_prompt('document_handoff')}
        return None
    return None


def main():
    event = {}
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict): raise ValueError('Hook input must be an object')
        from .checklist import handle as checklist_handle
        result = checklist_handle(event)
    except Exception as exc:
        from .checklist import failure
        result = failure(event, exc)
    if result: print(json.dumps(result))

if __name__ == '__main__': main()
