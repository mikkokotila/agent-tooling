import asyncio
import copy
import subprocess
from unittest.mock import patch

import pytest

from caller_pr_gates.hierarchy import link
from caller_pr_gates.store import Store

PARENT = 'https://github.com/owner/repo/issues/10'
CHILD = 'https://github.com/owner/repo/issues/11'


@pytest.fixture
def owned(tmp_path):
    store = Store(tmp_path)
    store.record_session('owner', 'codex', str(tmp_path))
    store.record_session('other', 'codex', str(tmp_path))
    scope = dict(agreed_scope='Deliver the issue', acceptance_criteria=['Native hierarchy'],
                 slice_count=1, slice_ids=['S1'], approved_at=1)
    store.record_scope('owner', scope)
    store.register_followup('owner', 'owner/repo', 11, 1, scope, kind='slice', issue_number=11)
    return store


class Client:
    def __init__(self, identity=5914810765):
        self.calls = 0
        self.nodes = dict(parent=dict(number=10, url=PARENT),
                          child=dict(fullDatabaseId=identity, number=11, url=CHILD, parent=None))

    async def graphql(self, query, variables):
        self.calls += 1
        return dict(repository=copy.deepcopy(self.nodes))


@pytest.mark.parametrize('identity', [5914810765, '5914810765'])
def test_link_verifies_the_exact_edge_and_retry_does_not_write(owned, identity):
    client = Client(identity)
    def write(argv, **kwargs):
        assert argv == ['gh', 'api', 'repos/owner/repo/issues/10/sub_issues', '--method', 'POST',
                        '-F', 'sub_issue_id=5914810765', '-F', 'replace_parent=false']
        client.nodes['child']['parent'] = dict(number=10, url=PARENT)
        return subprocess.CompletedProcess(argv, 0)
    with patch('caller_pr_gates.hierarchy.github_run', side_effect=write) as run:
        assert asyncio.run(link(owned, 'owner', PARENT, CHILD, client=client))['status'] == 'linked'
        assert client.calls == 2
        assert asyncio.run(link(owned, 'owner', PARENT, CHILD, client=client))['status'] == 'already_linked'
        assert run.call_count == 1 and client.calls == 3
    assert owned.prs('owner') == []  # Hierarchy writes never adopt a PR.


@pytest.mark.parametrize('sid,parent,child', [('other', PARENT, CHILD),
    ('owner', PARENT, 'https://github.com/elsewhere/repo/issues/11'),
    ('owner', PARENT, PARENT), ('owner', PARENT, 'https://github.com/owner/repo/pull/11')])
def test_foreign_or_invalid_edges_never_read_or_write(owned, sid, parent, child):
    client = Client()
    with patch('caller_pr_gates.hierarchy.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError):
            asyncio.run(link(owned, sid, parent, child, client=client))
    assert client.calls == 0


@pytest.mark.parametrize('field,value', [('parent', dict(number=12, url=PARENT.replace('/10', '/12'))),
    ('url', 'https://github.com/elsewhere/repo/issues/11'), ('fullDatabaseId', True)])
def test_existing_parent_and_unverified_identities_are_retained(owned, field, value):
    client = Client(); client.nodes['child'][field] = value
    with patch('caller_pr_gates.hierarchy.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError):
            asyncio.run(link(owned, 'owner', PARENT, CHILD, client=client))


@pytest.mark.parametrize('exit_code', [0, 1])
def test_a_write_without_verified_readback_never_claims_success(owned, exit_code):
    client = Client()
    with patch('caller_pr_gates.hierarchy.github_run', return_value=subprocess.CompletedProcess([], exit_code, stderr='write refused')):
        with pytest.raises((ValueError, RuntimeError)):
            asyncio.run(link(owned, 'owner', PARENT, CHILD, client=client))
    with owned._connect() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='issue_hierarchy_linked'").fetchone()[0] == 0
