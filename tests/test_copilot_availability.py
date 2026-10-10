"""Service availability is irrelevant; submitted change requests remain binding."""
import asyncio
import copy
import time
from unittest.mock import patch

import pytest

from caller_pr_gates import hook, monitor, supervisor
from caller_pr_gates.copilot import SERVICE_PATH, is_service_check
from caller_pr_gates.github import collect_with_client
from caller_pr_gates.handoff import evidence_fingerprint
from caller_pr_gates.policy import evaluate_pr
from caller_pr_gates.store import Store
from caller_pr_gates.workflow import evidence_digest
from test_github import Client, SHA, STAMP
from test_policy import snapshot, check, comment, receipt, REPLIED


def service(**changes):
    return check(id='20', name='copilot-pull-request-reviewer', conclusion='failure',
                 workflow_path=SERVICE_PATH, workflow_event='dynamic', workflow_id='284233750', **changes)


def states(data, dispositions=None):
    return {g['gate']: g['status'] for g in evaluate_pr(data, dispositions or [])}


@pytest.mark.parametrize('status,conclusion', [('completed','failure'), ('completed','cancelled'),
    ('completed','timed_out'), ('in_progress',None), ('queued',None), ('completed',None)])
def test_service_outcomes_never_block_and_raw_failure_is_preserved(status, conclusion):
    data=snapshot(); raw=service(); raw.update(status=status, conclusion=conclusion)
    data['checks'].append(raw)
    gates=evaluate_pr(data,[])
    assert all(g['status']=='PASS' for g in gates)
    for gate in gates:
        if gate['gate'] in {'CI_FAILED','CI_PENDING'}:
            assert gate['evidence']['nonblocking_service_checks']==[raw]
    assert data['checks'][-1]['conclusion']==conclusion


def test_missing_copilot_context_is_nonblocking_but_missing_real_ci_waits():
    data=snapshot();data['expected_checks'].append('copilot-pull-request-reviewer')
    assert states(data)['CI_PENDING']=='PASS'
    data['expected_checks'].append('security')
    assert states(data)['CI_PENDING']=='WAIT'


@pytest.mark.parametrize('change', [{'workflow_path':'.github/workflows/copilot-tests.yml'},
    {'workflow_event':'pull_request'}, {'workflow_id':None}, {'kind':'status'}])
def test_name_collision_cannot_exempt_real_ci(change):
    data=snapshot();raw=service();raw.update(change);data['checks'].append(raw)
    assert states(data)['CI_FAILED']=='FAIL'


def test_other_failures_pending_and_unknown_evidence_remain_enforced():
    data=snapshot();data['checks'].append(service())
    data['checks'][0]['conclusion']='failure'
    assert states(data)['CI_FAILED']=='FAIL'
    data['checks'][0].update(status='queued',conclusion=None)
    assert states(data)['CI_PENDING']=='WAIT'
    data['expected_checks']=None
    assert states(data)['CI_FAILED']=='UNKNOWN'


@pytest.mark.parametrize('outdated',[False,True])
def test_actual_copilot_change_request_needs_disposition_and_resolution(outdated):
    data=snapshot();data['checks'].append(service())
    data['comments']=[comment(author_login='copilot-pull-request-reviewer[bot]',is_outdated=outdated)]
    assert states(data)['REVIEW_HANDLED']=='FAIL'
    assert states(data)['REVIEW_CONCLUDED']=='FAIL'
    assert states(data,[receipt()])['REVIEW_HANDLED']=='PASS'
    assert states(data,[receipt()])['REVIEW_CONCLUDED']=='FAIL'
    data['comments'][0]['is_resolved']=True
    assert all(v=='PASS' for v in states(data,[receipt()]).values())


def test_copilot_request_changes_review_requires_verified_public_reply():
    data=snapshot();data['checks'].append(service())
    data['comments']=[comment(kind='review',thread_id=None,author_login='copilot-pull-request-reviewer[bot]',body='Review requests changes.')]
    record=receipt(disposition='nonfix',reason='Existing boundary guard already covers this case',reply_url='https://github.com/owner/repo/pull/1#issuecomment-30')
    assert states(data,[record])['REVIEW_CONCLUDED']=='FAIL'
    data['comments'][0]['replies']=[dict(url=record['reply_url'],author_login='agent',created_at=REPLIED,body=record['reason'])]
    assert all(v=='PASS' for v in states(data,[record]).values())


def test_service_changes_do_not_replay_real_failure_or_validation():
    data=snapshot();data['checks'][0]['conclusion']='failure';data['checks'].append(service())
    old=evidence_fingerprint(data,evaluate_pr(data,[]));validation=evidence_digest(data,[])
    data['checks'][-1].update(id='99',conclusion='success',run_attempt=4)
    assert evidence_fingerprint(data,evaluate_pr(data,[]))==old
    assert evidence_digest(data,[])==validation
    assert 'copilot' not in ' '.join(monitor.actionable(data,evaluate_pr(data,[]))).lower()
    data['comments']=[comment(author_login='copilot-pull-request-reviewer[bot]')]
    assert evidence_fingerprint(data,evaluate_pr(data,[]))!=old
    assert evidence_digest(data,[])!=validation


class CopilotClient(Client):
    def __init__(self, *, ordinary=False, app_id=15368):
        super().__init__();self.ordinary=ordinary;self.app_id=app_id

    async def get(self,path,params=None):
        result=await super().get(path,params) if not path.endswith('/attempts/1/jobs') else {'total_count':1,'jobs':[dict(id=201,name='copilot-pull-request-reviewer',head_sha=SHA,status='completed',conclusion='failure',started_at=STAMP,check_run_url='https://api.github.com/repos/mikkokotila/caller/check-runs/11')]}
        if path.endswith('/actions/runs'):
            result['total_count']+=1
            result['workflow_runs'].append(dict(id=101,name='Running Copilot Code Review',workflow_id=284233750,event='pull_request' if self.ordinary else 'dynamic',path='.github/workflows/copilot-tests.yml' if self.ordinary else SERVICE_PATH,head_sha=SHA,status='completed',conclusion='failure',created_at=STAMP))
        if path.endswith('/check-runs'):
            result['total_count']+=1
            result['check_runs'].append(dict(id=11,name='copilot-pull-request-reviewer',app={'id':self.app_id,'slug':'github-actions'},head_sha=SHA,status='completed',conclusion='failure',created_at=STAMP))
        return result


def test_collector_correlates_workflow_job_and_check_without_extra_log_calls():
    client=CopilotClient();data=asyncio.run(collect_with_client(client,'mikkokotila/caller',50))
    exempt=[c for c in data['checks'] if is_service_check(c)]
    assert {c['kind'] for c in exempt}=={'workflow','job','check'}
    assert all(c['conclusion']=='failure' for c in exempt)
    assert states(data)['CI_FAILED']=='PASS'
    assert not any('/logs' in p for p in client.paths)
    assert states(data)['REVIEW_HANDLED']=='FAIL'  # Actual fixture review remains.


@pytest.mark.parametrize('kwargs',[{'ordinary':True},{'app_id':999}])
def test_collector_does_not_exempt_repository_workflows_or_unlinked_apps(kwargs):
    data=asyncio.run(collect_with_client(CopilotClient(**kwargs),'mikkokotila/caller',50))
    assert states(data)['CI_FAILED']=='FAIL'


def test_stop_and_supervisor_do_not_report_or_resume_for_service_failure(tmp_path):
    store=Store(tmp_path);store.record_session('s','codex',str(tmp_path));store.register('s','codex','owner/repo',1,str(tmp_path))
    data=snapshot();data['checks'].append(service());data['collected_at']=time.time()
    gates=evaluate_pr(data,[]);store.set_snapshot('owner/repo',1,data,gates)
    async def evaluate(*args):return [(store.prs('s')[0],gates)]
    with patch.object(hook,'evaluate',evaluate),patch('caller_pr_gates.external._job_proof',side_effect=AssertionError('quota proof must not be needed')):
        assert hook.handle(dict(hook_event_name='Stop',session_id='s',cwd=str(tmp_path),last_assistant_message='Source work is complete; Copilot service result remains failed.'),store) is None
    async def collect(*args):return copy.deepcopy(data)
    result=asyncio.run(supervisor.tick(store,execute=True,collect_pr=collect,runner=lambda *args,**kw:pytest.fail('service failure must not resume a chat')))
    assert not result['actions'] and not result['errors']
