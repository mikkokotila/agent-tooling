from pathlib import Path
import time
import pytest
from caller_pr_gates.publication import reconcile
from test_workflow import local,git


def add(store,cwd,branch):
    with store._connect() as db:db.execute('INSERT INTO unpublished_work VALUES(?,?,?,?,?)',('s','Owner/Repo',branch,str(cwd),time.time()))

def count(store,cwd):
    with store._connect() as db:return db.execute('SELECT COUNT(*) FROM unpublished_work WHERE cwd=?',(str(cwd),)).fetchone()[0]

def test_exact_published_root_preserves_edits_and_other_worktrees(local,tmp_path):
    store,root,snap,_=local;branch=git(root,'branch','--show-current')
    work=tmp_path/'worktree';git(root,'worktree','add','--detach',str(work),'HEAD')
    store.register('s','codex','Owner/Repo',1,str(work));add(store,root,branch)
    assert count(store,root)==1 # Registration is not publication evidence.
    proof={**snap,'head_ref':branch,'head_repository':'Owner/Repo','state':'closed','merged':True}
    (root/'src.txt').write_text('real unpublished edit')
    reconcile(store,'Owner/Repo',1,proof);assert count(store,root)==1
    git(root,'checkout','--','src.txt')
    other=tmp_path/'unrelated';other.mkdir();git(other,'init','-q');git(other,'config','user.name','Test');git(other,'config','user.email','t@example.invalid')
    git(other,'remote','add','origin','https://github.com/Owner/Repo.git');(other/'source').write_text('other work');git(other,'add','.');git(other,'commit','-qm','Separate source')
    add(store,other,branch)
    reconcile(store,'Owner/Repo',1,proof)
    assert count(store,root)==0 and count(store,other)==1


def test_unknown_head_owner_and_source_mismatch_retain_publication(local):
    store,root,snap,_=local;branch=git(root,'branch','--show-current');add(store,root,branch)
    proof={**snap,'head_ref':branch,'head_repository':'Owner/Repo'}
    for changed in [{'head_sha_after':'b'*40},{'head_repository':'Other/Repo'},{'head_ref':'different'},{'state':None}]:
        reconcile(store,'Owner/Repo',1,{**proof,**changed});assert count(store,root)==1
    store.record_session('new','claude',str(root))
    with store._connect() as db:db.execute("UPDATE pr_watch SET owner_session='new' WHERE repository='Owner/Repo' AND number=1")
    reconcile(store,'Owner/Repo',1,proof);assert count(store,root)==1
