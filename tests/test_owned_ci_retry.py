"""Owned CI retries use lightweight ancestry evidence and a durable one-attempt bound."""
import json
import subprocess
from unittest.mock import patch

import pytest
from caller_pr_gates.cli import gateway
from test_cli import args, store
from test_policy import snapshot


@pytest.fixture
def retry_case(store):
    store.register('s', 'codex', 'owner/repo', 1, '/tmp')
    snap = snapshot()
    run = dict(id=12, name='Tests', path='.github/workflows/tests.yml', event='pull_request',
               head_sha=snap['head_sha'], status='completed', conclusion='failure', run_attempt=1,
               pull_requests=[{'url': 'https://api.github.com/repos/owner/repo/pulls/1'}])
    argv = ['api', 'repos/owner/repo/actions/runs/12/rerun', '--method', 'POST']
    value = args(argv); value.target = 'https://github.com/owner/repo/pull/1'
    reads = []
    def transport(argv, **kwargs):
        reads.append(argv)
        if argv[-1] == 'repos/owner/repo/actions/runs/12': body = run
        elif argv[-1] == 'repos/owner/repo/pulls/1': body = {'state': 'open', 'merged': False, 'head': {'sha': snap['head_sha']}, 'base': {'ref': snap['base_ref']}}
        elif argv[-1] == 'repos/owner/repo/branches/main': body = {'commit': {'sha': snap['base_sha']}}
        elif argv == ['gh', *value.arguments]: body = {}
        else: raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), '')
    async def validation(*a): return snap
    return value, snap, run, reads, transport, validation


def test_one_owned_failed_ci_retry_without_full_review_collection(store, retry_case):
    value, _, _, reads, transport, validation = retry_case
    with patch('caller_pr_gates.cli.github_run', side_effect=transport), patch('caller_pr_gates.github.collect_validation', side_effect=validation), patch('caller_pr_gates.github.collect_pr', side_effect=AssertionError('must not collect review/CI inventory')):
        gateway(value, store)
        assert len(reads) == 4  # Run + final PR/target + POST; preflight is separately bounded.
        with pytest.raises(ValueError, match='bounded retry'): gateway(value, store)
    assert sum(r == ['gh', *value.arguments] for r in reads) == 1


@pytest.mark.parametrize('change', ['unowned','wrong_head','behind','head_race','target_race','passed','running','copilot','review','other_pr','push_event','extra_fields','wrong_run'])
def test_rerun_cannot_escape_owned_current_head_ci(store, retry_case, change):
    value, snap, run, reads, transport, validation = retry_case
    if change == 'unowned': value.session = 'other'
    elif change == 'wrong_head': run['head_sha'] = 'b' * 40
    elif change == 'behind': snap['branch_comparison']['merge_base_sha'] = 'd' * 40
    elif change == 'head_race': snap['head_sha_after'] = 'b' * 40
    elif change == 'target_race': snap['base_sha_after'] = 'b' * 40
    elif change == 'passed': run['conclusion'] = 'success'
    elif change == 'running': run['status'] = 'in_progress'
    elif change == 'copilot': run['name'] = 'Copilot code review'
    elif change == 'review': run['path'] = '.github/workflows/review.yml'
    elif change == 'other_pr': run['pull_requests'][0]['url'] = 'https://api.github.com/repos/owner/repo/pulls/2'
    elif change == 'push_event': run['event'] = 'push'
    elif change == 'extra_fields': value.arguments += ['-f', 'body=anything']
    elif change == 'wrong_run': run['id'] = 99
    with patch('caller_pr_gates.cli.github_run', side_effect=transport), patch('caller_pr_gates.github.collect_validation', side_effect=validation):
        with pytest.raises(ValueError): gateway(value, store)
    assert not any(r == ['gh', *value.arguments] for r in reads)


def test_cancelled_readiness_keeps_its_existing_permitted_route(store, retry_case):
    value, _, run, _, transport, validation = retry_case
    run.update(name='pr_merge_readiness', path='.github/workflows/pr_merge_readiness.yml', event='pull_request_review', conclusion='cancelled')
    with patch('caller_pr_gates.cli.github_run', side_effect=transport), patch('caller_pr_gates.github.collect_validation', side_effect=validation): gateway(value, store)
