"""Regressions from PR #12 and the live Claude desktop/worktree mismatch."""
import asyncio
import json
import os
from unittest.mock import patch

import pytest

from caller_pr_gates import checklist, hook, monitor, prompts, supervisor
from caller_pr_gates.store import Store
from test_quiet_monitor import local, row, SID
from test_prompt_catalogue import catalogue, edit
from test_single_pr_checklist import tick
from test_workflow import local as git_local, git


def queued(local):
    tick(local, execute=True, spawn=lambda _: None)
    store, _, _, reader, collector = local
    messages=[]
    def runner(argv, **kwargs):
        messages.append(argv[argv.index('--message')+1] if '--message' in argv else kwargs['input_text'])
        return {'returncode':0}
    assert asyncio.run(monitor.deliver(store,SID,reader=reader,collector=collector,runner=runner,availability=lambda *a:'idle',checklist=True))
    return messages[0]


def test_unavailable_queue_releases_other_pr_and_waits_for_its_cadence(local):
    store=local[0]
    store.register(SID,'codex','owner/repo',2,row(store)['cwd'])
    prompt=queued(local)
    with patch.object(monitor,'consume',return_value=None), patch.object(monitor.time,'time',return_value=1001):
        result=checklist.handle(dict(hook_event_name='UserPromptSubmit',session_id=SID,cwd=row(store)['cwd'],prompt=prompt),store)
    assert result=={'continue':False,'stopReason':''}
    assert row(store)['delivery_state']=='pending' and row(store)['lease_until']==0
    assert row(store)['retry_at']==1600
    assert tick(local,1001,execute=True,spawn=lambda _:None)['workers']==1
    assert row(store,2)['delivery_state']=='reserved'
    assert tick(local,1600)['events']==0
    assert row(store)['delivery_state']=='pending'


@pytest.mark.parametrize('client', ['codex','claude'])
def test_authentic_queue_recovers_after_transient_proof_without_replaying_old_token(local,client):
    store,data,calls,reader,collector=local
    data['collected_at']=1000
    with store._connect() as db:
        db.execute('UPDATE sessions SET client=? WHERE session_id=?',(client,SID))
        db.execute('UPDATE pr_watch SET client=?',(client,))
    tick(local,execute=True,spawn=lambda _:None)
    old=row(store)['event_id'];original=monitor.consume;prompts=[]
    async def unavailable(*args):raise TimeoutError('temporary GitHub failure')
    def runner(argv,**kwargs):
        if client=='claude':
            assert '--print' in argv and '--resume' in argv and '--message' not in argv
            prompt=kwargs['input_text']
        else:
            assert 'queue' in argv and '--thread' in argv
            prompt=argv[argv.index('--message')+1]
        prompts.append(prompt)
        assert row(store)['lease_until']>1001 and row(store)['worker_pid']==os.getpid()
        event=dict(hook_event_name='UserPromptSubmit',session_id=SID,cwd=row(store)['cwd'],prompt=prompt)
        assert checklist.handle(event,store)=={'continue':False,'stopReason':''}
        return {'returncode':0}
    with patch.dict(os.environ,CALLER_HOOK_CLIENT=client), patch.object(monitor.time,'time',return_value=1001), patch.object(monitor,'consume',side_effect=lambda *a,**k:original(*a,**dict(k,reader=unavailable))):
        assert asyncio.run(monitor.deliver(store,SID,reader=reader,collector=collector,runner=runner,availability=lambda *a:'idle',checklist=True))
    assert len(prompts)==1
    event=dict(hook_event_name='UserPromptSubmit',session_id=SID,cwd=row(store)['cwd'],prompt=prompts[0])
    deferred=row(store)
    assert deferred['delivery_state']=='pending' and deferred['event_id']!=old
    assert deferred['retry_at']==1301 and deferred['lease_until']==0 and deferred['worker_pid'] is None
    assert deferred['delivered_fingerprint'] is None
    with store._connect() as db:
        db.execute('INSERT INTO watch_sessions VALUES(?,1,1002) ON CONFLICT(session_id) DO UPDATE SET busy=1', (SID,))
    # A delayed canceled callback cannot reject the replacement or mark a new turn idle.
    with patch.dict(os.environ,CALLER_HOOK_CLIENT=client):assert checklist.handle(event,store)['continue'] is False
    with store._connect() as db:assert db.execute('SELECT busy FROM watch_sessions WHERE session_id=?',(SID,)).fetchone()[0]==1
    with patch.object(monitor.time,'time',return_value=1300), patch.object(monitor,'revalidate',side_effect=AssertionError('cadence must avoid proof reads')):
        assert original(store,SID,checklist=True) is None
    with patch.object(monitor.time,'time',return_value=1301):
        assert tick(local,1301)['events']==0
        assert original(store,SID,full=False,reader=reader,checklist=True,hook_event_name='PostToolUse')
        assert original(store,SID,reader=reader,collector=collector,checklist=True) is None
    with store._connect() as db:
        receipts=db.execute("SELECT fields FROM audit WHERE event='pr_monitor_consumed'").fetchall()
    assert len(receipts)==1 and json.loads(receipts[0][0])['client']==client
    assert json.loads(receipts[0][0])['hook_event_name']=='PostToolUse'


def test_deferred_queue_survives_a_partial_outage_then_recovers_once(local):
    store,data,_,reader,_=local
    prompt=queued(local);old=row(store)['event_id']
    with patch.object(monitor,'consume',return_value=None), patch.object(monitor.time,'time',return_value=1001):
        assert checklist.handle(dict(hook_event_name='UserPromptSubmit',session_id=SID,prompt=prompt),store)['continue'] is False
    data['component_errors']={'reviews':{'kind':'transient','message':'temporary outage'}}
    assert tick(local,1301)['events']==0 and row(store)['event_id'] is None
    data.pop('component_errors')
    assert tick(local,1601)['events']==1 and row(store)['event_id']!=old
    assert monitor.consume(store,SID,full=False,reader=reader,checklist=True)
    assert tick(local,1901)['events']==0
    assert monitor.consume(store,SID,reader=reader,checklist=True) is None


def test_unavailable_native_proof_backs_off_without_starving_another_pr(local):
    store,data,calls,reader,collector=local
    data['collected_at']=1000
    store.register(SID,'codex','owner/repo',2,row(store)['cwd']);tick(local)
    attempted=[]
    async def proof(repo,number):
        attempted.append(number)
        if number==1:raise TimeoutError('temporary proof failure')
        return await reader(repo,number)
    with patch.object(monitor.time,'time',return_value=1001):
        assert monitor.consume(store,SID,full=False,reader=proof,checklist=True) is None
        assert monitor.consume(store,SID,full=False,reader=proof,checklist=True)
        assert monitor.consume(store,SID,full=False,reader=proof,checklist=True) is None
    assert attempted==[1,2]
    assert row(store)['delivery_state']=='pending' and row(store)['retry_at']==1600
    assert row(store,2)['delivery_state']=='consumed'


def test_live_wording_edit_accepts_authentic_already_queued_body(local,catalogue,tmp_path):
    prompt=queued(local)
    store,_,_,reader,collector=local
    edit(catalogue,'pr_checklist',prompts.baseline_text('pr_checklist')+' New human wording.')
    original=monitor.consume
    def consume(*args,**kwargs):
        return original(*args,**dict(kwargs,reader=reader,collector=collector))
    with patch.object(monitor,'consume',side_effect=consume):
        # Codex desktop retains the chat launch directory on a queued resume.
        launch=tmp_path/'launch';launch.mkdir()
        assert checklist.handle(dict(hook_event_name='UserPromptSubmit',session_id=SID,cwd=str(launch),prompt=prompt),store) is None
    assert row(store)['delivery_state']=='consumed'
    assert 'New human wording.' not in prompt


def test_changed_queued_body_is_terminal_without_new_authority(local):
    prompt=queued(local)+' Tampered body.'
    store=local[0]
    with patch.object(monitor,'consume',side_effect=AssertionError('unverified body cannot consume')):
        assert checklist.handle(dict(hook_event_name='UserPromptSubmit',session_id=SID,cwd=row(store)['cwd'],prompt=prompt),store)['continue'] is False
    assert row(store)['delivery_state']=='failed'


def test_callback_exception_cancels_queued_work_and_records_failure(local):
    prompt=queued(local);store=local[0]
    event=dict(hook_event_name='UserPromptSubmit',session_id=SID,prompt=prompt)
    assert checklist.failure(event,RuntimeError('failed revalidation'),store)=={'continue':False,'stopReason':''}
    assert row(store)['delivery_state']=='failed'


def test_store_failure_cannot_start_an_unverified_queued_turn():
    event=dict(hook_event_name='UserPromptSubmit',session_id=SID,prompt='<!-- pr-work-event:unverified-->\nAutomated PR hook')
    with patch.object(checklist,'Store',side_effect=RuntimeError('store unavailable')):
        assert checklist.failure(event,RuntimeError('callback failed'))=={'continue':False,'stopReason':''}


@pytest.mark.parametrize('prompt', [
    '<!-- issue-closeout:retired-->\nOld issue automation',
    'Durable prd review followup 11111111-1111-4111-8111-111111111111. The 30-minute check is due.\nOld document automation',
])
def test_store_failure_still_cancels_retired_native_prompts(prompt):
    event=dict(hook_event_name='UserPromptSubmit',session_id=SID,prompt=prompt)
    with patch.object(checklist,'Store',side_effect=RuntimeError('store unavailable')):
        assert checklist.failure(event,RuntimeError('callback failed'))=={'continue':False,'stopReason':''}


def test_posttool_in_another_checkout_does_not_acknowledge_old_work(local,tmp_path):
    tick(local)
    store=local[0]
    other=tmp_path/'another-worktree';other.mkdir()
    with patch.object(monitor,'revalidate',side_effect=AssertionError('wrong checkout must not fetch GitHub')):
        assert checklist.handle(dict(hook_event_name='PostToolUse',session_id=SID,cwd=str(other),tool_name='Read'),store) is None
    assert row(store)['delivery_state']=='pending' and row(store)['delivered_fingerprint'] is None


def test_registration_subdirectory_preserves_checkout_validation_guard(git_local,tmp_path):
    _,root,_,_=git_local
    sub=root/'package';sub.mkdir()
    store=Store(tmp_path/'subdir-state')
    store.register(SID,'codex','Owner/Repo',3,str(sub))
    with store._connect() as db:
        assert monitor.checkout(db.execute('SELECT cwd FROM pr_watch').fetchone()[0])==str(root.resolve())
        # Also admit genuine older receipts that stored the accepted subdirectory.
        db.execute('UPDATE pr_watch SET cwd=?',(str(sub),))
    result=checklist.handle(dict(hook_event_name='PreToolUse',session_id=SID,cwd=str(root),tool_name='Bash',tool_input={'command':'pytest tests'}),store)
    assert result['hookSpecificOutput']['permissionDecision']=='deny'


def test_rulesets_absent_on_plan_do_not_suppress_known_repair_work(local):
    data=local[1]
    data['component_errors']={'rules':{'kind':'feature_unavailable','message':'Rulesets unavailable on plan'}}
    data['component_status']={'rules':'not_available_on_plan'}
    assert tick(local)['events']==1
    data['component_errors']['rules']['kind']='permission_denied'
    assert tick(local,1300)['events']==0 and row(local[0])['event_id'] is None


def test_claude_inventory_launch_root_accepts_its_registered_worktree(git_local,tmp_path):
    _,root,_,_=git_local
    work=tmp_path/'worktree';git(root,'worktree','add','--detach',str(work),'HEAD')
    inventory=[dict(sessionId=SID,cwd=str(root),status='idle')]
    with patch.object(supervisor,'run_process',side_effect=lambda *a,**k:{'returncode':0,'stdout':json.dumps(inventory)}):
        assert supervisor.claude_availability(SID,str(work))=='idle'
        inventory[0]['status']='busy'
        assert supervisor.claude_availability(SID,str(work))=='busy'
        inventory[0]['cwd']=str(tmp_path);inventory[0]['status']='idle'
        assert supervisor.claude_availability(SID,str(work))=='unknown'


def test_unavailable_first_claude_pr_cannot_starve_eligible_second(local,tmp_path):
    store,_,calls,reader,collector=local
    second=tmp_path/'second';second.mkdir()
    store.record_session(SID,'claude',str(second))
    store.register(SID,'claude','owner/repo',2,str(second))
    with store._connect() as db:
        db.execute("UPDATE pr_watch SET client='claude'")
        db.execute("UPDATE sessions SET client='claude' WHERE session_id=?",(SID,))
    tick(local,execute=True,spawn=lambda _:None)
    calls.clear();launched=[]
    def availability(sid,cwd):return 'idle' if cwd==str(second) else 'unknown'
    def runner(argv,**kwargs):
        launched.append(kwargs)
        return {'returncode':0}
    assert asyncio.run(monitor.deliver(store,SID,reader=reader,collector=collector,runner=runner,availability=availability,checklist=True))
    assert calls==[('owner/repo',2)] and launched[0]['cwd']==str(second)
    assert row(store)['delivery_state']=='pending' and row(store,2)['delivery_state']=='queued'


def test_unavailable_claude_checkout_never_spends_github_reads(local):
    store=local[0];store.record_session(SID,'claude',row(store)['cwd'])
    with store._connect() as db:db.execute("UPDATE sessions SET client='claude' WHERE session_id=?",(SID,))
    tick(local,execute=True,spawn=lambda _:None)
    async def never(*args):pytest.fail('unavailable local checkout must not fetch GitHub')
    assert not asyncio.run(monitor.deliver(store,SID,reader=never,collector=never,availability=lambda *a:'unknown',checklist=True))
    assert row(store)['delivery_state']=='pending'


def test_claude_execution_error_cannot_count_as_successful_delivery(local):
    store,_,_,reader,collector=local
    with store._connect() as db:db.execute("UPDATE sessions SET client='claude' WHERE session_id=?",(SID,))
    tick(local,execute=True,spawn=lambda _:None)
    with pytest.raises(RuntimeError,match='Owner delivery failed'):
        asyncio.run(monitor.deliver(store,SID,reader=reader,collector=collector,availability=lambda *a:'idle',
            runner=lambda *a,**k:{'returncode':0,'stdout':json.dumps({'is_error':True,'result':'OAuth expired'})},checklist=True))


def test_worker_error_after_consumption_is_recorded_and_does_not_replay(local):
    store=local[0];tick(local)
    with store._connect() as db:
        db.execute("UPDATE pr_watch SET delivery_state='consumed',delivered_fingerprint=fingerprint,worker_pid=?,lease_until=?",(os.getpid(),9999999999))
    async def failed(*a,**k):raise RuntimeError('OAuth execution failed')
    with patch('sys.argv',['monitor','deliver','--session',SID]),patch('caller_pr_gates.store.Store',return_value=store),patch.object(monitor,'deliver',side_effect=failed):
        monitor.main()
    assert row(store)['delivery_state']=='failed' and row(store)['error']=='OAuth execution failed'
    assert row(store)['worker_pid'] is None and row(store)['lease_until']==0
    assert tick(local,1300)['events']==0


def test_worker_error_preserves_another_prs_successful_consumption(local):
    store=local[0];tick(local)
    store.register(SID,'codex','owner/repo',2,row(store)['cwd'])
    with store._connect() as db:
        db.execute("UPDATE pr_watch SET delivery_state='consumed',worker_pid=NULL,lease_until=0 WHERE number=1")
        db.execute("UPDATE pr_watch SET delivery_state='consumed',worker_pid=?,lease_until=? WHERE number=2",(os.getpid(),9999999999))
    async def failed(*a,**k):raise RuntimeError('Current execution failed')
    with patch('sys.argv',['monitor','deliver','--session',SID]),patch('caller_pr_gates.store.Store',return_value=store),patch.object(monitor,'deliver',side_effect=failed):
        monitor.main()
    assert row(store)['delivery_state']=='consumed' and row(store)['error'] is None
    assert row(store,2)['delivery_state']=='failed' and row(store,2)['worker_pid'] is None


def test_worker_claims_pid_before_fast_consumption_and_error(local):
    store,_,_,reader,collector=local;tick(local)
    tick(local,1001,execute=True,spawn=lambda _:None)
    async_deliver=monitor.deliver
    def runner(*a,**k):
        with store._connect() as db:
            db.execute("UPDATE pr_watch SET delivery_state='consumed' WHERE number=1")
        return {'returncode':1}
    async def failed(*a,**k):
        return await async_deliver(store,SID,reader=reader,collector=collector,runner=runner,checklist=True)
    with patch('sys.argv',['monitor','deliver','--session',SID]),patch('caller_pr_gates.store.Store',return_value=store),patch.object(monitor,'deliver',side_effect=failed):
        monitor.main()
    assert row(store)['delivery_state']=='failed' and row(store)['lease_until']==0
