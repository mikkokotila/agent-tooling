import json
import subprocess
from unittest.mock import patch
import pytest
from caller_pr_gates import ghrelay
from caller_pr_gates.cli import gateway
from test_cli import args, store


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
    monkeypatch.setattr(ghrelay, 'query_login', lambda: 'bit-mis')
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match="owner identity 'mikkokotila'"):
            gateway(write_argv(kind), store)
    assert store.prs('s') == []


def test_mutating_call_refuses_any_login_other_than_owner(store, monkeypatch):
    monkeypatch.setattr(ghrelay, 'query_login', lambda: 'someone-else')
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

    def lookup():
        calls.append(1)
        return 'mikkokotila'

    monkeypatch.setattr(ghrelay, 'query_login', lookup)
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
        gateway(args(['pr', 'comment', 'https://github.com/o/r/pull/77', '--body', 'One']), store)
        gateway(args(['pr', 'comment', 'https://github.com/o/r/pull/78', '--body', 'Two']), store)
    assert calls == [1]


def test_cached_login_is_keyed_by_token(monkeypatch):
    logins = iter(['mikkokotila', 'bit-mis'])
    calls = []

    def lookup():
        calls.append(1)
        return next(logins)

    monkeypatch.setattr(ghrelay, 'query_login', lookup)
    assert ghrelay.token_login('token-a') == 'mikkokotila'
    assert ghrelay.token_login('token-b') == 'bit-mis'
    assert ghrelay.token_login('token-a') == 'mikkokotila'
    assert len(calls) == 2


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
    def lookup():
        raise AssertionError('reads must not look up the owner identity')

    monkeypatch.setattr(ghrelay, 'query_login', lookup)
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
        gateway(args(argv), store)
    assert store.prs('s') == []
