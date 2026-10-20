"""Isolated gateway requests; no live configuration or credential is used."""
import base64
from copy import deepcopy
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from caller_pr_gates.cli import DOCS_CONTEXTS, DOCS_ENVIRONMENT, DOCS_SECRET, gateway, main
from caller_pr_gates.store import Store

REPO = 'Vaquum/Limen'
PR = f'repos/{REPO}/pulls/7'
DOCS_PROTECTION = f'repos/{REPO}/branches/main/protection'
DOCS_CHECKS = DOCS_PROTECTION + '/required_status_checks'
RULES = f'repos/{REPO}/rulesets/5406995'
POLICY = {'protected_branches': True, 'custom_branch_policies': False}
RETIRED = {'context': 'Workers Builds: limen', 'app_id': 85455}
CIPHERTEXT = base64.b64encode(b'isolated test ciphertext' * 4).decode()


class Remote:
    def __init__(self):
        branch = {'ref': 'main', 'sha': 'a' * 40, 'repo': {'id': 17, 'full_name': REPO}}
        checks = [{'context': f'check-{i}', 'app_id': 15368 + i} for i in range(22)]
        checks.insert(9, deepcopy(RETIRED))
        self.values = {
            PR: {'number': 7, 'url': 'https://api.github.com/' + PR, 'state': 'open',
                 'merged': False, 'draft': False, 'base': branch,
                 'head': {**deepcopy(branch), 'ref': 'docs-publisher', 'sha': 'b' * 40}},
            DOCS_ENVIRONMENT: {'name': 'documentation', 'url': 'https://api.github.com/' + DOCS_ENVIRONMENT,
                               'deployment_branch_policy': deepcopy(POLICY)},
            DOCS_ENVIRONMENT + '/secrets/public-key': {'key_id': '123456789'},
            DOCS_SECRET: {'name': 'CLOUDFLARE_DOCS_API_TOKEN',
                          'created_at': '2026-10-20T00:00:00Z', 'updated_at': '2026-10-20T00:00:00Z'},
            DOCS_PROTECTION: {
                'url': 'https://api.github.com/' + DOCS_PROTECTION,
                'required_status_checks': {'url': 'https://api.github.com/' + DOCS_CHECKS,
                    'strict': True, 'checks': checks, 'contexts': [c['context'] for c in checks],
                    'contexts_url': 'https://api.github.com/' + DOCS_CHECKS + '/contexts'},
                'enforce_admins': {'enabled': True},
                'required_pull_request_reviews': {'dismiss_stale_reviews': True,
                    'require_code_owner_reviews': True, 'required_approving_review_count': 1,
                    'require_last_push_approval': True, 'dismissal_restrictions': {'users': [], 'teams': []}},
                'required_conversation_resolution': {'enabled': True},
                'required_linear_history': {'enabled': True}, 'restrictions': None,
                'allow_force_pushes': {'enabled': False}, 'allow_deletions': {'enabled': False}},
            RULES: {'id': 5406995, 'name': 'Protect Main', 'target': 'branch', 'source_type': 'Repository',
                'source': REPO, 'enforcement': 'active', 'bypass_actors': [],
                'conditions': {'ref_name': {'include': ['~DEFAULT_BRANCH'], 'exclude': []}},
                'rules': [{'type': 'required_status_checks', 'parameters': {
                    'strict_required_status_checks_policy': True,
                    'required_status_checks': [{'context': f'check-{i}', 'integration_id': 15368 + i}
                                               for i in range(18)]}},
                    {'type': 'pull_request', 'parameters': {'require_code_owner_review': True,
                        'require_last_push_approval': True, 'required_review_thread_resolution': True}}],
                'updated_at': '2026-10-20T00:00:00Z'},
        }
        self.calls = []
        self.writes = []
        self.before_call = lambda argv, kwargs: None
        self.after_write = lambda: None
        self.write_exit = 0

    def payload(self, endpoint):
        if endpoint == DOCS_ENVIRONMENT:
            return {'deployment_branch_policy': deepcopy(POLICY)}
        if endpoint == DOCS_SECRET:
            return {'key_id': '123456789', 'encrypted_value': CIPHERTEXT}
        if endpoint == DOCS_CONTEXTS:
            return {'contexts': [RETIRED['context']]}
        return {'checks': [deepcopy(c) for c in self.values[DOCS_PROTECTION]['required_status_checks']['checks']
                           if c != RETIRED]}

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.before_call(argv, kwargs)
        path = argv[2]
        if len(argv) == 3:
            if path not in self.values:
                return subprocess.CompletedProcess(argv, 1, '', 'unavailable')
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.values[path]), '')
        method = {DOCS_CHECKS: 'PATCH', DOCS_CONTEXTS: 'DELETE'}.get(path, 'PUT')
        assert argv == ['gh', 'api', path, '--method', method, '--input', '-']
        assert kwargs['text'] is True and kwargs['capture_output'] is True
        self.writes.append((argv, kwargs['input']))
        if self.write_exit:
            return subprocess.CompletedProcess(argv, self.write_exit, '', 'private error must not be printed')
        payload = json.loads(kwargs['input'])
        if path == DOCS_ENVIRONMENT:
            self.values[path] = {'name': 'documentation', 'url': 'https://api.github.com/' + path, **payload}
        elif path == DOCS_SECRET:
            self.values[path] = {'name': 'CLOUDFLARE_DOCS_API_TOKEN',
                'created_at': '2026-10-20T00:00:00Z', 'updated_at': '2026-10-20T01:00:00Z'}
        elif path == DOCS_CONTEXTS:
            # Model the documented name DELETE; actual service outcomes require live readback.
            required = self.values[DOCS_PROTECTION]['required_status_checks']
            required['checks'] = [c for c in required['checks'] if c['context'] not in payload['contexts']]
            required['contexts'] = [name for name in required['contexts'] if name not in payload['contexts']]
        else:
            required = self.values[DOCS_PROTECTION]['required_status_checks']
            required['checks'] = payload['checks']
            required['contexts'] = [c['context'] for c in payload['checks']]
        self.after_write()
        return subprocess.CompletedProcess(argv, 0, '{}', '')


@pytest.fixture
def case(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    with (tmp_path / '.git/config').open('a') as config:
        config.write('[remote "origin"]\n\turl = https://github.com/Vaquum/Limen.git\n')
    store = Store(tmp_path / 'state')
    store.register('s', 'codex', REPO, 7, str(tmp_path))
    value = SimpleNamespace(session='s', client='codex', cwd=str(tmp_path), kind=None,
                            slice_id=None, target=f'https://github.com/{REPO}/pull/7')
    path = tmp_path / 'request.json'
    remote = Remote()

    def request(endpoint=DOCS_ENVIRONMENT, payload=None):
        path.write_text(json.dumps(remote.payload(endpoint) if payload is None else payload))
        value.arguments = ['api', endpoint, '--method', {DOCS_CHECKS: 'PATCH', DOCS_CONTEXTS: 'DELETE'}.get(endpoint, 'PUT'),
                           '--input', str(path)]
        return value

    request()
    return store, value, path, remote, request


def audits(store, suffix):
    with store._connect() as db:
        return [json.loads(row[0]) for row in db.execute('SELECT fields FROM audit WHERE event=?',
            ('github_documentation_configuration_' + suffix,))]


@pytest.mark.parametrize('endpoint', [DOCS_ENVIRONMENT, DOCS_SECRET])
def test_documented_cli_uses_frozen_json_and_verified_readbacks(case, monkeypatch, capsys, endpoint):
    store, value, path, remote, request = case
    request(endpoint)
    admitted = json.loads(path.read_text())
    original_protection = deepcopy(remote.values[DOCS_PROTECTION])
    original_rules = deepcopy(remote.values[RULES])
    if endpoint == DOCS_ENVIRONMENT:
        remote.values.pop(DOCS_ENVIRONMENT)
    elif endpoint == DOCS_SECRET:
        remote.values.pop(DOCS_SECRET)
    remote.before_call = lambda *_: path.write_text('{"unexpected":"changed after admission"}')
    monkeypatch.setattr('sys.argv', ['caller-pr-gates', 'github', '--session', 's', '--client', 'codex',
        '--cwd', value.cwd, '--target', value.target, '--', *value.arguments])
    with patch('caller_pr_gates.cli.Store', return_value=store), patch('caller_pr_gates.cli.github_run', remote):
        main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['stage'] == 'verified' and receipt['exit_code'] == 0
    assert audits(store, 'verified') == [receipt]
    assert len(remote.writes) == 1 and json.loads(remote.writes[0][1]) == admitted
    assert remote.writes[0][1] == json.dumps(admitted, sort_keys=True, separators=(',', ':'))
    assert CIPHERTEXT not in json.dumps(receipt) and 'encrypted_value' not in json.dumps(receipt)
    assert store.validation_plans('s') == [] and len(store.prs('s')) == 1
    with store._connect() as db:
        assert not db.execute("SELECT 1 FROM audit WHERE event='ownership_evidence'").fetchone()
        watch = db.execute('SELECT * FROM pr_watch').fetchone()
        assert watch['owner_session'] == 's' and watch['cwd'] == value.cwd and watch['state'] == 'open'
    assert remote.values[RULES] == original_rules
    assert remote.values[DOCS_PROTECTION] == original_protection
    assert not any(call[2] in {RULES, DOCS_PROTECTION, DOCS_CHECKS} for call in remote.calls)
    assert len(remote.calls) == (4 if endpoint == DOCS_ENVIRONMENT else 7)


def test_provisioning_sequence_and_matching_origin_relative_input(case, tmp_path, monkeypatch):
    store, value, path, remote, request = case
    remote.values.pop(DOCS_ENVIRONMENT)
    remote.values.pop(DOCS_SECRET)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / path.name).write_text('{"deployment_branch_policy":null}')
    monkeypatch.chdir(elsewhere)
    subprocess.run(['git', '-C', value.cwd, 'remote', 'set-url', 'origin',
                    'ssh://git@github.com/Vaquum/Limen.git'], check=True)
    with patch('caller_pr_gates.cli.github_run', remote):
        for endpoint in (DOCS_ENVIRONMENT, DOCS_SECRET):
            request(endpoint)
            value.arguments[-1] = path.name
            assert gateway(value, store)['stage'] == 'verified'
    assert [call[0][2] for call in remote.writes] == [DOCS_ENVIRONMENT, DOCS_SECRET]


@pytest.mark.parametrize('endpoint,payload', [
    (DOCS_ENVIRONMENT, {}), (DOCS_ENVIRONMENT, []),
    (DOCS_ENVIRONMENT, {'deployment_branch_policy': None}),
    (DOCS_ENVIRONMENT, {'deployment_branch_policy': {'protected_branches': 1, 'custom_branch_policies': 0}}),
    (DOCS_ENVIRONMENT, {'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True}}),
    (DOCS_ENVIRONMENT, {'deployment_branch_policy': POLICY, 'reviewers': []}),
    (DOCS_ENVIRONMENT, {'deployment_branch_policy': {**POLICY, 'extra': True}}),
    (DOCS_SECRET, {'key_id': '123456789', 'encrypted_value': 'plaintext'}),
    (DOCS_SECRET, {'key_id': '123456789', 'encrypted_value': base64.b64encode(b'x' * 48).decode()}),
    (DOCS_SECRET, {'key_id': '', 'encrypted_value': CIPHERTEXT}),
    (DOCS_SECRET, {'key_id': 123456789, 'encrypted_value': CIPHERTEXT}),
    (DOCS_SECRET, {'key_id': '123456789', 'encrypted_value': True}),
    (DOCS_SECRET, {'key_id': '123456789', 'encrypted_value': CIPHERTEXT, 'value': 'plaintext'}),
    (DOCS_CHECKS, {'checks': []}), (DOCS_CHECKS, {'checks': {}, 'strict': False}),
    (DOCS_CHECKS, {'checks': [{'context': 'check-0'}]}),
    *[(DOCS_CHECKS, {'checks': [{'context': 'check-0', 'app_id': app}]}) for app in (None, True, '15368', 0, -2)],
    (DOCS_CHECKS, {'checks': [{'context': '', 'app_id': 15368}]}),
    (DOCS_CHECKS, {'checks': [{'context': 'check-0', 'app_id': 15368, 'extra': 1}]}),
])
def test_only_the_exact_typed_payload_is_admitted(case, endpoint, payload):
    store, value, _, remote, request = case
    request(endpoint, payload)
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError):
        gateway(value, store)
    assert remote.calls == [] and audits(store, 'attempt') == []


@pytest.mark.parametrize('change', ['missing', 'malformed', 'stdin', 'method', 'headers', 'host', 'field', 'duplicate', 'cache'])
def test_request_file_and_flags_cannot_escape_admission(case, change):
    store, value, path, remote, _ = case
    if change == 'missing': path.unlink()
    elif change == 'malformed': path.write_text('not JSON')
    elif change == 'stdin': value.arguments[-1] = '-'
    elif change == 'method': value.arguments[3] = 'PATCH'
    else:
        value.arguments += {'headers': ['-H', 'X-Anything: x'], 'host': ['--hostname', 'other.invalid'],
                           'field': ['-f', 'strict=false'], 'duplicate': ['--input', str(path)],
                           'cache': ['--cache', '1h']}[change]
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError):
        gateway(value, store)
    assert remote.calls == []


@pytest.mark.parametrize('endpoint,method', [
    (DOCS_ENVIRONMENT + '/other', 'PUT'), (DOCS_ENVIRONMENT.replace('documentation', 'production'), 'PUT'),
    (DOCS_SECRET.replace('CLOUDFLARE_DOCS_API_TOKEN', 'OTHER'), 'PUT'),
    (DOCS_SECRET.replace('Vaquum/Limen', 'other/repo'), 'PUT'), (DOCS_SECRET, 'DELETE'),
    *[(DOCS_CHECKS, method) for method in ('PATCH', 'PUT', 'POST', 'DELETE')],
    *[(DOCS_CHECKS + '/contexts', method) for method in ('PATCH', 'PUT', 'POST', 'DELETE')],
    (DOCS_CHECKS.replace('/main/', '/other/'), 'PATCH'), (DOCS_PROTECTION, 'PUT'),
    (RULES, 'PUT'), (DOCS_CHECKS + '?strict=false', 'PATCH'), (DOCS_CHECKS + '/', 'PATCH'),
    ('repos/Vaquum/Limen', 'PATCH'), ('repos/Vaquum/Limen/statuses/' + 'a' * 40, 'POST'),
])
def test_unrelated_configuration_and_status_writes_stay_denied(case, endpoint, method):
    store, value, _, remote, _ = case
    value.arguments[1], value.arguments[3] = endpoint, method
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError):
        gateway(value, store)
    assert remote.calls == []


@pytest.mark.parametrize('change', ['unknown', 'foreign', 'child', 'client', 'cwd', 'session_cwd',
                                   'watch_cwd', 'closed', 'merged', 'target', 'issue', 'repository', 'origin'])
def test_recorded_owner_checkout_and_repository_are_required(case, change, tmp_path):
    store, value, _, remote, _ = case
    if change == 'unknown': value.session = 'missing'
    elif change == 'foreign':
        store.register('other', 'codex', REPO, 7, value.cwd)
        value.session = 'other'
    elif change == 'child':
        store.register('child', 'codex', REPO, 7, value.cwd, parent_session='s')
        value.session = 'child'
    elif change == 'client': value.client = 'claude'
    elif change == 'cwd': value.cwd = str(tmp_path / 'other')
    elif change == 'session_cwd': store.record_session('s', 'codex', str(tmp_path / 'other'))
    elif change == 'watch_cwd':
        with store._connect() as db: db.execute("UPDATE pr_watch SET cwd=?", (str(tmp_path / 'other'),))
    elif change in {'closed', 'merged'}:
        with store._connect() as db: db.execute("UPDATE pr_watch SET state=?", (change,))
    elif change == 'target': value.target = f'https://github.com/{REPO}/pull/99'
    elif change == 'issue': value.target = f'https://github.com/{REPO}/issues/7'
    elif change == 'repository': value.target = 'https://github.com/other/repo/pull/7'
    else:
        subprocess.run(['git', '-C', value.cwd, 'remote', 'set-url', 'origin', 'https://github.com/other/repo.git'], check=True)
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError):
        gateway(value, store)
    assert remote.calls == [] and audits(store, 'verified') == []


@pytest.mark.parametrize('change', ['closed', 'merged', 'unknown', 'draft', 'number', 'url', 'fork', 'branch', 'head'])
def test_live_pr_must_be_open_and_identify_the_owned_repository(case, change):
    store, value, _, remote, _ = case
    pr = remote.values[PR]
    if change == 'closed': pr['state'] = 'closed'
    elif change == 'merged': pr['merged'] = True
    elif change == 'unknown': pr.pop('merged')
    elif change == 'draft': pr['draft'] = True
    elif change == 'number': pr['number'] = 99
    elif change == 'url': pr['url'] = pr['url'].replace('/7', '/99')
    elif change == 'fork': pr['head']['repo']['full_name'] = 'other/Limen'
    elif change == 'branch': pr['base']['ref'] = 'other'
    else: pr['head']['sha'] = ''
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError):
        gateway(value, store)
    assert remote.writes == []


@pytest.mark.parametrize('change', ['environment', 'policy', 'name', 'url', 'public_key', 'key'])
def test_provisioning_is_a_fresh_prerequisite(case, change):
    store, value, _, remote, request = case
    request(DOCS_SECRET)
    if change == 'environment': remote.values.pop(DOCS_ENVIRONMENT)
    elif change == 'policy': remote.values[DOCS_ENVIRONMENT]['deployment_branch_policy'] = None
    elif change == 'name': remote.values[DOCS_ENVIRONMENT]['name'] = 'production'
    elif change == 'url': remote.values[DOCS_ENVIRONMENT]['url'] += '/other'
    elif change == 'public_key': remote.values.pop(DOCS_ENVIRONMENT + '/secrets/public-key')
    else:
        remote.values[DOCS_ENVIRONMENT + '/secrets/public-key']['key_id'] = 'new-key'
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError):
        gateway(value, store)
    assert remote.writes == []


@pytest.mark.parametrize('change', ['delete_extra', 'add', 'reorder', 'binding', 'strict', 'contexts',
                                   'already_removed', 'duplicate_retired', 'no_strict', 'no_admin',
                                   'no_threads', 'no_stale_dismissal', 'no_approval', 'rules_hidden', 'rules_bypass'])
def test_classic_protection_admission_is_removed_regardless_of_delta_or_authority(case, change):
    store, value, path, remote, request = case
    request(DOCS_CHECKS)
    payload = json.loads(path.read_text())
    before = remote.values[DOCS_PROTECTION]
    required = before['required_status_checks']
    if change == 'delete_extra': payload['checks'].pop()
    elif change == 'add': payload['checks'].append({'context': 'new', 'app_id': 17})
    elif change == 'reorder': payload['checks'].reverse()
    elif change == 'binding': payload['checks'][0]['app_id'] = -1
    elif change == 'strict': payload['strict'] = True
    elif change == 'contexts': required['contexts'].reverse()
    elif change == 'already_removed': required['checks'].remove(RETIRED)
    elif change == 'duplicate_retired': required['checks'].append(deepcopy(RETIRED))
    elif change == 'no_strict': required['strict'] = False
    elif change == 'no_admin': before['enforce_admins']['enabled'] = False
    elif change == 'no_threads': before['required_conversation_resolution']['enabled'] = False
    elif change == 'no_stale_dismissal': before['required_pull_request_reviews']['dismiss_stale_reviews'] = False
    elif change == 'no_approval': before['required_pull_request_reviews']['required_approving_review_count'] = 0
    elif change == 'rules_hidden': remote.values[RULES].pop('bypass_actors')
    else: remote.values[RULES]['bypass_actors'] = [{'actor_id': 17, 'actor_type': 'User'}]
    path.write_text(json.dumps(payload))
    original = deepcopy(remote.values)
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError, match='API gateway permits'):
        gateway(value, store)
    assert remote.calls == [] and remote.writes == []
    assert remote.values == original and audits(store, 'verified') == []


def test_denied_protection_write_preserves_same_context_from_another_app_in_order(case):
    store, value, _, remote, request = case
    required = remote.values[DOCS_PROTECTION]['required_status_checks']
    other = {'context': RETIRED['context'], 'app_id': 99}
    required['checks'].insert(2, other)
    required['contexts'].insert(2, other['context'])
    request(DOCS_CHECKS)
    original = deepcopy(remote.values)
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError, match='API gateway permits'):
        gateway(value, store)
    assert remote.calls == [] and remote.values == original
    assert required['checks'][2] == other


@pytest.mark.parametrize('change', ['owner', 'checkout', 'closed'])
def test_changes_during_preflight_refuse_the_write(case, change, tmp_path):
    store, value, _, remote, request = case
    request(DOCS_SECRET)
    def race(argv, kwargs):
        if argv == ['gh', 'api', DOCS_ENVIRONMENT + '/secrets/public-key']:
            with store._connect() as db:
                if change == 'owner': db.execute("UPDATE pr_watch SET owner_session='other'")
                elif change == 'checkout': db.execute('UPDATE pr_watch SET cwd=?', (str(tmp_path / 'other'),))
                else: db.execute("UPDATE pr_watch SET state='closed'")
    remote.before_call = race
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError):
        gateway(value, store)
    assert remote.writes == []


@pytest.mark.parametrize('endpoint,change', [
    *[(endpoint, change) for endpoint in (DOCS_ENVIRONMENT, DOCS_SECRET)
      for change in ('exit', 'environment', 'head', 'base', 'closed', 'read_error', 'owner')],
    *[(DOCS_SECRET, change) for change in ('secret_missing', 'secret_name', 'secret_timestamp')],
])
def test_unverified_write_or_any_readback_drift_stays_incomplete_without_retry(case, change, endpoint):
    store, value, _, remote, request = case
    request(endpoint)
    def drift():
        if change == 'secret_missing': remote.values.pop(DOCS_SECRET)
        elif change == 'secret_name': remote.values[DOCS_SECRET]['name'] = 'OTHER'
        elif change == 'secret_timestamp': remote.values[DOCS_SECRET]['updated_at'] = None
        elif change == 'environment': remote.values[DOCS_ENVIRONMENT]['deployment_branch_policy'] = None
        elif change == 'head': remote.values[PR]['head']['sha'] = 'c' * 40
        elif change == 'base': remote.values[PR]['base']['sha'] = 'c' * 40
        elif change == 'closed': remote.values[PR]['state'] = 'closed'
        elif change == 'read_error': remote.values.pop(DOCS_ENVIRONMENT)
        elif change == 'owner':
            with store._connect() as db: db.execute("UPDATE pr_watch SET owner_session='other'")
    if change == 'exit': remote.write_exit = 1
    remote.after_write = drift
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(RuntimeError, match='incomplete; read back'):
        gateway(value, store)
    assert len(remote.writes) == 1 and audits(store, 'verified') == []
    incomplete = audits(store, 'incomplete')
    assert len(incomplete) == 1 and incomplete[0]['exit_code'] == (1 if change == 'exit' else 0)
    assert 'private error' not in json.dumps(incomplete)
    assert CIPHERTEXT not in json.dumps(incomplete)
    assert incomplete[0]['stage'] == ('write' if change == 'exit' else 'readback')


@pytest.mark.parametrize('change', ['addition', 'rebind'])
@pytest.mark.parametrize('timing', ['after_payload', 'before_relay'])
def test_concurrent_administrator_edit_is_not_overwritten_or_reported_verified(case, change, timing):
    """Deny the unsafe operation, including the two previously failing interleavings."""
    store, value, _, remote, request = case
    request(DOCS_CHECKS)
    concurrent = {'context': 'concurrently-required', 'app_id': 98765}
    def interleave(argv, kwargs):
        if argv == ['gh', 'api', DOCS_CHECKS, '--method', 'PATCH', '--input', '-']:
            required = remote.values[DOCS_PROTECTION]['required_status_checks']
            if change == 'addition':
                required['checks'].append(concurrent)
                required['contexts'].append(concurrent['context'])
            else:
                required['checks'][0]['app_id'] = 98765
    remote.before_call = interleave
    if timing == 'after_payload':
        # Another writer can also change the authority after the caller saves its list.
        interleave(['gh', 'api', DOCS_CHECKS, '--method', 'PATCH', '--input', '-'], {})
    original = deepcopy(remote.values)
    with patch('caller_pr_gates.cli.github_run', remote), pytest.raises(ValueError, match='API gateway permits'):
        gateway(value, store)
    # Retain the lost-update assertions as regression traps if PATCH is ever re-admitted.
    if remote.writes:
        after = remote.values[DOCS_PROTECTION]['required_status_checks']['checks']
        if change == 'addition': assert concurrent in after
        else: assert after[0]['app_id'] == 98765
    assert remote.calls == [] and remote.writes == []
    assert remote.values == original
    assert audits(store, 'attempt') == [] and audits(store, 'verified') == []
