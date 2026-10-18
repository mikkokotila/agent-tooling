"""A durable queue acknowledgement must not hide a missing owner receipt."""
import asyncio
from contextlib import contextmanager
import json
import sqlite3
import threading
from unittest.mock import patch

import pytest

from caller_pr_gates import monitor
from caller_pr_gates.store import Store
from test_quiet_monitor import local, row, SID


def poll(local, now):
    store, _, _, reader, collector = local
    return asyncio.run(monitor.tick(store, now=now, execute=True, reader=reader,
                       collector=collector, spawn=lambda _: None, checklist=True))


def enqueue(local, now=1000):
    poll(local, now)
    store, _, _, reader, collector = local
    with patch.object(monitor.time, 'time', return_value=now):
        assert asyncio.run(monitor.deliver(store, SID, reader=reader, collector=collector,
                           runner=lambda *a, **k: {'returncode': 0}, checklist=True))


def test_queue_exit_zero_retains_a_receipt_deadline(local):
    enqueue(local)
    state = row(local[0])
    assert state['delivery_state'] == 'queued'
    assert state['lease_until'] == 0
    assert json.loads(state['payload'])['queue_deadline'] == 1300


def test_unloaded_owner_failure_stays_visible_after_restart_without_requeue(local):
    enqueue(local)
    store, data, calls, reader, collector = local
    original = row(store)
    store = Store(store.state_dir)
    local = store, data, calls, reader, collector
    for now in [1300, 4600, 87400]:
        result = poll(local, now)
        assert result['checked'] == 1 and result['workers'] == 0 and result['errors'] == 1
        assert result['unconsumed_deliveries'][0]['event_id'] == original['event_id']
        state = row(store)
        assert state['delivery_state'] == 'queued'  # Still authentic and consumable.
        assert state['error'].startswith(monitor.UNCONSUMED)
        assert json.loads(state['payload'])['queue_deadline'] == 1300
    assert len(calls) == 5  # Initial poll, delivery, then exactly three scheduled polls.
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='pr_delivery_unconsumed'").fetchone()[0] == 1


def test_health_without_a_due_pr_poll_adds_no_remote_reads(local):
    enqueue(local)
    poll(local, 1300)
    before = len(local[2])
    result = poll(local, 1301)
    assert result['checked'] == 0 and result['errors'] == 1 and result['workers'] == 0
    assert len(local[2]) == before


def test_poll_outage_remains_visible_without_repeating_delivery_audits(local):
    enqueue(local)
    poll(local, 1300)
    store, data, _, reader, collector = local

    async def unavailable(*args):
        raise RuntimeError('GitHub outage detail')

    for now in [1600, 1900]:
        result = asyncio.run(monitor.tick(store, now=now, execute=True, reader=unavailable,
                            collector=collector, spawn=lambda _: None, checklist=True))
        assert result['errors'] == 2 and result['workers'] == 0
        assert row(store)['error'] == 'RuntimeError: GitHub outage detail'
    # A new fingerprint rebuilds the work payload without resetting this
    # queued event's deadline or its once-only diagnostic receipt.
    data['head_sha'] = 'b' * 40
    assert poll(local, 2200)['errors'] == 1
    state = row(store)
    assert state['error'].startswith(monitor.UNCONSUMED)
    assert json.loads(state['payload'])['queue_deadline'] == 1300
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='pr_delivery_unconsumed'").fetchone()[0] == 1


def test_late_native_receipt_clears_only_the_delivery_failure(local):
    enqueue(local)
    poll(local, 1300)
    store, _, _, reader, collector = local
    with patch.object(monitor.time, 'time', return_value=1301):
        assert monitor.consume(store, SID, event_id=row(store)['event_id'], reader=reader,
                               collector=collector, checklist=True)
    assert row(store)['delivery_state'] == 'consumed' and row(store)['error'] is None
    assert monitor.unconsumed_deliveries(store, 2000) == []


def test_busy_owner_is_never_declared_stalled_or_restarted(local):
    enqueue(local)
    with local[0]._connect() as db:
        db.execute('INSERT INTO watch_sessions VALUES(?,?,?)', (SID, 1, 1100))
    assert poll(local, 1300)['errors'] == 0
    assert row(local[0])['delivery_state'] == 'queued'


@pytest.mark.parametrize('state,merged', [('closed', True), ('closed', False)])
def test_retirement_cancels_an_expired_delivery_without_failure(local, state, merged):
    enqueue(local)
    local[1].update(state=state, merged=merged)
    result = poll(local, 1300)
    assert result['retired'] == 1 and result['errors'] == 0 and result['workers'] == 0
    assert row(local[0])['event_id'] is None


def test_legacy_codex_queue_gets_one_fixed_grace_period(local):
    enqueue(local)
    store = local[0]
    with store._connect() as db:
        payload = json.loads(row(store)['payload'])
        payload.pop('queue_deadline')
        db.execute('UPDATE pr_watch SET payload=?', (json.dumps(payload),))
    assert monitor.unconsumed_deliveries(store, 2000) == []
    assert monitor.unconsumed_deliveries(store, 2299) == []
    assert len(monitor.unconsumed_deliveries(store, 2300)) == 1
    assert json.loads(row(store)['payload'])['queue_deadline'] == 2300


def test_claude_delivery_is_untouched_by_codex_guard(local):
    enqueue(local)
    store = local[0]
    with store._connect() as db:
        db.execute("UPDATE pr_watch SET client='claude',error='existing Claude error'")
    before = row(store)
    assert monitor.unconsumed_deliveries(store, 87400) == []
    assert row(store) == before


def test_receipt_cannot_be_audited_as_missing_after_it_commits(local):
    enqueue(local)
    store = local[0]
    connected = store._connect
    committed = threading.Event()
    workers = []

    def receipt():
        with sqlite3.connect(store.path, timeout=5) as db:
            db.execute("UPDATE pr_watch SET delivery_state='consumed',error=NULL")
            db.execute("INSERT INTO audit(event,fields,recorded_at) VALUES('test_receipt','{}',2000)")
        committed.set()

    @contextmanager
    def racing_connection():
        with connected() as db:
            def trace(sql):
                if sql.startswith('SELECT busy FROM watch_sessions') and not workers:
                    worker = threading.Thread(target=receipt)
                    workers.append(worker)
                    worker.start()
                    # Without the guard's transaction, this receipt commits
                    # before the stale missing-receipt audit is written.
                    committed.wait(.1)
            db.set_trace_callback(trace)
            yield db

    with patch.object(store, '_connect', racing_connection):
        monitor.unconsumed_deliveries(store, 2000)
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert committed.is_set()
    with connected() as db:
        events = [r[0] for r in db.execute("SELECT event FROM audit WHERE event IN ('test_receipt','pr_delivery_unconsumed') ORDER BY id")]
    assert events == ['pr_delivery_unconsumed', 'test_receipt']
    assert row(store)['delivery_state'] == 'consumed' and row(store)['error'] is None
