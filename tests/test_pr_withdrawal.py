"""Withdrawal is an owner-bound lifecycle action, never a passing PR receipt."""
from copy import deepcopy
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from caller_pr_gates import cli, ghrelay, progress, withdrawal
from caller_pr_gates.policy import evaluate_pr
from caller_pr_gates.store import Store
from test_policy import check, comment
from test_workflow import local

SID = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
REPO = 'Owner/Repo'
URL = 'https://github.com/Owner/Repo/pull/1'
REASON = 'The original consumers merged independently; this uninstalled candidate is withdrawn.'


@pytest.fixture
def context(local, tmp_path, monkeypatch):
    _, root, snapshot, plan = local
    store = Store(tmp_path / 'withdrawal-state')
    store.register(SID, 'codex', REPO, 1, str(root))
    store.record_scope(SID, dict(agreed_scope='Repair the requested fixture', acceptance_criteria=['Preserve its proof'],
                               slice_count=0, slice_ids=[], approved_at=1))
    snapshot = deepcopy(snapshot)
    store.set_snapshot(REPO, 1, snapshot, evaluate_pr(snapshot, []))
    normalized = progress.normalize_plan(plan, root)
    row = store.save_validation_plan(SID, REPO, 1, root, normalized, progress.fingerprint(root, normalized), 'original')
    run = store.begin_validation(row['plan_id'], 'regression', row['input_digest'], session_id=SID)
    actual = subprocess.run(normalized['checks'][0]['argv'], cwd=root, capture_output=True)
    assert actual.returncode == 1
    store.finish_validation(run['run_id'], actual.returncode, progress.fingerprint(root, normalized), session_id=SID)
    snapshot.update(mergeable=False, merge_state_status='DIRTY', review_decision='CHANGES_REQUESTED',
                    merge_readiness_head_sha=snapshot['head_sha'], expected_checks=['test'],
                    checks=[check(head_sha=snapshot['head_sha'], conclusion='failure')], comments=[comment()])
    store.set_snapshot(REPO, 1, snapshot, evaluate_pr(snapshot, []))
    pull = dict(number=1, url=f'https://api.github.com/repos/{REPO}/pulls/1', state='open', merged=False, draft=False,
                head=dict(sha=snapshot['head_sha'], ref='feature', repo=dict(id=10, full_name=REPO)),
                base=dict(ref='main', repo=dict(id=10, full_name=REPO)))
    value = dict(store=store, root=root, snapshot=snapshot, pull=pull, calls=[], reads=0, read_hook=None, write_hook=None)
    class Reader:
        async def get(self, path):
            assert path == f'/repos/{REPO}/pulls/1'
            value['reads'] += 1
            if value['read_hook']:
                value['read_hook'](value['reads'])
            return deepcopy(pull)
    async def collect(repository, number):
        assert (repository, number) == (REPO, 1)
        return deepcopy(snapshot)
    def write(argv, **options):
        value['calls'].append((argv, options))
        pull['state'] = 'closed'
        if value['write_hook']:
            return value['write_hook'](argv)
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
    monkeypatch.setenv('CODEX_THREAD_ID', SID)
    monkeypatch.setattr(withdrawal, 'GitHubClient', Reader)
    monkeypatch.setattr(withdrawal, 'collect_pr', collect)
    monkeypatch.setattr(cli, 'github_run', write)
    return value


def arguments(context, argv=None, reason=REASON):
    return SimpleNamespace(session=SID, client='codex', cwd=str(context['root']), kind=None, slice_id=None,
                           target=None, arguments=argv or ['pr', 'close', URL], withdrawn_reason=reason)


def events(store, name):
    with store._connect() as db:
        return [json.loads(row['fields']) for row in db.execute('SELECT fields FROM audit WHERE event=? ORDER BY id', (name,))]


def evidence(store):
    with store._connect() as db:
        return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                for table in ('validation_plans', 'validation_runs', 'dispositions', 'scopes', 'pr_scope_bindings')}


def test_conflicted_failed_changes_requested_candidate_closes_without_completion(context, capsys):
    store = context['store']
    before = evidence(store)
    cli.gateway(arguments(context), store)
    result = json.loads(capsys.readouterr().out)
    assert result['closed'] is True and result['withdrawn'] is True
    assert result['completion_verified'] is False and result['gates_waived'] is False
    failed = {gate['gate'] for gate in result['unresolved_gates'] if gate['status'] == 'FAIL'}
    assert {'MERGE_CONFLICT', 'CI_FAILED', 'REVIEW_HANDLED'}.issubset(failed)
    assert context['calls'][0][0] == ['gh', 'pr', 'close', URL, '--comment', 'Withdrawn without merging: ' + REASON]
    assert context['calls'][0][1]['timeout'] == 90
    assert evidence(store) == before
    assert not store.prs(SID)
    assert events(store, 'pr_withdrawn') == [{key: value for key, value in result.items() if key not in {'closed', 'withdrawn'}}]
    assert events(store, 'pr_monitor_retired')[-1]['gates_waived'] is False
    with store._connect() as db:
        watch = dict(db.execute('SELECT * FROM pr_watch').fetchone())
    assert watch['state'] == 'closed' and watch['lease_until'] == 0 and watch['worker_pid'] is None
    assert all(gate['status'] != 'PASS' for gate in store.snapshot(REPO, 1)['gates'] if gate['gate'] in failed)
    assert (context['root'] / '.git').exists()


@pytest.mark.parametrize('native', [None, OTHER])
def test_session_argument_cannot_impersonate_actual_native_root(context, monkeypatch, native):
    context['store'].record_session(OTHER, 'codex', str(context['root']))
    if native is None:
        monkeypatch.delenv('CODEX_THREAD_ID')
    else:
        monkeypatch.setenv('CODEX_THREAD_ID', native)
    with pytest.raises(ValueError, match='native|canonical'):
        cli.gateway(arguments(context), context['store'])
    assert context['reads'] == 0 and not context['calls']


def test_native_child_uses_recorded_root_and_pr_checkout_not_global_session_cwd(context, monkeypatch, tmp_path):
    store = context['store']
    store.record_session(SID, 'codex', str(tmp_path))
    store.record_session(OTHER, 'codex', str(tmp_path), parent_session=SID)
    monkeypatch.setenv('CODEX_THREAD_ID', OTHER)
    cli.gateway(arguments(context), store)
    assert events(store, 'pr_withdrawn')[0]['native_requester'] == OTHER


@pytest.mark.parametrize('change', ['owner', 'client', 'cwd', 'registration'])
def test_canonical_identity_or_receipt_mismatch_refuses(context, change):
    store = context['store']
    with store._connect() as db:
        if change == 'registration':
            db.execute('UPDATE prs SET cwd=? WHERE session_id=?', ('/other/checkout', SID))
        else:
            key = {'owner': 'owner_session', 'client': 'client', 'cwd': 'cwd'}[change]
            db.execute('UPDATE pr_watch SET ' + key + '=?', (OTHER if change == 'owner' else 'other',))
    with pytest.raises(ValueError, match='canonical'):
        cli.gateway(arguments(context), store)
    assert context['reads'] == 0 and not context['calls']


@pytest.mark.parametrize('argv', [
    ['pr', 'merge', URL], ['pr', 'close', '1'], ['issue', 'close', URL],
    ['pr', 'close', URL, '--delete-branch'], ['pr', 'close', URL, '--repo', REPO],
    ['pr', 'close', 'https://github.com/Owner/Repo/issues/1'],
    ['api', f'repos/{REPO}/pulls/1', '--method', 'PATCH'],
])
def test_withdrawal_accepts_no_other_mutation_or_flags(context, argv):
    with pytest.raises(ValueError):
        cli.gateway(arguments(context, argv), context['store'])
    assert context['reads'] == 0 and not context['calls']


@pytest.mark.parametrize('reason', ['', '  ', 'x' * 2001])
def test_reason_must_be_explicit_bounded_public_text(context, reason):
    with pytest.raises(ValueError, match='public reason'):
        cli.gateway(arguments(context, reason=reason), context['store'])
    assert not context['calls']


@pytest.mark.parametrize('option', ['kind', 'slice_id', 'nonrequired_quota_approval', 'target'])
def test_unrelated_gateway_options_and_conflicting_target_are_refused(context, option):
    args = arguments(context)
    setattr(args, option, 'https://github.com/Owner/Repo/pull/2' if option == 'target' else 'extra')
    with pytest.raises(ValueError):
        cli.gateway(args, context['store'])
    assert not context['calls'] and context['reads'] == 0


def test_existing_github_owner_identity_enforcement_is_preserved(context, monkeypatch):
    monkeypatch.setattr(ghrelay, 'token_login', lambda token=None: 'foreign-login')
    with pytest.raises(ValueError, match='owner identity'):
        cli.gateway(arguments(context), context['store'])
    assert context['reads'] == 0 and not context['calls']


@pytest.mark.parametrize('field, value', [
    ('state', 'closed'), ('merged', True), ('draft', True), ('number', 2), ('head.sha', 'b' * 40),
    ('head.repo.full_name', 'Other/Repo'), ('base.repo.id', 11), ('head.ref', ''),
])
def test_live_identity_must_match_clean_owned_source(context, field, value):
    target = context['pull']
    path = field.split('.')
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match='Withdrawal'):
        cli.gateway(arguments(context), context['store'])
    assert not context['calls'] and not events(context['store'], 'pr_withdrawal_attempt')


@pytest.mark.parametrize('active', ['lease', 'queued', 'worker', 'validation', 'recovery'])
def test_active_workers_and_leases_are_preserved(context, active):
    store = context['store']
    with store._connect() as db:
        if active == 'validation':
            db.execute("UPDATE validation_runs SET status='running',finished_at=NULL")
        elif active == 'recovery':
            db.execute('INSERT INTO recovery VALUES(?,?,?,?,?,?)', (REPO, 1, SID, json.dumps({'lease_until': time.time() + 600}), 1, 1))
        else:
            key, value = {'lease': ('lease_until', time.time() + 600), 'queued': ('delivery_state', 'queued'),
                          'worker': ('worker_pid', os.getpid())}[active]
            db.execute('UPDATE pr_watch SET ' + key + '=?', (value,))
    with pytest.raises(ValueError, match='active PR workers'):
        cli.gateway(arguments(context), store)
    assert not context['calls'] and not events(store, 'pr_withdrawal_attempt')


def test_head_move_after_collection_refuses_before_write_and_cannot_replay(context):
    def move(read):
        if read == 2:
            context['pull']['head']['sha'] = 'b' * 40
    context['read_hook'] = move
    with pytest.raises(ValueError, match='identity changed'):
        cli.gateway(arguments(context), context['store'])
    assert not context['calls'] and not events(context['store'], 'pr_withdrawn')
    context['read_hook'] = None
    context['pull']['head']['sha'] = context['snapshot']['head_sha']
    with pytest.raises(ValueError, match='without replay'):
        cli.gateway(arguments(context), context['store'])
    assert not context['calls']


@pytest.mark.parametrize('failure', ['command', 'readback', 'head', 'merged', 'owner', 'source'])
def test_failed_or_ambiguous_close_never_retires_or_claims_completion(context, failure):
    store = context['store']
    before = evidence(store)
    def after_write(argv):
        if failure == 'head':
            context['pull']['head']['sha'] = 'b' * 40
        elif failure == 'merged':
            context['pull']['merged'] = True
        elif failure == 'owner':
            with store._connect() as db:
                db.execute('UPDATE pr_watch SET owner_session=?', (OTHER,))
        elif failure == 'source':
            (context['root'] / 'src.txt').write_text('unpublished change')
        return subprocess.CompletedProcess(argv, 1 if failure == 'command' else 0, stdout='', stderr='failed')
    def readback(read):
        if read == 3 and failure == 'readback':
            raise RuntimeError('Readback unavailable')
    context['write_hook'], context['read_hook'] = after_write, readback
    with pytest.raises((ValueError, RuntimeError)):
        cli.gateway(arguments(context), store)
    assert len(context['calls']) == 1
    assert evidence(store) == before
    assert not events(store, 'pr_withdrawn') and not events(store, 'pr_monitor_retired')
    assert events(store, 'pr_withdrawal_unverified')[0]['completion_verified'] is False
    assert store.prs(SID)


@pytest.mark.parametrize('operation', ['merge', 'close'])
def test_normal_merge_and_close_keep_all_existing_gates(context, monkeypatch, operation):
    async def blocked(*args):
        return [dict(head_sha=context['snapshot']['head_sha'], gates=[dict(gate='MERGE_CONFLICT', status='FAIL')])]
    monkeypatch.setattr(cli, 'inspect', blocked)
    with pytest.raises(ValueError, match='every separate PR gate'):
        cli.gateway(arguments(context, ['pr', operation, URL], reason=None), context['store'])
    assert not context['calls'] and not events(context['store'], 'pr_withdrawal_attempt')
