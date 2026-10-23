"""Approved integration/document policy and exact protection delta regressions."""
import asyncio
from copy import deepcopy
import json
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from caller_pr_gates import cli, github, governance, hook
from caller_pr_gates.policy import branch_freshness_gate, evaluate_pr
from test_branch_freshness import behind
from test_cli import args
from test_github import Client, SHA
from test_policy import snapshot
from test_workflow import local, git

REPOSITORIES = ['Vaquum/Limen', 'autonomio/poise', 'Vaquum/Origo']


class RepositoryClient(Client):
    def __init__(self, repository, strict=False, queue=False, error=False):
        super().__init__()
        self.repository, self.strict, self.queue, self.error = repository, strict, queue, error

    async def get(self, path, params=None):
        path = path.replace('/repos/' + self.repository, '/repos/mikkokotila/caller')
        if path.endswith('/rules/branches/main'):
            if self.error:
                raise RuntimeError('503 rules unavailable')
            return [dict(type='required_status_checks', parameters=dict(required_status_checks=[dict(context='test')],
                    strict_required_status_checks_policy=self.strict))] + ([dict(type='merge_queue')] if self.queue else [])
        value = await super().get(path, params)
        if '/compare/' in path:
            value.update(merge_base_commit={'sha': 'd' * 40}, ahead_by=1, behind_by=1, status='diverged')
        if '/pulls/50' in path:
            value.update(state='open', merged=False)
        return value

    async def graphql(self, query, variables):
        result = await super().graphql(query, variables)
        if query == github.SUMMARY_QUERY:
            pull = result['repository']['pullRequest']
            pull['baseRef']['branchProtectionRule']['requiresStrictStatusChecks'] = self.strict
        return result


@pytest.mark.parametrize('repository', REPOSITORIES)
@pytest.mark.parametrize('profile', ['full', 'validation'])
@pytest.mark.parametrize('strict,queue,error,expected', [
    (False, False, False, 'PASS'), (True, False, False, 'FAIL'),
    (True, True, False, 'PASS'), (False, False, True, 'UNKNOWN'),
])
def test_live_repository_policy_controls_behind_head(repository, profile, strict, queue, error, expected):
    client = RepositoryClient(repository, strict, queue, error)
    collect = github.collect_with_client if profile == 'full' else github.collect_validation_with_client
    data = asyncio.run(collect(client, repository, 50))
    assert branch_freshness_gate(data)['status'] == expected
    assert data['freshness_policy']['merge_queue'] is queue
    if profile == 'full':
        # The policy change cannot hide the existing real review obligation.
        assert next(g for g in evaluate_pr(data, []) if g['gate'] == 'REVIEW_HANDLED')['status'] == 'FAIL'


@pytest.mark.parametrize('repository', REPOSITORIES)
def test_missing_or_stale_policy_never_bypasses_freshness(repository):
    data = dict(behind(snapshot()), repository=repository)
    assert branch_freshness_gate(data)['status'] == 'UNKNOWN'
    data['freshness_policy'] = dict(repository=repository, head_sha='b' * 40, base_ref='main', required=False)
    assert branch_freshness_gate(data)['status'] == 'UNKNOWN'
    data['repository'] = 'unrelated/repository'
    assert branch_freshness_gate(data)['status'] == 'FAIL'


@pytest.mark.parametrize('repository', REPOSITORIES)
def test_ordinary_titles_do_not_publish_document_contracts(local, repository):
    store, root, _, _ = local
    git(root, 'remote', 'set-url', 'origin', f'https://github.com/{repository}.git')
    value = args(['issue', 'create', '--repo', repository, '--title', 'fix: retire slice requirements', '--body', 'Authorized engineering'])
    value.cwd = str(root)
    with patch('caller_pr_gates.cli.github_run', return_value=subprocess.CompletedProcess([], 0,
               stdout=f'https://github.com/{repository}/issues/42\n')):
        cli.gateway(value, store)
    assert store.followups('s') == []
    with patch('caller_pr_gates.github.collect_issue', side_effect=AssertionError('not explicitly requested')):
        hook._register_document(store, 's', repository, 42, True)
    value.kind = 'prd'
    with pytest.raises(ValueError, match='record the user-agreed scope'):
        cli.gateway(value, store)


@pytest.fixture
def configuration(local, tmp_path):
    store, root, _, _ = local
    repo = 'Vaquum/Origo'
    git(root, 'remote', 'set-url', 'origin', f'https://github.com/{repo}.git')
    store.register('s', 'codex', repo, 614, str(root))
    store.record_scope('s', dict(agreed_scope='Remove Origo slice/version checks and strict freshness',
        acceptance_criteria=['Preserve other protection'], slice_count=0, slice_ids=[],
        approval_evidence='Do it all now.', approved_at=1))
    protection = dict(strict=True, checks=[dict(context=name, app_id=15368)
        for name in ['CodeQL', 'pr_checks_slice', 'pr_checks_version', 'pr_checks_tests']])
    rules = dict(name='Protect-Main', target='branch', enforcement='active',
        conditions={'ref_name': {'include': ['refs/heads/main'], 'exclude': []}}, bypass_actors=[],
        rules=[dict(type='pull_request', parameters=dict(required_approving_review_count=1)),
               dict(type='required_status_checks', parameters=dict(strict_required_status_checks_policy=True,
                    required_status_checks=[dict(context=c['context'], integration_id=c['app_id']) for c in protection['checks']]))],
        id=5406599, source=repo, source_type='Repository')
    pr = dict(number=614, state='open', merged=False, draft=False,
        base=dict(ref='main', sha='b' * 40, repo=dict(full_name=repo)),
        head=dict(ref='cleanup', sha='a' * 40, repo=dict(full_name=repo)))
    values = {f'repos/{repo}/pulls/614': pr,
        f'repos/{repo}/rulesets/5406599': rules,
        f'repos/{repo}/branches/main/protection/required_status_checks': protection}
    writes = []
    def remote(argv, **kwargs):
        target = argv[2]
        if '--method' in argv:
            body = json.loads(kwargs['input']); writes.append((argv, body))
            values[target].update(body)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(values[target]))
    return store, root, values, writes, remote, tmp_path / 'request.json'


@pytest.mark.parametrize('ruleset', [False, True])
def test_governance_write_freezes_only_approved_delta_and_reads_it_back(configuration, ruleset):
    store, root, values, writes, remote, path = configuration
    endpoint = next(key for key in values if ('/rulesets/' in key if ruleset else '/protection/' in key))
    before = deepcopy(values[endpoint])
    payload = governance.desired(before, 'Vaquum/Origo', ruleset)
    path.write_text(json.dumps(payload))
    value = args(['api', endpoint, '--method', 'PUT' if ruleset else 'PATCH', '--input', str(path)])
    value.target, value.cwd = 'https://github.com/Vaquum/Origo/pull/614', str(root)
    with patch('caller_pr_gates.cli.github_run', remote):
        receipt = cli.gateway(value, store)
    assert receipt['payload_sha256'] and len(writes) == 1
    assert writes[0][0][-1] == '-' and writes[0][1] == payload
    assert 'pr_checks_slice' not in json.dumps(values[endpoint]) and 'pr_checks_version' not in json.dumps(values[endpoint])
    assert 'pr_checks_prd' in json.dumps(values[endpoint]) and 'CodeQL' in json.dumps(values[endpoint])
    if ruleset:
        assert values[endpoint]['rules'][0] == before['rules'][0]
        assert values[endpoint]['bypass_actors'] == before['bypass_actors']


@pytest.mark.parametrize('mutation', ['security', 'approval', 'bypass', 'owner', 'race', 'readback'])
def test_governance_route_rejects_unapproved_or_racing_settings(configuration, mutation):
    store, root, values, writes, remote, path = configuration
    endpoint = 'repos/Vaquum/Origo/rulesets/5406599'
    payload = governance.desired(values[endpoint], 'Vaquum/Origo', True)
    if mutation == 'security':
        payload['rules'][1]['parameters']['required_status_checks'].pop(0)
    elif mutation == 'approval':
        payload['rules'][0]['parameters']['required_approving_review_count'] = 0
    elif mutation == 'bypass':
        payload['bypass_actors'] = [{'actor_id': 1, 'actor_type': 'Integration', 'bypass_mode': 'always'}]
    elif mutation == 'owner':
        store.record_session('foreign', 'codex', str(root))
    path.write_text(json.dumps(payload))
    value = args(['api', endpoint, '--method', 'PUT', '--input', str(path)])
    value.target, value.cwd = 'https://github.com/Vaquum/Origo/pull/614', str(root)
    if mutation == 'owner': value.session = 'foreign'
    count = 0
    def interleaved(argv, **kwargs):
        nonlocal count
        if argv[2] == endpoint and '--method' not in argv:
            count += 1
            if mutation == 'race' and count == 2:
                values[endpoint]['name'] = 'Changed concurrently'
            if mutation == 'readback' and count == 3:
                values[endpoint]['bypass_actors'] = [{'actor_id': 1}]
        return remote(argv, **kwargs)
    with patch('caller_pr_gates.cli.github_run', interleaved), pytest.raises((ValueError, RuntimeError)):
        cli.gateway(value, store)
    assert len(writes) == (1 if mutation == 'readback' else 0)


@pytest.mark.parametrize('mode', ['create', 'exists', 'unavailable', 'other_name'])
def test_only_missing_required_origo_prd_label_can_be_created(configuration, mode):
    store, root, values, writes, remote, path = configuration
    endpoint = 'repos/Vaquum/Origo/labels'
    body = dict(name='prd', color='5319e7', description='Product requirements document')
    path.write_text(json.dumps(dict(body, name='other') if mode == 'other_name' else body))
    value = args(['api', endpoint, '--method', 'POST', '--input', str(path)])
    value.target, value.cwd = 'https://github.com/Vaquum/Origo/pull/614', str(root)
    def labels(argv, **kwargs):
        if argv[2] == endpoint:
            values[endpoint + '/prd'] = json.loads(kwargs['input'])
            writes.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body))
        if argv[2] == endpoint + '/prd' and not writes:
            return subprocess.CompletedProcess(argv, 0 if mode == 'exists' else 1,
                stdout=json.dumps(body), stderr='GitHub 503 unavailable' if mode == 'unavailable' else 'gh: Not Found (HTTP 404)')
        return remote(argv, **kwargs)
    with patch('caller_pr_gates.cli.github_run', labels):
        if mode == 'create':
            assert cli.gateway(value, store)['endpoint'] == endpoint
        else:
            with pytest.raises(ValueError): cli.gateway(value, store)
    assert len(writes) == (1 if mode == 'create' else 0)
