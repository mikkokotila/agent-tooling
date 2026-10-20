"""Public quota-review reproduction; all state and API responses are isolated fixtures."""
import asyncio
import copy
import time

import pytest

from caller_pr_gates import github, progress
from caller_pr_gates.copilot import is_service_review
from caller_pr_gates.handoff import evidence_fingerprint
from caller_pr_gates.policy import evaluate_pr, evaluate_review
from caller_pr_gates.workflow import evidence_digest
from test_collection_profiles import BudgetClient
from test_policy import COMMENTED, FIXED, REPLIED, SHA, comment, receipt, snapshot


# Body and bot node verified from public Limen review 5578995082.
QUOTA = 'Copilot was unable to review this pull request because the user who requested the review has reached their quota limit.'
BILLING = ('\n\nAn organization owner can choose the repository organization as the billing source for reviews requested by members with a paid Copilot license. '
           '[Learn about Copilot code review billing](https://docs.github.com/en/copilot/concepts/agents/code-review#code-review-usage). '
           '[Review the Copilot code review billing settings](https://github.com/organizations/Vaquum/settings/copilot/policies).')
BOT = {'login': 'copilot-pull-request-reviewer', '__typename': 'Bot', 'id': 'BOT_kgDOCnlnWA'}


def raw_review(body=QUOTA, state='COMMENTED'):
    return dict(id='PRR_fixture', fullDatabaseId=20, body=body, state=state,
                author=dict(BOT), createdAt=COMMENTED, updatedAt=COMMENTED,
                url='https://github.com/owner/repo/pull/1#pullrequestreview-20')


def notice(**changes):
    return dict(github._comment(raw_review(), 'review'), **changes)


@pytest.mark.parametrize('body', [QUOTA, QUOTA + BILLING, ' \n' + QUOTA + BILLING + '\n'])
@pytest.mark.parametrize('login', [BOT['login'], BOT['login'] + '[bot]'])
def test_exact_authenticated_service_notice_needs_no_disposition_or_reply(body, login):
    data = snapshot(); data['comments'] = [notice(body=body, author_login=login)]
    before = copy.deepcopy(data)
    gates = evaluate_review(data, [])
    assert all(g['status'] == 'PASS' for g in gates)
    assert gates[0]['evidence']['obligations'] == []
    assert gates[0]['evidence']['nonblocking_service_reviews'] == data['comments']
    assert data == before


@pytest.mark.parametrize('changes', [
    {'author_login': 'another-reviewer'}, {'author_login': 'copilot-pull-request-reviewer-helper'},
    {'author_type': 'User'}, {'author_type': None}, {'author_node_id': 'BOT_other'},
    {'author_node_id': None}, {'kind': 'inline'}, {'kind': 'issue'},
    {'review_state': None}, {'review_state': 'CHANGES_REQUESTED'},
    {'review_state': 'APPROVED'}, {'review_state': 'DISMISSED'},
])
def test_name_or_body_cannot_replace_provenance_and_non_change_request_state(changes):
    data = snapshot(); data['comments'] = [notice(**changes)]
    assert not is_service_review(data['comments'][0])
    assert all(g['status'] == 'FAIL' for g in evaluate_review(data, []))
    assert evidence_digest(data, []) != evidence_digest(snapshot(), [])


@pytest.mark.parametrize('missing', ['id', 'url', 'updated_at', 'author_login'])
def test_incomplete_notice_identity_is_unknown_not_exempt(missing):
    data = snapshot(); data['comments'] = [notice(**{missing: None})]
    assert not is_service_review(data['comments'][0])
    assert all(g['status'] == 'UNKNOWN' for g in evaluate_review(data, []))


@pytest.mark.parametrize('body', [
    'Guard the boundary case', QUOTA + '\n\nFix the unchecked index.',
    'Fix the unchecked index.\n\n' + QUOTA, QUOTA + BILLING + '\n\nP1: validate the input.',
    QUOTA + BILLING.replace('https://docs.github.com/', 'https://example.com/'),
    'Copilot was unable to review this pull request.', '```\n' + QUOTA + '\n```',
])
def test_actual_mixed_or_unrecognized_body_remains_review_feedback(body):
    data = snapshot(); data['comments'] = [notice(body=body)]
    gates = evaluate_review(data, [])
    assert all(g['status'] == 'FAIL' for g in gates)
    assert gates[0]['evidence']['nonblocking_service_reviews'] == []
    assert evidence_digest(data, []) != evidence_digest(snapshot(), [])


@pytest.mark.parametrize('outdated', [False, True])
def test_notice_does_not_exempt_other_copilot_findings(outdated):
    data = snapshot()
    data['comments'] = [notice(), comment(author_login=BOT['login'], is_outdated=outdated)]
    gates = evaluate_review(data, [])
    assert all(g['status'] == 'FAIL' for g in gates)
    assert [o['id'] for o in gates[0]['evidence']['obligations']] == ['inline:20']


def acknowledged_notice():
    reason = 'This is a service quota notice; no source change is requested.'
    reply = comment(id='issue:30', kind='issue', thread_id=None, author_login='agent',
                    body=reason, created_at=REPLIED, updated_at=REPLIED,
                    url='https://github.com/owner/repo/pull/1#issuecomment-30')
    parent = notice(replies=[copy.deepcopy(reply)])
    record = receipt(comment_id=parent['id'], disposition='nonfix', reason=reason,
                     reply_url=reply['url'])
    data = snapshot(); data['comments'] = [parent, reply]
    return data, record


def test_existing_verified_quota_acknowledgement_does_not_become_orphan_feedback():
    data, record = acknowledged_notice()
    before = copy.deepcopy((data, record))
    gates = evaluate_review(data, [record])
    assert all(g['status'] == 'PASS' for g in gates)
    assert gates[0]['evidence']['obligations'] == []
    assert gates[0]['evidence']['verified_conclusion_replies'] == [record['reply_url']]
    assert evidence_digest(data, [record]) == evidence_digest(snapshot(), [])
    assert (data, record) == before


@pytest.mark.parametrize('invalid', ['receipt', 'parent_revision', 'actor', 'body', 'date', 'reply_revision', 'edge'])
def test_retired_notice_does_not_authenticate_an_unverified_reply(invalid):
    data, record = acknowledged_notice(); records = [record]
    parent, reply = data['comments']
    if invalid == 'receipt': records = []
    elif invalid == 'parent_revision': parent['updated_at'] = FIXED
    elif invalid == 'actor': reply['author_login'] = 'another-author'
    elif invalid == 'body': reply['body'] = 'This is a separate finding.'
    elif invalid == 'date': reply['created_at'] = '2026-10-04T10:00:00Z'
    elif invalid == 'reply_revision': reply['updated_at'] = None
    elif invalid == 'edge': parent['replies'] = []
    gates = evaluate_review(data, records)
    assert all(g['status'] != 'PASS' for g in gates)
    assert gates[0]['evidence']['verified_conclusion_replies'] == []


def test_recorded_quota_disposition_without_reply_does_not_block_necessary_validation():
    data = snapshot(); data.update(comments=[notice()], collected_at=time.time())
    record = receipt(comment_id='review:20', disposition='nonfix', reason='Service-only quota notice')
    row = {'input_digest': 'inputs', 'plan': {'checks': [{'id': 'necessary'}]}}
    gates = evaluate_pr(data, [record])
    result = progress.evaluate_progress(row, [], 'inputs', data, gates, {'head': SHA, 'clean': True})
    assert result['phase'] == 'VALIDATE_REQUIRED'
    assert all(g['status'] == 'PASS' for g in gates)


def test_notice_changes_do_not_replay_real_failure_or_add_validation_generations():
    data = snapshot(); data['checks'][0]['conclusion'] = 'failure'
    original = evidence_fingerprint(data, evaluate_pr(data, []))
    validation = evidence_digest(data, [])
    data['comments'] = [notice(), notice(id='review:21', body=QUOTA + BILLING, updated_at=REPLIED)]
    assert evidence_fingerprint(data, evaluate_pr(data, [])) == original
    assert evidence_digest(data, []) == validation
    assert next(g for g in evaluate_pr(data, []) if g['gate'] == 'CI_FAILED')['status'] == 'FAIL'
    data['comments'][0]['body'] += '\n\nFix the real boundary defect.'
    assert evidence_fingerprint(data, evaluate_pr(data, [])) != original
    assert evidence_digest(data, []) != validation


def test_fresh_notice_proof_keeps_preexisting_validation_identities():
    data = snapshot(); data['comments'] = [notice(author_type=None, author_node_id=None)]
    previous = evidence_digest(data, [])
    data['comments'] = [notice(), notice(id='review:21', updated_at=REPLIED)]
    assert evidence_digest(data, [], previous) == previous


class NoticeClient(BudgetClient):
    def __init__(self, tmp_path, body=QUOTA + BILLING, state='COMMENTED'):
        super().__init__(tmp_path)
        self.review = raw_review(body, state)

    async def get(self, path, params=None):
        value = await super().get(path, params)
        return [] if path.endswith('/comments') else value

    async def graphql(self, query, variables):
        value = await super().graphql(query, variables)
        if query in {github.REVIEWS_QUERY, github.BATCH_QUERY}:
            assert 'author { login __typename ... on Bot { id } }' in query
            value['repository']['pullRequest']['reviews']['nodes'] = [copy.deepcopy(self.review)]
        return value


@pytest.mark.parametrize('profile,rest_reads,graphql_reads', [('full', 13, 5), ('batch', 12, 1), ('review', 4, 3)])
@pytest.mark.parametrize('body,state,expected', [(QUOTA + BILLING, 'COMMENTED', 'PASS'),
                                               (QUOTA, 'CHANGES_REQUESTED', 'FAIL'),
                                               ('', 'CHANGES_REQUESTED', 'FAIL')])
def test_existing_collection_profiles_preserve_provenance_state_and_request_budget(tmp_path, profile, rest_reads, graphql_reads, body, state, expected):
    client = NoticeClient(tmp_path, body, state)
    if profile == 'review':
        data = asyncio.run(github.collect_review_with_client(client, 'mikkokotila/caller', 50))
    else:
        data = asyncio.run(github.collect_with_client(github._BatchClient(client) if profile == 'batch' else client,
                                                     'mikkokotila/caller', 50))
    assert len(client.paths) == rest_reads and len(client.graphs) == graphql_reads
    assert len(data['comments']) == 1
    current = data['comments'][0]
    assert current['author_type'] == 'Bot' and current['author_node_id'] == BOT['id']
    assert current['review_state'] == state
    assert current['body'] == (body or 'Review requests changes.')
    assert all(g['status'] == expected for g in evaluate_review(data, []))


@pytest.mark.parametrize('profile', ['full', 'review'])
def test_service_notice_cannot_waive_collection_coherence(tmp_path, profile):
    client = NoticeClient(tmp_path); client.lifecycle_race = True
    collect = github.collect_with_client if profile == 'full' else github.collect_review_with_client
    with pytest.raises(github.EvidenceError, match='changed|Changed'):
        asyncio.run(collect(client, 'mikkokotila/caller', 50))
