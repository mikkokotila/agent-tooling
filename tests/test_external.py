import asyncio
import copy
import time
from unittest.mock import patch

import pytest

from caller_pr_gates import external, hook, supervisor
from caller_pr_gates.policy import evaluate_pr
from caller_pr_gates.store import Store

SID = '11111111-1111-4111-8111-111111111111'
URL = 'https://github.com/o/r/actions/runs/100/job/200'
HEAD = 'a' * 40
REPORT = '**Issue found:** Review remains blocked.\n\n**Cause:** Copilot account quota is exhausted.\n\n**System correction:** Failure is held unresolved pending account recovery.\n\n**Human decision:** Account recovery.'


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / 'state')
    store.record_session(SID, 'codex', str(tmp_path))
    store.register(SID, 'codex', 'o/r', 508, str(tmp_path))
    snapshot = dict(repository='o/r', number=508, state='closed', merged=True, draft=False,
        head_sha=HEAD, head_sha_after=HEAD, collected_at=time.time(), comments=[],
        author_login='human', actor_login='human', expected_checks=[], commit_shas=[HEAD],
        checks=[dict(id='100', kind='workflow', name='Running Copilot Code Review', status='completed', conclusion='failure', url='https://github.com/o/r/actions/runs/100'),
                dict(id='200', kind='job', name='copilot-pull-request-reviewer', status='completed', conclusion='failure', url=URL),
                dict(id='300', kind='check', name='copilot-pull-request-reviewer', status='completed', conclusion='failure', url=URL)])
    from test_policy import closing_link
    snapshot.update(base_ref='main', **closing_link(HEAD, 'o/r', 507, 'CLOSED'))
    gates = evaluate_pr(snapshot, [])
    store.set_snapshot('o/r', 508, snapshot, gates)
    return store, snapshot, gates


async def proof(*args):
    return {'job': dict(id=200, run_id=100, head_sha=HEAD, name='copilot-pull-request-reviewer', conclusion='failure'),
            'workflow': dict(id=100, head_sha=HEAD),
            'log': "Error: You have exceeded your monthly quota. errorType: 'quota', statusCode: 402"}


def hold(state, **kwargs):
    store, snap, gates = state
    return asyncio.run(external.hold_verified_failure(store, SID, snap, gates, proof_reader=proof, **kwargs))


def test_verified_quota_is_unresolved_not_passing_or_complete(state):
    store, snap, gates = state
    result = hold(state)
    assert result['status'] == 'held'
    assert next(g for g in store.snapshot('o/r', 508)['gates'] if g['gate'] == 'CI_FAILED')['status'] == 'FAIL'
    assert store.prs(SID)[0]['status'] == 'active'
    assert hold(state) == result


@pytest.mark.parametrize('merge_state,decision', [('UNSTABLE',None),('BLOCKED',None),('BLOCKED','CHANGES_REQUESTED')])
def test_verified_quota_stays_quiet_while_merge_readiness_waits(state,merge_state,decision):
    store,snap,_=state
    from test_policy import fresh_branch
    snap.update(state='open',merged=False,mergeable=True,merge_state_status=merge_state,
                review_decision=decision,merge_readiness_head_sha=HEAD,**fresh_branch(HEAD))
    gates=evaluate_pr(snap,[]);store.set_snapshot('o/r',508,snap,gates)
    result=hold((store,snap,gates))
    assert result['status']=='held'
    assert external.current_hold(store,SID,snap,gates)==result
    assert next(g for g in gates if g['gate']=='MERGE_READY')['status']=='WAIT'
    assert next(g for g in gates if g['gate']=='CI_FAILED')['status']=='FAIL'


@pytest.mark.parametrize('change', ['head', 'unrelated_failure', 'pending', 'draft', 'reviews', 'quota_words_only', 'wrong_job'])
def test_hold_cannot_cover_unproved_or_actionable_failures(state, change):
    store, snap, gates = state
    reader = proof
    if change == 'head': snap['head_sha'] = 'b' * 40; snap['head_sha_after'] = snap['head_sha']
    if change == 'unrelated_failure': snap['checks'].append(dict(id='400', kind='job', name='Unit tests', status='completed', conclusion='failure', url=URL))
    if change == 'pending': snap['checks'].append(dict(id='400', kind='job', name='Unit tests', status='in_progress', conclusion=None, url=URL))
    if change == 'draft': snap['draft'] = True
    if change == 'reviews':
        gates = copy.deepcopy(gates); gates[-2]['status'] = 'FAIL'
    else: gates = evaluate_pr(snap, [])
    if change in {'quota_words_only', 'wrong_job'}:
        async def reader(*args):
            data = await proof()
            if change == 'quota_words_only': data['log'] = 'A test mentions quota and 402'
            else: data['job']['id'] = 999
            return data
    assert asyncio.run(external.hold_verified_failure(store, SID, snap, gates, proof_reader=reader)) is None
    assert store.external_blocker(SID, 'o/r', 508) is None


def test_fresh_timestamps_do_not_retry_new_reviews_and_checks_do(state):
    store, snap, gates = state
    held = hold(state)
    snap['collected_at'] += 5000
    assert external.current_hold(store, SID, snap, gates) == held
    snap['comments'] = [dict(id='issue:9', updated_at='2026-10-05T20:00:00Z')]
    assert external.current_hold(store, SID, snap, gates) is None
    assert store.external_blocker(SID, 'o/r', 508) is None


def test_unknown_evidence_neither_releases_hold_nor_passes(state):
    store, snap, gates = state
    hold(state)
    unknown = copy.deepcopy(gates); unknown[0]['status'] = 'UNKNOWN'
    assert external.current_hold(store, SID, snap, unknown) is None
    assert store.external_blocker(SID, 'o/r', 508)


def test_merged_quota_stop_yields_with_failure_retained_and_no_reclassification(state):
    store, snap, gates = state
    hold(state)
    async def evaluate(*args): return [(store.prs(SID)[0], gates)]
    event = dict(hook_event_name='Stop', session_id=SID, cwd='/tmp', last_assistant_message=REPORT)
    with patch.object(hook, 'evaluate', evaluate), patch.object(external, '_job_proof', side_effect=AssertionError('unchanged holds do not reread logs')):
        for _ in range(3):
            result = hook.handle(event, store)
            assert result is None  # Already reported; do not repeat unchanged feedback.
    assert next(g for g in store.snapshot('o/r', 508)['gates'] if g['gate'] == 'CI_FAILED')['status'] == 'FAIL'


def test_held_failure_does_not_allow_false_completion_report(state):
    store, snap, gates = state
    hold(state)
    async def evaluate(*args): return [(store.prs(SID)[0], gates)]
    with patch.object(hook, 'evaluate', evaluate):
        result = hook.handle(dict(hook_event_name='Stop', session_id=SID, cwd='/tmp', last_assistant_message='PR #508 is complete. All CI pass.'), store)
    assert result['decision'] == 'block'


def test_reviewed_due_document_with_quota_yields_but_job_stays_unfinished(state):
    store, snap, gates = state
    scope = dict(agreed_scope='Original work', acceptance_criteria=['Original criterion'], slice_count=0, slice_ids=[], approved_at=1.0)
    store.record_scope(SID, scope)
    job = store.register_followup(SID, 'o/r', 508, time.time() - 4000, scope)
    receipt = dict(read_at=time.time() - 1, head_sha=HEAD, comment_ids=[], scope_digest=supervisor.scope_digest(scope))
    store.update_followup(job['id'], 'pending', {'review_read': receipt})
    snap['collected_at'] = time.time()
    store.set_snapshot('o/r', 508, snap, gates)
    hold(state)
    async def evaluate(*args): return [(store.prs(SID)[0], gates)]
    async def finish(*args): return None
    with patch.object(hook, 'evaluate', evaluate), patch.object(hook, '_finish_due_followups', finish):
        result = hook.handle(dict(hook_event_name='Stop', session_id=SID, cwd='/tmp', last_assistant_message=REPORT), store)
    assert result is None
    assert store.followups(SID)[0]['status'] == 'pending'


def test_supervisor_does_not_dispatch_held_pr_or_document(state):
    store, snap, gates = state
    scope = dict(agreed_scope='Original work', acceptance_criteria=[], slice_count=0, slice_ids=[], approved_at=1.0)
    store.record_scope(SID, scope)
    job = store.register_followup(SID, 'o/r', 508, time.time() - 4000, scope)
    store.update_followup(job['id'], 'pending', {'review_read': dict(read_at=time.time() - 1, head_sha=HEAD, comment_ids=[], scope_digest=supervisor.scope_digest(scope))})
    hold(state)
    async def collect(*args): return {**snap, 'collected_at': time.time()}
    result = asyncio.run(supervisor.tick(store, execute=True, collect_pr=collect, collect_issue=collect,
        evaluate_pr=lambda *args: gates, evaluate_review=lambda *args: gates,
        runner=lambda *args, **kw: pytest.fail('must not queue/resume unchanged blocker')))
    assert result['actions'] == [] and result['completed'] == [] and result['errors'] == []
    assert store.followups(SID)[0]['status'] == 'pending'
    assert len(result['deferred']) == 2


def test_child_can_hand_back_before_parent_pr_is_green(state):
    store, snap, gates = state
    store.record_session('child', 'codex', '/tmp', parent_session=SID)
    with patch.object(hook, 'evaluate', side_effect=AssertionError('child handback is not parent completion')):
        result = hook.handle(dict(hook_event_name='SubagentStop', session_id=SID, agent_id='child', cwd='/tmp'), store)
    assert 'decision' not in result
    assert store.prs(SID)[0]['number'] == 508
