"""Large PR evidence must retain every commit without trusting capped PR lists."""
import copy

import pytest

from caller_pr_gates import github
from test_github import Client, SHA, OTHER, STAMP


BASE = 'c' * 40


class LargeClient(Client):
    def __init__(self, count=257, corrupt=None, race=None):
        super().__init__(head_race=race == 'head')
        self.count, self.corrupt, self.race = count, corrupt, race
        self.pages, self.graphs = [], []
        self.commits = [{'sha': f'{i:040x}', 'commit': {'committer': {'date': STAMP}}}
                        for i in range(1, count)]
        self.commits.append({'sha': SHA, 'commit': {'committer': {'date': STAMP}}})

    async def get(self, path, params=None):
        if '/compare/' in path and params.get('per_page') == 100:
            assert path.endswith(f'/{BASE}...{SHA}')
            page = params['page']
            self.pages.append(page)
            data = dict(base_commit={'sha': BASE}, merge_base_commit={'sha': BASE},
                        total_commits=self.count, ahead_by=self.count, behind_by=0, status='ahead',
                        commits=copy.deepcopy(self.commits[(page - 1) * 100:page * 100]))
            if self.corrupt:
                self.corrupt(data, page)
            return data
        data = await super().get(path, params)
        if path.endswith('/pulls/50'):
            data['commits'] = self.count + (self.race == 'count' and self.reads > 1)
            data['base']['sha'] = OTHER if self.race == 'base' and self.reads > 1 else BASE
        return data

    async def graphql(self, query, variables):
        self.graphs.append(query)
        assert query != github.COMMITS_QUERY, 'The capped PR connection cannot prove large chronology'
        return await super().graphql(query, variables)


@pytest.mark.parametrize('count', [251, 257, 300])
@pytest.mark.parametrize('collector', [github.collect_with_client, github.collect_review_with_client])
@pytest.mark.anyio
async def test_complete_large_chronology_reaches_actual_head_with_bounded_pages(count, collector):
    client = LargeClient(count)
    result = await collector(client, 'mikkokotila/caller', 50)
    assert result['commit_shas'] == [c['sha'] for c in client.commits]
    assert result['commit_dates'] == {c['sha']: STAMP for c in client.commits}
    assert client.pages == [1, 2, 3]
    assert result['head_sha'] == result['head_sha_after'] == SHA


def corrupt_comparison(kind):
    def change(data, page):
        if kind == 'truncated' and page == 3:
            data['commits'].pop()
        elif kind == 'duplicate' and page == 2:
            data['commits'][0]['sha'] = f'{1:040x}'
        elif kind == 'missing-head' and page == 3:
            data['commits'][-1]['sha'] = OTHER
        elif kind == 'count':
            data['total_commits'] = 250
        elif kind == 'wrong-base':
            data['base_commit']['sha'] = OTHER
        elif kind == 'ancestor-race' and page == 2:
            data['merge_base_commit']['sha'] = OTHER
        elif kind == 'malformed-date':
            del data['commits'][0]['commit']['committer']['date']
    return change


@pytest.mark.parametrize('kind', ['truncated', 'duplicate', 'missing-head', 'count',
                                 'wrong-base', 'ancestor-race', 'malformed-date'])
@pytest.mark.anyio
async def test_large_comparison_never_credits_incomplete_or_changed_evidence(kind):
    with pytest.raises(github.EvidenceError):
        await github.collect_with_client(LargeClient(corrupt=corrupt_comparison(kind)),
                                         'mikkokotila/caller', 50)


@pytest.mark.parametrize('race', ['head', 'base', 'count'])
@pytest.mark.parametrize('collector', [github.collect_with_client, github.collect_review_with_client])
@pytest.mark.anyio
async def test_large_chronology_rejects_final_pr_source_or_count_change(race, collector):
    with pytest.raises(github.EvidenceError):
        await collector(LargeClient(race=race), 'mikkokotila/caller', 50)


@pytest.fixture
def anyio_backend():
    return 'asyncio'
