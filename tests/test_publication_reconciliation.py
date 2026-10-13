from pathlib import Path
import asyncio
import json
import time
from unittest.mock import patch
import pytest
from caller_pr_gates.publication import reconcile, refresh
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
    proof={**snap,'head_ref':branch,'head_repository':'Owner/Repo','base_sha_after':None}
    for changed in [{'head_sha_after':'b'*40},{'head_repository':'Other/Repo'},{'head_ref':None},{'state':None}]:
        reconcile(store,'Owner/Repo',1,{**proof,**changed});assert count(store,root)==1
    store.record_session('new','claude',str(root))
    with store._connect() as db:db.execute("UPDATE pr_watch SET owner_session='new' WHERE repository='Owner/Repo' AND number=1")
    reconcile(store,'Owner/Repo',1,proof);assert count(store,root)==1


def test_renamed_published_branch_retires_only_its_exact_owned_tip(local):
    store,root,snap,_=local
    branch=git(root,'branch','--show-current');add(store,root,branch)
    git(root,'branch','unfinished')
    git(root,'switch','-q','unfinished')
    (root/'src.txt').write_text('unpublished source')
    git(root,'add','src.txt');git(root,'commit','-qm','Unpublished change')
    add(store,root,'unfinished');git(root,'switch','-q',branch)
    original=store.snapshot('Owner/Repo',1)
    proof={**snap,'head_ref':'renamed-pr-branch','head_repository':'Owner/Repo','base_sha_after':None}
    reconcile(store,'Owner/Repo',1,proof)
    with store._connect() as db:
        assert [r['branch'] for r in db.execute('SELECT * FROM unpublished_work')]==['unfinished']
        evidence=json.loads(db.execute("SELECT fields FROM audit WHERE event='publication_reconciled'").fetchone()[0])
    assert evidence['proof']=='live_pr_commit' and evidence['completion'] is False
    assert store.snapshot('Owner/Repo',1)==original


@pytest.fixture
def delivered(local):
    store, root, snap, _ = local
    git(root, 'branch', 'shipped')
    if git(root, 'branch', '--show-current') != 'main':
        git(root, 'branch', '-m', 'main')
    (root / 'src.txt').write_text('delivered base advanced')
    git(root, 'add', 'src.txt'); git(root, 'commit', '-qm', 'Another merged change')
    base = git(root, 'rev-parse', 'HEAD')
    add(store, root, 'shipped'); add(store, root, 'main')
    return store, root, snap, base


class LiveBase:
    def __init__(self, base, change=None):
        self.base, self.change, self.calls = base, change, []

    async def get(self, path):
        self.calls.append(path)
        if len(self.calls) == 1:
            return dict(full_name='Owner/Repo', default_branch='main')
        if self.change:
            self.change(len(self.calls))
        return dict(name='main', commit={'sha': self.base})


def test_full_snapshot_retires_shipped_branch_after_switch_and_clean_main(delivered):
    store, root, snap, base = delivered
    proof = {**snap, 'head_ref': 'shipped', 'head_repository': 'Owner/Repo',
             'state': 'closed', 'merged': True, 'base_sha': base, 'base_sha_after': base}
    original = store.snapshot('Owner/Repo', 1)
    reconcile(store, 'Owner/Repo', 1, proof)
    assert count(store, root) == 0
    assert store.snapshot('Owner/Repo', 1) == original
    assert git(root, 'branch', '--show-current') == 'main'


def test_published_pr_head_retires_after_switch_without_default_ancestry(delivered):
    store, root, snap, _ = delivered
    proof = {**snap, 'head_ref': 'shipped', 'head_repository': 'Owner/Repo',
             'state': 'closed', 'merged': True, 'base_sha_after': None}
    reconcile(store, 'Owner/Repo', 1, proof)
    with store._connect() as db:
        assert [r['branch'] for r in db.execute('SELECT * FROM unpublished_work')] == ['main']


def test_live_default_retires_without_an_active_pr_and_preserves_unpublished_commits(delivered):
    store, root, _, base = delivered
    git(root, 'switch', '-qc', 'unfinished')
    (root / 'src.txt').write_text('real unpublished commit')
    git(root, 'add', 'src.txt'); git(root, 'commit', '-qm', 'Unpublished fix')
    add(store, root, 'unfinished'); git(root, 'switch', '-q', 'main')
    with store._connect() as db:
        db.execute("UPDATE prs SET status='retired_merged'")
    client = LiveBase(base)
    result = asyncio.run(refresh(store, 's', cwd=root, client=client))
    assert len(result['retired']) == 2 and result['completion'] is False and not result['errors']
    with store._connect() as db:
        assert [r['branch'] for r in db.execute('SELECT * FROM unpublished_work')] == ['unfinished']
        assert {json.loads(r['fields'])['completion'] for r in db.execute("SELECT * FROM audit WHERE event='publication_reconciled'")} == {False}
    assert len(client.calls) == 3 and store.prs('s') == []


@pytest.mark.parametrize('dirty', ['tracked', 'untracked', 'missing_ref', 'other_origin'])
def test_source_without_exact_clean_publication_identity_stays_local(delivered, dirty):
    store, root, _, base = delivered
    if dirty == 'tracked': (root / 'src.txt').write_text('unpublished edit')
    elif dirty == 'untracked': (root / 'new.py').write_text('unpublished file')
    elif dirty == 'missing_ref':
        git(root, 'branch', '-D', 'shipped')
        with store._connect() as db: db.execute("DELETE FROM unpublished_work WHERE branch='main'")
    else: git(root, 'remote', 'set-url', 'origin', 'https://github.com/Other/Repo.git')
    before = count(store, root); client = LiveBase(base)
    result = asyncio.run(refresh(store, 's', cwd=root, client=client))
    assert count(store, root) == before and not result['retired'] and not client.calls


@pytest.mark.parametrize('race', ['source', 'branch_ref', 'owner', 'record', 'remote'])
def test_source_owner_record_and_remote_races_never_retire(delivered, race):
    store, root, _, base = delivered
    if race == 'branch_ref':
        git(root, 'switch', '-qc', 'new-work')
        (root / 'src.txt').write_text('new unpublished commit')
        git(root, 'add', 'src.txt'); git(root, 'commit', '-qm', 'Unpublished')
        unpublished = git(root, 'rev-parse', 'HEAD'); git(root, 'switch', '-q', 'main')
        with store._connect() as db: db.execute("DELETE FROM unpublished_work WHERE branch='main'")
    def change(call):
        if call != 3: return
        if race == 'source': (root / 'new.txt').write_text('racing source')
        elif race == 'branch_ref': git(root, 'update-ref', 'refs/heads/shipped', unpublished)
        elif race == 'owner':
            store.record_session('foreign', 'codex', str(root))
            with store._connect() as db: db.execute("UPDATE sessions SET parent_session='foreign' WHERE session_id='s'")
        elif race == 'record':
            with store._connect() as db: db.execute('UPDATE unpublished_work SET updated_at=updated_at+1')
        else: client.base = 'a' * 40
    before = count(store, root); client = LiveBase(base, change)
    result = asyncio.run(refresh(store, 's', cwd=root, client=client))
    assert count(store, root) == before and not result['retired']
    assert bool(result['errors']) == (race == 'remote')


def test_remote_failure_retains_records_and_gate_evidence(delivered):
    store, root, _, base = delivered
    original = store.snapshot('Owner/Repo', 1)
    def unavailable(call): raise RuntimeError('GitHub quota exhausted')
    result = asyncio.run(refresh(store, 's', cwd=root, client=LiveBase(base, unavailable)))
    assert count(store, root) == 2 and not result['retired'] and len(result['errors']) == 1
    assert store.snapshot('Owner/Repo', 1) == original


def test_explicit_checkout_and_session_cannot_retire_unrelated_records(delivered, tmp_path):
    store, root, _, base = delivered
    other = tmp_path / 'another-worktree'; git(root, 'worktree', 'add', '--detach', str(other), 'HEAD')
    add(store, other, 'main')
    store.record_session('foreign', 'claude', str(root))
    with store._connect() as db:
        db.execute('INSERT INTO unpublished_work VALUES(?,?,?,?,?)', ('foreign', 'Owner/Repo', 'main', str(root), time.time()))
    result = asyncio.run(refresh(store, 's', cwd=root, client=LiveBase(base)))
    assert len(result['retired']) == 2 and count(store, other) == 1 and count(store, root) == 1


def test_cli_reconciliation_has_no_completion_or_pr_adoption(delivered, capsys, monkeypatch):
    from caller_pr_gates import cli
    store, root, _, base = delivered
    monkeypatch.setenv('CALLER_GATE_SESSION', 's')
    monkeypatch.setenv('CALLER_GATE_OPERATION', 'test')
    with store._connect() as db: db.execute("UPDATE prs SET status='retired_merged'")
    with patch('sys.argv', ['caller-pr-gates', 'publication-reconcile', '--session', 's', '--cwd', str(root)]), \
         patch.object(cli, 'Store', return_value=store), patch('caller_pr_gates.transport.GitHubClient', return_value=LiveBase(base)):
        cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result['completion'] is False and len(result['retired']) == 2
    assert store.prs('s') == []


def test_stop_after_fast_forward_sync_has_no_false_publication_request(delivered):
    from caller_pr_gates import hook
    store, root, _, base = delivered
    with store._connect() as db: db.execute("UPDATE prs SET status='retired_merged'")
    event = dict(hook_event_name='Stop', session_id='s', cwd=str(root), last_assistant_message='Published source work is delivered.')
    with patch('caller_pr_gates.transport.GitHubClient', return_value=LiveBase(base)):
        assert hook.handle(event, store) is None
    assert count(store, root) == 0
