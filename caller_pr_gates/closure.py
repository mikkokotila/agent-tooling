"""Prove issue closing links and retain finite, owner-bound merge closeouts."""
from __future__ import annotations
from .prompts import render_prompt

import asyncio
import re
import time

QUERY = '''query($owner:String!, $repo:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$repo) { defaultBranchRef { name }
    pullRequest(number:$number) { headRefOid baseRefName state merged
      closingIssuesReferences(first:100, after:$cursor) { nodes {
        number url state repository { nameWithOwner }
      } pageInfo { hasNextPage endCursor } }
    }
  }
}'''
CLOSING = re.compile(r'\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s*:?\s+'
                     r'(?:https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/|'
                     r'(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#)[1-9][0-9]*\b', re.I)
MARKER = '<!-- issue-closeout:'


def has_closing_reference(body):
    body = re.sub(r'<!--.*?-->', '', str(body or ''), flags=re.S)
    body = re.sub(r'^\s*(`{3,}|~{3,}).*?^\s*\1\s*$', '', body, flags=re.M | re.S)
    body = re.sub(r'`[^`\n]*`', '', body)
    return bool(CLOSING.search(body))


async def collect(client, repository, number):
    from .github import EvidenceError, _component_error, _mapping
    owner, repo = repository.split('/')
    result, cursor, seen = {}, None, set()
    try:
        while True:
            response = await client.graphql(QUERY, dict(owner=owner, repo=repo, number=number, cursor=cursor))
            root = _mapping(response.get('repository'), 'issue-closure repository')
            default = _mapping(root.get('defaultBranchRef'), 'default branch').get('name')
            pull = _mapping(root.get('pullRequest'), 'issue-closure PR')
            proof = dict(default_branch=default, closure_head_sha=pull.get('headRefOid'), closure_base_ref=pull.get('baseRefName'))
            if not all(isinstance(value, str) and value for value in proof.values()):
                raise EvidenceError('Issue closure lacks current branch/head evidence')
            if not re.fullmatch(r'[a-fA-F0-9]{40}', proof['closure_head_sha']):
                raise EvidenceError('Issue closure head is malformed')
            if not isinstance(pull.get('merged'), bool) or pull.get('state') not in {'OPEN', 'CLOSED', 'MERGED'}:
                raise EvidenceError('Issue closure lacks current PR lifecycle')
            if pull['merged'] != (pull['state'] == 'MERGED'):
                raise EvidenceError('Issue closure PR lifecycle is inconsistent')
            proof.update(closure_merged=pull['merged'], closure_state=pull['state'])
            if result and any(result[key] != value for key, value in proof.items()):
                raise EvidenceError('PR or default branch changed during issue-link enumeration')
            result.update(proof)
            connection = _mapping(pull.get('closingIssuesReferences'), 'closing issue links')
            nodes, page = connection.get('nodes'), _mapping(connection.get('pageInfo'), 'issue-link pagination')
            if not isinstance(nodes, list) or not isinstance(page.get('hasNextPage'), bool):
                raise EvidenceError('Incomplete closing-issue enumeration')
            issues = result.setdefault('closing_issues', [])
            for node in nodes:
                node = _mapping(node, 'linked issue')
                linked_repo = _mapping(node.get('repository'), 'linked issue repository').get('nameWithOwner')
                linked_number = node.get('number')
                url = f'https://github.com/{linked_repo}/issues/{linked_number}'
                if (not isinstance(linked_repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', linked_repo)
                        or type(linked_number) is not int or linked_number < 1 or node.get('state') not in {'OPEN', 'CLOSED'}
                        or node.get('url') != url or any(i['url'] == url for i in issues)):
                    raise EvidenceError('Linked issue identity/state is malformed or duplicated')
                issues.append(dict(repository=linked_repo, number=linked_number, url=url, state=node['state']))
            if not page['hasNextPage']: break
            cursor = page.get('endCursor')
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise EvidenceError('Issue-link pagination cursor is missing or repeated')
            seen.add(cursor)
        return result
    except Exception as error:
        return {'closure_error': _component_error(error, 'graphql:closingIssuesReferences')}


def gate(snapshot):
    from .policy import _gate
    if snapshot.get('state') == 'closed' and snapshot.get('merged') is False:
        return _gate('ISSUE_CLOSURE', 'PASS', [], {'applicable': False, 'source_closed_without_merge': True})
    issues = snapshot.get('closing_issues')
    proof = {key: snapshot.get(key) for key in ('default_branch', 'base_ref', 'closing_issues', 'closure_head_sha', 'closure_base_ref')}
    if ((snapshot.get('component_errors') or {}).get('issue_closure')
            or not isinstance(issues, list) or not isinstance(snapshot.get('default_branch'), str) or not snapshot.get('default_branch')
            or not isinstance(snapshot.get('base_ref'), str) or not snapshot.get('base_ref')
            or snapshot.get('closure_head_sha') != snapshot.get('head_sha')
            or snapshot.get('closure_base_ref') != snapshot.get('base_ref')):
        return _gate('ISSUE_CLOSURE', 'UNKNOWN', ['GitHub closing-issue and default-branch evidence is unavailable or stale'], proof)
    if not issues:
        return _gate('ISSUE_CLOSURE', 'FAIL', ['Add a closing reference to the actual existing issue, such as Closes #123; do not invent or create an issue to satisfy this check'], proof)
    if snapshot['base_ref'] != snapshot['default_branch']:
        return _gate('ISSUE_CLOSURE', 'FAIL', ['The PR targets a non-default branch, so its merge will not automatically close the linked issue'], proof)
    for issue in issues:
        if (not isinstance(issue, dict) or issue.get('state') not in {'OPEN', 'CLOSED'}
                or not isinstance(issue.get('repository'), str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', issue['repository']) or type(issue.get('number')) is not int
                or issue.get('number', 0) < 1 or issue.get('url') != f"https://github.com/{issue['repository']}/issues/{issue['number']}"):
            return _gate('ISSUE_CLOSURE', 'UNKNOWN', ['Linked issue identity/state is incomplete'], proof)
    if len({i['url'] for i in issues}) != len(issues):
        return _gate('ISSUE_CLOSURE', 'UNKNOWN', ['Linked issue inventory contains duplicate targets'], proof)
    pending = [issue['url'] for issue in issues if issue['state'] != 'CLOSED'] if snapshot.get('merged') is True else []
    return _gate('ISSUE_CLOSURE', 'FAIL' if pending else 'PASS',
                 ['Merged PR still has an open linked issue: ' + url for url in pending], proof)


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS issue_closeouts (
      repository TEXT NOT NULL, number INTEGER NOT NULL, issue_url TEXT NOT NULL,
      owner_session TEXT NOT NULL, head_sha TEXT NOT NULL, state TEXT NOT NULL,
      next_check_at REAL NOT NULL, checked_at REAL, error TEXT, reported_at REAL,
      PRIMARY KEY(repository,number,issue_url,owner_session))''')
    columns = {r['name'] for r in db.execute('PRAGMA table_info(issue_closeouts)')}
    for name, definition in [('event_id', 'TEXT'), ('delivery_state', 'TEXT'), ('lease_until', 'REAL NOT NULL DEFAULT 0'), ('worker_pid', 'INTEGER')]:
        if name not in columns: db.execute(f'ALTER TABLE issue_closeouts ADD COLUMN {name} {definition}')


def record(db, owner, repository, number, snapshot, *, now=None):
    """Only previously verified links create closeouts; historic merged PRs are not adopted."""
    now = time.time() if now is None else now
    if snapshot.get('state') != 'open' or snapshot.get('merged') is not False or gate(snapshot)['status'] != 'PASS' or not snapshot.get('closing_issues'): return
    for issue in snapshot['closing_issues']:
        db.execute('''INSERT OR IGNORE INTO issue_closeouts
          (repository,number,issue_url,owner_session,head_sha,state,next_check_at)
          VALUES(?,?,?,?,?,'pending',?)''', (repository, number, issue['url'], owner, snapshot['head_sha'], now))


async def read_live(repository, number):
    from github_interface.client import GitHubClient
    return await collect(GitHubClient(), repository, number)


async def refresh(store, sid=None, *, force=False, now=None, reader=None, timeout=12, keys=None):
    now = time.time() if now is None else now
    with store._connect() as db:
        query = "SELECT * FROM issue_closeouts WHERE state!='complete'" + (' AND owner_session=?' if sid else '')
        rows = [dict(row) for row in db.execute(query, (sid,) if sid else ())]
    groups = {}
    for row in rows:
        if (keys is None or (row['repository'], row['number']) in keys) and (force or row['next_check_at'] <= now):
            groups.setdefault((row['repository'], row['number']), []).append(row)
    semaphore = asyncio.Semaphore(4)
    async def check(key, targets):
        async with semaphore:
            try:
                proof = await asyncio.wait_for((reader or read_live)(*key), timeout)
                if proof.get('closure_error'): raise ValueError(proof['closure_error'].get('message'))
                if proof.get('closure_merged') is not True or proof.get('closure_state') != 'MERGED':
                    raise ValueError('Live source PR is not confirmed merged')
                if any(proof.get('closure_head_sha') != target['head_sha'] for target in targets):
                    raise ValueError('Merged source head differs from the recorded fix')
                issues = {issue['url']: issue['state'] for issue in proof['closing_issues']}
                if any(issues.get(target['issue_url']) not in {'OPEN', 'CLOSED'} for target in targets):
                    raise ValueError('Previously linked issue is absent from live closing evidence')
                with store._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    for target in targets:
                        state = issues.get(target['issue_url'])
                        db.execute('UPDATE issue_closeouts SET state=?,checked_at=?,next_check_at=?,error=NULL WHERE repository=? AND number=? AND issue_url=? AND owner_session=?',
                            ('complete' if state == 'CLOSED' else 'open', now, now + 300, *key, target['issue_url'], target['owner_session']))
                        if state == 'OPEN':
                            import uuid
                            db.execute("UPDATE issue_closeouts SET event_id=?,delivery_state='pending' WHERE repository=? AND number=? AND issue_url=? AND owner_session=? AND delivery_state IS NULL AND reported_at IS NULL", (str(uuid.uuid4()), *key, target['issue_url'], target['owner_session']))
                        else:
                            db.execute('UPDATE issue_closeouts SET event_id=NULL,lease_until=0 WHERE repository=? AND number=? AND issue_url=? AND owner_session=?', (*key, target['issue_url'], target['owner_session']))
            except Exception as error:
                with store._connect() as db:
                    db.execute("UPDATE issue_closeouts SET state='unknown',checked_at=?,next_check_at=?,error=? WHERE repository=? AND number=? AND state!='complete'", (now, now + 300, str(error)[:300], *key))
    await asyncio.gather(*(check(key, targets) for key, targets in groups.items()))


def task_gates(store, sid):
    if not hasattr(store, '_connect'): return []
    with store._connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM issue_closeouts WHERE owner_session=? AND state!='complete'", (sid,))]
    return [dict(gate='ISSUE_CLOSED', status='FAIL' if row['state'] == 'open' else 'UNKNOWN',
        details=[render_prompt('issue_closeout_gate', number=row['number'], issue_url=row['issue_url'])],
        evidence=row) for row in rows]


def message(rows):
    return '\n'.join(render_prompt('issue_closeout', repository=row['repository'], number=row['number'], issue_url=row['issue_url']) for row in rows)


def consume(store, sid, *, event_id=None, reader=None):
    with store._connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM issue_closeouts WHERE owner_session=? AND state='open' AND delivery_state IN ('pending','reserved','queued') AND reported_at IS NULL", (sid,))]
    if not rows or event_id and not any(row['event_id'] == event_id for row in rows): return None
    rows.sort(key=lambda row: row['event_id'] != event_id)
    keys = list(dict.fromkeys((row['repository'], row['number']) for row in rows))[:3]
    asyncio.run(refresh(store, sid, force=True, reader=reader, timeout=2, keys=keys))
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = [dict(row) for row in db.execute("SELECT * FROM issue_closeouts WHERE owner_session=? AND state='open' AND delivery_state IN ('pending','reserved','queued') AND reported_at IS NULL", (sid,))]
        rows = [row for row in rows if (row['repository'], row['number']) in keys]
        if event_id and not any(row['event_id'] == event_id for row in rows): return None
        for row in rows:
            db.execute("UPDATE issue_closeouts SET reported_at=?,delivery_state='consumed',lease_until=0 WHERE repository=? AND number=? AND issue_url=? AND owner_session=?", (time.time(), row['repository'], row['number'], row['issue_url'], sid))
    return message(rows) if rows else None


def dispatch(store, *, now=None, spawn=None):
    import subprocess, sys
    now = time.time() if now is None else now
    count = 0
    with store._connect() as db:
        db.execute("UPDATE issue_closeouts SET delivery_state='failed',lease_until=0,error='Closeout worker expired; explicit retry required' WHERE delivery_state IN ('reserved','queued') AND lease_until>0 AND lease_until<=?", (now,))
        owners = [r[0] for r in db.execute("SELECT DISTINCT owner_session FROM issue_closeouts WHERE state='open' AND delivery_state='pending' AND reported_at IS NULL")]
    for sid in owners:
        with store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            busy = db.execute('SELECT busy FROM watch_sessions WHERE session_id=?', (sid,)).fetchone()
            if busy and busy['busy']: continue
            if db.execute('SELECT 1 FROM pr_watch WHERE owner_session=? AND lease_until>?', (sid, now)).fetchone(): continue
            if db.execute('SELECT 1 FROM issue_closeouts WHERE owner_session=? AND lease_until>?', (sid, now)).fetchone(): continue
            claimed = db.execute("UPDATE issue_closeouts SET delivery_state='reserved',lease_until=? WHERE owner_session=? AND state='open' AND delivery_state='pending' AND reported_at IS NULL", (now + 3000, sid)).rowcount
        if not claimed: continue
        try:
            worker = (spawn or (lambda args: subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)))([sys.executable, '-m', 'caller_pr_gates.closure', 'deliver', '--session', sid])
            with store._connect() as db:
                db.execute("UPDATE issue_closeouts SET worker_pid=? WHERE owner_session=? AND delivery_state='reserved'", (getattr(worker, 'pid', None), sid))
            count += 1
        except Exception as error:
            failed(store, sid, error)
    return count


def failed(store, sid, error):
    with store._connect() as db:
        db.execute("UPDATE issue_closeouts SET delivery_state='failed',lease_until=0,error=? WHERE owner_session=? AND delivery_state IN ('reserved','queued')", (str(error)[:300], sid))


async def deliver(store, sid, *, reader=None, runner=None, availability=None):
    from pathlib import Path
    from .supervisor import resume_command, run_process, claude_availability
    await refresh(store, sid, force=True, reader=reader)
    owner = store.session(sid)
    if not owner or owner['root_session'] != sid or not Path(owner['cwd']).is_dir():
        raise ValueError('Exact owner metadata is unavailable; no alternate chat is allowed')
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        busy = db.execute('SELECT busy FROM watch_sessions WHERE session_id=?', (sid,)).fetchone()
        if busy and busy['busy']:
            db.execute("UPDATE issue_closeouts SET delivery_state='pending',lease_until=0 WHERE owner_session=? AND delivery_state='reserved'", (sid,))
            return False
        rows = [dict(r) for r in db.execute("SELECT * FROM issue_closeouts WHERE owner_session=? AND delivery_state='reserved' AND state='open' AND reported_at IS NULL", (sid,))]
    if not rows:
        with store._connect() as db:
            db.execute("UPDATE issue_closeouts SET delivery_state='failed',lease_until=0,error='Closeout evidence changed before delivery; explicit retry required' WHERE owner_session=? AND delivery_state='reserved'", (sid,))
        return False
    if owner['client'] == 'claude' and (availability or claude_availability)(sid, owner['cwd']) != 'idle':
        with store._connect() as db:
            db.execute("UPDATE issue_closeouts SET delivery_state='pending',lease_until=0 WHERE owner_session=? AND delivery_state='reserved'", (sid,))
        return False
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        busy = db.execute('SELECT busy FROM watch_sessions WHERE session_id=?', (sid,)).fetchone()
        if busy and busy['busy']:
            db.execute("UPDATE issue_closeouts SET delivery_state='pending',lease_until=0 WHERE owner_session=? AND delivery_state='reserved'", (sid,))
            return False
        tokens = {row['event_id'] for row in rows}
        rows = [dict(r) for r in db.execute("SELECT * FROM issue_closeouts WHERE owner_session=? AND delivery_state='reserved' AND state='open' AND reported_at IS NULL AND lease_until>?", (sid, time.time())) if r['event_id'] in tokens]
        if not rows: return False  # Native consumption, expiry or closure won the claim.
        for row in rows:
            db.execute("UPDATE issue_closeouts SET delivery_state='queued' WHERE owner_session=? AND event_id=? AND delivery_state='reserved'", (sid, row['event_id']))
    prompt = MARKER + rows[0]['event_id'] + '-->\n' + message(rows)
    args, stdin = resume_command(owner['client'], sid, prompt)
    result = await asyncio.to_thread(runner or run_process, args, cwd=owner['cwd'], input_text=stdin, timeout=20 if owner['client']=='codex' else 2700)
    if result.get('returncode') != 0: raise RuntimeError('Closeout delivery failed; explicit retry required')
    with store._connect() as db:
        db.execute('UPDATE issue_closeouts SET lease_until=0 WHERE owner_session=? AND worker_pid=?', (sid, __import__('os').getpid()))
    return True


def retry(store, sid, repository, number):
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("SELECT * FROM issue_closeouts WHERE owner_session=? AND repository=? AND number=? AND state!='complete'", (sid, repository, number)).fetchall()
        if any(r['lease_until'] > time.time() for r in rows): raise ValueError('Closeout still has a live worker lease')
        db.execute("UPDATE issue_closeouts SET delivery_state=NULL,event_id=NULL,reported_at=NULL,worker_pid=NULL,error=NULL,next_check_at=0 WHERE owner_session=? AND repository=? AND number=? AND state!='complete'", (sid, repository, number))
    return bool(rows)


if __name__ == '__main__':
    import argparse
    from .store import Store
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['deliver'])
    parser.add_argument('--session', required=True)
    args = parser.parse_args()
    store = Store()
    try: asyncio.run(deliver(store, args.session))
    except Exception as error: failed(store, args.session, error)
