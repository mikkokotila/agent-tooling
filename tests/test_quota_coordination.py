import asyncio
import concurrent.futures
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import httpx
import pytest
from caller_pr_gates.quota import Coordinator,CoolingDown,account_key
from caller_pr_gates.transport import GitHubClient
from caller_pr_gates import ghrelay

def limited(*,reset=1100,retry=None,secondary=False):
    headers={'x-ratelimit-remaining':'0','x-ratelimit-reset':str(reset),'x-ratelimit-resource':'core'}
    if retry is not None:headers['retry-after']=str(retry)
    return {'http_status':403,'message':'secondary rate limit exceeded' if secondary else 'API rate limit exceeded','rate_limit_headers':headers}

@pytest.fixture
def clocked(tmp_path):
    now=[1000.,100.]
    def make(boot='boot-a'):return Coordinator(tmp_path,wall=lambda:now[0],monotonic=lambda:now[1],boot=boot,lease_seconds=10)
    return now,make

def test_account_resource_isolation_extension_restart_and_clock_changes(clocked):
    now,make=clocked;c=make();p=c.admit('alice','core');c.throttle(p,limited())
    with pytest.raises(CoolingDown):make().admit('alice','core')
    assert c.admit('bob','core').token is None
    assert c.admit('alice','graphql').token is None
    c.throttle(p,limited(reset=1050));now[:]=[1100,200]
    with pytest.raises(CoolingDown):c.admit('alice','core') # Earlier reset never shortens.
    now[0]=3000
    with pytest.raises(CoolingDown):c.admit('alice','core') # Forward wall jump cannot skip monotonic deadline.
    now[:]=[900,400]
    with pytest.raises(CoolingDown):c.admit('alice','core') # Backward jump remains conservative.
    now[:]=[1200,400];probe=c.admit('alice','core');assert probe.token
    assert c.finish(probe,success=True)
    assert make().admit('alice','core').token is None

def test_secondary_is_account_wide_and_honors_both_deadlines(clocked):
    now,make=clocked;c=make();p=c.admit('a','core');c.throttle(p,limited(reset=1100,retry=200,secondary=True))
    for resource in ['core','graphql','search']:
        with pytest.raises(CoolingDown):c.admit('a',resource)
    assert c.admit('b','core')
    now[:]=[1200,300]
    with pytest.raises(CoolingDown):c.admit('a','graphql')
    now[:]=[1202,302];probe=c.admit('a','graphql');assert c.finish(probe,success=True)

def test_failed_probe_backs_off_and_late_workers_cannot_clear_newer_limit(clocked):
    now,make=clocked;c=make();p=c.admit('a','core');c.throttle(p,limited())
    now[:]=[1102,202];old=c.admit('a','core')
    with pytest.raises(CoolingDown):make().admit('a','core')
    now[:]=[1113,213];new=make().admit('a','core')
    assert old.token!=new.token
    assert not c.finish(old,success=True)
    assert not c.throttle(old,limited(reset=9999))
    assert c.throttle(new,limited(reset=1300))
    assert not c.finish(new,success=True)
    with pytest.raises(CoolingDown):c.admit('a','core')
    now[:]=[1302,402];probe=c.admit('a','core');assert c.finish(probe,success=False)
    with pytest.raises(CoolingDown):c.admit('a','core')

def test_ordinary_late_response_is_fenced(clocked):
    _,make=clocked;c=make();late=c.admit('a','core');c.throttle(c.admit('a','core'),limited())
    assert not c.finish(late,success=True)

def test_reboot_reanchors_deadline_only_once(clocked):
    now,make=clocked;c=make();c.throttle(c.admit('a','core'),limited())
    now[:]=[9000,1];new=make('boot-b')
    with pytest.raises(CoolingDown):new.admit('a','core')
    now[:]=[9200,201];probe=new.admit('a','core');assert new.finish(probe,success=True)

def competing_probe(directory):
    c=Coordinator(directory,wall=lambda:2000,monotonic=lambda:2000,boot='test',lease_seconds=60)
    try:return bool(c.admit('a','core').token)
    except CoolingDown:return False

def test_atomic_expiring_probe_lease_across_processes(tmp_path):
    c=Coordinator(tmp_path,wall=lambda:1000,monotonic=lambda:1000,boot='test')
    c.throttle(c.admit('a','core'),limited())
    with concurrent.futures.ProcessPoolExecutor(5,mp_context=multiprocessing.get_context('spawn')) as pool:
        assert sum(pool.map(competing_probe,[str(tmp_path)]*5))==1

def test_python_requests_stop_before_http_and_preserve_healthy_resource(tmp_path,monkeypatch):
    c=Coordinator(tmp_path,boot='test');calls=[]
    monkeypatch.setattr('github_interface.client.get_token',lambda user=None:'fake-account')
    async def request(self,method,url,**kwargs):
        calls.append(str(url))
        if url=='/graphql':return httpx.Response(200,json={'data':{}},request=httpx.Request(method,'https://api.github.com/graphql'))
        return httpx.Response(403,json={'message':'API rate limit exceeded'},headers={'x-ratelimit-resource':'core','x-ratelimit-remaining':'0','retry-after':'60'},request=httpx.Request(method,'https://api.github.com/user'))
    monkeypatch.setattr(httpx.AsyncClient,'request',request)
    async def scenario():
        client=GitHubClient(coordinator=c)
        with pytest.raises(RuntimeError):await client.get('/user')
        for operation in ['codex','claude','cli','monitor','supervisor']:
            with pytest.raises(CoolingDown):await GitHubClient(coordinator=c).get('/repos/o/r/pulls/1')
        assert await client.graphql('query { viewer {login} }',{})=={}
    asyncio.run(scenario());assert calls==['/user','/graphql']
    with c.db() as db:
        rows=[json.loads(r[0]) for r in db.execute('SELECT fields FROM attribution')]
    assert sum(r['requests'] for r in rows)==2
    assert 'fake-account' not in json.dumps(rows)

def test_actual_gh_socket_guards_requests_inside_one_child(tmp_path):
    c=Coordinator(tmp_path,boot='test');calls=[]
    def upstream(method,url,**kwargs):
        calls.append(url)
        return httpx.Response(403,json={'message':'API rate limit exceeded'},headers={'x-ratelimit-resource':'core','x-ratelimit-remaining':'0','retry-after':'60'})
    result=ghrelay.run(['gh','api','/user'],coordinator=c,upstream=upstream,text=True,capture_output=True,timeout=10)
    assert result.returncode and len(calls)==1
    result=ghrelay.run(['gh','api','/user'],coordinator=c,upstream=upstream,text=True,capture_output=True,timeout=10)
    assert result.returncode and len(calls)==1 # No second upstream HTTP request.
    assert 'cooldown' in result.stderr or 'cooldown' in result.stdout

def test_gh_mutation_is_not_retried_on_ambiguous_transport_failure(tmp_path):
    calls=[]
    def fail(*args,**kwargs):calls.append(args);raise httpx.ReadTimeout('ambiguous write')
    result=ghrelay.run(['gh','api','repos/o/r/issues/1/comments','-X','POST','-f','body=test'],coordinator=Coordinator(tmp_path,boot='test'),upstream=fail,text=True,capture_output=True,timeout=10)
    assert result.returncode and len(calls)==1

def test_probe_waiter_cancellation_cannot_release_owner_lease(clocked):
    now,make=clocked;c=make();c.throttle(c.admit('a','core'),limited());now[:]=[1102,202]
    owner=c.admit('a','core')
    with pytest.raises(CoolingDown):c.admit('a','core')
    assert c.finish(owner,success=True)

def test_primary_fallback_is_bounded_and_permission_errors_do_not_cool(clocked):
    _,make=clocked;c=make();p=c.admit('a','core')
    assert not c.throttle(p,{'http_status':403,'message':'Permission denied'})
    assert c.admit('a','core')
    assert c.throttle(p,{'http_status':403,'message':'API rate limit exceeded'})
    with c.db() as db:assert db.execute('SELECT duration FROM cooldowns').fetchone()[0]==60
