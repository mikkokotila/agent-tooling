"""Delivery is acknowledgement; fresh unresolved work must survive a stopped turn."""
import asyncio
import os
from unittest.mock import patch

import pytest

from caller_pr_gates import checklist, monitor
from test_policy import check, comment
from test_quiet_monitor import local, row, SID
from test_single_pr_checklist import tick


@pytest.mark.parametrize('client', ['codex', 'claude'])
@pytest.mark.parametrize('count', [1, 2])
@pytest.mark.parametrize('work', ['ci', 'review', 'base'])
def test_stopped_owner_receives_explicit_checklist_again_at_existing_cadence(local, client, count, work):
    store, data, calls, reader, collector = local
    data['collected_at'] = 1000
    if work != 'ci':
        data['checks'] = [check()]
        if work == 'review':
            data['comments'] = [comment(author_login='copilot', is_outdated=True)]
        else:
            data['branch_comparison'].update(behind_by=1, merge_base_sha='d' * 40, status='diverged')
    with store._connect() as db:
        db.execute('UPDATE sessions SET client=? WHERE session_id=?', (client, SID))
        db.execute('UPDATE pr_watch SET client=?', (client,))
    if count == 2:
        store.register(SID, client, 'owner/repo', 2, row(store)['cwd'])
    messages = []
    original = monitor.consume
    def consume(*args, **kwargs):
        return original(*args, **dict(kwargs, reader=reader, collector=collector))
    def runner(argv, **kwargs):
        if client == 'codex':
            assert argv[1:4] == ['queue', '--thread', SID]
            prompt = argv[argv.index('--message') + 1]
        else:
            assert argv[1:4] == ['--print', '--resume', SID]
            prompt = kwargs['input_text']
        messages.append(prompt)
        event = dict(hook_event_name='UserPromptSubmit', session_id=SID, cwd=row(store)['cwd'], prompt=prompt)
        assert checklist.handle(event, store) is None
        assert row(store)['delivery_state'] == 'consumed'
        assert checklist.handle(dict(event, hook_event_name='Stop'), store) is None
        return {'returncode': 0}
    interval = 300 if count == 1 else 600
    with patch.dict(os.environ, CALLER_HOOK_CLIENT=client), patch.object(monitor, 'consume', side_effect=consume):
        for now in [1000, 1000 + interval]:
            data['collected_at'] = now
            with patch.object(monitor.time, 'time', return_value=now):
                assert tick(local, now, execute=True, spawn=lambda _: None)['workers'] == 1
                assert asyncio.run(monitor.deliver(store, SID, reader=reader, collector=collector,
                    runner=runner, availability=lambda *args: 'idle', checklist=True))
                before = len(calls)
                assert tick(local, now + interval - 1)['checked'] == 0
                assert len(calls) == before
    assert len(messages) == 2 and messages[0] != messages[1]
    assert messages[0].split('-->\n', 1)[1] == messages[1].split('-->\n', 1)[1]
    assert 'Automated PR hook' in messages[1]


@pytest.mark.parametrize('hold', ['busy', 'lease'])
def test_unchanged_work_does_not_interrupt_active_owner_or_worker(local, hold):
    store, _, _, reader, collector = local
    tick(local)
    assert monitor.consume(store, SID, reader=reader, collector=collector, checklist=True)
    with store._connect() as db:
        if hold == 'busy':
            db.execute('INSERT INTO watch_sessions VALUES(?,1,1000)', (SID,))
        else:
            db.execute('UPDATE pr_watch SET lease_until=2000')
    assert tick(local, 1300, execute=True, spawn=lambda _: pytest.fail('active owner cannot be resumed'))['events'] == 0


@pytest.mark.parametrize('state', ['passed', 'pending', 'outage', 'merged', 'closed'])
def test_nonactionable_fresh_state_cancels_continuation_after_receipt(local, state):
    store, data, _, reader, collector = local
    tick(local)
    assert monitor.consume(store, SID, reader=reader, collector=collector, checklist=True)
    if state == 'passed': data['checks'] = [check()]
    elif state == 'pending': data['checks'] = [check(status='in_progress', conclusion=None)]
    elif state == 'outage': data['component_errors'] = {'review': 'GitHub quota unavailable'}
    else: data.update(state='closed', merged=state == 'merged')
    result = tick(local, 1300, execute=True, spawn=lambda _: pytest.fail('cannot resume nonactionable work'))
    assert result['workers'] == 0 and row(store)['event_id'] is None
