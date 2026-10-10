"""Reconcile publication only for clean, exact owned Git source identities."""
from __future__ import annotations
from pathlib import Path
import subprocess

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

def reconcile(store,repository,number,evidence):
    """No ownership adoption; no broad branch/session deletion on lifecycle reads."""
    raw_head=evidence.get('head') or {}
    head=evidence.get('head_sha') or raw_head.get('sha')
    after=evidence.get('head_sha_after',head)
    branch=evidence.get('head_ref') or raw_head.get('ref')
    repo=evidence.get('head_repository') or (raw_head.get('repo') or {}).get('full_name')
    if not head or head!=after or not branch or repo!=repository or evidence.get('state') not in {'open','closed'} or type(evidence.get('merged')) is not bool:return
    published=set(evidence.get('commit_shas') or [])|{head}
    with store._connect() as db:
        owners=[dict(r) for r in db.execute('SELECT * FROM prs WHERE repository=? AND number=?',(repository,number))]
        parents=store._parents(db)
        watch=db.execute('SELECT owner_session FROM pr_watch WHERE repository=? AND number=?',(repository,number)).fetchone()
        candidates=[dict(r) for r in db.execute('SELECT * FROM unpublished_work WHERE repository=? AND branch=?',(repository,branch))]
    identities={r['cwd']:source(r['cwd']) for r in owners+candidates}
    for row in candidates:
        local=identities[row['cwd']]
        if not local or local['root']!=str(Path(row['cwd']).resolve()) or not local['clean'] or local['head'] not in published or local['branch']!=branch:continue
        origin=local['origin'].removesuffix('.git').rstrip('/')
        if origin not in {f'https://github.com/{repository}',f'git@github.com:{repository}'}:continue
        matched=any(store._root(o['session_id'],parents)==store._root(row['session_id'],parents)
            and identities[o['cwd']] and identities[o['cwd']]['common']==local['common'] for o in owners)
        if not matched or watch and store._root(watch['owner_session'],parents)!=store._root(row['session_id'],parents):continue
        # Compare the source twice, then CAS the original durable record. A
        # racing local edit/ownership update conservatively retains the blocker.
        if source(row['cwd'])!=local:continue
        with store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=source(row['cwd'])
            if current!=local:continue
            actual=[dict(r) for r in db.execute('SELECT * FROM prs WHERE repository=? AND number=?',(repository,number))]
            current_watch=db.execute('SELECT owner_session FROM pr_watch WHERE repository=? AND number=?',(repository,number)).fetchone()
            if actual!=owners or dict(current_watch or {})!=dict(watch or {}) or store._parents(db)!=parents:continue
            cursor=db.execute('DELETE FROM unpublished_work WHERE session_id=? AND repository=? AND branch=? AND cwd=? AND updated_at=?',
                (row['session_id'],repository,branch,row['cwd'],row['updated_at']))
            if cursor.rowcount:store._audit(db,'publication_reconciled',dict(repository=repository,number=number,cwd=row['cwd'],head_sha=local['head'],completion=False))
