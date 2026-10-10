import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from caller_pr_gates import cli, hook, monitor, supervisor
from caller_pr_gates.store import Store

SID = '44444444-4444-4444-8444-444444444444'
A = {'agreed_scope':'Fix the first authorized defect only', 'acceptance_criteria':['First defect fixed'],
     'slice_count':1, 'slice_ids':['BUG-01'], 'approved_at':100.0,
     'approval_evidence':'Fix the listed bugs one by one'}
B = {'agreed_scope':'Fix the next authorized defect only', 'acceptance_criteria':['Next defect fixed'],
     'slice_count':1, 'slice_ids':['BUG-02'], 'approved_at':200.0,
     'approval_evidence':'That PR is merged; move to the next one'}


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path/'state')
    store.record_session(SID, 'codex', str(tmp_path))
    store.record_scope(SID, A)
    return store


def slice_job(store, number, scope):
    job = store.register_followup(SID, 'owner/repo', number, 0, scope, kind='slice', issue_number=number)
    store.update_followup(job['id'], 'pending', {'slice_id':scope['slice_ids'][0]})
    return next(j for j in store.followups(SID) if j['id']==job['id'])


def test_next_authorized_item_preserves_original_and_survives_restart(state):
    state.record_scope(SID, B, work_item='BUG-02')
    restarted = Store(state.state_dir)
    assert restarted.get_scope(SID)==B
    assert restarted.get_scope(SID, work_item='original')==A
    assert restarted.get_scope(SID, work_item='BUG-02')==B
    assert restarted.scope_baselines(SID)==[A,B]
    with state._connect() as db:
        assert json.loads(db.execute('SELECT scope FROM scopes WHERE session_id=?',(SID,)).fetchone()[0])==A


@pytest.mark.parametrize('work_item', ['../next','', 'x'*129])
def test_work_item_identifier_is_bounded(state, work_item):
    with pytest.raises(ValueError, match='stable identifier'):
        state.record_scope(SID, B, work_item=work_item)
    assert state.get_scope(SID)==A


def test_each_baseline_remains_immutable_and_requires_actual_approval(state):
    with pytest.raises(ValueError, match='immutable'):
        state.record_scope(SID, B)
    with pytest.raises(ValueError, match='approval_evidence'):
        state.record_scope(SID, dict(B,approval_evidence=' '),work_item='BUG-02')
    state.record_scope(SID,B,work_item='BUG-02')
    with pytest.raises(ValueError, match='immutable'):
        state.record_scope(SID,dict(B,slice_ids=['EXTRA']),work_item='BUG-02')
    assert state.get_scope(SID,work_item='original')==A
    assert state.get_scope(SID)==B


def test_existing_pr_binding_cannot_move_to_selected_next_scope(state):
    state.register(SID,'codex','owner/repo',1,state.session(SID)['cwd'])
    state.record_scope(SID,B,work_item='BUG-02')
    state.register(SID,'codex','owner/repo',2,state.session(SID)['cwd'])
    state.register(SID,'codex','owner/repo',1,state.session(SID)['cwd'])
    assert state.pr_scope(SID,'owner/repo',1)==A
    assert state.pr_scope(SID,'owner/repo',2)==B
    restarted=Store(state.state_dir)
    assert restarted.pr_scope(SID,'owner/repo',1)==A
    assert restarted.pr_scope(SID,'owner/repo',2)==B


def test_due_old_review_verifies_original_scope_after_new_item_selected(state):
    old=slice_job(state,1,A)
    state.record_scope(SID,B,work_item='BUG-02')
    slice_job(state,2,B)
    read_at=time.time()-1
    state.update_followup(old['id'],'pending',{'review_read':{'read_at':read_at,'head_sha':'current-head',
                         'comment_ids':[], 'scope_digest':supervisor.scope_digest(A)}})
    old=next(j for j in state.followups(SID) if j['id']==old['id'])
    snapshot={'collected_at':time.time(),'head_sha':'current-head','comments':[]}
    gates=[{'gate':name,'status':'PASS'} for name in ('PR_NOT_DRAFT','CI_FAILED','CI_PENDING','REVIEW_HANDLED','REVIEW_CONCLUDED','MERGE_CONFLICT')]
    assert supervisor.complete_verified_followup(state,old,snapshot,gates)[0]
    assert state.get_scope(SID)==B
    assert state.followup_scope(old)==A
    assert hook._scope_gate(state,SID)==[]


def test_undeclared_replacement_scope_never_authorizes_old_review(state):
    job=slice_job(state,1,A)
    state.record_scope(SID,B,work_item='BUG-02')
    tampered=dict(job,scope=dict(A,acceptance_criteria=['Unapproved new criterion']))
    assert state.followup_scope(tampered) is None
    assert supervisor.recorded_job_scope(state,tampered) is None
    with state._connect() as db:
        db.execute('UPDATE followups SET scope=? WHERE id=?',(json.dumps(tampered['scope']),job['id']))
    assert 'no recorded immutable baseline' in hook._scope_gate(state,SID)[0]


def test_old_slice_count_cannot_borrow_allowance_from_new_item(state):
    slice_job(state,1,A)
    extra=state.register_followup(SID,'owner/repo',3,0,A,kind='slice',issue_number=3)
    state.update_followup(extra['id'],'pending',{'slice_id':'EXTRA'})
    state.record_scope(SID,B,work_item='BUG-02')
    slice_job(state,2,B)
    assert 'original approved slice IDs/count' in hook._scope_gate(state,SID)[0]


def test_old_document_reregistration_keeps_its_exact_baseline(state):
    old=slice_job(state,1,A)
    state.record_scope(SID,B,work_item='BUG-02')
    assert state.register_followup(SID,'owner/repo',1,0,A,kind='slice',issue_number=1)['id']==old['id']
    with pytest.raises(ValueError,match='cannot change its scope'):
        state.register_followup(SID,'owner/repo',1,0,B,kind='slice',issue_number=1)


def test_second_work_item_publication_uses_its_own_count_without_bypassing_first(state):
    slice_job(state,1,A)
    state.record_scope(SID,B,work_item='BUG-02')
    args=SimpleNamespace(session=SID,client='codex',cwd=state.session(SID)['cwd'],kind='slice',slice_id='BUG-02',target=None,
                         arguments=['issue','create','--title','Slice 2','--body','Second authorized defect'])
    async def collect(*args): return {'created_at':0}
    with patch.object(cli.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout='https://github.com/owner/repo/issues/2\n')),patch('caller_pr_gates.github.collect_issue',collect):
        cli.gateway(args,state)
    assert len(state.followups(SID))==2
    assert hook._scope_gate(state,SID)==[]
    with patch.object(cli.subprocess,'run',side_effect=AssertionError('must not publish duplicate')):
        with pytest.raises(ValueError,match='already has a published document'):
            cli.gateway(args,state)


def test_concurrent_work_items_keep_both_baselines(state):
    def record(i):
        scope=dict(B,agreed_scope=f'Authorized defect {i}',slice_ids=[f'BUG-{i}'])
        Store(state.state_dir).record_scope(SID,scope,work_item=f'BUG-{i}')
        return scope
    with ThreadPoolExecutor(max_workers=2) as pool:
        scopes=list(pool.map(record,[2,3]))
    assert all(s in state.scope_baselines(SID) for s in scopes)
    assert state.get_scope(SID,work_item='original')==A


def test_new_child_scope_cannot_be_reparented_and_follows_actual_parent(state):
    state.record_session('child','claude','/project',SID)
    state.record_scope('child',B,work_item='BUG-02')
    assert state.get_scope(SID)==B
    assert state.get_scope('child',work_item='original')==A
    state.record_scope('ownerless',B,work_item='NEXT')
    with pytest.raises(ValueError,match='cannot be reparented'):
        state.record_session('ownerless','codex','/project',SID)


def test_monitor_message_uses_pr_baseline_not_current_selection(state):
    state.register(SID,'codex','owner/repo',1,state.session(SID)['cwd'])
    state.record_scope(SID,B,work_item='BUG-02')
    row={'owner_session':SID,'repository':'owner/repo','number':1}
    payload=monitor.work_payload(state,row,{'head_sha':'actual-head'},['CI failing: tests'])
    text=monitor.message([dict(row,payload=payload)])
    assert A['agreed_scope'] in text
    assert B['agreed_scope'] not in text


def test_cli_creates_named_item_and_reuses_its_approved_timestamp(state,tmp_path,capsys,monkeypatch):
    path=tmp_path/'scope.json';path.write_text(json.dumps({k:v for k,v in B.items() if k!='approved_at'}))
    monkeypatch.setenv('CALLER_PR_GATES_STATE_DIR',str(state.state_dir))
    monkeypatch.setattr(sys,'argv',['caller-pr-gates','scope','--session',SID,'--work-item','BUG-02','--file',str(path)])
    cli.main();first=json.loads(capsys.readouterr().out)
    cli.main();second=json.loads(capsys.readouterr().out)
    assert first['scope']==second['scope'] and first['immutable']
    assert state.get_scope(SID,work_item='original')==A
