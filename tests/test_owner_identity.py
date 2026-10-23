import json
import subprocess
from unittest.mock import patch
import pytest
from caller_pr_gates import ghrelay
from caller_pr_gates.cli import gateway
from caller_pr_gates.quota import Coordinator, CoolingDown, account_key
from test_cli import args, store


_real_query_login = ghrelay.query_login


def write_argv(kind):
    if kind == 'comment':
        return args(['pr', 'comment', 'https://github.com/o/r/pull/77', '--body', 'Reply'])
    if kind == 'pr-create':
        return args(['pr', 'create', '--title', 'Fix', '--body', 'Fix. Closes #76'])
    value = args(['api', 'repos/o/r/pulls/77/comments', '--method', 'POST',
                  '-F', 'in_reply_to=123', '-f', 'body=Fixed'])
    value.target = 'https://github.com/o/r/pull/77'
    return value


@pytest.mark.parametrize('kind', ['comment', 'pr-create', 'api-reply'])
def test_mutating_call_refuses_non_owner_login(store, monkeypatch, kind):
    monkeypatch.setattr(ghrelay, 'query_login', lambda token: 'bit-mis')
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match="owner identity 'mikkokotila'"):
            gateway(write_argv(kind), store)
    assert store.prs('s') == []


def test_mutating_call_refuses_any_login_other_than_owner(store, monkeypatch):
    monkeypatch.setattr(ghrelay, 'query_login', lambda token: 'someone-else')
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='someone-else'):
            gateway(write_argv('comment'), store)


def test_mutating_call_passes_for_owner_login(store):
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(write_argv('comment'), store)
    assert run.call_count == 1
    assert [(p['repository'], p['number']) for p in store.prs('s')] == [('o/r', 77)]


def test_owner_lookup_is_cached_across_mutating_calls(store, monkeypatch):
    calls = []

    def lookup(token):
        calls.append(token)
        return 'mikkokotila'

    monkeypatch.setattr(ghrelay, 'query_login', lookup)
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
        gateway(args(['pr', 'comment', 'https://github.com/o/r/pull/77', '--body', 'One']), store)
        gateway(args(['pr', 'comment', 'https://github.com/o/r/pull/78', '--body', 'Two']), store)
    assert calls == ['isolated-test-credential']


def test_cached_login_is_keyed_by_token(monkeypatch):
    logins = iter(['mikkokotila', 'bit-mis'])
    calls = []

    def lookup(token):
        calls.append(token)
        return next(logins)

    monkeypatch.setattr(ghrelay, 'query_login', lookup)
    assert ghrelay.token_login('token-a') == 'mikkokotila'
    assert ghrelay.token_login('token-b') == 'bit-mis'
    assert ghrelay.token_login('token-a') == 'mikkokotila'
    assert calls == ['token-a', 'token-b']


def test_lookup_queries_the_exact_admitted_token(monkeypatch):
    seen = []
    monkeypatch.setattr(ghrelay, 'query_login', lambda token: seen.append(token) or 'mikkokotila')
    ghrelay.require_owner('token-exact')
    assert seen == ['token-exact']


def test_corrupt_identity_cache_falls_back_to_lookup(monkeypatch, tmp_path):
    cache = tmp_path / 'hook-state' / 'github-identity.json'
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('not json')
    assert ghrelay.token_login('token-a') == 'mikkokotila'
    assert len(json.loads(cache.read_text())) == 1


@pytest.mark.parametrize('argv', [
    ['pr', 'view', 'https://github.com/o/r/pull/77'],
    ['api', 'repos/o/r/pulls/77'],
])
def test_reads_need_no_owner_lookup(store, monkeypatch, argv):
    def lookup(token):
        raise AssertionError('reads must not look up the owner identity')

    monkeypatch.setattr(ghrelay, 'query_login', lookup)
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
        gateway(args(argv), store)
    assert store.prs('s') == []


@pytest.mark.parametrize('method', ['POST', 'PATCH', 'PUT', 'DELETE'])
def test_relay_authorization_refuses_mutating_methods_for_non_owner(monkeypatch, method):
    monkeypatch.setattr(ghrelay, 'query_login', lambda token: 'bit-mis')
    with pytest.raises(ValueError, match="owner identity 'mikkokotila'"):
        ghrelay.authorize_request('token-foreign', method)


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_relay_authorization_skips_reads_without_lookup(monkeypatch, method):
    def lookup(token):
        raise AssertionError('reads must not look up the owner identity')

    monkeypatch.setattr(ghrelay, 'query_login', lookup)
    assert ghrelay.authorize_request('token-foreign', method) is None


def test_relay_authorization_passes_mutations_for_owner(monkeypatch):
    assert ghrelay.authorize_request('token-owner', 'POST') is None


def graphql_body(query):
    return json.dumps({'query': query}).encode()


@pytest.mark.parametrize('query', [
    'query($o:String!){repository(owner:$o){id}}',
    '{ viewer { login } }',
    '# leading comment\nquery { viewer { login } }',
    'query { search(query:"mutation:unrelated") { nodes { __typename } } }',
    'query { field(arg:"a # not a comment") }',
    'query { mutation: viewer { login } }',
    'query { mutation : viewer { login } }',
    'query { subscription: x }',
    'query { f(arg:"""mutation""") }',
    'query mutation { viewer { login } }',
    'query subscription { x }',
    'query($mutation:Boolean!){viewer @include(if:$mutation){login}}',
    'query($subscription:Int){x(y:$subscription)}',
    'query { viewer { ...mutation } } fragment mutation on User { login }',
    'query { ...subscription } fragment subscription on T { x }',
    'query Q { f(arg:"""a\\\\""" # still string\nzzz""") }',
])
def test_graphql_reads_skip_owner_lookup(monkeypatch, query):
    def lookup(token):
        raise AssertionError('GraphQL reads must not look up the owner identity')

    monkeypatch.setattr(ghrelay, 'query_login', lookup)
    assert ghrelay.authorize_request('token-foreign', 'POST', '/graphql', graphql_body(query)) is None


@pytest.mark.parametrize('path,body', [
    ('/graphql', graphql_body('mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id}}}')),
    ('/graphql', graphql_body('query A { x } mutation B { y }')),
    ('/graphql', graphql_body('subscription { x }')),
    ('/graphql', graphql_body('# reviewer said "lgtm"\nmutation { updateIssue { id } }')),
    ('/graphql', graphql_body('mutation#x\nM { updateIssue { id } }')),
    ('/graphql', graphql_body('#x\rmutation { updateIssue { id } }')),
    ('/graphql', graphql_body('#x\r\nmutation { y }')),
    ('/graphql', graphql_body('""" " # """ mutation { updateIssue { id } }')),
    ('/graphql', graphql_body('mutation mutation { y }')),
    ('/graphql', graphql_body('query Q { f(arg:"""a\\\\""" ) } # trap tail""" more mutation M { y }')),
    ('/graphql', graphql_body('query { f(arg:"""abc) }')),
    ('/graphql', graphql_body('query { x(arg:"unterminated) }')),
    ('/graphql', graphql_body('')),
    ('/graphql', json.dumps({}).encode()),
    ('/graphql', b'not json'),
    ('/graphql', b'x' * (1024 * 1024 + 1)),
    ('/repos/o/r/issues/1/comments', graphql_body('query { x }')),
])
def test_graphql_writes_and_unknown_documents_require_owner(monkeypatch, path, body):
    monkeypatch.setattr(ghrelay, 'query_login', lambda token: 'bit-mis')
    with pytest.raises(ValueError, match="owner identity 'mikkokotila'"):
        ghrelay.authorize_request('token-foreign', 'POST', path, body)


def test_lookup_yields_to_an_existing_shared_cooldown(monkeypatch):
    coordinator = Coordinator()
    account = account_key('https://api.github.com', 'token-cooling')
    coordinator.throttle(coordinator.admit(account, 'core'),
                         RuntimeError('GitHub 403: API rate limit exceeded for user ID 1'))

    def lookup(token):
        raise AssertionError('no request may be sent during a shared cooldown')

    monkeypatch.setattr(ghrelay, 'query_login', lookup)
    with pytest.raises(CoolingDown):
        ghrelay.token_login('token-cooling')


def test_rate_limited_lookup_records_shared_backoff(monkeypatch):
    calls = []

    def lookup(token):
        calls.append(token)
        raise RuntimeError('HTTP 403: API rate limit exceeded for user ID 1')

    monkeypatch.setattr(ghrelay, 'query_login', lookup)
    with pytest.raises(RuntimeError, match='refusing the GitHub write'):
        ghrelay.token_login('token-limited')
    with pytest.raises(CoolingDown):
        ghrelay.token_login('token-limited')
    assert calls == ['token-limited']


def test_lookup_timeout_reaps_the_child(monkeypatch):
    import subprocess as real_subprocess
    calls = []

    class Child:
        returncode = None

        def communicate(self, timeout=None):
            raise real_subprocess.TimeoutExpired('gh', timeout)

        def kill(self):
            calls.append('kill')

        def wait(self):
            calls.append('wait')

    monkeypatch.setattr(ghrelay, 'query_login', _real_query_login)
    monkeypatch.setattr(ghrelay.subprocess, 'Popen', lambda *args, **kwargs: Child())
    with pytest.raises(RuntimeError, match='refusing the GitHub write'):
        ghrelay.token_login('token-slow')
    assert calls == ['kill', 'wait']


def test_unrelated_lookup_failure_records_no_throttle(monkeypatch):
    calls = []

    def lookup(token):
        calls.append(token)
        raise RuntimeError('network unreachable')

    monkeypatch.setattr(ghrelay, 'query_login', lookup)
    with pytest.raises(RuntimeError, match='refusing the GitHub write'):
        ghrelay.token_login('token-flaky')
    with pytest.raises(RuntimeError, match='refusing the GitHub write'):
        ghrelay.token_login('token-flaky')
    assert calls == ['token-flaky', 'token-flaky']
