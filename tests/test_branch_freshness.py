import asyncio
import sys
from unittest.mock import patch

import pytest

from caller_pr_gates import cli, monitor, workflow
from caller_pr_gates.github import collect_with_client, _branch_evidence
from caller_pr_gates.handoff import evidence_fingerprint, hold_unresolved, current_hold
from caller_pr_gates.policy import branch_freshness_gate, evaluate_pr
from test_policy import SHA, OTHER, snapshot, fresh_branch
import test_policy as fixtures
from test_quiet_monitor import local, tick, row, SID
from test_workflow import local as workflow_local, publish, bind, run


def behind(data, base='d' * 40):
    data.update(base_sha=base, base_sha_after=base)
    data['branch_comparison'] = dict(base_sha=base, head_sha=data['head_sha'],
        merge_base_sha='c' * 40, ahead_by=2, behind_by=1, status='diverged')
    return data


def test_mergeable_does_not_prove_latest_base_is_integrated():
    data = behind(snapshot())
    gates = {g['gate']: g['status'] for g in evaluate_pr(data, [])}
    assert gates['MERGE_CONFLICT'] == 'PASS'
    assert gates['BRANCH_UP_TO_DATE'] == 'FAIL'
    assert all(status == 'PASS' for name, status in gates.items() if name != 'BRANCH_UP_TO_DATE')


@pytest.mark.parametrize('base', ['c' * 40, SHA])
def test_ahead_and_identical_have_proved_current_base_ancestry(base):
    assert branch_freshness_gate(dict(snapshot(), **fresh_branch(SHA, base)))['status'] == 'PASS'


@pytest.mark.parametrize('field,value', [
    ('base_sha', None), ('base_sha_after', OTHER), ('base_ref_after', 'new-target'),
    ('branch_comparison', None), ('branch_comparison', 'bad'),
])
def test_missing_or_racing_proof_never_passes(field, value):
    data = snapshot(); data[field] = value
    assert branch_freshness_gate(data)['status'] == 'UNKNOWN'


@pytest.mark.parametrize('field,value', [
    ('head_sha', OTHER), ('base_sha', OTHER), ('merge_base_sha', OTHER),
    ('behind_by', True), ('behind_by', -1), ('ahead_by', None),
    ('status', 'diverged'), ('status', 'identical'),
])
def test_contradictory_or_malformed_comparison_is_unknown(field, value):
    data = snapshot(); data['branch_comparison'][field] = value
    assert branch_freshness_gate(data)['status'] == 'UNKNOWN'


def test_equal_counts_with_different_commit_ids_are_not_identical():
    data = snapshot(); data['branch_comparison'].update(ahead_by=0, status='identical')
    assert branch_freshness_gate(data)['status'] == 'UNKNOWN'


def test_closed_source_keeps_unresolved_ci_but_retires_freshness():
    data = behind(snapshot()); data['state'] = 'closed'
    gate = branch_freshness_gate(data)
    assert gate['status'] == 'PASS' and gate['evidence']['applicable'] is False


@pytest.mark.parametrize('mode', ['stable', 'moves', 'error', 'retarget'])
def test_collector_rereads_current_branch_and_contains_ancestry_failure(mode):
    class Client(fixtures.CollectorTests.Client):
        branch_reads = 0
        async def get(self, path, params=None):
            if path == '/repos/owner/repo/branches/main':
                self.branch_reads += 1
                return {'commit': {'sha': OTHER if mode == 'moves' and self.branch_reads > 1 else 'c' * 40}}
            if '/compare/' in path:
                assert path.endswith('c' * 40 + '...' + SHA)
                assert params == {'per_page': 1}
                if mode == 'error': raise RuntimeError('GitHub 503 unavailable')
            data = await super().get(path, params)
            if mode == 'retarget' and path.endswith('/pulls/1') and self.reads > 1:
                data['base']['ref'] = 'other'
            return data
    client = Client()
    data = asyncio.run(collect_with_client(client, 'owner/repo', 1))
    assert client.branch_reads == 2
    gates = {g['gate']: g['status'] for g in evaluate_pr(data, [])}
    assert gates['BRANCH_UP_TO_DATE'] == ('PASS' if mode == 'stable' else 'UNKNOWN')
    assert gates['CI_FAILED'] == 'PASS'
    assert gates['REVIEW_HANDLED'] == 'FAIL'


def test_slash_target_branch_is_encoded_and_compare_uses_exact_shas():
    paths = []
    class Client:
        async def get(self, path, params=None):
            paths.append(path)
            if '/branches/' in path: return {'commit': {'sha': 'c' * 40}}
            return dict(base_commit={'sha': 'c' * 40}, merge_base_commit={'sha': 'c' * 40},
                        ahead_by=1, behind_by=0, status='ahead')
    data = asyncio.run(_branch_evidence(Client(), '/repos/owner/repo', 'release/v2', SHA))
    assert paths[0].endswith('/branches/release%2Fv2')
    assert data['branch_comparison']['base_sha'] == 'c' * 40


@pytest.mark.parametrize('status', ['PASS', 'UNKNOWN'])
def test_rollout_and_api_outage_preserve_legacy_event_identity(status):
    data = snapshot(); gates = evaluate_pr(data, [])
    old = [g for g in gates if g['gate'] != 'BRANCH_UP_TO_DATE']
    legacy = dict(data)
    for key in ['base_ref', 'base_ref_after', 'base_sha', 'base_sha_after', 'branch_comparison']:
        legacy.pop(key)
    expected = evidence_fingerprint(legacy, old)
    if status == 'UNKNOWN':
        data['branch_comparison'] = None
        data['component_errors'] = {'branch_freshness': {'kind': 'api', 'message': 'unavailable'}}
        gates = evaluate_pr(data, [])
    assert evidence_fingerprint(data, gates) == expected


def test_new_base_commit_wakes_exact_owner_once_without_changed_pr_head(local):
    store, data, _, reader, collector = local
    tick(local); monitor.consume(store, SID, reader=reader, collector=collector)
    old_head = data['head_sha']
    behind(data)
    assert tick(local, 1300)['events'] == 1
    text = monitor.consume(store, SID, reader=reader, collector=collector)
    assert 'Branch is behind current main' in text
    assert data['head_sha'] == old_head and row(store)['owner_session'] == SID
    assert tick(local, 1600)['events'] == 0


def test_branch_api_outage_after_consumed_failure_does_not_replay_ci(local):
    store, data, _, reader, collector = local
    behind(data)
    tick(local); monitor.consume(store, SID, reader=reader, collector=collector)
    old_identity = row(store)['fingerprint']
    data['branch_comparison'] = None
    data['component_errors'] = {'branch_freshness': {'kind': 'api', 'message': '503 unavailable'}}
    for now in [1300, 1600, 1900]:
        assert tick(local, now)['events'] == 0
        assert row(store)['fingerprint'] == old_identity
        saved = store.snapshot('owner/repo', 1)
        assert next(g for g in saved['gates'] if g['gate'] == 'BRANCH_UP_TO_DATE')['status'] == 'UNKNOWN'


def test_branch_api_outage_preserves_truthful_unresolved_handoff(local):
    store, data, *_ = local
    behind(data)
    old = hold_unresolved(store, SID, 'owner/repo', 1, data, evaluate_pr(data, []))
    data['branch_comparison'] = None
    data['component_errors'] = {'branch_freshness': {'message': '503'}}
    held = current_hold(store, SID, data, evaluate_pr(data, []))
    assert held['fingerprint'] == old['fingerprint'] and held['obligation_complete'] is False


def test_already_integrated_base_cancels_queued_branch_work(local):
    store, data, _, reader, collector = local
    data['checks'] = []; data['expected_checks'] = []
    behind(data); tick(local)
    data.update(**fresh_branch())
    assert monitor.consume(store, SID, reader=reader, collector=collector) is None
    assert row(store)['event_id'] is None


def test_native_cached_delivery_rejects_a_new_target_tip(local):
    store, data, _, _, collector = local
    behind(data); tick(local)
    async def reader(*args):
        return dict(state='open', merged=False, head={'sha': data['head_sha']},
                    base={'ref': 'main', 'sha': data['base_sha']})
    async def branch_reader(*args): return {'commit': {'sha': OTHER}}
    assert monitor.consume(store, SID, reader=reader, collector=collector, full=False, branch_reader=branch_reader) is None
    assert row(store)['next_check_at'] == 0
    assert row(store)['event_id'] is None


def test_stale_embedded_pr_base_does_not_discard_valid_branch_alert(local):
    store, data, _, _, collector = local
    behind(data); tick(local)
    async def reader(*args):
        return dict(state='open', merged=False, head={'sha': data['head_sha']},
                    base={'ref': 'main', 'sha': 'c' * 40})
    text = monitor.consume(store, SID, reader=reader, collector=collector, full=False)
    assert 'Branch is behind current main' in text
    assert row(store)['delivery_state'] == 'consumed'
    assert tick(local, 1300)['events'] == 0


def test_native_branch_outage_keeps_obligation_without_accelerating_poll_or_replaying(local):
    store, data, _, reader, collector = local
    behind(data); tick(local)
    before = row(store)
    async def outage(*args): raise RuntimeError('GitHub unavailable')
    assert monitor.consume(store, SID, reader=reader, collector=collector, full=False, branch_reader=outage) is None
    after = row(store)
    assert after['event_id'] == before['event_id'] and after['next_check_at'] == before['next_check_at']
    assert after['delivery_state'] == before['delivery_state']


def test_native_merged_source_retires_even_if_branch_probe_fails(local):
    store, data, _, reader, collector = local
    tick(local); data.update(state='closed', merged=True)
    async def outage(*args): raise RuntimeError('branch deleted')
    assert monitor.consume(store, SID, reader=reader, collector=collector, full=False, branch_reader=outage) is None
    assert row(store)['state'] == 'merged' and row(store)['event_id'] is None


def test_managed_validation_requires_base_sync_but_publication_still_comes_first(workflow_local):
    store, root, data, _ = workflow_local
    behind(data)
    store.set_snapshot('Owner/Repo', 1, data, evaluate_pr(data, []))
    bind(workflow_local)
    with patch('caller_pr_gates.workflow._run_process', side_effect=AssertionError('must synchronize first')):
        with pytest.raises(ValueError, match='SYNC_BASE_REQUIRED'):
            run(workflow_local)
        (root / 'src.txt').write_text('fixed')
        with pytest.raises(ValueError, match='PUBLISH_REQUIRED'):
            run(workflow_local)
    assert store.validation_runs(store.validation_plans('s')[0]['plan_id']) == []
    publish(workflow_local)
    assert run(workflow_local)['run']['status'] == 'pass'


@pytest.mark.parametrize('mode', ['behind', 'unknown', 'outage'])
@pytest.mark.parametrize('option', ['--background', '--worker'])
def test_cli_validation_always_refreshes_base_before_launch_or_worker(workflow_local, monkeypatch, capsys, mode, option):
    store, root, data, _ = workflow_local
    bind(workflow_local)
    async def fresh(*args):
        if mode == 'outage': raise RuntimeError('GitHub unavailable')
        current = dict(data)
        if mode == 'behind': behind(current)
        else: current['branch_comparison'] = None
        return current
    monkeypatch.setattr(cli, 'Store', lambda: store)
    monkeypatch.setattr('caller_pr_gates.github.collect_validation', fresh)
    monkeypatch.setattr(sys, 'argv', ['caller-pr-gates', 'validate', '--session', 's',
        '--pr', 'https://github.com/Owner/Repo/pull/1', '--cwd', str(root), '--check', 'regression', option])
    with patch('caller_pr_gates.workflow._run_process', side_effect=AssertionError('stale snapshot must not run')):
        with pytest.raises(SystemExit) as error: cli.main()
    assert error.value.code == 1
    expected = {'behind': 'SYNC_BASE_REQUIRED', 'unknown': 'REMOTE_READ_REQUIRED',
                'outage': 'GitHub unavailable'}[mode]
    assert expected in capsys.readouterr().err
    assert store.validation_runs(store.validation_plans('s')[0]['plan_id']) == []
