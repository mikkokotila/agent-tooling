"""Regressions for the observed 15-chat contention and native inbox failures."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from unittest.mock import patch

import pytest

from caller_pr_gates import checklist, hook, monitor
from caller_pr_gates.collections import Collections
from caller_pr_gates.quota import Coordinator
from caller_pr_gates.store import Store
from test_quiet_monitor import local, row, SID
from test_single_pr_checklist import tick
from test_policy import snapshot, check


def traced(original, statements):
    @contextmanager
    def connection(self):
        with original(self) as db:
            db.set_trace_callback(statements.append)
            yield db
    return connection


def test_warm_registry_and_coordination_open_do_not_take_writer_locks(tmp_path):
    store = Store(tmp_path/'registry')
    co = Coordinator(tmp_path/'coordination')
    Collections(co, 'account')
    store.set_preflight('owner/repo',1,{'profile':'validation','collected_at':None})
    statements = []
    with patch.object(Store, '_connect', traced(Store._connect, statements)), patch.object(Coordinator, 'db', traced(Coordinator.db, statements)):
        Store(tmp_path/'registry')
        Collections(Coordinator(tmp_path/'coordination'), 'account')
        assert store.consume_prd_notices('no-document-work', 'codex') == []
        assert store.preflight('owner/repo',1)['attempted_at']
    assert not any(s.upper().startswith(('BEGIN IMMEDIATE','CREATE ','INSERT ','UPDATE ','ALTER ')) for s in statements)
    assert not any('FROM prs' in s for s in statements)


def test_fifteen_collection_waiters_do_not_block_the_writer(tmp_path):
    async def scenario():
        co=Coordinator(tmp_path);collections=Collections(co,'account')
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def producer():
            calls.append(1);entered.set();await release.wait();return {'observed':time.time()}
        boundary=time.time()
        leader=asyncio.create_task(collections.run('full-pr','target',producer,boundary=boundary))
        await entered.wait()
        statements=[]
        with patch.object(Coordinator,'db',traced(Coordinator.db,statements)):
            followers=[asyncio.create_task(collections.run('full-pr','target',producer,boundary=boundary)) for _ in range(15)]
            await asyncio.sleep(.15)
            during_wait=list(statements)
            release.set()
            results=await asyncio.gather(leader,*followers)
        assert not any(s.upper().startswith('BEGIN IMMEDIATE') for s in during_wait)
        assert len(calls)==1 and all(r==results[0] for r in results)
    asyncio.run(scenario())


@pytest.mark.parametrize('error', [RuntimeError('GitHub discovery timed out'), subprocess.TimeoutExpired('git', 3)])
@pytest.mark.parametrize('audit_fails', [False, True])
def test_discovery_failure_does_not_discard_an_owned_native_message(local, error, audit_fails):
    tick(local);store,data,_,reader,_=local
    async def branch(*args):return {'commit':{'sha':data['base_sha']}}
    original=monitor.consume
    audit = store.audit
    with patch.object(store,'audit',side_effect=sqlite3.OperationalError('database is locked') if audit_fails else audit), patch.object(hook,'discover',side_effect=error), patch.object(monitor,'consume',side_effect=lambda *a,**k:original(*a,**dict(k,reader=reader,branch_reader=branch))):
        result=checklist.handle(dict(hook_event_name='PostToolUse',session_id=SID,cwd=row(store)['cwd'],tool_name='Read'),store)
    assert 'Automated PR hook' in result['hookSpecificOutput']['additionalContext']
    assert row(store)['delivery_state']=='consumed'
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='checklist_discovery_error'").fetchone()[0]==int(not audit_fails)


def test_failed_revalidation_diagnostics_do_not_discard_other_owned_work(local):
    store,data,_,reader,_=local
    store.register(SID,'codex','owner/repo',2,row(store)['cwd'])
    tick(local)
    async def proof(repo,number):
        if number==1:raise TimeoutError('temporary proof failure')
        return await reader(repo,number)
    async def branch(*args):return {'commit':{'sha':data['base_sha']}}
    with patch.object(store,'audit',side_effect=sqlite3.OperationalError('database is locked')):
        text=monitor.consume(store,SID,full=False,reader=proof,branch_reader=branch)
    assert 'pull/2' in text and 'pull/1' not in text
    assert row(store)['delivery_state']=='pending' and row(store,2)['delivery_state']=='consumed'


def test_overlapping_callbacks_consume_one_event_only_once(local):
    tick(local);store=local[0];barrier=threading.Barrier(2)
    async def revalidate(store,rows,*args,**kwargs):
        barrier.wait(timeout=5);return rows
    with patch.object(monitor,'revalidate',side_effect=revalidate),ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:monitor.consume(store,SID,checklist=True),range(2)))
    assert sum(bool(r) for r in results)==1
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='pr_monitor_consumed'").fetchone()[0]==1


def test_native_proof_can_tolerate_a_two_second_response_without_extra_reads(local):
    tick(local);store,data,calls,reader,_=local
    async def slower(*args):
        await asyncio.sleep(2.1);return await reader(*args)
    branch_reads=[]
    async def branch(*args):branch_reads.append(args);return {'commit':{'sha':data['base_sha']}}
    calls.clear()
    assert monitor.consume(store,SID,full=False,reader=slower,branch_reader=branch,checklist=True)
    assert len(calls)==len(branch_reads)==1


def test_fifteen_actual_native_callback_processes_deliver_once_without_cross_talk(tmp_path):
    root=Path(__file__).resolve().parent.parent;state=tmp_path/'state';store=Store(state)
    # Historical session/PR shape observed in the live registry. No user data.
    stamp=time.time()
    with store._connect() as db:
        db.executemany('INSERT INTO sessions VALUES(?,?,?,NULL,?,?)',[(f'history-{i}','codex',str(tmp_path),stamp,stamp) for i in range(2000)])
        db.executemany("INSERT INTO prs VALUES(?,?,?,?,?,'complete',?,?)",[('history/repo',i+1,f'history-{i}','codex',str(tmp_path),stamp,stamp) for i in range(458)])
    owners=[];data=snapshot();data.update(repository='owner/repo',mergeable=True,collected_at=time.time(),checks=[check(conclusion='failure')])
    for i in range(15):
        sid=str(uuid.uuid4());client='claude' if i%2 else 'codex';cwd=tmp_path/f'checkout-{i}';cwd.mkdir()
        store.register(sid,client,'owner/repo',i+1,str(cwd));owners.append((sid,client,cwd,i+1))
    async def reader(repo,number):
        return dict(state='open',merged=False,mergeable=True,head={'sha':data['head_sha']},base={'ref':data['base_ref'],'sha':data['base_sha']})
    async def collector(repo,number):return dict(data,number=number)
    assert asyncio.run(monitor.tick(store,reader=reader,collector=collector,checklist=True))['events']==15
    # Run the configured native command and real hook entrypoint; only GitHub
    # transport is replaced. These processes never contact production GitHub.
    worker='''import json,os
from caller_pr_gates import hook,monitor
d=json.loads(os.environ['HOOK_TEST_PROOF'])
async def reader(*args): return d
async def branch(*args): return {'commit':{'sha':d['base']['sha']}}
original=monitor.consume
monitor.consume=lambda *a,**k: original(*a,**dict(k,reader=reader,branch_reader=branch))
hook.main()
'''
    proof=asyncio.run(reader('owner/repo',1))
    def batch():
        running=[]
        for sid,client,cwd,number in owners:
            config=json.loads((root/'config'/f'{client}-hooks.json').read_text().replace('${HOME}',str(Path.home())))
            parts=shlex.split(config['hooks']['PostToolUse'][0]['hooks'][0]['command']);argv=[]
            for part in parts:
                if part.startswith('PYTHONPATH='):part='PYTHONPATH='+str(root)
                if part.startswith('CALLER_PR_GATES_STATE_DIR='):part='CALLER_PR_GATES_STATE_DIR='+str(state)
                if part.endswith('/caller-pr-stop-gate/bin/python'):part=sys.executable
                argv.append(part)
            argv=argv[:-2]+['-c',worker]
            env=dict(os.environ,HOOK_TEST_PROOF=json.dumps(proof))
            proc=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
            proc.stdin.write(json.dumps(dict(hook_event_name='PostToolUse',session_id=sid,cwd=str(cwd),tool_name='Read')));proc.stdin.close();proc.stdin=None
            running.append((proc,number))
        results=[]
        for proc,number in running:
            out,err=proc.communicate(timeout=20)
            assert proc.returncode==0 and not err,(out,err)
            results.append((out,number))
        return results
    for out,number in batch():
        text=json.loads(out)['hookSpecificOutput']['additionalContext']
        assert f'https://github.com/owner/repo/pull/{number}.' in text
        assert text.count('https://github.com/')==1
    assert all(not out.strip() for out,_ in batch())
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='pr_monitor_consumed'").fetchone()[0]==15
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='checklist_native_error'").fetchone()[0]==0
