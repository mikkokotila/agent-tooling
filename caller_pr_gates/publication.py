"""Reconcile publication only for clean, exact owned Git source identities."""
from __future__ import annotations
from datetime import datetime
import math
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

def source(cwd):
    def git(*args):
        r=subprocess.run(['git','-C',str(cwd),*args],capture_output=True,text=True,timeout=5,check=True)
        return r.stdout.strip()
    try:
        root=Path(git('rev-parse','--show-toplevel')).resolve()
        common=Path(git('rev-parse','--path-format=absolute','--git-common-dir')).resolve()
        return dict(root=str(root),common=str(common),head=git('rev-parse','HEAD'),branch=git('branch','--show-current'),
                    clean=not git('status','--porcelain=v1','--untracked-files=all'),origin=git('remote','get-url','origin'))
    except (OSError,ValueError,subprocess.SubprocessError):return None

def branch_source(cwd, branch):
    """Bind the recorded branch ref as well as the current checkout."""
    local = source(cwd)
    if local is None:
        return None
    try:
        tip = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--verify', 'refs/heads/' + branch],
                             capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        return dict(local, branch_head=tip)
    except (OSError, subprocess.SubprocessError):
        return None


def on_base(cwd, tip, base):
    if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40}', base):
        return False
    try:
        return subprocess.run(['git', '-C', str(cwd), 'merge-base', '--is-ancestor', tip, base],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def exact_source(row, local):
    if not local or not local['clean'] or local['root'] != str(Path(row['cwd']).resolve()):
        return False
    origin = local['origin'].removesuffix('.git').rstrip('/')
    return origin in {f"https://github.com/{row['repository']}", f"git@github.com:{row['repository']}"}


def _pr_source(pull, repository, branch, head, base):
    if not isinstance(pull, dict) or type(pull.get('number')) is not int or pull['number'] <= 0:
        return False
    source, target = pull.get('head'), pull.get('base')
    if not isinstance(source, dict) or not isinstance(target, dict):
        return False
    source_repo, target_repo = source.get('repo'), target.get('repo')
    return (isinstance(source_repo, dict) and source_repo.get('full_name') == repository
            and source.get('ref') == branch and source.get('sha') == head
            and isinstance(target_repo, dict) and target_repo.get('full_name') == repository
            and target.get('ref') == base)


def _merged_at(value):
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if at.tzinfo is None:
            return None
        timestamp = at.timestamp()
        return timestamp if math.isfinite(timestamp) else None
    except (ValueError, OverflowError):
        return None


def _merged_identity(pull, repository, branch, head, base):
    if not _pr_source(pull, repository, branch, head, base):
        return None
    merge = pull.get('merge_commit_sha')
    at = _merged_at(pull.get('merged_at'))
    if (pull.get('state') != 'closed' or pull.get('merged') is not True or at is None
            or not isinstance(merge, str) or not re.fullmatch(r'[0-9a-f]{40}', merge)):
        return None
    return dict(number=pull['number'], merge_commit_sha=merge, merged_at=pull['merged_at'], merged_timestamp=at)


def retire_record(store, row, local, parents, authorized, evidence):
    """CAS one proved source record; never mark its PR/review gates complete."""
    if branch_source(row['cwd'], row['branch']) != local:
        return False
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if store._parents(db) != parents or branch_source(row['cwd'], row['branch']) != local or not authorized(db):
            return False
        cursor = db.execute('DELETE FROM unpublished_work WHERE session_id=? AND repository=? AND branch=? AND cwd=? AND updated_at=?',
                            (row['session_id'], row['repository'], row['branch'], row['cwd'], row['updated_at']))
        if cursor.rowcount:
            store._audit(db, 'publication_reconciled', dict(evidence, session_id=row['session_id'],
                         repository=row['repository'], branch=row['branch'], cwd=row['cwd'],
                         head_sha=local['branch_head'], completion=False))
        return bool(cursor.rowcount)


def reconcile(store,repository,number,evidence):
    """No ownership adoption; no broad branch/session deletion on lifecycle reads."""
    raw_head=evidence.get('head') or {}
    head=evidence.get('head_sha') or raw_head.get('sha')
    after=evidence.get('head_sha_after',head)
    branch=evidence.get('head_ref') or raw_head.get('ref')
    repo=evidence.get('head_repository') or (raw_head.get('repo') or {}).get('full_name')
    if not head or head!=after or not branch or repo!=repository or evidence.get('state') not in {'open','closed'} or type(evidence.get('merged')) is not bool:return
    published=set(evidence.get('commit_shas') or [])|{head}
    base = evidence.get('base_sha')
    if (evidence.get('base_ref') != evidence.get('default_branch')
            or not evidence.get('default_branch') or evidence.get('base_sha_after') != base
            or evidence.get('branch_error')):
        base = None
    with store._connect() as db:
        owners=[dict(r) for r in db.execute('SELECT * FROM prs WHERE repository=? AND number=?',(repository,number))]
        parents=store._parents(db)
        watch=db.execute('SELECT owner_session FROM pr_watch WHERE repository=? AND number=?',(repository,number)).fetchone()
        candidates=[dict(r) for r in db.execute('SELECT * FROM unpublished_work WHERE repository=?',(repository,))]
    identities={r['cwd']:source(r['cwd']) for r in owners}
    for row in candidates:
        local=branch_source(row['cwd'], row['branch'])
        if not exact_source(row, local):continue
        # Branch names can change when an owned task moves to a worktree.
        # The exact recorded tip in the live PR's commits proves publication.
        published_head = local['branch_head'] in published
        delivered_base = on_base(row['cwd'], local['branch_head'], base)
        if not published_head and not delivered_base:continue
        matched=any(o['status'] != 'misattributed_read_only' and store._root(o['session_id'],parents)==store._root(row['session_id'],parents)
            and identities[o['cwd']] and identities[o['cwd']]['common']==local['common'] for o in owners)
        if not matched or watch and store._root(watch['owner_session'],parents)!=store._root(row['session_id'],parents):continue
        def authorized(db):
            actual=[dict(r) for r in db.execute('SELECT * FROM prs WHERE repository=? AND number=?',(repository,number))]
            current_watch=db.execute('SELECT owner_session FROM pr_watch WHERE repository=? AND number=?',(repository,number)).fetchone()
            return actual == owners and dict(current_watch or {}) == dict(watch or {})
        retire_record(store, row, local, parents, authorized, dict(number=number,
                      proof='live_default_ancestry' if delivered_base else 'live_pr_commit', base_sha=base))


async def refresh(store, session_id, *, cwd=None, client=None):
    """Reconcile delivered source even after its last owned PR retired."""
    from .transport import GitHubClient
    with store._connect() as db:
        parents = store._parents(db)
        task = store._root(session_id, parents)
        rows = [dict(r) for r in db.execute('SELECT * FROM unpublished_work')
                if store._root(r['session_id'], parents) == task
                and (cwd is None or Path(r['cwd']).resolve() == Path(cwd).resolve())]
    candidates = [(row, branch_source(row['cwd'], row['branch'])) for row in rows]
    candidates = [(row, local) for row, local in candidates if exact_source(row, local)]
    result = dict(retired=[], errors=[], completion=False)
    if not candidates:
        return result
    client = client or GitHubClient()
    for repository in sorted({row['repository'] for row, _ in candidates}):
        try:
            metadata = await client.get('/repos/' + repository)
            branch = metadata.get('default_branch')
            if metadata.get('full_name') != repository or not isinstance(branch, str) or not branch:
                raise ValueError('Default-branch identity is unavailable')
            path = '/repos/' + repository + '/branches/' + quote(branch, safe='')
            first = await client.get(path)
            after = await client.get(path)
            sha = (first.get('commit') or {}).get('sha')
            if first.get('name') != branch or after.get('name') != branch or sha != (after.get('commit') or {}).get('sha'):
                raise ValueError('Default branch changed during publication reconciliation')
            listings, proofs = {}, {}
            for row, local in candidates:
                if row['repository'] != repository:
                    continue
                evidence = dict(proof='live_default_ancestry', target_branch=branch, base_sha=sha)
                if not on_base(row['cwd'], local['branch_head'], sha):
                    recorded_branch = row['branch']
                    if recorded_branch not in listings:
                        listings[recorded_branch] = await client.get('/repos/' + repository + '/pulls', params={
                            'state': 'closed', 'head': repository.split('/')[0] + ':' + recorded_branch,
                            'base': branch, 'sort': 'updated', 'direction': 'desc', 'per_page': 100, 'page': 1,
                        })
                    pulls = listings[recorded_branch]
                    if not isinstance(pulls, list) or len(pulls) > 100:
                        raise ValueError('Closed pull-request discovery is unavailable')
                    # GitHub reports whole seconds; that entire second must predate the marker.
                    matches = [pull for pull in pulls
                               if _pr_source(pull, repository, recorded_branch, local['branch_head'], branch)
                               and pull.get('state') == 'closed'
                               and (merged := _merged_at(pull.get('merged_at'))) is not None
                               and merged + 1 <= row['updated_at']]
                    if not matches:
                        continue
                    number = matches[0]['number']
                    key = (number, recorded_branch, local['branch_head'])
                    if key not in proofs:
                        pull_path = '/repos/' + repository + '/pulls/' + str(number)
                        before = _merged_identity(await client.get(pull_path), repository, recorded_branch, local['branch_head'], branch)
                        latest = _merged_identity(await client.get(pull_path), repository, recorded_branch, local['branch_head'], branch)
                        if before is None or latest is None or before != latest or before['number'] != number:
                            raise ValueError('Merged pull-request identity changed or is unavailable')
                        proofs[key] = before
                    proof = proofs[key]
                    if (proof['merged_timestamp'] + 1 > row['updated_at']
                            or not on_base(row['cwd'], proof['merge_commit_sha'], sha)):
                        continue
                    current_metadata = await client.get('/repos/' + repository)
                    if (current_metadata.get('full_name') != repository
                            or current_metadata.get('default_branch') != branch):
                        raise ValueError('Repository default branch changed during merged publication reconciliation')
                    current = await client.get(path)
                    if current.get('name') != branch or (current.get('commit') or {}).get('sha') != sha:
                        raise ValueError('Default branch changed during merged publication reconciliation')
                    evidence = dict(proof='preexisting_merged_pr', number=number,
                                    merge_commit_sha=proof['merge_commit_sha'], merged_at=proof['merged_at'],
                                    marker_updated_at=row['updated_at'], target_branch=branch, base_sha=sha)
                if retire_record(store, row, local, parents, lambda db: True,
                                 evidence):
                    result['retired'].append({k: row[k] for k in ('repository', 'branch', 'cwd')})
        except Exception as exc:
            result['errors'].append(dict(repository=repository, error=str(exc)[:600]))
    return result
