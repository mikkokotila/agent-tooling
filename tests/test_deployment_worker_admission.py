"""A dead delivery deadline cannot block installation; live/unknown work can."""
import json
import sys
import time
from pathlib import Path

import pytest
from caller_pr_gates.store import Store
from caller_pr_gates import monitor
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'tools'))
import manage


def test_bounded_wait_does_not_deploy_or_change_a_live_lease(monkeypatch,tmp_path):
    clock=[0.0];observed=[]
    monkeypatch.setattr(manage.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(manage.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    monkeypatch.setattr(manage,'live_leases',lambda state:observed.append(state) or [{'table':'pr_watch','count':1}])
    with pytest.raises(RuntimeError,match='deployment deferred without changing state'):
        manage.wait_for_workers(tmp_path,12)
    assert clock[0]==12 and observed==[tmp_path]*4


def test_bounded_wait_returns_only_after_worker_admission(monkeypatch,tmp_path):
    clock=[0.0];reads=[0]
    def leases(state):
        reads[0]+=1
        return [{'table':'pr_watch','count':1}] if reads[0]<3 else []
    monkeypatch.setattr(manage,'live_leases',leases)
    monkeypatch.setattr(manage.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(manage.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    assert manage.wait_for_workers(tmp_path,30) is None
    assert clock[0]==10 and reads[0]==3


@pytest.mark.parametrize('arguments,expected', [([],600),(['--wait-for-workers','0'],None),(['--wait-for-workers','30'],30)])
def test_install_command_waits_before_deployment_without_changing_the_command_contract(monkeypatch,tmp_path,capsys,arguments,expected):
    calls=[]
    monkeypatch.setattr(manage.sys,'argv',['manage.py','install',*arguments])
    monkeypatch.setattr(manage,'BUILD',tmp_path/'build')
    monkeypatch.setattr(manage,'locked',lambda path:manage.contextlib.nullcontext())
    monkeypatch.setattr(manage,'verify',lambda:{'source_fingerprint':'verified'})
    monkeypatch.setattr(manage,'wait_for_workers',lambda state,seconds:calls.append(('wait',seconds)))
    monkeypatch.setattr(manage,'deploy',lambda **kwargs:calls.append(('deploy',kwargs['verified_fingerprint'])) or {'status':'installed'})
    manage.main()
    assert calls==([] if expected is None else [('wait',expected)])+[('deploy','verified')]
    assert json.loads(capsys.readouterr().out)=={'status':'installed'}


@pytest.fixture
def worker(tmp_path):
    store=Store(tmp_path);store.record_session('owner','claude',str(tmp_path));store.register('owner','claude','o/r',1,str(tmp_path))
    monitor.bootstrap(store)
    with store._connect() as db:
        db.execute("UPDATE pr_watch SET delivery_state='consumed',worker_pid=12345,lease_until=?",(time.time()+1000,))
    return store


def test_finished_claude_delivery_is_not_live_and_state_is_untouched(worker,monkeypatch):
    monkeypatch.setattr(manage,'process_alive',lambda pid:False)
    monkeypatch.setattr('caller_pr_gates.supervisor.claude_availability',lambda sid,cwd:'idle')
    with worker._connect() as db:before=dict(db.execute('SELECT * FROM pr_watch').fetchone())
    assert manage.live_leases(worker.state_dir)==[]
    with worker._connect() as db:assert dict(db.execute('SELECT * FROM pr_watch').fetchone())==before


@pytest.mark.parametrize('change',['alive','busy','unknown','reserved','queued','codex','missing_pid','missing_owner'])
def test_live_uncertain_or_unconsumed_worker_still_blocks(worker,monkeypatch,change):
    monkeypatch.setattr(manage,'process_alive',lambda pid:change=='alive' or pid is None)
    monkeypatch.setattr('caller_pr_gates.supervisor.claude_availability',lambda sid,cwd:change if change in {'busy','unknown'} else 'idle')
    with worker._connect() as db:
        if change in {'reserved','queued'}:db.execute('UPDATE pr_watch SET delivery_state=?',(change,))
        if change=='codex':db.execute("UPDATE pr_watch SET client='codex'")
        if change=='missing_pid':db.execute('UPDATE pr_watch SET worker_pid=NULL')
        if change=='missing_owner':db.execute("UPDATE pr_watch SET owner_session='unknown'")
    assert manage.live_leases(worker.state_dir)==[{'table':'pr_watch','count':1}]


def test_pid_lookup_is_conservative(monkeypatch):
    def gone(*args):raise ProcessLookupError
    monkeypatch.setattr(manage.os,'kill',gone)
    assert not manage.process_alive(123)
    assert manage.process_alive(None) and manage.process_alive(-1)
    def uncertain(*args):raise PermissionError
    monkeypatch.setattr(manage.os,'kill',uncertain)
    assert manage.process_alive(123)
