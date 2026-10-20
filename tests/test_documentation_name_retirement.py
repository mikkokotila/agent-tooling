"""Targeted request and readback regressions, not proof of undocumented GitHub semantics."""
from copy import deepcopy
import json
import subprocess
from unittest.mock import patch

import pytest

from caller_pr_gates.cli import DOCS_CONTEXTS, gateway, main
from test_documentation_configuration import (
    DOCS_CHECKS, DOCS_ENVIRONMENT, DOCS_PROTECTION, DOCS_SECRET, POLICY, PR,
    REPO, RETIRED, RULES, audits, case,
)

BODY = {'contexts': ['Workers Builds: limen']}
DELETE = ['gh', 'api', DOCS_CONTEXTS, '--method', 'DELETE', '--input', '-']


def test_documented_registration_and_delete_freeze_only_the_retired_name(case, monkeypatch, capsys, tmp_path):
    store, value, path, remote, request = case
    request(DOCS_CONTEXTS)
    before = deepcopy(remote.values[DOCS_PROTECTION])
    rules = deepcopy(remote.values[RULES])
    # The supported setup refreshes the same owner context; no guard relaxation or adoption.
    store.record_session('s', 'codex', str(tmp_path / 'primary'))
    monkeypatch.setattr('sys.argv', ['caller-pr-gates', 'register', value.target,
        '--session', 's', '--client', 'codex', '--cwd', value.cwd])
    with patch('caller_pr_gates.cli.Store', return_value=store):
        main()
    capsys.readouterr()
    def freeze(argv, kwargs):
        path.write_text('{"contexts":["different-name"]}')
        assert kwargs['timeout'] == (90 if argv == DELETE else 30)
    remote.before_call = freeze
    monkeypatch.setattr('sys.argv', ['caller-pr-gates', 'github', '--session', 's', '--client', 'codex',
        '--cwd', value.cwd, '--target', value.target, '--', *value.arguments])
    with patch('caller_pr_gates.cli.Store', return_value=store), patch('caller_pr_gates.cli.github_run', remote):
        main()
    receipt = json.loads(capsys.readouterr().out)
    assert remote.writes == [(DELETE, '{"contexts":["Workers Builds: limen"]}')]
    assert receipt['stage'] == 'verified' and receipt['exit_code'] == 0
    assert receipt['before'] == {'classic': before, 'ruleset': rules}
    assert receipt['after'] == {'classic': remote.values[DOCS_PROTECTION], 'ruleset': rules}
    assert receipt['environment']['deployment_branch_policy'] == POLICY
    assert receipt['secret'] == remote.values[DOCS_SECRET]
    assert audits(store, 'verified') == [receipt]
    assert [call[2] for call in remote.calls] == [
        DOCS_ENVIRONMENT, DOCS_SECRET, PR, DOCS_PROTECTION, RULES, PR, DOCS_CONTEXTS,
        DOCS_PROTECTION, RULES, DOCS_ENVIRONMENT, DOCS_SECRET, PR,
    ]
    after = deepcopy(before)
    after['required_status_checks']['checks'].remove(RETIRED)
    after['required_status_checks']['contexts'].remove(RETIRED['context'])
    assert remote.values[DOCS_PROTECTION] == after
    assert store.validation_plans('s') == [] and len(store.prs('s')) == 1
    with store._connect() as db:
        assert not db.execute("SELECT 1 FROM audit WHERE event='ownership_evidence'").fetchone()


@pytest.mark.parametrize('duplicate_contexts', [False, True])
@pytest.mark.parametrize('timing', ['before_read', 'before_delete'])
def test_retirement_includes_every_app_binding_at_only_the_approved_name(case, duplicate_contexts, timing):
    store, value, _, remote, request = case
    request(DOCS_CONTEXTS)
    def add_binding():
        required = remote.values[DOCS_PROTECTION]['required_status_checks']
        required['checks'].insert(2, {'context': RETIRED['context'], 'app_id': 99})
        if duplicate_contexts:
            required['contexts'].insert(2, RETIRED['context'])
    if timing == 'before_read': add_binding()
    else: remote.before_call = lambda argv, _: add_binding() if argv == DELETE else None
    original = deepcopy(remote.values[DOCS_PROTECTION])
    with patch('caller_pr_gates.cli.github_run', remote):
        receipt = gateway(value, store)
    assert receipt['stage'] == 'verified'
    expected = deepcopy(original)
    required = expected['required_status_checks']
    required['checks'] = [c for c in required['checks'] if c['context'] != RETIRED['context']]
    required['contexts'] = [name for name in required['contexts'] if name != RETIRED['context']]
    assert remote.values[DOCS_PROTECTION] == expected
    assert remote.writes == [(DELETE, json.dumps(BODY, separators=(',', ':')))]


@pytest.mark.parametrize('change', ['addition', 'rebind'])
@pytest.mark.parametrize('timing', ['before_read', 'before_delete', 'after_delete'])
def test_other_names_are_never_replayed_and_concurrent_drift_remains_incomplete(case, change, timing):
    store, value, _, remote, request = case
    request(DOCS_CONTEXTS)
    concurrent = {'context': 'concurrently-required', 'app_id': 98765}
    def interleave():
        required = remote.values[DOCS_PROTECTION]['required_status_checks']
        if change == 'addition':
            required['checks'].insert(3, concurrent)
            required['contexts'].insert(3, concurrent['context'])
        else: required['checks'][0]['app_id'] = 98765
    if timing == 'before_read': interleave()
    elif timing == 'before_delete': remote.before_call = lambda argv, _: interleave() if argv == DELETE else None
    else: remote.after_write = interleave
    before = deepcopy(remote.values[DOCS_PROTECTION])
    with patch('caller_pr_gates.cli.github_run', remote):
        if timing == 'before_read':
            assert gateway(value, store)['stage'] == 'verified'
        else:
            with pytest.raises(RuntimeError, match='incomplete; read back'):
                gateway(value, store)
            receipt, = audits(store, 'incomplete')
            assert receipt['before']['classic'] == before
            assert receipt['after']['classic'] == remote.values[DOCS_PROTECTION]
            assert audits(store, 'verified') == []
    after = remote.values[DOCS_PROTECTION]['required_status_checks']
    if change == 'addition': assert after['checks'][3] == concurrent
    else: assert after['checks'][0]['app_id'] == 98765
    assert RETIRED['context'] not in after['contexts']
    assert remote.writes == [(DELETE, json.dumps(BODY, separators=(',', ':')))]
    assert all(call == DELETE for call in remote.calls if len(call) > 3)


@pytest.mark.parametrize('payload', [
    {}, [], {'contexts': []}, {'contexts': None}, {'contexts': RETIRED['context']},
    {'contexts': ['other']}, {'contexts': [RETIRED['context'], 'other']},
    {'contexts': [RETIRED['context'], RETIRED['context']]},
    {'contexts': [RETIRED['context'].upper()]}, {'contexts': [RETIRED['context'] + ' ']},
    {**BODY, 'app_id': 85455}, {**BODY, 'checks': []}, {**BODY, 'strict': True},
])
def test_only_the_single_exact_name_payload_is_admitted(case, payload):
    store, value, _, remote, request = case
    request(DOCS_CONTEXTS, payload)
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError):
        gateway(value, store)
    assert remote.calls == [] and audits(store, 'verified') == []


@pytest.mark.parametrize('change', [
    'PATCH', 'PUT', 'POST', 'GET', 'HEAD', 'stdin', 'header', 'field', 'host', 'query',
    'branch', 'repository', 'protection', 'ruleset', 'trailing_slash', 'no_input', 'malformed',
])
def test_no_alternative_method_flag_body_or_route_is_forwarded(case, change):
    store, value, path, remote, request = case
    request(DOCS_CONTEXTS)
    if change in {'PATCH', 'PUT', 'POST', 'GET', 'HEAD'}: value.arguments[3] = change
    elif change == 'stdin': value.arguments[-1] = '-'
    elif change == 'header': value.arguments += ['--header', 'If-Match: fabricated']
    elif change == 'field': value.arguments += ['--field', 'contexts[]=other']
    elif change == 'host': value.arguments += ['--hostname', 'other.example']
    elif change == 'query': value.arguments[1] += '?contexts[]=other'
    elif change == 'branch': value.arguments[1] = DOCS_CONTEXTS.replace('/main/', '/other/')
    elif change == 'repository': value.arguments[1] = DOCS_CONTEXTS.replace('Vaquum/Limen', 'other/repo')
    elif change == 'protection': value.arguments[1] = DOCS_PROTECTION
    elif change == 'ruleset': value.arguments[1] = RULES
    elif change == 'trailing_slash': value.arguments[1] += '/'
    elif change == 'no_input': value.arguments = value.arguments[:-2]
    else: path.write_text('{')
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError):
        gateway(value, store)
    assert remote.calls == [] and remote.writes == []


@pytest.mark.parametrize('change', ['foreign', 'child', 'client', 'cwd', 'origin', 'closed', 'draft', 'branch'])
def test_name_retirement_reuses_exact_owner_and_live_pr_guards(case, change, tmp_path):
    store, value, _, remote, request = case
    request(DOCS_CONTEXTS)
    if change in {'foreign', 'child'}:
        store.register(change, 'codex', REPO, 7, value.cwd, parent_session='s' if change == 'child' else None)
        value.session = change
    elif change == 'client': value.client = 'claude'
    elif change == 'cwd': value.cwd = str(tmp_path / 'primary')
    elif change == 'origin':
        subprocess.run(['git', '-C', value.cwd, 'remote', 'set-url', 'origin', 'https://github.com/other/repo'], check=True)
    elif change == 'closed': remote.values[PR]['state'] = 'closed'
    elif change == 'draft': remote.values[PR]['draft'] = True
    else: remote.values[PR]['base']['ref'] = 'other'
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError):
        gateway(value, store)
    assert remote.writes == [] and audits(store, 'verified') == []


@pytest.mark.parametrize('change', [
    'environment', 'policy', 'secret', 'secret_name', 'secret_time', 'classic', 'strict', 'admins',
    'reviews', 'threads', 'contexts', 'app', 'already_retired', 'ruleset', 'rules_id', 'rules_bypass',
])
def test_provisioning_metadata_and_complete_authorities_are_prerequisites(case, change):
    store, value, _, remote, request = case
    request(DOCS_CONTEXTS)
    classic = remote.values[DOCS_PROTECTION]
    required = classic['required_status_checks']
    if change == 'environment': remote.values.pop(DOCS_ENVIRONMENT)
    elif change == 'policy': remote.values[DOCS_ENVIRONMENT]['deployment_branch_policy'] = None
    elif change == 'secret': remote.values.pop(DOCS_SECRET)
    elif change == 'secret_name': remote.values[DOCS_SECRET]['name'] = 'OTHER'
    elif change == 'secret_time': remote.values[DOCS_SECRET]['updated_at'] = ''
    elif change == 'classic': remote.values.pop(DOCS_PROTECTION)
    elif change == 'strict': required['strict'] = False
    elif change == 'admins': classic['enforce_admins']['enabled'] = False
    elif change == 'reviews': classic['required_pull_request_reviews']['required_approving_review_count'] = 0
    elif change == 'threads': classic['required_conversation_resolution']['enabled'] = False
    elif change == 'contexts': required['contexts'].append('unknown')
    elif change == 'app': required['checks'][0]['app_id'] = True
    elif change == 'already_retired':
        required['contexts'].remove(RETIRED['context'])
        required['checks'].remove(RETIRED)
    elif change == 'ruleset': remote.values.pop(RULES)
    elif change == 'rules_id': remote.values[RULES]['id'] = 1
    else: remote.values[RULES].pop('bypass_actors')
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError):
        gateway(value, store)
    assert remote.writes == [] and audits(store, 'verified') == []
    assert not any('public-key' in call[2] for call in remote.calls)


@pytest.mark.parametrize('change', ['head', 'owner'])
def test_identity_change_during_authority_reads_prevents_delete(case, change):
    store, value, _, remote, request = case
    request(DOCS_CONTEXTS)
    def drift(argv, _):
        if argv == ['gh', 'api', RULES]:
            if change == 'head': remote.values[PR]['head']['sha'] = 'c' * 40
            else:
                with store._connect() as db: db.execute("UPDATE pr_watch SET owner_session='other'")
    remote.before_call = drift
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError):
        gateway(value, store)
    assert remote.writes == [] and audits(store, 'verified') == []


@pytest.mark.parametrize('change', [
    'no_effect', 'strict', 'admins', 'reviews', 'threads', 'reorder', 'rules', 'classic_missing',
    'rules_missing', 'policy', 'secret', 'head', 'owner',
])
def test_readback_drift_is_retained_without_retry_or_rollback(case, change):
    store, value, _, remote, request = case
    request(DOCS_CONTEXTS)
    before = deepcopy(remote.values[DOCS_PROTECTION])
    def drift():
        classic = remote.values[DOCS_PROTECTION]
        if change == 'no_effect': remote.values[DOCS_PROTECTION] = deepcopy(before)
        elif change == 'strict': classic['required_status_checks']['strict'] = False
        elif change == 'admins': classic['enforce_admins']['enabled'] = False
        elif change == 'reviews': classic['required_pull_request_reviews']['dismiss_stale_reviews'] = False
        elif change == 'threads': classic['required_conversation_resolution']['enabled'] = False
        elif change == 'reorder': classic['required_status_checks']['checks'].reverse()
        elif change == 'rules': remote.values[RULES]['updated_at'] = '2026-10-20T11:00:00Z'
        elif change == 'classic_missing': remote.values.pop(DOCS_PROTECTION)
        elif change == 'rules_missing': remote.values.pop(RULES)
        elif change == 'policy': remote.values[DOCS_ENVIRONMENT]['deployment_branch_policy'] = None
        elif change == 'secret': remote.values[DOCS_SECRET]['updated_at'] = '2026-10-20T11:00:00Z'
        elif change == 'head': remote.values[PR]['head']['sha'] = 'c' * 40
        else:
            with store._connect() as db: db.execute("UPDATE pr_watch SET owner_session='other'")
    remote.after_write = drift
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(RuntimeError, match='incomplete; read back'):
        gateway(value, store)
    receipt, = audits(store, 'incomplete')
    assert receipt['exit_code'] == 0 and receipt['before']['classic'] == before
    assert receipt['after'].get('classic') == remote.values.get(DOCS_PROTECTION)
    assert receipt['after'].get('ruleset') == remote.values.get(RULES)
    assert sum(call == ['gh', 'api', DOCS_PROTECTION] for call in remote.calls) == 2
    assert sum(call == ['gh', 'api', RULES] for call in remote.calls) == 2
    assert remote.writes == [(DELETE, json.dumps(BODY, separators=(',', ':')))]
    assert audits(store, 'verified') == []


@pytest.mark.parametrize('outcome', ['exit', 'timeout_before', 'timeout_after'])
def test_failed_or_unknown_exit_still_reads_both_authorities_without_retry(case, outcome):
    store, value, _, remote, request = case
    request(DOCS_CONTEXTS)
    before = deepcopy(remote.values[DOCS_PROTECTION])
    if outcome == 'exit': remote.write_exit = 7
    def run(argv, **kwargs):
        if argv == DELETE and outcome == 'timeout_before':
            remote.calls.append(argv)
            remote.writes.append((argv, kwargs['input']))
            raise subprocess.TimeoutExpired(argv, 90, stderr='private error')
        result = remote(argv, **kwargs)
        if argv == DELETE and outcome == 'timeout_after':
            raise subprocess.TimeoutExpired(argv, 90, stderr='private error')
        return result
    with patch('caller_pr_gates.cli.github_run', run), pytest.raises(RuntimeError, match='incomplete; read back'):
        gateway(value, store)
    receipt, = audits(store, 'incomplete')
    if outcome == 'exit': assert receipt['exit_code'] == 7
    else: assert 'exit_code' not in receipt
    assert receipt['before']['classic'] == before
    assert receipt['after'] == {'classic': remote.values[DOCS_PROTECTION], 'ruleset': remote.values[RULES]}
    assert len(remote.writes) == 1 and audits(store, 'verified') == []
    assert 'private error' not in json.dumps(receipt)
