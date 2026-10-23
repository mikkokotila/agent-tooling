"""Superseded closure retires empty source, never qualifies a merge or task."""
from copy import deepcopy
import json
import os
import time
import subprocess
from types import SimpleNamespace

import pytest

from caller_pr_gates import cli, superseded, workflow
from test_workflow import local


URL = 'https://github.com/Owner/Repo/pull/1'


class Client:
    def __init__(self, head):
        self.head = head
        self.base = 'b' * 40
        self.tree = 'd' * 40
        self.raw = dict(number=1, state='open', merged=False, draft=False, changed_files=0,
                        head=dict(sha=head, ref='feature', repo=dict(id=7, full_name='Owner/Repo')),
                        base=dict(sha=self.base, ref='main', repo=dict(id=7, full_name='Owner/Repo')))
        self.repository = dict(id=7, full_name='Owner/Repo', default_branch='main')
        self.files = []
        self.comparison = dict(files=[], merge_base_commit=dict(sha='c' * 40, commit=dict(tree=dict(sha=self.tree))))
        self.commit = dict(sha=head, tree=dict(sha=self.tree))
        self.reads = 0
        self.paths = []
        self.base_reads = 0
        self.base_race = False
        self.race = False
        self.closed = False
        self.close_change = {}

    async def get(self, path, **kwargs):
        self.paths.append(path)
        prefix = '/repos/Owner/Repo'
        if path == prefix:
            return deepcopy(self.repository)
        if path == prefix + '/pulls/1':
            self.reads += 1
            value = deepcopy(self.raw)
            if self.race and self.reads > 1:
                value['head']['sha'] = 'e' * 40
            if self.closed:
                value.update(state='closed', **self.close_change)
            return value
        if path.endswith('/files'):
            return deepcopy(self.files)
        if '/compare/' in path:
            return deepcopy(self.comparison)
        if '/git/commits/' in path:
            return deepcopy(self.commit)
        if path.endswith('/heads/main'):
            self.base_reads += 1
            return {'object': {'sha': 'e' * 40 if self.base_race and self.base_reads > 1 else self.base}}
        if path.endswith('/heads/feature'):
            return {'object': {'sha': self.head}}
        raise AssertionError(path)


@pytest.fixture
def prepared(local, monkeypatch):
    store, root, snap, _ = local
    client = Client(snap['head_sha'])
    calls = []
    monkeypatch.setattr(superseded, 'GitHubClient', lambda: client)

    def run(argv, **kwargs):
        with store._connect() as db:
            lease = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=?', ('Owner/Repo', 1)).fetchone()
            assert lease['worker_pid'] == os.getpid() and lease['lease_until'] > time.time()
        calls.append(argv)
        client.closed = True
        return subprocess.CompletedProcess(argv, 0, stdout='closed\n', stderr='')

    monkeypatch.setattr(cli, 'github_run', run)

    async def inspect(*a):
        return [{'gates': [{'gate': 'REVIEW_HANDLED', 'status': 'FAIL'}]}]

    monkeypatch.setattr(cli, 'inspect', inspect)
    args = SimpleNamespace(session='s', client='codex', cwd=str(root), kind=None, slice_id=None,
                           target=None, superseded_reason='The fix already reached main; this PR has no remaining content.',
                           arguments=['pr', 'close', URL])
    return store, root, snap, client, calls, args


def test_zero_diff_close_retains_actual_failure_and_pending_obligations(prepared, local, capsys):
    store, root, snap, client, calls, args = prepared
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, local[3])['plan']
    assert workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'regression')['run']['exit_code'] == 1
    before = store.validation_runs(row['plan_id'])
    snapshot = deepcopy(store.snapshot('Owner/Repo', 1))
    cli.gateway(args, store)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['closed'] is True and receipt['superseded'] is True
    assert receipt['gates_waived'] is False and receipt['completion_verified'] is False
    assert calls == [['gh', 'pr', 'close', URL, '--comment', 'Closed as superseded: ' + args.superseded_reason]]
    assert store.validation_runs(row['plan_id']) == before
    assert store.snapshot('Owner/Repo', 1) == snapshot
    assert not store.prs('s')
    assert workflow.task_progress(store, 's')[0]['status'] != 'PASS'
    with store._connect() as db:
        events = [row[0] for row in db.execute('SELECT event FROM audit')]
    assert 'pr_superseded_close_attempt' in events and 'pr_superseded_closed' in events
    assert events.index('pr_superseded_close_attempt') < events.index('pr_superseded_closed')


@pytest.mark.parametrize('change', [
    'missing_reason', 'blank_reason', 'long_reason', 'merge', 'extra_flag', 'issue', 'target',
    'session', 'client', 'cwd', 'dirty', 'head', 'unknown_files', 'nonempty_count',
    'nonempty_files', 'compare_files', 'different_tree', 'foreign_repo', 'foreign_repo_id',
    'wrong_default', 'default_head', 'draft', 'already_closed', 'already_merged', 'head_race',
    'missing_merge_base', 'missing_tree', 'live_base_race',
])
def test_superseded_close_refuses_unproven_or_foreign_source(prepared, tmp_path, change):
    store, root, _, client, calls, args = prepared
    if change == 'missing_reason': args.superseded_reason = None
    elif change == 'blank_reason': args.superseded_reason = ' '
    elif change == 'long_reason': args.superseded_reason = 'x' * 2001
    elif change == 'merge': args.arguments[1] = 'merge'
    elif change == 'extra_flag': args.arguments += ['--delete-branch']
    elif change == 'issue': args.arguments[2] = URL.replace('/pull/', '/issues/')
    elif change == 'target': args.target = URL[:-1] + '2'
    elif change == 'session': args.session = 'another'
    elif change == 'client': args.client = 'claude'
    elif change == 'cwd': args.cwd = str(tmp_path)
    elif change == 'dirty': (root / 'src.txt').write_text('unpublished')
    elif change == 'head': client.raw['head']['sha'] = 'e' * 40
    elif change == 'unknown_files': client.raw.pop('changed_files')
    elif change == 'nonempty_count': client.raw['changed_files'] = 1
    elif change == 'nonempty_files': client.files = [{'filename': 'src.txt'}]
    elif change == 'compare_files': client.comparison['files'] = [{'filename': 'src.txt'}]
    elif change == 'different_tree': client.commit['tree']['sha'] = 'e' * 40
    elif change == 'foreign_repo': client.raw['head']['repo']['full_name'] = 'Other/Repo'
    elif change == 'foreign_repo_id': client.raw['head']['repo']['id'] = 8
    elif change == 'wrong_default': client.repository['default_branch'] = 'other'
    elif change == 'default_head': client.raw['head']['ref'] = 'main'
    elif change == 'draft': client.raw['draft'] = True
    elif change == 'already_closed': client.raw['state'] = 'closed'
    elif change == 'already_merged': client.raw['merged'] = True
    elif change == 'head_race': client.race = True
    elif change == 'missing_merge_base': client.comparison['merge_base_commit'].pop('sha')
    elif change == 'missing_tree': client.commit.pop('tree')
    elif change == 'live_base_race': client.base_race = True
    with pytest.raises((ValueError, RuntimeError)):
        cli.gateway(args, store)
    assert calls == []
    assert store.prs('s')


def test_failed_upstream_close_never_retires_or_reports_success(prepared, monkeypatch):
    store, _, _, _, _, args = prepared
    monkeypatch.setattr(cli, 'github_run', lambda *a, **kw: subprocess.CompletedProcess(a, 1))
    with pytest.raises(RuntimeError, match='close failed'):
        cli.gateway(args, store)
    assert store.prs('s')
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='pr_superseded_closed'").fetchone()[0] == 0


def test_failed_readback_is_explicitly_unverified_and_does_not_retire(prepared):
    store, _, _, client, calls, args = prepared
    client.close_change = {'changed_files': 1}
    with pytest.raises(ValueError):
        cli.gateway(args, store)
    assert len(calls) == 1 and store.prs('s')
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='pr_superseded_close_unverified'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='pr_superseded_closed'").fetchone()[0] == 0


@pytest.mark.parametrize('operation', ['merge', 'close'])
def test_normal_merge_and_close_gates_are_unchanged(prepared, monkeypatch, operation):
    store, _, _, _, calls, args = prepared
    args.superseded_reason = None
    args.arguments[1] = operation

    async def inspect(*a):
        return [{'gates': [{'gate': 'REVIEW_HANDLED', 'status': 'FAIL'}]}]

    monkeypatch.setattr(cli, 'inspect', inspect)
    with pytest.raises(ValueError, match='every separate PR gate'):
        cli.gateway(args, store)
    assert calls == []


def test_lagging_pr_base_sha_uses_the_live_default_branch_without_sync(prepared, capsys):
    store, _, _, client, _, args = prepared
    client.raw['base']['sha'] = 'a' * 40
    cli.gateway(args, store)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['base_sha'] == client.base
    assert f"/repos/Owner/Repo/compare/{client.base}...{client.head}" in client.paths
    assert all('a' * 40 not in path for path in client.paths)


def test_registration_receipt_cannot_close_another_canonical_owners_pr(prepared):
    store, root, _, _, calls, args = prepared
    store.record_session('claimant', 'codex', str(root))
    store.register('claimant', 'codex', 'Owner/Repo', 1, str(root))
    assert store.prs('claimant')
    args.session = 'claimant'
    with pytest.raises(ValueError, match='canonical PR owner'):
        cli.gateway(args, store)
    assert calls == []
    with store._connect() as db:
        assert db.execute('SELECT owner_session FROM pr_watch').fetchone()[0] == 's'


def test_canonical_owner_move_during_proof_refuses_before_mutation(prepared, monkeypatch):
    store, root, _, client, calls, args = prepared
    store.record_session('next', 'codex', str(root))
    original = client.get

    async def moved(path, **kwargs):
        value = await original(path, **kwargs)
        if path.endswith('/pulls/1') and client.reads == 2:
            with store._connect() as db:
                db.execute("UPDATE pr_watch SET owner_session='next'")
        return value

    monkeypatch.setattr(client, 'get', moved)
    with pytest.raises(ValueError, match='canonical'):
        cli.gateway(args, store)
    assert calls == []


def test_existing_owner_lease_is_never_overwritten(prepared):
    store, _, _, _, calls, args = prepared
    with store._connect() as db:
        db.execute('UPDATE pr_watch SET lease_until=?', (time.time() + 60,))
    with pytest.raises(ValueError, match='active owner work'):
        cli.gateway(args, store)
    assert calls == []
