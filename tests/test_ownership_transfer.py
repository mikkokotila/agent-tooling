import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest

from caller_pr_gates import ownership
from caller_pr_gates.store import Store

OLD = '11111111-1111-4111-8111-111111111111'
NEW = '22222222-2222-4222-8222-222222222222'
REPO = 'owner/repo'


def baseline(approval='Original human request'):
    return dict(agreed_scope='Repair the existing PR only', acceptance_criteria=['Keep its regression guard'],
                slice_count=0, slice_ids=[], approved_at=1, approval_evidence=approval)


@pytest.fixture
def context(tmp_path, monkeypatch):
    old, new = tmp_path / 'original', tmp_path / 'destination'
    old.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(old), *args], check=True, text=True, capture_output=True).stdout.strip()
    git('init', '-b', 'main'); git('config', 'user.email', 'fixture@example.test'); git('config', 'user.name', 'Fixture')
    (old / 'source.txt').write_text('original\n')
    git('add', 'source.txt'); git('commit', '-m', 'fix: retain regression')
    git('remote', 'add', 'origin', 'https://github.com/owner/repo.git')
    git('worktree', 'add', '--detach', str(new))
    head = git('rev-parse', 'HEAD')
    home = tmp_path / 'codex'; (home / 'sessions').mkdir(parents=True)
    monkeypatch.setenv('CODEX_HOME', str(home))
    quote = 'Repair all open PRs by this contributor in this repository.'
    raw = json.dumps(dict(type='response_item', payload=dict(type='message', role='user',
                     content=[dict(type='input_text', text=quote)])))
    for sid in (OLD, NEW):
        (home / 'sessions' / (sid + '.jsonl')).write_text(raw + '\n')
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY,cwd TEXT,archived INTEGER,rollout_path TEXT)')
        for sid in (OLD, NEW):
            db.execute('INSERT INTO threads VALUES(?,?,0,?)', (sid, str(old), str(home / 'sessions' / (sid + '.jsonl'))))
    store = Store(tmp_path / 'state')
    store.record_session(OLD, 'codex', str(old)); store.record_scope(OLD, baseline())
    store.register(OLD, 'codex', REPO, 1, str(old))
    store.record_session(NEW, 'codex', str(new)); store.record_scope(NEW, baseline('Authorized repair'))
    store.register(NEW, 'codex', REPO, 1, str(new))
    grant = dict(repository=REPO, number=1, author='contributor', head_sha=head,
                 **{'from':dict(session_id=OLD,client='codex',cwd=str(old)),
                    'to':dict(session_id=NEW,client='codex',cwd=str(new))},
                 human_messages=[dict(line_sha256=hashlib.sha256(raw.encode()).hexdigest(), quote=quote)])
    scope = dict(baseline(quote), pr_ownership_transfer=grant)
    store.record_scope(NEW, scope, work_item='human-transfer')
    pull = dict(number=1,url='https://api.github.com/repos/owner/repo/pulls/1',state='open',merged=False,draft=False,
                head=dict(sha=head,repo=dict(full_name=REPO)),base=dict(repo=dict(full_name=REPO)),user=dict(login='contributor'))
    async def reader(path):
        assert path == '/repos/owner/repo/pulls/1'
        return deepcopy(pull)
    return dict(store=store,old=old,new=new,home=home,grant=grant,scope=scope,pull=pull,reader=reader)


def transfer(context, work_item='human-transfer', reader=None):
    return asyncio.run(ownership.transfer(context['store'],NEW,REPO,1,'codex',str(context['new']),work_item,
                                         reader=reader or context['reader']))


def watch(store):
    with store._connect() as db:
        return dict(db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=1',(REPO,)).fetchone())


def test_transfer_preserves_original_evidence_and_retry_limits(context):
    store=context['store']
    store.record_recovery(REPO,1,OLD,dict(ci_reruns=dict(head_sha=context['grant']['head_sha'],run_ids=['123'])))
    with store._connect() as db:
        before={table:[tuple(row) for row in db.execute('SELECT * FROM '+table)]
                for table in ('prs','scopes','scope_items','validation_plans','validation_runs','dispositions')}
    receipt=transfer(context)
    assert watch(store)['owner_session']==NEW
    assert watch(store)['cwd']==str(context['new'])
    assert store.pr_scope(NEW,REPO,1)==store.pr_scope(OLD,REPO,1)==baseline()
    assert receipt['completion_verified'] is False
    assert store.recovery(REPO,1,NEW)['ci_reruns']['run_ids']==['123']
    assert store.recovery(REPO,1,OLD)['ci_reruns']['run_ids']==['123']
    with store._connect() as db:
        assert {table:[tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in before}==before
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='pr_owner_transferred'").fetchone()[0]==1
    with pytest.raises(ValueError,match='expected original owner'):
        transfer(context)


@pytest.mark.parametrize('sid',[OLD,NEW])
def test_archived_session_refuses_without_changing_owner(context,sid):
    with sqlite3.connect(context['home']/'state_5.sqlite') as db:
        db.execute('UPDATE threads SET archived=1 WHERE id=?',(sid,))
    with pytest.raises(ValueError,match='archived'):
        transfer(context)
    assert watch(context['store'])['owner_session']==OLD


def test_assistant_quote_cannot_authorize_a_transfer(context):
    path=context['home']/'sessions'/(NEW+'.jsonl')
    record=json.loads(path.read_text());record['payload']['role']='assistant'
    raw=json.dumps(record);path.write_text(raw+'\n')
    scope=deepcopy(context['scope']);scope['pr_ownership_transfer']['human_messages'][0]['line_sha256']=hashlib.sha256(raw.encode()).hexdigest()
    context['store'].record_scope(NEW,scope,work_item='forged-role')
    with pytest.raises(ValueError,match='human user message'):
        transfer(context,'forged-role')


@pytest.mark.parametrize('field,value',[('repository','other/repo'),('number',2),('author','somebody'),('head_sha','b'*40)])
def test_grant_cannot_select_another_pr_author_or_head(context,field,value):
    scope=deepcopy(context['scope']);scope['pr_ownership_transfer'][field]=value
    context['store'].record_scope(NEW,scope,work_item='wrong-grant')
    with pytest.raises(ValueError):
        transfer(context,'wrong-grant')
    assert watch(context['store'])['owner_session']==OLD


@pytest.mark.parametrize('change',[dict(owner_session=NEW),dict(cwd='/other/checkout'),dict(state='merged'),
                                  dict(lease_until=time.time()+600),dict(delivery_state='queued')])
def test_changed_owner_or_monitor_worker_refuses(context,change):
    with context['store']._connect() as db:
        for key,value in change.items():db.execute('UPDATE pr_watch SET '+key+'=?',(value,))
    with pytest.raises(ValueError):transfer(context)


def test_a_head_move_during_collection_refuses(context):
    calls=0
    async def changed(path):
        nonlocal calls
        calls+=1
        pull=deepcopy(context['pull'])
        if calls==2:pull['head']['sha']='c'*40
        return pull
    with pytest.raises(ValueError,match='Live PR'):
        transfer(context,reader=changed)
    assert watch(context['store'])['owner_session']==OLD


def test_dirty_original_checkout_is_preserved_and_refused(context):
    (context['old']/'source.txt').write_text('work still in progress\n')
    with pytest.raises(ValueError,match='clean exact source'):
        transfer(context)
    assert (context['old']/'source.txt').read_text()=='work still in progress\n'
    assert watch(context['store'])['owner_session']==OLD


@pytest.mark.parametrize('running_number',[1,2])
def test_running_validation_is_scoped_to_the_transferred_pr(context,running_number):
    with context['store']._connect() as db:
        db.execute('INSERT INTO validation_plans VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   ('plan',OLD,REPO,running_number,str(context['old']),'digest','{}','[]',1,1,1))
        db.execute('INSERT INTO validation_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('run','plan','application','digest','running',None,'digest',None,1,None,None,None))
    if running_number==1:
        with pytest.raises(ValueError,match='running validation'):transfer(context)
    else:
        transfer(context)
        assert watch(context['store'])['owner_session']==NEW


def test_general_registration_does_not_transfer(context):
    context['store'].register(NEW,'codex',REPO,1,str(context['new']))
    assert watch(context['store'])['owner_session']==OLD
