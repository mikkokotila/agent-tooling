"""Regressions from PR #12 and the live Claude desktop/worktree mismatch."""
import asyncio
import json
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
        messages.append(argv[argv.index('--message')+1])
        return {'returncode':0}
    assert asyncio.run(monitor.deliver(store,SID,reader=reader,collector=collector,runner=runner,checklist=True))
    return messages[0]


def test_rejected_queue_releases_other_pr_without_replaying_failure(local):
    store=local[0]
    store.register(SID,'codex','owner/repo',2,row(store)['cwd'])
    prompt=queued(local)
    with patch.object(monitor,'consume',return_value=None):
        result=checklist.handle(dict(hook_event_name='UserPromptSubmit',session_id=SID,cwd=row(store)['cwd'],prompt=prompt),store)
    assert result=={'continue':False,'stopReason':''}
    assert row(store)['delivery_state']=='failed' and row(store)['lease_until']==0
    assert tick(local,1001,execute=True,spawn=lambda _:None)['workers']==1
    assert row(store,2)['delivery_state']=='reserved'
    assert tick(local,1600)['events']==0
    assert row(store)['delivery_state']=='failed'


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
        assert db.execute('SELECT cwd FROM pr_watch').fetchone()[0]==str(root.resolve())
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
    with store._connect() as db:db.execute("UPDATE pr_watch SET client='claude'")
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
    tick(local,execute=True,spawn=lambda _:None)
    async def never(*args):pytest.fail('unavailable local checkout must not fetch GitHub')
    assert not asyncio.run(monitor.deliver(store,SID,reader=never,collector=never,availability=lambda *a:'unknown',checklist=True))
    assert row(store)['delivery_state']=='pending'
