"""Close an owned, content-empty PR without granting any merge or task credit."""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import quote

from .publication import exact_source, source
from .transport import GitHubClient


_SHA = re.compile(r'[0-9a-f]{40}')


def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError('Superseded closure requires complete commit/tree identities')
    return value


def _identity(pull, repository, number, repository_id, default_branch, *, closed=False):
    if (not isinstance(pull, dict) or pull.get('number') != number
            or type(pull.get('number')) is not int
            or pull.get('state') != ('closed' if closed else 'open')
            or pull.get('merged') is not False or pull.get('draft') is not False
            or type(pull.get('changed_files')) is not int or pull['changed_files'] != 0):
        raise ValueError('Superseded closure requires an open, unmerged, non-draft zero-file PR')
    head, base = pull.get('head') or {}, pull.get('base') or {}
    for endpoint in (head, base):
        repo = endpoint.get('repo') or {}
        if repo.get('full_name') != repository or repo.get('id') != repository_id:
            raise ValueError('Superseded closure requires exact same-repository source and target')
    if (base.get('ref') != default_branch or not isinstance(head.get('ref'), str)
            or not head['ref'] or head['ref'] == default_branch):
        raise ValueError('Superseded closure requires the live default target and a distinct source branch')
    return dict(head_sha=_sha(head.get('sha')), head_ref=head['ref'], base_ref=default_branch)


async def _proof(client, repository, number, expected_head):
    prefix = f'/repos/{repository}'
    path = f'{prefix}/pulls/{number}'
    pull, repo = await asyncio.gather(client.get(path), client.get(prefix))
    if (not isinstance(repo, dict) or repo.get('full_name') != repository
            or type(repo.get('id')) is not int or repo['id'] <= 0
            or not isinstance(repo.get('default_branch'), str) or not repo['default_branch']):
        raise ValueError('Superseded closure requires the live repository/default-branch identity')
    identity = _identity(pull, repository, number, repo['id'], repo['default_branch'])
    if identity['head_sha'] != expected_head:
        raise ValueError('Superseded PR head differs from the exact owned checkout')
    base_path = f"{prefix}/git/ref/heads/{quote(identity['base_ref'], safe='')}"
    head_path = f"{prefix}/git/ref/heads/{quote(identity['head_ref'], safe='')}"
    base_ref, head_ref = await asyncio.gather(client.get(base_path), client.get(head_path))
    # PR.base.sha can retain an older analyzed base after main advances. The
    # target ref, read independently, names the live content to compare.
    live_base = _sha((base_ref.get('object') or {}).get('sha'))
    if (head_ref.get('object') or {}).get('sha') != identity['head_sha']:
        raise ValueError('Superseded source branch changed before collection')
    files, comparison, commit = await asyncio.gather(
        client.get(f'{path}/files', params={'per_page': 100}),
        client.get(f"{prefix}/compare/{live_base}...{identity['head_sha']}"),
        client.get(f"{prefix}/git/commits/{identity['head_sha']}"))
    if files != [] or not isinstance(comparison, dict) or comparison.get('files') != []:
        raise ValueError('Superseded closure refuses any remaining or unknown PR content')
    merge_base = comparison.get('merge_base_commit') or {}
    merge_tree = _sha(((merge_base.get('commit') or {}).get('tree') or {}).get('sha'))
    head_tree = _sha((commit.get('tree') or {}).get('sha'))
    if commit.get('sha') != identity['head_sha'] or head_tree != merge_tree:
        raise ValueError('Superseded PR content is not already represented by its target merge base')
    final, final_repo, final_base, final_head = await asyncio.gather(
        client.get(path), client.get(prefix), client.get(base_path), client.get(head_path))
    if (final_repo.get('id') != repo['id'] or final_repo.get('default_branch') != identity['base_ref']
            or _identity(final, repository, number, repo['id'], identity['base_ref']) != identity
            or (final_base.get('object') or {}).get('sha') != live_base
            or (final_head.get('object') or {}).get('sha') != identity['head_sha']):
        raise ValueError('Superseded PR or target changed during collection')
    return dict(repository=repository, number=number, repository_id=repo['id'], base_sha=live_base, **identity,
                merge_base_sha=_sha(merge_base.get('sha')), content_tree=head_tree,
                collected_at=datetime.now(timezone.utc).isoformat())


async def _closed(client, proof):
    prefix = f"/repos/{proof['repository']}"
    pull, repo, base, head = await asyncio.gather(
        client.get(f"{prefix}/pulls/{proof['number']}"), client.get(prefix),
        client.get(f"{prefix}/git/ref/heads/{quote(proof['base_ref'], safe='')}"),
        client.get(f"{prefix}/git/ref/heads/{quote(proof['head_ref'], safe='')}"))
    expected = {key: proof[key] for key in ('head_sha', 'head_ref', 'base_ref')}
    if (_identity(pull, proof['repository'], proof['number'], proof['repository_id'], proof['base_ref'], closed=True) != expected
            or repo.get('id') != proof['repository_id'] or repo.get('default_branch') != proof['base_ref']
            or (base.get('object') or {}).get('sha') != proof['base_sha']
            or (head.get('object') or {}).get('sha') != proof['head_sha']):
        raise ValueError('Superseded close did not retain the verified PR/default-branch identities')
    return pull


def _owner(store, sid, repository, number, client, cwd, db):
    session = db.execute('SELECT * FROM sessions WHERE session_id=?', (sid,)).fetchone()
    root = store._root(sid, store._parents(db))
    row = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=?', (repository, number)).fetchone()
    if (not session or not row or row['owner_session'] != root or row['client'] != client
            or Path(row['cwd']).resolve() != Path(cwd).resolve() or row['state'] != 'open'):
        raise ValueError('Superseded closure requires the exact canonical PR owner/client/checkout')
    return {key: row[key] for key in ('repository', 'number', 'owner_session', 'client', 'cwd', 'state')}


def _claim(store, sid, pin, receipt):
    from .monitor import _worker_alive
    now, pid = time.time(), os.getpid()
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if _owner(store, sid, pin['repository'], pin['number'], pin['client'], pin['cwd'], db) != pin:
            raise ValueError('Canonical superseded owner changed during proof collection')
        row = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=?',
                         (pin['repository'], pin['number'])).fetchone()
        if (row['lease_until'] > now or row['delivery_state'] in {'reserved', 'queued'}
                or row['worker_pid'] is not None and _worker_alive(row['worker_pid'])):
            raise ValueError('Superseded PR has active owner work; wait for its existing lease')
        # The existing per-PR lease also blocks explicit ownership transfer.
        db.execute('UPDATE pr_watch SET lease_until=?,worker_pid=? WHERE repository=? AND number=?',
                   (now + 180, pid, pin['repository'], pin['number']))
        store._audit(db, 'pr_superseded_close_attempt', receipt)


def _release(store, pin):
    with store._connect() as db:
        db.execute("""UPDATE pr_watch SET lease_until=0,worker_pid=NULL WHERE repository=? AND number=?
            AND owner_session=? AND client=? AND cwd=? AND worker_pid=?""",
            (pin['repository'], pin['number'], pin['owner_session'], pin['client'], pin['cwd'], os.getpid()))


def close(args, store, sid, argv, run):
    from .cli import reference
    reason = getattr(args, 'superseded_reason', None)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError('Superseded closure requires a nonempty reason of at most 2000 characters')
    if len(argv) != 3 or argv[:2] != ['pr', 'close']:
        raise ValueError('Superseded closure accepts only pr close with one explicit PR URL')
    target = argv[2]
    repository, number, issue = reference(target)
    if issue or getattr(args, 'target', None) not in (None, target):
        raise ValueError('Superseded closure requires one matching explicit PR target')
    owner = next((row for row in store.prs(sid)
                  if row['repository'] == repository and row['number'] == number), None)
    if (owner is None or owner['client'] != args.client
            or Path(owner['cwd']).resolve() != Path(args.cwd).resolve()):
        raise ValueError('Superseded closure requires this exact recorded session/client/checkout owner')
    local = source(args.cwd, stable=True)
    if not exact_source(owner, local):
        raise ValueError('Superseded closure requires the clean, exact owned repository checkout')
    with store._connect() as db:
        pin = _owner(store, sid, repository, number, args.client, args.cwd, db)
    client = GitHubClient()
    proof = asyncio.run(asyncio.wait_for(_proof(client, repository, number, local['head']), 65))
    if source(args.cwd, stable=True) != local:
        raise ValueError('Superseded closure checkout changed during collection')
    receipt = dict(session_id=sid, owner_session=pin['owner_session'], client=args.client,
                   cwd=str(Path(args.cwd).resolve()), reason=reason.strip(), **proof,
                   gates_waived=False, completion_verified=False)
    _claim(store, sid, pin, receipt)
    try:
        result = run(['gh', 'pr', 'close', target, '--comment', 'Closed as superseded: ' + reason.strip()],
                     cwd=args.cwd, text=True, capture_output=True, timeout=90)
        if result.returncode:
            raise RuntimeError('Superseded close failed; inspect live PR state before retrying')
        try:
            closed = asyncio.run(asyncio.wait_for(_closed(client, proof), 30))
            with store._connect() as db:
                current = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=?', (repository, number)).fetchone()
                if (not current or any(current[key] != pin[key] for key in pin if key != 'state')
                        or current['worker_pid'] != os.getpid()
                        or current['state'] not in {'open', 'closed'}
                        or current['state'] == 'open' and current['lease_until'] <= time.time()):
                    raise ValueError('Superseded close lost its canonical owner lease')
            if source(args.cwd, stable=True) != local:
                raise ValueError('Superseded closure checkout changed before read-back')
        except Exception:
            store.audit('pr_superseded_close_unverified', receipt)
            raise
        from .monitor import retire
        retire(store, repository, number, closed)
        store.audit('pr_superseded_closed', receipt)
        return dict(closed=True, superseded=True, **receipt)
    finally:
        _release(store, pin)
