"""Current-head readiness and migration of the separately appended local rules."""
import asyncio
from pathlib import Path

import pytest

from caller_pr_gates import github, monitor, policy, prompts
from test_policy import SHA, OTHER, snapshot, check
from test_collection_profiles import BudgetClient
from test_installation import manage, installed, original_bytes


def readiness(data):
    return next(g for g in policy.evaluate_pr(data, []) if g['gate'] == 'MERGE_READY')


@pytest.mark.parametrize('state,decision,status', [
    ('CLEAN', None, 'PASS'), ('HAS_HOOKS', 'APPROVED', 'PASS'),
    ('BLOCKED', None, 'WAIT'), ('BLOCKED', 'CHANGES_REQUESTED', 'WAIT'),
    ('CLEAN', 'CHANGES_REQUESTED', 'WAIT'), ('CLEAN', 'REVIEW_REQUIRED', 'WAIT'),
    ('BEHIND', None, 'FAIL'), ('DIRTY', None, 'FAIL'), ('DRAFT', None, 'FAIL'),
    ('UNKNOWN', None, 'UNKNOWN'), ('UNSTABLE', None, 'WAIT'),
    (None, None, 'UNKNOWN'), ('FUTURE_STATE', None, 'UNKNOWN'),
    ('CLEAN', 'FUTURE_DECISION', 'UNKNOWN'),
])
def test_readiness_does_not_confuse_dispositions_with_github_decision(state, decision, status):
    data = snapshot(); data.update(merge_state_status=state, review_decision=decision)
    assert readiness(data)['status'] == status
    if state == 'BLOCKED' and decision == 'CHANGES_REQUESTED':
        gates = policy.evaluate_pr(data, [])
        assert all(g['status'] == 'PASS' for g in gates if g['gate'] != 'MERGE_READY')
        assert monitor.actionable(data, gates, checklist=True) == []


@pytest.mark.parametrize('change', [{'merge_readiness_head_sha': OTHER}, {'mergeable': None}])
def test_missing_or_wrong_head_readiness_never_passes(change):
    data = snapshot(); data.update(change)
    assert readiness(data)['status'] == 'UNKNOWN'


def test_missing_nullable_review_decision_is_not_no_required_review():
    data = snapshot(); del data['review_decision']
    assert readiness(data)['status'] == 'UNKNOWN'


@pytest.mark.parametrize('state', ['BEHIND', 'DIRTY', 'DRAFT'])
def test_actual_branch_work_remains_actionable(state):
    data = snapshot(); data['merge_state_status'] = state
    assert len(monitor.actionable(data, policy.evaluate_pr(data, []), checklist=True)) == 1


def test_copilot_quota_has_no_merge_authority_but_real_ci_still_does():
    data = snapshot(); data['merge_state_status'] = 'UNSTABLE'
    data['checks'].append(check(id='service', kind='workflow', name='Copilot', conclusion='failure',
        workflow_event='dynamic', workflow_path='dynamic/agents/copilot-pull-request-reviewer', workflow_id='1'))
    assert readiness(data)['status'] == 'PASS'
    data['checks'].append(check(id='actual', name='build', conclusion='failure'))
    assert readiness(data)['status'] != 'PASS'
    assert monitor.actionable(data, policy.evaluate_pr(data, []), checklist=True)


def test_readiness_reuses_existing_graphql_batch_without_extra_calls(tmp_path):
    class Client(BudgetClient):
        async def graphql(self, query, variables):
            result = await super().graphql(query, variables)
            if query == github.BATCH_QUERY:
                result['repository']['pullRequest'].update(mergeStateStatus='BLOCKED', reviewDecision='CHANGES_REQUESTED')
            return result
    client = Client(tmp_path)
    data = asyncio.run(github.collect_with_client(github._BatchClient(client), 'mikkokotila/caller', 50))
    assert len(client.graphs) == 1 and len(client.paths) == 12
    assert data['merge_state_status'] == 'BLOCKED' and data['review_decision'] == 'CHANGES_REQUESTED'
    assert data['merge_readiness_head_sha'] == SHA
    assert 'mergeStateStatus reviewDecision' in github.SUMMARY_QUERY
    assert 'mergeStateStatus reviewDecision' in github.BATCH_QUERY


def test_known_unmanaged_rules_move_into_catalogue_with_rollback(installed):
    home, service = installed
    rules = (manage.ROOT/'tests/fixtures/unmanaged-completion-rules.md').read_bytes()
    for client, name in [('codex','AGENTS.md'), ('claude','CLAUDE.md')]:
        target = home/('.'+client)/name
        target.write_bytes(target.read_bytes()+b'\n\n'+rules)
    before = original_bytes(home)
    manage.deploy(home, service)
    for client, name in [('codex','AGENTS.md'), ('claude','CLAUDE.md')]:
        text = (home/('.'+client)/name).read_text()
        assert '## Done means the merge button is green' not in text
        assert '20 minutes' in text and 'MERGE_READY' in text
        assert 'Personal prefix.' in text and 'Personal suffix.' in text
    manage.rollback(home, service)
    assert original_bytes(home) == before


def test_edited_unmanaged_rules_fail_without_deleting_human_text(installed):
    home, service = installed
    target = home/'.codex/AGENTS.md'
    target.write_bytes(target.read_bytes()+b'\n'+(manage.ROOT/'tests/fixtures/unmanaged-completion-rules.md').read_bytes()+b'Human addition.\n')
    before = original_bytes(home)
    with pytest.raises(RuntimeError, match='Unmanaged completion rules changed'):
        manage.deploy(home, service)
    assert original_bytes(home) == before


def test_single_prompt_requires_repair_and_bounded_wait():
    text = prompts.baseline_text('pr_checklist')
    assert '20 minutes' in text and 'after CI concludes' in text
    assert 'MERGE_READY' in text and 'resume the PR in the same turn' in text
    assert 'an unchanged failure still needs repair' in text
    assert 'An unchanged-input retry refusal requires diagnosis and a meaningful fix' in text
    assert 'Receiving this checklist never discharges that work' in text
    assert 'clickable link, exact steps' in text
