"""Offline request budgets, mutable evidence and cross-process sharing boundaries."""
import asyncio, json, multiprocessing, time, os
from pathlib import Path
import pytest, httpx
from functools import wraps

def async_test(function):
    @wraps(function)
    def run(*args,**kwargs):return asyncio.run(function(*args,**kwargs))
    return run

from caller_pr_gates import github, cli, progress, workflow
from caller_pr_gates.collections import Collections
from caller_pr_gates.quota import Coordinator
from caller_pr_gates.transport import GitHubClient
from caller_pr_gates.policy import evaluate_pr,evaluate_review
from caller_pr_gates.store import Store
from test_github import Client,SHA,STAMP
from test_policy import closing_graphql,fresh_branch

class BudgetClient(Client):
    def __init__(self,tmp_path):
        super().__init__();self.coordinator=Coordinator(tmp_path);self.account='fixture';self.graphs=[]
        self.ci='success';self.body='Please fix this edge case';self.rule_context='test';self.fail=False
    async def get(self,path,params=None):
        if self.fail and path.endswith('/check-runs'):raise RuntimeError('503 unavailable')
        value=await super().get(path,params)
        if path.endswith('/check-runs'):value['check_runs'][0]['conclusion']=self.ci
        if path.endswith('/comments'):value[0]['body']=self.body
        return value
    async def graphql(self,query,variables):
        self.graphs.append(query)
        if query==github.BATCH_QUERY:
            result=closing_graphql(variables,merged=True)
            result['viewer']={'login':'agent'}
            pull=result['repository']['pullRequest']
            pull['baseRef']={'branchProtectionRule':{'requiredStatusCheckContexts':[self.rule_context]}}
            for q in [github.THREAD_QUERY,github.REVIEWS_QUERY,github.COMMITS_QUERY]:
                part=await super().graphql(q,variables);pull.update(part['repository']['pullRequest'])
            return result
        return await super().graphql(query,variables)

@async_test
async def test_full_budget_batching_immutable_files_and_same_head_mutations(tmp_path):
    old=Client();await github.collect_with_client(old,'mikkokotila/caller',50)
    assert len(old.paths)==13 # 12 REST baseline + this fixture's one workflow job page.
    c=BudgetClient(tmp_path)
    first=await github.collect_with_client(github._BatchClient(c),'mikkokotila/caller',50)
    assert len(c.paths)==12 and len(c.graphs)==1
    assert [(g['gate'],g['status']) for g in evaluate_pr(first,[])]==[(g['gate'],g['status']) for g in evaluate_pr(await github.collect_with_client(Client(),'mikkokotila/caller',50),[])]
    c.paths.clear();c.graphs.clear();c.ci='failure';c.body='Changed review';c.rule_context='another'
    second=await github.collect_with_client(github._BatchClient(c),'mikkokotila/caller',50)
    assert len(c.paths)==11 and len(c.graphs)==1
    assert not any(p.endswith('/files') for p in c.paths)
    assert second['expected_checks']==['another']
    assert second['comments'][0]['body']=='Changed review'
    assert next(g for g in evaluate_pr(second,[]) if g['gate']=='CI_FAILED')['status']=='FAIL'
    c.fail=True
    with pytest.raises(RuntimeError,match='unavailable'):
        await github.collect_with_client(github._BatchClient(c),'mikkokotila/caller',50)

@async_test
async def test_validation_and_review_profiles_retain_only_required_proof(tmp_path):
    c=Client();pre=await github.collect_validation_with_client(c,'mikkokotila/caller',50)
    assert len(c.paths)==5 and pre['profile']=='validation'
    assert 'checks' not in pre and 'comments' not in pre
    c=Client();review=await github.collect_review_with_client(c,'mikkokotila/caller',50)
    assert len(c.paths)==4 and review['profile']=='review'
    full=await github.collect_with_client(Client(),'mikkokotila/caller',50)
    assert evaluate_review(review,[])==evaluate_review(full,[])
    s=Store(tmp_path)
    for part in [pre,review]:
        with pytest.raises(ValueError,match='Partial'):s.set_snapshot('mikkokotila/caller',50,part,[])
    # Plan + N checks: one baseline, then two 5-REST preflights per check.
    n=3
    assert 13+10*n < 13*(1+2*n)

@async_test
async def test_profile_races_and_missing_ancestry_fail_closed():
    with pytest.raises(github.EvidenceError):await github.collect_validation_with_client(Client(head_race=True),'mikkokotila/caller',50)
    from caller_pr_gates.policy import branch_freshness_gate
    with pytest.raises(github.EvidenceError):await github.collect_review_with_client(Client(head_race=True),'mikkokotila/caller',50)
    class Outage(Client):
        async def get(self,path,params=None):
            if '/branches/' in path:raise RuntimeError('quota unavailable')
            return await super().get(path,params)
    p=await github.collect_validation_with_client(Outage(),'mikkokotila/caller',50)
    p.update(state='open',merged=False)
    assert branch_freshness_gate(p)['status']=='UNKNOWN'

@async_test
async def test_conditional_read_is_authenticated_and_never_falls_back_on_error(tmp_path,monkeypatch):
    from github_interface import client as original
    monkeypatch.setattr(original,'get_token',lambda *a:'secret-test-token')
    c=GitHubClient(coordinator=Coordinator(tmp_path));seen=[];responses=[200,304,503,503]
    async def request(self,method,path,**kw):
        seen.append({**dict(self.headers),**kw['headers']});status=responses.pop(0)
        return httpx.Response(status,headers={'etag':'"version1"'} if status==200 else {},json={'fresh':True} if status==200 else None,request=httpx.Request(method,'https://api.github.com'+path))
    monkeypatch.setattr(httpx.AsyncClient,'request',request)
    assert await c.get('/repos/o/r/pulls/1')=={'fresh':True}
    assert await c.get('/repos/o/r/pulls/1')=={'fresh':True}
    assert seen[1]['if-none-match']=='"version1"' and seen[1]['authorization']=='Bearer secret-test-token'
    with pytest.raises(RuntimeError):await c.get('/repos/o/r/pulls/1')
    with c.coordinator.db() as db:
        rows=[json.loads(r[0]) for r in db.execute('SELECT fields FROM attribution')]
    assert sum(r.get('requests',0) for r in rows)==4
    assert 'secret-test-token' not in json.dumps(rows)

@async_test
async def test_strict_fresh_boundary_profiles_accounts_and_failure(tmp_path):
    co=Coordinator(tmp_path);a=Collections(co,'a');calls=[]
    async def produce():calls.append(1);await asyncio.sleep(.02);return {'at':time.time()}
    first,second=await asyncio.gather(a.run('full','PR',produce),a.run('full','PR',produce))
    assert first==second and len(calls)==1
    await a.run('full','PR',produce);assert len(calls)==2 # A later --fresh cannot reuse.
    await a.run('review','PR',produce);await Collections(co,'b').run('full','PR',produce)
    assert len(calls)==4
    async def bad():raise RuntimeError('unavailable')
    results=await asyncio.gather(a.run('full','bad',bad),a.run('full','bad',bad),return_exceptions=True)
    assert all(isinstance(r,RuntimeError) for r in results)
    assert await a.run('full','bad',produce)


def child_collection(directory,start,queue):
    async def task():
        co=Coordinator(directory);collection=Collections(co,'same')
        async def producer():
            queue.put('request');await asyncio.sleep(.04);return {'complete':True}
        start.wait();queue.put(await collection.run('full','target',producer))
    asyncio.run(task())

def test_actual_processes_coalesce_monitor_supervisor_and_stop_status(tmp_path):
    ctx=multiprocessing.get_context('spawn');start=ctx.Event();queue=ctx.Queue()
    # Precreate schema, then align entry into the explicit pending window.
    Collections(Coordinator(tmp_path),'same')
    workers=[ctx.Process(target=child_collection,args=(str(tmp_path),start,queue)) for _ in range(2)]
    for worker in workers:worker.start()
    start.set()
    for worker in workers:worker.join(5);assert worker.exitcode==0
    rows=[queue.get(timeout=1) for _ in range(3)]
    assert rows.count('request')==1 and rows.count({'complete':True})==2

from test_workflow import local,publish,git

def test_narrow_preflight_never_promotes_complete_and_unknown_invalidates(local):
    store,root,snap,plan=local
    (root/'src.txt').write_text('fixed');publish(local)
    row=workflow.save_plan(store,'s','Owner/Repo',1,root,plan)['plan']
    before=snap['collected_at'];pre={**snap,'profile':'validation','collected_at':before+.001}
    store.set_preflight('Owner/Repo',1,pre)
    assert workflow.current_phase(store,row)['phase']=='VALIDATE_REQUIRED'
    run=store.begin_validation(row['plan_id'],'regression',row['input_digest'])
    store.finish_validation(run['run_id'],0,row['input_digest'])
    assert workflow.current_phase(store,row)['phase']=='WAIT_REMOTE'
    assert store.snapshot('Owner/Repo',1)['snapshot']['collected_at']==before
    store.set_preflight('Owner/Repo',1,{'profile':'validation','collected_at':None})
    assert workflow.current_phase(store,row)['phase']=='REMOTE_READ_REQUIRED'
    store.set_preflight('Owner/Repo',1,pre);snap['collected_at']=time.time()+.01
    store.set_snapshot('Owner/Repo',1,snap,evaluate_pr(snap,[]))
    assert workflow.current_phase(store,row)['phase']=='COMPLETE'


def test_plan_and_background_launcher_worker_use_profiles(local,tmp_path,monkeypatch):
    import sys
    store,root,snap,plan=local
    (root/'src.txt').write_text('fixed');publish(local)
    plan={'checks':[dict(id=f'c{i}',argv=[sys.executable,'check.py',str(i)],reason='Distinct fixture check') for i in range(3)]}
    file=tmp_path/'plan.json';file.write_text(json.dumps(plan));counts={'full':0,'preflight':0}
    async def narrow(*a):counts['preflight']+=1;return {**snap,'profile':'validation','collected_at':time.time()}
    async def full(*a):counts['full']+=1;return {**snap,'collected_at':time.time()}
    monkeypatch.setattr(github,'collect_validation',narrow);monkeypatch.setattr(github,'collect_pr',full)
    monkeypatch.setattr(cli,'Store',lambda:store)
    monkeypatch.setattr(workflow,'launch_check',lambda *a:{'started':True})
    monkeypatch.setattr(workflow,'run_check',lambda *a,**k:{'fixture':True})
    with store._connect() as db:db.execute('DELETE FROM snapshots')
    base=['caller-pr-gates','--session','s','--pr','https://github.com/Owner/Repo/pull/1','--cwd',str(root)]
    monkeypatch.setattr(sys,'argv',[base[0],'validation-plan',*base[1:],'--file',str(file)])
    assert cli.main() is None
    for i in range(3):
        for mode in ['--background','--worker']:
            monkeypatch.setattr(sys,'argv',[base[0],'validate',*base[1:],'--check',f'c{i}',mode]);assert cli.main() is None
    assert counts=={'full':1,'preflight':7}
    # An unplanned check is rejected locally without even a narrow API read.
    monkeypatch.setattr(sys,'argv',[base[0],'validate',*base[1:],'--check','absent','--background'])
    with pytest.raises(SystemExit) as failure:cli.main()
    assert failure.value.code==1 and counts=={'full':1,'preflight':7}

@async_test
async def test_cooldown_blocks_collection_before_healthy_resource_reads(tmp_path):
    co=Coordinator(tmp_path);p=co.admit('same','core')
    co.throttle(p,{'http_status':403,'message':'API rate limit exceeded','rate_limit_headers':{'x-ratelimit-resource':'core'}})
    async def unexpected():pytest.fail('No graph/other reads should start for an impossible collection')
    with pytest.raises(RuntimeError,match='rate limit'):await Collections(co,'same').run('full-pr','target',unexpected)
    assert co.admit('same','graphql') # Independently healthy standalone operations remain available.

@async_test
async def test_collection_cancellation_and_late_worker_are_fenced(tmp_path):
    co=Coordinator(tmp_path);a=Collections(co,'a');entered=asyncio.Event();hold=asyncio.Event()
    async def producer():entered.set();await hold.wait();return {'old':True}
    task=asyncio.create_task(a.run('full','target',producer));await entered.wait()
    # Simulate a crashed lease and a replacement token while the old worker runs.
    with co.db() as db:db.execute("UPDATE collections SET token='newer',phase='result',payload='{}',started=0,started_mono=0 WHERE account='a'")
    hold.set()
    with pytest.raises(RuntimeError,match='late'):await task
    with co.db() as db:assert db.execute("SELECT token,payload FROM collections WHERE account='a'").fetchone()['payload']=='{}'
    task=asyncio.create_task(a.run('full','another',producer));await asyncio.sleep(.001);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    async def healthy():return {'fresh':True}
    assert await a.run('full','another',healthy)=={'fresh':True}

@async_test
async def test_repeated_review_action_budget_and_changed_replies():
    c=Client()
    for _ in range(3):await github.collect_review_with_client(c,'mikkokotila/caller',50)
    assert len(c.paths)==12 # address+resolve+review-read: 4 REST each, no CI/rules/ancestry reload.
    assert not any('/actions/' in p or '/check-runs' in p or '/rules/' in p for p in c.paths)

@async_test
async def test_failed_fresh_inspection_cannot_leave_old_passing_snapshot(local,monkeypatch):
    store,root,snap,plan=local
    async def fail(*a):raise RuntimeError('GitHub 403: API rate limit exceeded')
    monkeypatch.setattr(github,'collect_pr',fail)
    result=await cli.inspect(store,'s','https://github.com/Owner/Repo/pull/1')
    assert result[0]['gates'][0]['status']=='UNKNOWN'
    cached=store.snapshot('Owner/Repo',1)
    assert cached['gates'][0]['status']=='UNKNOWN' and 'collected_at' not in cached['snapshot']

@async_test
async def test_waiter_cancel_does_not_release_collection_owner(tmp_path):
    co=Coordinator(tmp_path);a=Collections(co,'same');entered=asyncio.Event();hold=asyncio.Event()
    async def produce():entered.set();await hold.wait();return {'fresh':True}
    owner=asyncio.create_task(a.run('full','target',produce))
    await asyncio.sleep(.001)
    waiter=asyncio.create_task(a.run('full','target',produce))
    await asyncio.sleep(.002);waiter.cancel()
    with pytest.raises(asyncio.CancelledError):await waiter
    await entered.wait();hold.set()
    assert await owner=={'fresh':True}

@async_test
async def test_actual_hook_status_and_monitor_supervisor_overlap(tmp_path,monkeypatch):
    from caller_pr_gates import hook,monitor,supervisor
    class OpenBudget(BudgetClient):
        async def get(self,path,params=None):
            value=await super().get(path,params)
            if path.endswith('/pulls/50'):value.update(state='open',merged=False)
            return value
        async def graphql(self,query,variables):
            value=await super().graphql(query,variables)
            if query==github.BATCH_QUERY:value['repository']['pullRequest'].update(state='OPEN',merged=False)
            return value
    for callers in ['hook-status','monitor-supervisor']:
        c=OpenBudget(tmp_path/callers);s=Store(tmp_path/(callers+'-registry'))
        s.record_session('s','codex',str(tmp_path));s.register('s','codex','mikkokotila/caller',50,str(tmp_path))
        collections=Collections(c.coordinator,c.account);fulls=[]
        async def collect(*a):
            async def produce():fulls.append(1);return await github.collect_with_client(github._BatchClient(c),'mikkokotila/caller',50)
            return await collections.run('full-pr','mikkokotila/caller/pull/50',produce)
        monkeypatch.setattr(github,'collect_pr',collect)
        if callers=='hook-status':
            await asyncio.gather(hook.evaluate(s,'s'),cli.inspect(s,'s','https://github.com/mikkokotila/caller/pull/50'))
        else:
            async def reader(*a):return await c.get('/repos/mikkokotila/caller/pulls/50')
            await asyncio.gather(monitor.tick(s,reader=reader,collector=collect),supervisor._tick(s,collect_pr=collect))
        assert len(fulls)==1 and len(c.graphs)==1
        assert len(c.paths)==12+(callers=='monitor-supervisor') # One extra monitor lifecycle probe.


def test_old_coordination_schema_migrates_without_freshening_old_result(tmp_path):
    co=Coordinator(tmp_path)
    with co.db() as db:
        db.execute('CREATE TABLE collections(account TEXT,key TEXT,token TEXT,phase TEXT,boundary REAL,started REAL,lease REAL,boot TEXT,payload TEXT,error TEXT,PRIMARY KEY(account,key))')
        db.execute("INSERT INTO collections VALUES('a','key','old','result',0,0,0,'old','{}',NULL)")
    Collections(co,'a')
    with co.db() as db:
        row=db.execute('SELECT * FROM collections').fetchone()
        assert row['token']=='old' and row['started_mono'] is None and row['payload']=='{}'
