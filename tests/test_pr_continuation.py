"""A delivered failure must lead to action or an evidenced wait, without polling more."""
import os
from unittest.mock import patch

import pytest
from caller_pr_gates import monitor, turns
from caller_pr_gates.policy import evaluate_pr
from test_quiet_monitor import local, tick, row, SID, OTHER


def delivered(local):
    store, _, _, reader, collector = local
    tick(local)
    monitor.consume(store, SID, reader=reader, collector=collector)
    turns.start_turn(store, {'prompt': 'Fix the owned PR', 'turn_id': 'work'}, SID)
    return store


def stop(store, **extra):
    return monitor.native({'hook_event_name': 'Stop', 'session_id': SID, 'turn_id': 'work', **extra}, store, None)


def test_idle_after_delivery_has_one_recovery_at_existing_cadence_then_stays_quiet(local):
    store = delivered(local)
    calls = len(local[2]); original = row(store)['event_id']
    assert stop(store) is None
    assert len(local[2]) == calls  # Stop uses only already collected evidence.
    assert row(store)['delivery_state'] == 'pending' and row(store)['event_id'] != original
    assert row(store)['retry_at'] == row(store)['next_check_at'] == 1300
    with patch.object(monitor.time, 'time', return_value=1000):
        assert monitor.consume(store, SID, reader=local[3], collector=local[4]) is None
    assert len(local[2]) == calls  # Deferred native consumption also makes no reads.
    launched = []
    assert tick(local, 1299, True, launched.append)['workers'] == 0
    assert len(local[2]) == calls
    assert tick(local, 1300, True, launched.append)['workers'] == 1
    monitor.consume(store, SID, reader=local[3], collector=local[4])
    assert stop(store)['systemMessage']
    assert store.recovery('owner/repo', 1, SID)['idle_retry_outcome'] == 'stalled'
    assert stop(store) is None
    assert tick(local, 1600, True, launched.append)['workers'] == 0
    assert tick(local, 1900, True, launched.append)['workers'] == 0
    assert len(launched) == 1


def test_actual_async_question_receipt_waits_until_new_user_turn(local):
    store = delivered(local)
    monitor.native({'hook_event_name': 'PostToolUse', 'session_id': SID,
                    'tool_name': 'functions.request_user_input_async',
                    'tool_input': {'questions': [{'title': 'Authorize the excluded code change?'}]},
                    'tool_response': {}}, store, None)
    assert store.recovery('owner/repo', 1, SID)['awaiting_user_turn'] == 'work'
    assert stop(store) is None and row(store)['delivery_state'] == 'consumed'
    turns.start_turn(store, {'prompt': 'Continue the authorized fix', 'turn_id': 'next'}, SID)
    monitor.native({'hook_event_name': 'Stop', 'session_id': SID, 'turn_id': 'next'}, store, None)
    assert row(store)['delivery_state'] == 'pending'


def test_prose_blocker_or_failed_question_tool_does_not_prove_a_wait(local):
    store = delivered(local)
    monitor.native({'hook_event_name': 'PostToolUse', 'session_id': SID,
                    'tool_name': 'request_user_input_async', 'tool_input': {'questions': ['please approve']},
                    'tool_response': {'isError': True}}, store, None)
    stop(store, last_assistant_message='Merge is blocked; authorization is pending')
    assert row(store)['delivery_state'] == 'pending'


@pytest.mark.parametrize('condition', ['unknown', 'pending', 'merged', 'information', 'stale_turn', 'stale_evidence', 'lease', 'external'])
def test_unavailable_waiting_retired_and_unrelated_turns_never_recover(local, condition):
    store = delivered(local); data = local[1]; event = {'hook_event_name': 'Stop', 'session_id': SID, 'turn_id': 'work'}
    if condition == 'unknown':
        store.set_snapshot('owner/repo', 1, data, [{'gate': 'GITHUB_DATA', 'status': 'UNKNOWN'}])
    elif condition == 'pending':
        data['checks'][0].update(status='in_progress', conclusion=None)
        store.set_snapshot('owner/repo', 1, data, evaluate_pr(data, []))
    elif condition == 'merged':
        monitor.retire(store, 'owner/repo', 1, {'state': 'closed', 'merged': True, 'head': {'sha': data['head_sha']}})
    elif condition == 'information':
        turns.start_turn(store, {'prompt': 'Explain the remaining failure', 'turn_id': 'work'}, SID)
    elif condition == 'stale_turn':
        event['turn_id'] = 'old'
    elif condition == 'stale_evidence':
        data['collected_at'] = 1
        store.set_snapshot('owner/repo', 1, data, evaluate_pr(data, []))
    elif condition == 'lease':
        with store._connect() as db: db.execute('UPDATE pr_watch SET lease_until=?', (10**12,))
    with patch.object(monitor, 'quiet_hold', return_value=condition == 'external'):
        monitor.native(event, store, None)
    assert row(store)['delivery_state'] != 'pending'


def test_background_process_wait_is_local_and_does_not_create_another_delivery(local):
    store = delivered(local)
    plan = store.save_validation_plan(SID, 'owner/repo', 1, str(local[0].state_dir.parent),
                                     {'checks': [{'id': 'check', 'argv': ['python', '-V'], 'reason': 'required', 'cwd': '.'}], 'input_paths': [], 'env_names': []}, 'inputs', '[]')
    store.begin_validation(plan['plan_id'], 'check', 'inputs', owner_pid=os.getpid(), session_id=SID)
    stop(store)
    assert row(store)['delivery_state'] == 'consumed'
    assert store.recovery('owner/repo', 1, SID)['awaiting_validation'] is True
    run = store.validation_runs(plan['plan_id'])[0]
    store.finish_validation(run['run_id'], 0, 'inputs', session_id=SID)
    launched = []
    assert tick(local, 1300, True, launched.append)['workers'] == 1
    assert len(launched) == 1


def test_foreign_owner_and_repeated_explicit_retry_respect_ownership_and_lease(local):
    store = delivered(local)
    monitor.native({'hook_event_name': 'Stop', 'session_id': OTHER}, store, None)
    assert row(store)['delivery_state'] == 'consumed'
    stop(store)
    monitor.consume(store, SID, reader=local[3], collector=local[4])
    stop(store)
    assert monitor.retry(store, SID, 'owner/repo', 1)
    assert store.recovery('owner/repo', 1, SID)['idle_retry_fingerprint'] is None


def test_multiple_pr_question_receipt_is_bound_to_explicit_target(local):
    store = delivered(local)
    store.register(SID, 'codex', 'owner/repo', 2, store.session(SID)['cwd'])
    monitor.native({'hook_event_name': 'PostToolUse', 'session_id': SID,
                    'tool_name': 'request_user_input_async',
                    'tool_input': {'questions': [{'title': 'Decision for https://github.com/owner/repo/pull/1'}]},
                    'tool_response': {}}, store, None)
    assert store.recovery('owner/repo', 1, SID)['awaiting_user_turn'] == 'work'
    assert store.recovery('owner/repo', 2, SID) is None


def test_nested_command_guard_reads_executable_command_not_edited_file_data(local):
    from caller_pr_gates import hook
    store = delivered(local)
    event = {'tool_name': 'functions.exec', 'cwd': store.session(SID)['cwd'], 'tool_input': {'code': "await tools.exec_command({cmd:'pytest tests/test_fix.py'});"}}
    with patch.object(hook, '_candidate_dirs', return_value=[event['cwd']]), patch.object(hook, '_git_context', return_value=(event['cwd'], 'owner/repo', 'fix')):
        assert hook._local_validation_guard(event, store, SID)
        event['tool_input']['code'] = "await tools.apply_patch('*** Add File: note.txt\\n+pytest tests/test_fix.py');"
        assert hook._local_validation_guard(event, store, SID) is None


def test_stop_without_a_recorded_user_turn_does_not_adopt_work(local):
    store = local[0]
    tick(local)
    monitor.consume(store, SID, reader=local[3], collector=local[4])
    assert stop(store) is None
    assert row(store)['delivery_state'] == 'consumed'
