import asyncio
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from caller_pr_gates import monitor, hook, turns, workflow
from caller_pr_gates.store import Store
from test_policy import snapshot, check

SID = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'


@pytest.fixture
def local(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state')
    store.register(SID, 'codex', 'owner/repo', 1, str(tmp_path))
    data = snapshot()
    data.update(repository='owner/repo', number=1, mergeable=True, collected_at=time.time())
    data['checks'] = [check(conclusion='failure')]
    calls = []
    async def reader(repo, number):
        calls.append((repo, number))
        return dict(state=data['state'], merged=data['merged'], mergeable=data['mergeable'], head={'sha': data['head_sha']}, base={'ref': data['base_ref'], 'sha': data['base_sha']})
    async def collector(repo, number):
        return dict(data, number=number)
    async def branch_reader(repo, ref):
        return {'commit': {'sha': data['base_sha']}}
    monkeypatch.setattr(monitor, 'probe_branch', branch_reader)
    from caller_pr_gates import closure
    async def closeout_reader(repo, number):
        return dict(closure_head_sha=data['head_sha'], closure_merged=True, closure_state='MERGED', closing_issues=[dict(issue, state='CLOSED') for issue in data['closing_issues']])
    monkeypatch.setattr(closure, 'read_live', closeout_reader)
    return store, data, calls, reader, collector


def tick(local, now=1000, execute=False, spawn=None):
    store, _, _, reader, collector = local
    return asyncio.run(monitor.tick(store, now=now, execute=execute, reader=reader, collector=collector, spawn=spawn))


def row(store, number=1):
    with store._connect() as db:
        return dict(db.execute('SELECT * FROM pr_watch WHERE number=?', (number,)).fetchone())


def test_single_pr_five_minute_cadence(local):
    assert tick(local)['events'] == 1
    assert row(local[0])['next_check_at'] == 1300
    assert tick(local, 1299)['checked'] == 0
    assert len(local[2]) == 1
    assert tick(local, 1300)['checked'] == 1
    assert len(local[2]) == 2


def test_multiple_prs_ten_minute_cadence(local):
    local[0].register(SID, 'codex', 'owner/repo', 2, local[0].session(SID)['cwd'])
    assert tick(local)['checked'] == 2
    assert row(local[0], 1)['next_check_at'] == 1600
    assert row(local[0], 2)['next_check_at'] == 1600
    assert tick(local, 1599)['checked'] == 0


@pytest.mark.parametrize('state,merged', [('closed', True), ('closed', False)])
def test_closed_pr_retires_before_expensive_collection_and_preserves_evidence(local, state, merged):
    store, data, _, reader, _ = local
    data.update(state=state, merged=merged)
    async def never(*args):
        raise AssertionError('closed work must not be collected or resurrected')
    result = asyncio.run(monitor.tick(store, now=1000, execute=True, reader=reader, collector=never,
                                      spawn=lambda args: pytest.fail('must not wake')))
    assert result['retired'] == 1
    assert row(store)['event_id'] is None
    assert store.prs(SID) == []
    assert tick(local, 9999)['checked'] == 0
    with store._connect() as db:
        evidence = json.loads(db.execute("SELECT fields FROM audit WHERE event='pr_monitor_retired'").fetchone()[0])
    assert evidence['gates_waived'] is False


def test_unchanged_failure_is_delivered_at_most_once(local):
    launched = []
    assert tick(local, execute=True, spawn=launched.append)['workers'] == 1
    with local[0]._connect() as db:
        db.execute("UPDATE pr_watch SET delivery_state='queued',delivered_fingerprint=fingerprint")
    for now in [1300, 1600, 1900, 2200]:
        assert tick(local, now, True, launched.append)['workers'] == 0
    assert len(launched) == 1


def test_new_ci_attempt_creates_one_new_work_item(local):
    tick(local)
    store, data, _, reader, collector = local
    monitor.consume(store, SID, reader=reader, collector=collector)
    data['checks'] = [check(id='22', run_attempt=2, conclusion='failure')]
    assert tick(local, 1300)['events'] == 1
    assert tick(local, 1600)['events'] == 0


def test_pending_ci_never_generates_chat_work(local):
    local[1]['checks'] = [check(status='in_progress', conclusion=None)]
    assert tick(local, execute=True, spawn=lambda _: pytest.fail('pending CI must stay quiet'))['events'] == 0
    assert row(local[0])['event_id'] is None


def test_api_outage_stays_local_without_fabricating_a_pass(local):
    store, _, _, _, collector = local
    async def fail(*args):
        raise RuntimeError('GitHub unavailable')
    result = asyncio.run(monitor.tick(store, now=1000, execute=True, reader=fail, collector=collector,
                                      spawn=lambda _: pytest.fail('API errors must not wake chats')))
    assert result['errors'] == 1
    assert store.snapshot('owner/repo', 1) is None
    assert 'unavailable' in row(store)['error']


def test_registration_cannot_steal_canonical_owner(local):
    tick(local)
    store, _, _, reader, collector = local
    token = row(store)['event_id']
    store.register(OTHER, 'codex', 'owner/repo', 1, store.session(SID)['cwd'])
    assert monitor.consume(store, OTHER, event_id=token, reader=reader, collector=collector) is None
    assert row(store)['owner_session'] == SID
    assert monitor.consume(store, SID, event_id=token, reader=reader, collector=collector)


def test_foreign_chat_cannot_consume_owned_event(local):
    tick(local)
    store, _, calls, reader, collector = local
    assert monitor.consume(store, OTHER, event_id=row(store)['event_id'], reader=reader, collector=collector) is None
    assert len(calls) == 1
    assert row(store)['delivery_state'] == 'pending'


def test_merge_between_poll_and_delivery_cancels_wakeup(local):
    tick(local, execute=True, spawn=lambda _: None)
    store, data, _, reader, collector = local
    data.update(state='closed', merged=True)
    result = asyncio.run(monitor.deliver(store, SID, reader=reader, collector=collector,
                                         runner=lambda *a, **k: pytest.fail('stale PR must not wake')))
    assert result is False
    assert row(store)['state'] == 'merged'


def test_ci_recovery_between_poll_and_delivery_cancels_wakeup(local):
    tick(local, execute=True, spawn=lambda _: None)
    store, data, _, reader, collector = local
    data['checks'] = [check(conclusion='success')]
    result = asyncio.run(monitor.deliver(store, SID, reader=reader, collector=collector,
                                         runner=lambda *a, **k: pytest.fail('already fixed CI must not wake')))
    assert result is False
    assert row(store)['event_id'] is None


def test_missing_owner_never_falls_back_to_another_chat(local):
    tick(local, execute=True, spawn=lambda _: None)
    store, _, _, reader, collector = local
    with patch.object(store, 'session', return_value=None):
        with pytest.raises(ValueError, match='owner metadata'):
            asyncio.run(monitor.deliver(store, SID, reader=reader, collector=collector))


@pytest.mark.parametrize('client', ['codex', 'claude'])
def test_delivery_uses_exact_existing_owner_and_readable_work(local, client):
    store, _, _, reader, collector = local
    with store._connect() as db:
        db.execute('UPDATE sessions SET client=?', (client,))
    tick(local, execute=True, spawn=lambda _: None)
    calls = []
    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return {'returncode': 0}
    assert asyncio.run(monitor.deliver(store, SID, reader=reader, collector=collector, runner=runner,
                                      availability=lambda *_: 'idle'))
    args, kwargs = calls[0]
    assert SID in args
    text = args[args.index('--message') + 1] if client == 'codex' else kwargs['input_text']
    assert 'CI failing:' in text
    assert 'https://github.com/owner/repo/pull/1' in text
    assert 'Separate gate evidence' not in text
    assert '"gate"' not in text
    assert row(store)['delivery_state'] == 'queued'


def test_busy_owner_keeps_work_in_native_inbox_without_prompt(local):
    store = local[0]
    with store._connect() as db:
        db.execute('INSERT INTO watch_sessions VALUES(?,?,?)', (SID, 1, time.time()))
    assert tick(local, execute=True, spawn=lambda _: pytest.fail('busy owner must not receive queue'))['workers'] == 0
    assert row(store)['delivery_state'] == 'pending'


def test_stale_queued_message_stops_without_recovery_loop(local):
    store = local[0]
    event = dict(session_id=SID, hook_event_name='UserPromptSubmit', prompt=monitor.MARKER + str(__import__('uuid').uuid4()) + '-->\nOld work')
    result = monitor.native(event, store, None)
    assert result['continue'] is False
    with store._connect() as db:
        assert db.execute('SELECT busy FROM watch_sessions WHERE session_id=?', (SID,)).fetchone()[0] == 0


def test_bootstrap_uses_latest_actual_registration_without_adopting_reads(local):
    store = local[0]
    store.register(OTHER, 'claude', 'owner/repo', 1, store.session(SID)['cwd'])
    with store._connect() as db:
        db.execute('DELETE FROM pr_watch')
    monitor.bootstrap(store)
    assert row(store)['owner_session'] == OTHER
    assert row(store)['armed'] == 0
    assert tick(local, execute=True, spawn=lambda _: pytest.fail('history must establish a quiet baseline'))['events'] == 0


def test_merge_conflict_is_actionable_even_with_green_ci(local):
    local[1]['checks'] = [check(conclusion='success')]
    local[1]['mergeable'] = False
    assert tick(local)['events'] == 1
    assert 'Merge conflict' in row(local[0])['payload']


def test_missing_checkout_on_merged_work_is_unknown_without_monitor_revival(local):
    store = local[0]
    with store._connect() as db:
        db.execute("UPDATE pr_watch SET state='merged'")
    fake = dict(repository='owner/repo', number=1, cwd='/does/not/exist', plan_id='old')
    with patch.object(store, 'validation_plans', return_value=[fake]):
        progress = workflow.task_progress(store, SID)
    assert len(progress) == 1 and progress[0]['status'] == 'UNKNOWN'
    assert progress[0]['evidence']['phase'] == 'UNKNOWN'
    with store._connect() as db:
        assert db.execute('SELECT state FROM pr_watch').fetchone()['state'] == 'merged'


def test_read_only_redirect_is_not_engineering_mutation():
    event = dict(hook_event_name='PostToolUse', tool_name='Bash',
                 tool_input={'command': 'rg --files /tmp 2>/dev/null'}, tool_response={'exit_code': 0})
    assert hook._local_mutation(event) is None


def test_gateway_mention_does_not_exempt_direct_write():
    assert hook._direct_github_write('echo "caller-pr-gates github "; gh pr edit https://github.com/o/r/pull/1 --title hi')
    assert not hook._direct_github_write('rg "gh pr edit" /tmp/source.py')
    assert hook._direct_github_write('caller-pr-gates github -- pr edit https://github.com/o/r/pull/1 --title hi; gh pr close https://github.com/o/r/pull/2')
    assert not hook._direct_github_write('caller-pr-gates github -- pr edit https://github.com/o/r/pull/1 --title hi')


def test_exact_explanation_request_is_informational():
    assert turns._informational('There are some hooks on this machine. Explain to me what they are and why they are broken.')


def test_unpublished_source_blocks_local_runs_until_pr_exists(local):
    store = local[0]
    with store._connect() as db:
        db.execute('INSERT INTO unpublished_work VALUES(?,?,?,?,?)', (SID, 'owner/repo', 'new-fix', '/repo', time.time()))
    event = dict(hook_event_name='PreToolUse', session_id=SID, cwd='/repo', tool_name='Bash', tool_input={'command': 'pytest'})
    with patch.object(hook, '_candidate_dirs', return_value=['/repo']), patch.object(hook, '_git_context', return_value=('/repo','owner/repo','new-fix')):
        assert 'non-draft PR before local checks' in hook._local_validation_guard(event, store, SID)['systemMessage']


def test_conflict_unknown_does_not_become_passing_evidence(local):
    from caller_pr_gates.policy import evaluate_pr
    data = dict(local[1], mergeable=None)
    assert next(g for g in evaluate_pr(data, []) if g['gate'] == 'MERGE_CONFLICT')['status'] == 'UNKNOWN'


def test_reply_alone_does_not_close_a_review_thread():
    from test_policy import comment, receipt, statuses, REPLIED
    reply = dict(id='inline:21', author_login='agent', body='Added the boundary guard',
                 url='reply', created_at=REPLIED)
    data = snapshot()
    data['comments'] = [comment(replies=[reply])]
    gates = statuses(data, [receipt(reply_url='reply')])
    assert gates['REVIEW_HANDLED'] == 'PASS'
    assert gates['REVIEW_CONCLUDED'] == 'FAIL'
    data['comments'][0]['is_resolved'] = True
    assert statuses(data, [receipt(reply_url='reply')])['REVIEW_CONCLUDED'] == 'PASS'


def test_consumed_codex_event_releases_lease_for_next_pr(local):
    tick(local, execute=True, spawn=lambda _: None)
    store, _, _, reader, collector = local
    assert row(store)['lease_until'] > 0
    assert monitor.consume(store, SID, reader=reader, collector=collector)
    assert row(store)['lease_until'] == 0


def test_expired_queued_delivery_does_not_retry_unchanged_evidence(local):
    tick(local, execute=True, spawn=lambda _: None)
    store = local[0]
    with store._connect() as db:
        db.execute("UPDATE pr_watch SET delivery_state='queued',delivered_fingerprint=fingerprint")
    assert tick(local, 5000, execute=True, spawn=lambda _: pytest.fail('unchanged event must not restart'))['workers'] == 0
    assert row(store)['delivery_state'] == 'failed'


def test_explicit_retry_respects_live_lease_then_rearms(local):
    tick(local, execute=True, spawn=lambda _: None)
    store = local[0]
    with store._connect() as db:
        db.execute('UPDATE pr_watch SET lease_until=?', (time.time() + 300,))
    with pytest.raises(ValueError, match='live worker lease'):
        monitor.retry(store, SID, 'owner/repo', 1)
    with store._connect() as db:
        db.execute("UPDATE pr_watch SET lease_until=0,delivery_state='failed',delivered_fingerprint=fingerprint")
    assert monitor.retry(store, SID, 'owner/repo', 1)
    assert tick(local, 1001)['events'] == 1


def test_other_registration_preserves_canonical_worker_and_state(local):
    with local[0]._connect() as db:
        db.execute('UPDATE pr_watch SET lease_until=?,worker_pid=123,terminal_notice=1', (time.time() + 300,))
    local[0].register(OTHER, 'claude', 'owner/repo', 1, local[0].session(SID)['cwd'])
    state = row(local[0])
    assert state['owner_session'] == SID
    assert state['lease_until'] > time.time()
    assert (state['worker_pid'], state['terminal_notice']) == (123, 1)


def test_closed_pr_informs_active_engineering_owner_once_without_queue(local):
    store, data, _, reader, _ = local
    turns.start_turn(store, {'prompt': 'Fix this PR'}, SID)
    with store._connect() as db:
        db.execute('INSERT INTO watch_sessions VALUES(?,?,?)', (SID, 1, time.time()))
    data.update(state='closed', merged=True)
    tick(local, execute=True, spawn=lambda _: pytest.fail('closed PR cannot queue'))
    assert row(store)['terminal_notice'] == 1
    event = dict(session_id=SID, hook_event_name='PostToolUse')
    output = monitor.native(event, store, None)
    assert 'is merged' in output['hookSpecificOutput']['additionalContext']
    assert monitor.native(event, store, None) is None


def test_closed_pr_never_interrupts_information_request(local):
    store, data, _, _, _ = local
    turns.start_turn(store, {'prompt': 'Explain this hook'}, SID)
    with store._connect() as db:
        db.execute('INSERT INTO watch_sessions VALUES(?,?,?)', (SID, 1, time.time()))
    data.update(state='closed', merged=True)
    tick(local)
    assert row(store)['terminal_notice'] == 0


def test_background_failure_creates_actionable_changed_event(local):
    store, data, _, reader, collector = local
    data['checks'] = [check(conclusion='success')]
    assert tick(local)['events'] == 0
    failure = {'plan_id': 'real-plan', 'run_id': 'real-run', 'check_id': 'regression'}
    with patch.object(monitor, 'local_failures', return_value=[failure]):
        assert tick(local, 1300)['events'] == 1
        assert 'Background local checks failed: regression' in row(store)['payload']
        monitor.consume(store, SID, reader=reader, collector=collector)
        assert tick(local, 1600)['events'] == 0


def test_unknown_mergeability_preserves_proven_unchanged_quota_hold(local):
    from caller_pr_gates.external import gate_fingerprint
    from caller_pr_gates.policy import evaluate_pr
    store, data, _, _, _ = local
    gates = evaluate_pr(data, [])
    store.record_external_blocker(SID, 'owner/repo', 1, data['head_sha'], gate_fingerprint(data, gates), 'verified quota', 'https://github.com/owner/repo/actions/runs/2/job/3')
    data['mergeable'] = None
    assert monitor.quiet_hold(store, SID, data, evaluate_pr(data, []))
    assert tick(local, execute=True, spawn=lambda _: pytest.fail('quota must stay held'))['events'] == 0


def test_finishing_closed_branch_does_not_manufacture_new_pr(local):
    store = local[0]
    root = store.session(SID)['cwd']
    turns.start_turn(store, {'prompt': 'Fix this PR'}, SID)
    with store._connect() as db:
        db.execute("UPDATE pr_watch SET state='merged',branch='old-fix',updated_at=?", (time.time() + 1,))
    event = dict(hook_event_name='PostToolUse', session_id=SID, cwd=root, tool_name='Edit', tool_input={'file_path': root + '/source.py'})
    done = type('Done', (), {'returncode': 0, 'stdout': '{"found":false}'})()
    with patch.object(hook, '_mutation_dirs', return_value=[root]), patch.object(hook, '_git_context', return_value=(root, 'owner/repo', 'old-fix')), patch.object(hook, 'discover_current_pr', return_value={'found':False}):
        hook.discover(event, store)
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM unpublished_work').fetchone()[0] == 0


def test_persistent_service_polls_without_relaunch_and_accounts_for_work_time(local):
    store = local[0]
    sleeps, calls = [], []
    times = iter([0, 4, 60, 66])
    async def fake_tick(store, *, execute):
        calls.append(execute)
        return {'checked': 1, 'workers': 0}
    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError()
    with patch.object(monitor, 'tick', fake_tick):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(monitor.serve(store, sleeper=sleep, clock=lambda: next(times)))
    assert calls == [True, True]
    assert sleeps == [56, 54]
    assert json.loads((store.state_dir/'monitor-health.json').read_text())['checked'] == 1


def test_persistent_service_retains_local_error_without_restart_or_prompt(local):
    store = local[0]
    async def fail(*args, **kwargs):
        raise RuntimeError('registry unavailable')
    async def stop(delay):
        assert 0 < delay <= 60
        raise asyncio.CancelledError()
    with patch.object(monitor, 'tick', fail):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(monitor.serve(store, sleeper=stop))
    health = json.loads((store.state_dir/'monitor-health.json').read_text())
    assert health['errors'] == 1 and 'registry unavailable' in health['service_error']
    assert row(store)['event_id'] is None
