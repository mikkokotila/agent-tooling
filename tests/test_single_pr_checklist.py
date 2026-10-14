"""Production native routing, owner isolation and quiet lifecycle regressions."""
import asyncio
import io
import json
import os
import time
from unittest.mock import patch

import pytest

from caller_pr_gates import checklist, hook, monitor, prompts, troubleshooting, turns
from caller_pr_gates.store import Store
from test_policy import check, comment
from test_quiet_monitor import local, row, SID, OTHER
from test_prompt_catalogue import catalogue, edit, global_targets


def tick(local, now=1000, **kwargs):
    store, _, _, reader, collector = local
    return asyncio.run(monitor.tick(store, now=now, reader=reader, collector=collector, checklist=True, **kwargs))


@pytest.mark.parametrize('client', ['codex', 'claude'])
def test_native_unowned_turns_and_stop_never_poll_or_inject(tmp_path, client):
    store = Store(tmp_path / 'state')
    with patch.dict(os.environ, CALLER_HOOK_CLIENT=client), patch.object(monitor, 'probe', side_effect=AssertionError('unexpected GitHub read')):
        for name in ['SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop', 'StopFailure']:
            event = dict(hook_event_name=name, session_id=SID, cwd=str(tmp_path), prompt='Explain this.')
            assert checklist.handle(event, store) is None


def test_same_editable_checklist_is_used_by_both_native_clients(catalogue, tmp_path):
    text = prompts.baseline_text('pr_checklist') + ' Human checklist edit.'
    edit(catalogue, 'pr_checklist', text)
    for client in ['codex', 'claude']:
        with patch.dict(os.environ, CALLER_HOOK_CLIENT=client):
            result = checklist.handle(dict(hook_event_name='PreToolUse', session_id=SID, cwd=str(tmp_path),
                tool_name='Bash', tool_input={'command':'gh pr create --draft --title example'}), Store(tmp_path / client))
        output = result['hookSpecificOutput']
        assert output['permissionDecision'] == 'deny'
        assert output['permissionDecisionReason'] == text.replace('${pr_urls}', 'publication in this chat')


def test_global_mirrors_only_checklist_preserving_personal_text(catalogue, tmp_path):
    targets = global_targets(tmp_path)
    for target in targets:
        target.write_text('Personal prefix.\n' + target.read_text() + '\nPersonal suffix.')
    text = prompts.baseline_text('pr_checklist') + ' Mirrored checklist edit.'
    edit(catalogue, 'pr_checklist', text)
    assert prompts.sync_instruction_files(targets, checklist=True)['changed'] == 2
    for target in targets:
        value = target.read_text()
        assert value.startswith('Personal prefix.') and value.endswith('Personal suffix.')
        assert 'Mirrored checklist edit.' in value
        assert '<!-- troubleshooting-report:' not in value and '<!-- pr-progress:' not in value
        assert value.count('<!-- caller-pr-gates:begin -->') == 1
    assert prompts.sync_instruction_files(targets, checklist=True)['changed'] == 0


def test_owned_stop_is_silent_even_with_known_ci_failure(local):
    tick(local)
    store = local[0]
    before = row(store)
    with patch.object(hook, 'evaluate', side_effect=AssertionError('Stop must not collect')):
        assert checklist.handle(dict(hook_event_name='Stop', session_id=SID, cwd=before['cwd']), store) is None
    assert row(store)['event_id'] == before['event_id']


def test_owner_and_first_checkout_cannot_be_replaced(local, tmp_path):
    store = local[0]
    original = row(store)
    other_dir = tmp_path / 'second'; other_dir.mkdir()
    store.register(OTHER, 'claude', 'owner/repo', 1, str(other_dir))
    store.register(SID, 'codex', 'owner/repo', 1, str(other_dir))
    assert (row(store)['owner_session'], row(store)['cwd']) == (SID, original['cwd'])
    tick(local)
    assert monitor.consume(store, OTHER, event_id=row(store)['event_id'], checklist=True) is None


def test_subagent_inherits_chat_and_cannot_mark_parent_idle(local):
    store = local[0]
    cwd = row(store)['cwd']
    checklist.handle(dict(hook_event_name='PreToolUse', session_id=SID, cwd=cwd), store)
    assert checklist.handle(dict(hook_event_name='SubagentStart', session_id=SID, cwd=cwd, agent_id=OTHER), store) is None
    assert store.session(OTHER)['root_session'] == SID
    assert checklist.handle(dict(hook_event_name='Stop', session_id=OTHER, cwd=cwd), store) is None
    with store._connect() as db:
        assert db.execute('SELECT busy FROM watch_sessions WHERE session_id=?', (SID,)).fetchone()[0] == 1


def test_validation_guard_is_bound_to_canonical_owner_checkout_and_branch(local):
    store = local[0]
    cwd = row(store)['cwd']
    store.register(OTHER, 'codex', 'owner/repo', 1, cwd)
    event = dict(hook_event_name='PreToolUse', session_id=OTHER, cwd=cwd, tool_name='Bash', tool_input={'command':'pytest tests'})
    with patch.object(hook, '_git_context', return_value=(cwd, 'owner/repo', 'feature')):
        assert checklist.handle(event, store) is None
        assert checklist.handle(dict(event, session_id=SID), store)['hookSpecificOutput']['permissionDecision'] == 'deny'
    data = local[1]; data['head_ref'] = 'feature'
    store.set_snapshot('owner/repo', 1, data, [])
    with patch.object(hook, '_git_context', return_value=(cwd, 'owner/repo', 'main')):
        assert checklist.handle(dict(event, session_id=SID), store) is None


def test_delivery_uses_registered_checkout_after_chat_changes_cwd(local, tmp_path):
    tick(local, execute=True, spawn=lambda _: None)
    store, _, _, reader, collector = local
    original = row(store)['cwd']
    other = tmp_path / 'another-checkout'; other.mkdir()
    store.record_session(SID, 'codex', str(other))
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return {'returncode':0}
    assert asyncio.run(monitor.deliver(store, SID, reader=reader, collector=collector, runner=runner, checklist=True))
    assert calls[0][1]['cwd'] == original
    assert 'Automated PR hook' in str(calls[0])


def test_one_pr_per_queued_message_and_receipt(local):
    store, _, _, reader, collector = local
    store.register(SID, 'codex', 'owner/repo', 2, store.session(SID)['cwd'])
    tick(local, execute=True, spawn=lambda _: None)
    assert len(monitor.pending(store, SID, ('reserved',))) == 2
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return {'returncode':0}
    assert asyncio.run(monitor.deliver(store, SID, reader=reader, collector=collector, runner=runner, checklist=True))
    assert len(monitor.pending(store, SID, ('queued',))) == 1
    assert len(monitor.pending(store, SID, ('pending',))) == 1
    assert str(calls[0]).count('https://github.com/owner/repo/pull/') == 1


@pytest.mark.parametrize('merged', [True, False])
def test_closed_before_delivery_is_silent_retirement_without_waivers(local, merged):
    tick(local, execute=True, spawn=lambda _: None)
    store, data, _, reader, collector = local
    data.update(state='closed', merged=merged)
    assert not asyncio.run(monitor.deliver(store, SID, reader=reader, collector=collector,
        runner=lambda *a, **k: pytest.fail('closed work cannot wake chat'), checklist=True))
    assert row(store)['event_id'] is None and row(store)['terminal_notice'] == 0
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM issue_closeouts').fetchone()[0] == 0
    assert store.snapshot('owner/repo', 1)['gates'][1]['status'] != 'PASS' or any(
        g['status'] == 'FAIL' for g in store.snapshot('owner/repo', 1)['gates'])


def test_queued_merged_checklist_is_rejected_before_turn_start(local):
    tick(local)
    store, data, _, reader, collector = local
    queued = monitor.MARKER + row(store)['event_id'] + '-->\n' + checklist.message([row(store)])
    data.update(state='closed', merged=True)
    original = monitor.consume
    def consume(*args, **kwargs):
        return original(*args, **dict(kwargs, reader=reader, collector=collector))
    with patch.object(monitor, 'consume', side_effect=consume), patch.object(turns, 'start_turn', side_effect=AssertionError('not a human turn')):
        result = checklist.handle(dict(hook_event_name='UserPromptSubmit', session_id=SID, cwd=row(store)['cwd'], prompt=queued), store)
    assert result == {'continue':False, 'stopReason':''}
    assert row(store)['state'] == 'merged'


@pytest.mark.parametrize('body', [
    'Durable prd review followup aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa. The 30-minute check is due.\nOld text',
    '<!-- issue-closeout:old -->\nOld text',
    '<!-- pr-work-event:old -->\nOld API details'])
def test_legacy_messages_are_silently_rejected_as_automation(tmp_path, body):
    with patch.object(turns, 'start_turn', side_effect=AssertionError('not human authorization')):
        result = checklist.handle(dict(hook_event_name='UserPromptSubmit', session_id=SID, cwd=str(tmp_path), prompt=body), Store(tmp_path / 'state'))
    assert result == {'continue':False, 'stopReason':''}


def test_pending_interruptions_and_issue_work_cannot_wake_chat(local):
    store, data, *_ = local
    data['checks'] = [check(status='in_progress', conclusion=None)]
    data['closing_issues'] = []
    assert tick(local)['events'] == 0
    data['checks'] = [check(conclusion='cancelled')]
    assert tick(local, 1300)['events'] == 0
    assert row(store)['event_id'] is None


def test_unchanged_failure_and_unavailable_evidence_never_replay(local):
    tick(local)
    store, data, _, reader, collector = local
    assert monitor.consume(store, SID, reader=reader, collector=collector, checklist=True)
    assert tick(local, 1300)['events'] == 0
    data['component_errors'] = {'review':'GitHub 403 rate-limit request ID changes'}
    assert tick(local, 1600)['events'] == 0
    data['component_errors'] = {}
    assert tick(local, 1900)['events'] == 0
    data['comments'] = [comment()]
    assert tick(local, 2200)['events'] == 1


def test_losing_review_inventory_in_outage_never_replays_known_repairs(local):
    store,data,_,reader,collector=local
    data['comments']=[comment()]
    tick(local)
    assert monitor.consume(store,SID,reader=reader,collector=collector,checklist=True)
    data['comments']=[]
    data['component_errors']={'review':'GitHub quota exhausted'}
    assert tick(local,1300)['events']==0 and row(store)['event_id'] is None
    data['comments']=[comment()];data['component_errors']={}
    assert tick(local,1600)['events']==0


def test_cached_retired_advisory_entrypoint_stays_silent():
    with patch('sys.stdin', io.StringIO(json.dumps(dict(hook_event_name='SessionStart', session_id=SID)))), patch('sys.stdout', new_callable=io.StringIO) as output:
        troubleshooting.main()
    assert output.getvalue() == ''
