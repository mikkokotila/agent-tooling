import asyncio
import json
import pytest
from caller_pr_gates.errors import diagnostic, error_identity
from caller_pr_gates import hook
from caller_pr_gates.store import Store
from test_handoff import quota_exhausted

@pytest.mark.parametrize('cut', [None, 600, 400])
def test_quota_identity_is_independent_of_display_and_telemetry(cut):
    a=str(quota_exhausted('AAA:111','2026-10-09 19:40:01','5001'))
    b=str(quota_exhausted('BBB:222222222','2026-10-10 20:41:02','5012'))
    assert error_identity(a[:cut]) == error_identity(b[:cut]) == error_identity(a)

@pytest.mark.parametrize('status,message,headers,kind', [
    (403,'Resource not accessible by integration',{'x-ratelimit-remaining':'42'},'permission_denied'),
    (403,'Forbidden',{},'permission_denied'),
    (429,'Unrelated response',{},'api_error'),
    (401,'Bad credentials',{'x-ratelimit-remaining':'0'},'permission_denied'),
    (403,'API rate limit exceeded',{},'rate_limited'),
    (403,'You have exceeded a secondary rate limit',{},'rate_limited'),
])
def test_error_classification_requires_actual_exhaustion(status,message,headers,kind):
    error=f'GitHub {status} rate-limit: {headers} body='+json.dumps({'message':message})
    assert diagnostic(error)['kind']==kind

def test_structured_path_status_component_remain_distinct():
    a=diagnostic(quota_exhausted('AAA','2026-10-09 19:40:01'),'/repos/o/r/pulls/1')
    b=diagnostic(quota_exhausted('BBB','2026-10-10 20:41:02'),'/repos/o/r/pulls/1')
    assert error_identity(a,'ci')==error_identity(b,'ci')
    assert error_identity(a,'ci')!=error_identity(b,'reviews')
    assert error_identity(a,'ci')!=error_identity(dict(b,path='/repos/o/r/pulls/2'),'ci')
    assert error_identity(a,'ci')!=error_identity(dict(b,http_status=429),'ci')

def test_full_redacted_diagnostics_survive_display_limit(tmp_path,monkeypatch):
    store=Store(tmp_path);store.register('s','codex','o/r',1,str(tmp_path))
    async def fail(*args):raise RuntimeError('GitHub 403: API rate limit exceeded '+('x'*800)+' github_pat_SECRET_123')
    monkeypatch.setattr('caller_pr_gates.github.collect_pr',fail)
    asyncio.run(hook.evaluate(store,'s'))
    saved=store.snapshot('o/r',1)
    error=saved['snapshot']['component_errors']['collection']
    assert len(saved['gates'][0]['details'][0])==600
    assert len(error['response_body'])>800
    assert 'SECRET' not in json.dumps(saved) and '[REDACTED]' in error['response_body']

@pytest.mark.parametrize('active',[True,False])
@pytest.mark.parametrize('client',['codex','claude'])
def test_truthful_quota_handoff_yields_after_restart(tmp_path,monkeypatch,active,client):
    store=Store(tmp_path);store.register('s',client,'o/r',1,str(tmp_path))
    with store._connect() as db:db.execute('INSERT INTO unpublished_work VALUES(?,?,?,?,?)',('s','o/r','fix',str(tmp_path),1))
    errors=iter([quota_exhausted('AAA','2026-10-09 19:40:01'),quota_exhausted('BBB','2026-10-10 20:41:02')])
    async def fail(*args):raise next(errors)
    monkeypatch.setattr('caller_pr_gates.github.collect_pr',fail)
    monkeypatch.setenv('CALLER_HOOK_CLIENT',client)
    event=dict(hook_event_name='Stop',session_id='s',cwd=str(tmp_path),turn_id='t')
    first=hook.handle(dict(event,last_assistant_message='Publishing next.'),store)
    assert first['decision']=='block'
    result=hook.handle(dict(event,last_assistant_message='Blocked: GitHub quota exhausted; work remains incomplete.',stop_hook_active=active),Store(tmp_path))
    assert result is None
    assert store.prs('s')[0]['status']=='active'
    assert store.recovery('o/r',1,'s')['obligation_complete'] is False
