import asyncio
import copy
import time
from types import SimpleNamespace

import pytest

from caller_pr_gates import closure, monitor, handoff, cli
from caller_pr_gates.policy import evaluate_pr
from caller_pr_gates.store import Store
from test_policy import snapshot, closing_graphql, SHA, OTHER, check

SID = '11111111-1111-4111-8111-111111111111'
OTHER_SID = '22222222-2222-4222-8222-222222222222'


@pytest.mark.parametrize('body', ['Closes #2', 'FIXES: owner/repo#2', 'Resolved https://github.com/owner/repo/issues/2'])
def test_closing_keyword_recognized(body):
    assert closure.has_closing_reference(body)


@pytest.mark.parametrize('body', ['Related #2', '<!-- Closes #2 -->', '`Closes #2`', '```text\nCloses #2\n```', 'Closes #0'])
def test_references_and_examples_are_not_closing_intent(body):
    assert not closure.has_closing_reference(body)


def test_gate_requires_actual_link_default_branch_and_current_head():
    data = snapshot()
    assert closure.gate(data)['status'] == 'PASS'
    for mutation, expected in [({'closing_issues': []}, 'FAIL'), ({'default_branch': 'release'}, 'FAIL'),
            ({'closing_issues': None}, 'UNKNOWN'), ({'closure_head_sha': OTHER}, 'UNKNOWN'),
            ({'closure_base_ref': 'release'}, 'UNKNOWN'), ({'default_branch': {}}, 'UNKNOWN')]:
        assert closure.gate(dict(data, **mutation))['status'] == expected


def test_merge_does_not_prove_issue_closed():
    data = dict(snapshot(), state='closed', merged=True)
    assert closure.gate(data)['status'] == 'FAIL'
    data['closing_issues'][0]['state'] = 'CLOSED'
    assert closure.gate(data)['status'] == 'PASS'
    data['merged'] = False
    data['closing_issues'] = None
    assert closure.gate(data)['status'] == 'PASS'


def test_complete_paginated_links_and_head_races():
    class Client:
        def __init__(self, race=False): self.race = race
        async def graphql(self, query, variables):
            result = closing_graphql(variables, OTHER if self.race and variables['cursor'] else SHA)
            connection = result['repository']['pullRequest']['closingIssuesReferences']
            if not variables['cursor']:
                connection['pageInfo'] = dict(hasNextPage=True, endCursor='next')
            else:
                connection['nodes'][0].update(number=3, url='https://github.com/owner/repo/issues/3')
            return result
    good = asyncio.run(closure.collect(Client(), 'owner/repo', 1))
    assert [i['number'] for i in good['closing_issues']] == [2, 3]
    bad = asyncio.run(closure.collect(Client(True), 'owner/repo', 1))
    assert 'closure_error' in bad


def test_link_outage_does_not_hide_independent_failed_ci():
    data = snapshot()
    data['checks'] = [check(conclusion='failure')]
    data['component_errors'] = {'issue_closure': {'message': 'Unavailable'}}
    gates = {g['gate']: g['status'] for g in evaluate_pr(data, [])}
    assert gates['ISSUE_CLOSURE'] == 'UNKNOWN'
    assert gates['CI_FAILED'] == 'FAIL'
    data['component_errors'] = {}
    data['closing_issues'] = []
    gates = evaluate_pr(data, [])
    proof = handoff.closure_identity(data, gates)
    identity = handoff.evidence_fingerprint(data, gates)
    data['component_errors'] = {'issue_closure': {'message': 'Unavailable'}}
    data['closing_issues'] = None
    assert handoff.evidence_fingerprint(data, evaluate_pr(data, []), previous_closure=proof) == identity
    assert closure.gate(data)['status'] == 'UNKNOWN'


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / 'state')
    store.register(SID, 'codex', 'owner/repo', 1, str(tmp_path))
    data = snapshot()
    store.set_snapshot('owner/repo', 1, data, evaluate_pr(data, []))
    monitor.retire(store, 'owner/repo', 1, dict(state='closed', merged=True, head={'sha': SHA}))
    return store


async def open_reader(repo, number):
    result = snapshot()
    return dict(result, closure_merged=True, closure_state='MERGED')


async def closed_reader(repo, number):
    result = await open_reader(repo, number)
    result['closing_issues'][0]['state'] = 'CLOSED'
    return result


def rows(store):
    with store._connect() as db: return [dict(r) for r in db.execute('SELECT * FROM issue_closeouts')]


def refresh(store, reader=open_reader, **kwargs):
    asyncio.run(closure.refresh(store, SID, force=True, reader=reader, **kwargs))


def test_merge_retires_source_and_preserves_distinct_closeout(state):
    assert state.prs(SID) == []
    assert rows(state)[0]['state'] == 'pending'
    assert closure.task_gates(state, SID)[0]['status'] == 'UNKNOWN'
    refresh(state, closed_reader)
    assert closure.task_gates(state, SID) == []
    assert rows(state)[0]['state'] == 'complete'
    assert state.snapshot('owner/repo', 1)['snapshot']['merged'] is False


def test_merged_existing_work_is_not_adopted_by_reference(tmp_path):
    store = Store(tmp_path / 'state')
    monitor.retire(store, 'owner/repo', 1, dict(state='closed', merged=True, head={'sha': SHA}))
    assert rows(store) == []


def test_open_issue_delivery_once_to_exact_owner(state):
    refresh(state)
    spawned = []
    assert closure.dispatch(state, spawn=spawned.append) == 1
    assert '--session' in spawned[0] and spawned[0][-1] == SID
    calls = []
    def runner(args, **kwargs): calls.append(args); return {'returncode': 0}
    assert asyncio.run(closure.deliver(state, SID, reader=open_reader, runner=runner))
    assert calls[0][0:2] == ['codex', 'queue']
    assert calls[0][3] == SID
    assert closure.consume(state, OTHER_SID, reader=open_reader) is None
    token = rows(state)[0]['event_id']
    text = closure.consume(state, SID, event_id=token, reader=open_reader)
    assert 'https://github.com/owner/repo/issues/2' in text
    assert 'Verify' in text and 'do not create issues' in text
    assert closure.consume(state, SID, event_id=token, reader=open_reader) is None
    for _ in range(3):
        refresh(state)
        assert closure.dispatch(state, spawn=spawned.append) == 0
    assert len(spawned) == 1
    assert closure.task_gates(state, SID)[0]['status'] == 'FAIL'
    assert state.prs(SID) == []


def test_delivery_failure_never_retries_automatically_and_lease_is_respected(state):
    refresh(state)
    closure.dispatch(state, spawn=lambda args: SimpleNamespace(pid=123))
    with pytest.raises(ValueError, match='live worker lease'):
        closure.retry(state, SID, 'owner/repo', 1)
    closure.failed(state, SID, 'Delivery rejected')
    for _ in range(3):
        refresh(state)
        assert closure.dispatch(state, spawn=lambda args: pytest.fail('must remain failed')) == 0
    assert closure.retry(state, SID, 'owner/repo', 1)
    refresh(state)
    assert closure.dispatch(state, spawn=lambda args: SimpleNamespace(pid=124)) == 1


@pytest.mark.parametrize('change', ['closed', 'outage', 'not_merged', 'head_changed', 'missing_link'])
def test_stale_or_unproved_queued_closeout_does_not_restart(state, change):
    refresh(state)
    closure.dispatch(state, spawn=lambda args: None)
    async def reader(repo, number):
        if change == 'outage': raise RuntimeError('GitHub unavailable')
        result = await open_reader(repo, number)
        if change == 'closed': result['closing_issues'][0]['state'] = 'CLOSED'
        if change == 'not_merged': result['closure_merged'] = False
        if change == 'head_changed': result['closure_head_sha'] = OTHER
        if change == 'missing_link': result['closing_issues'] = []
        return result
    token = rows(state)[0]['event_id']
    assert closure.consume(state, SID, event_id=token, reader=reader) is None
    assert state.prs(SID) == []
    assert rows(state)[0]['state'] == ('complete' if change == 'closed' else 'unknown')


def test_busy_owner_consumes_native_inbox_without_second_worker(state):
    refresh(state)
    with state._connect() as db: db.execute('INSERT INTO watch_sessions VALUES(?,?,?)', (SID, 1, time.time()))
    assert closure.dispatch(state, spawn=lambda args: pytest.fail('busy chat must use inbox')) == 0
    assert closure.consume(state, SID, reader=open_reader)


def test_creation_missing_closing_intent_rejected_before_github_write(state, monkeypatch):
    args = SimpleNamespace(session=SID, arguments=['pr', 'create', '--title', 'Fix', '--body', 'Related #2'],
                           target=None, kind=None, slice_id=None, client='codex', cwd=state.session(SID)['cwd'])
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **k: pytest.fail('missing closing reference must not publish'))
    with pytest.raises(ValueError, match='ISSUE_CLOSURE FAIL'):
        cli.gateway(args, state)


def test_claude_resumes_the_same_existing_session(state):
    state = Store(state.state_dir.parent / 'claude-state')
    state.register(SID, 'claude', 'owner/repo', 1, str(state.state_dir.parent))
    data = snapshot()
    state.set_snapshot('owner/repo', 1, data, evaluate_pr(data, []))
    monitor.retire(state, 'owner/repo', 1, dict(state='closed', merged=True, head={'sha': SHA}))
    refresh(state)
    closure.dispatch(state, spawn=lambda args: None)
    calls = []
    def runner(args, **kwargs): calls.append((args, kwargs)); return {'returncode': 0}
    assert asyncio.run(closure.deliver(state, SID, reader=open_reader, runner=runner, availability=lambda *args: 'idle'))
    args, kwargs = calls[0]
    assert args[:4] == ['claude', '--print', '--resume', SID]
    assert kwargs['input_text'].startswith(closure.MARKER)


@pytest.mark.parametrize('issue_state', ['OPEN', 'CLOSED'])
def test_managed_merge_verifies_actual_issue_closure(tmp_path, monkeypatch, issue_state):
    from caller_pr_gates import github
    store = Store(tmp_path / 'merge-state')
    store.register(SID, 'codex', 'owner/repo', 1, str(tmp_path))
    data = snapshot()
    reads = []
    async def collector(repo, number):
        result = copy.deepcopy(data)
        if reads:
            result.update(state='closed', merged=True)
            result['closing_issues'][0]['state'] = issue_state
        reads.append(1)
        return result
    async def reader(repo, number):
        result = await open_reader(repo, number)
        result['closing_issues'][0]['state'] = issue_state
        return result
    calls = []
    monkeypatch.setattr(github, 'collect_pr', collector)
    monkeypatch.setattr(closure, 'read_live', reader)
    monkeypatch.setattr(cli.subprocess, 'run', lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0))
    args = SimpleNamespace(session=SID, arguments=['pr', 'merge', 'https://github.com/owner/repo/pull/1', '--squash'],
                           target=None, kind=None, slice_id=None, client='codex', cwd=str(tmp_path))
    if issue_state == 'OPEN':
        with pytest.raises(ValueError, match='ISSUE_CLOSED incomplete'): cli.gateway(args, store)
    else:
        assert cli.gateway(args, store) is None
    assert calls == [['gh', 'pr', 'merge', 'https://github.com/owner/repo/pull/1', '--squash', '--match-head-commit', SHA]]
    assert store.prs(SID) == []
    assert rows(store)[0]['state'] == ('open' if issue_state == 'OPEN' else 'complete')


def test_prior_approval_cannot_waive_new_target_commits(tmp_path, monkeypatch):
    from caller_pr_gates import github
    store = Store(tmp_path / 'merge-state')
    store.register(SID, 'codex', 'owner/repo', 1, str(tmp_path))
    data = snapshot()
    data.update(review_decision='APPROVED', mergeable=True)
    data['branch_comparison'].update(behind_by=1, status='diverged', merge_base_sha=OTHER)
    async def collector(*args): return data
    monkeypatch.setattr(github, 'collect_pr', collector)
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **kw: pytest.fail('old approval must not authorize merge'))
    args = SimpleNamespace(session=SID, arguments=['pr', 'merge', 'https://github.com/owner/repo/pull/1', '--squash'],
                           target=None, kind=None, slice_id=None, client='codex', cwd=str(tmp_path))
    with pytest.raises(ValueError, match='PR_GATE FAIL'): cli.gateway(args, store)


def test_native_consumption_winning_claim_prevents_duplicate_claude_launch(state):
    with state._connect() as db: db.execute("UPDATE sessions SET client='claude' WHERE session_id=?", (SID,))
    refresh(state)
    closure.dispatch(state, spawn=lambda args: None)
    def available(*args):
        # Another native-hook process consumed this exact claim during availability probing.
        with state._connect() as db:
            db.execute("UPDATE issue_closeouts SET delivery_state='consumed',reported_at=?,lease_until=0 WHERE owner_session=?", (time.time(), SID))
        return 'idle'
    assert not asyncio.run(closure.deliver(state, SID, reader=open_reader,
        availability=available, runner=lambda *a, **kw: pytest.fail('consumed claim must not launch')))
