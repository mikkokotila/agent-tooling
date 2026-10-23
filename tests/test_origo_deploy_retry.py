"""Deliberate Git/HTTP protocol controls; no deployment or market data."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import subprocess
from threading import Event
from types import SimpleNamespace

import pytest

from caller_pr_gates.cli import gateway
from caller_pr_gates.monitor import retire
from caller_pr_gates import origo_deploy_retry as target
from caller_pr_gates.origo_secret_retirement import FIXED
from caller_pr_gates.store import Store


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class Remote:
    def __init__(self, revision, workflow_blob, project_blob):
        self.calls, self.writes = [], []
        self.code, self.fault = 201, None
        self.before_http, self.after_http = lambda: None, lambda: None
        branch = {'ref': 'main', 'sha': revision,
                  'repo': {'id': 17, 'full_name': target.REPOSITORY}}
        self.run = {'id': target.RUN, 'head_sha': revision, 'head_branch': 'main', 'event': 'push',
                    'workflow_id': target.WORKFLOW_ID, 'path': target.WORKFLOW,
                    'repository': {'id': 17, 'full_name': target.REPOSITORY},
                    'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}
        self.others, self.total_delta = [], 0
        self.values = {
            'repos/Vaquum/Origo/pulls/608': {'number': 608, 'state': 'closed', 'merged': True,
                'draft': False, 'head': {**deepcopy(branch), 'sha': target.ORIGIN_MERGE}, 'base': deepcopy(branch), 'merge_commit_sha': target.ORIGIN_MERGE},
            'repos/Vaquum/Origo/git/ref/heads/main': {'object': {'sha': revision}},
            'repos/Vaquum/Origo/actions/workflows/deploy_on_merge.yml': {
                'id': target.WORKFLOW_ID, 'path': target.WORKFLOW, 'name': 'Deploy On Merge', 'state': 'active'},
            f'repos/Vaquum/Origo/compare/{target.ORIGIN_MERGE}...{revision}': {
                'status': 'ahead', 'merge_base_commit': {'sha': target.ORIGIN_MERGE}},
            **{f'repos/Vaquum/Origo/contents/{path}?ref={revision}': {
                'path': path, 'type': 'file', 'sha': blob}
               for path, blob in {**target.BOUNDARIES, 'pyproject.toml': project_blob}.items()},
            f'repos/Vaquum/Origo/actions/runs/{target.RUN}/attempts/1/jobs?per_page=100': {
                'total_count': 1, 'jobs': [{'id': target.JOB, 'run_id': target.RUN,
                    'name': 'Build Images And Deploy', 'status': 'completed', 'conclusion': 'failure',
                    'steps': [{'name': name, 'number': i + 1, 'status': 'completed',
                               'conclusion': 'failure' if i == 12 else 'success'}
                              for i, name in enumerate(target.PREFIX)]}]},
        }
        self.log = ('2026-10-23T08:55:31Z echo \'post-up: deployment verified\'\n'
                    '2026-10-23T08:55:31Z   ORIGO_NATIVE_JOBS_ONLY: true\n'
                    f'2026-10-23T09:21:58.4363392Z {target.FAILURE}\n')

    async def logs(self, path):
        assert path == f'/repos/Vaquum/Origo/actions/jobs/{target.JOB}/logs'
        return self.log

    def http(self, method, url, **kwargs):
        self.before_http()
        self.writes.append((method, url, kwargs['content']))
        if self.code == 201:
            self.run.update(run_attempt=2, status='queued', conclusion=None)
        self.after_http()
        if self.fault == 'timeout':
            raise subprocess.TimeoutExpired('deliberate transport-uncertainty control', 30)
        if self.fault == 'interrupt':
            raise KeyboardInterrupt('deliberate interrupted mutation control')
        return SimpleNamespace(status_code=self.code)

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert argv[:2] == ['gh', 'api'] and argv[3] == '--include'
        endpoint = argv[2]
        if argv[4:]:
            assert argv[4:] == ['--method', 'POST'] and endpoint == target.ENDPOINT
            upstream = kwargs['upstream']
            response = upstream('POST', 'https://api.github.com/' + target.ENDPOINT,
                                content=b'', follow_redirects=False, timeout=15)
            if self.fault in {'duplicate', 'redirect', 'body'}:
                upstream('POST', 'https://api.github.com/' + target.ENDPOINT + ('/other' if self.fault == 'redirect' else ''),
                         content=b'changed' if self.fault == 'body' else b'', follow_redirects=False, timeout=15)
            if self.fault == 'malformed':
                return subprocess.CompletedProcess(argv, 0, 'deliberate malformed response', '')
            code, value = response.status_code, None
        elif '/runs?per_page=100&page=' in endpoint:
            items = [self.run, *self.others]
            page = int(endpoint.rpartition('=')[2])
            code, value = 200, {'total_count': len(items) + self.total_delta,
                               'workflow_runs': items[(page - 1) * 100:page * 100]}
        elif endpoint == f'repos/Vaquum/Origo/actions/runs/{target.RUN}':
            code, value = 200, self.run
        else:
            code, value = 200, self.values[endpoint]
        return subprocess.CompletedProcess(argv, int(code >= 400),
            f'HTTP/2.0 {code}\n\n' + (json.dumps(value) if value is not None else ''),
            'private-child-output-control')


@pytest.fixture
def case(tmp_path, monkeypatch):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    for key, value in (('user.name', 'Deliberate protocol fixture'),
                       ('user.email', 'protocol@example.invalid'), ('commit.gpgsign', 'false')):
        git(tmp_path, 'config', key, value)
    git(tmp_path, 'remote', 'add', 'origin', 'https://github.com/Vaquum/Origo.git')
    (tmp_path / '.gitignore').write_text('state/\n')
    for name in FIXED | set(target.BOUNDARIES) | {'origo/source.py'}:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Deliberate immutable source protocol control.\n')
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion="3.34.56"\n[tool.origo.release]\nnative_jobs_only_version="3.34.56"\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'test: establish immutable deployment protocol control')
    origin = git(tmp_path, 'rev-parse', 'HEAD')
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion="3.34.57"\n[tool.origo.release]\nnative_jobs_only_version="3.34.57"\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'test: establish explicit descendant release control')
    revision = git(tmp_path, 'rev-parse', 'HEAD')
    workflow = git(tmp_path, 'rev-parse', f'HEAD:{target.WORKFLOW}')
    project = git(tmp_path, 'rev-parse', 'HEAD:pyproject.toml')
    for name, value in (('ORIGIN_MERGE', origin), ('MERGE', revision), ('WORKFLOW_BLOB', workflow), ('PROJECT_BLOB', project),
                        ('BOUNDARIES', {path: git(tmp_path, 'rev-parse', f'{origin}:{path}') for path in target.BOUNDARIES})):
        monkeypatch.setattr(target, name, value)
    store = Store(tmp_path / 'state')
    store.register('s', 'codex', target.REPOSITORY, 608, str(tmp_path))
    retire(store, target.REPOSITORY, 608, {'state': 'closed', 'merged': True,
                                         'head': {'sha': origin, 'ref': 'topic'}})
    store.record_scope('s', {'agreed_scope': 'Explicit deployment continuation protocol control.',
        'approval_evidence': 'Original provider retirement; exact latest descendant only.',
        'acceptance_criteria': ['One exact retry, no source ownership transfer.'],
        'slice_count': 0, 'slice_ids': [], 'approved_at': 1,
        target.GRANT: {'repository': target.REPOSITORY, 'authorizing_pr': target.PR,
            'source_merge': origin, 'deployment_sha': revision, 'run_id': target.RUN,
            'version': target.VERSION, 'approval_evidence': 'Explicit continuation protocol control.'}},
        work_item=target.WORK_ITEM)
    args = SimpleNamespace(session='s', client='codex', cwd=str(tmp_path), target=target.PR,
                           arguments=['api', target.ENDPOINT, '--method', 'POST'])
    remote = Remote(revision, workflow, project)
    monkeypatch.setattr('caller_pr_gates.cli.github_run', remote)
    monkeypatch.setattr(target.httpx, 'request', remote.http)
    monkeypatch.setattr('caller_pr_gates.transport.GitHubClient', lambda: remote)
    monkeypatch.setattr(target.time, 'sleep', lambda _seconds: None)
    return store, args, remote, revision


def receipts(store, kind):
    with store._connect() as db:
        return [json.loads(row[0]) for row in db.execute('SELECT fields FROM audit WHERE event=?', (target.EVENT + kind,))]


def test_only_exact_native_push_retry_is_requested_and_merged_owner_stays_retired(case):
    store, args, remote, _revision = case
    before = store.closed_review_owner('s', target.REPOSITORY, 608, args.cwd)
    receipt = gateway(args, store)
    assert receipt['status'] == 'retry_requested_verified' and receipt['deployment_success'] is False
    assert receipt['from_attempt'] == 1 and receipt['to_attempt'] == 2 and receipt['write_requests'] == 1
    assert remote.writes == [('POST', 'https://api.github.com/' + target.ENDPOINT, b'')]
    assert receipts(store, 'verified') == [receipt] and len(receipts(store, 'attempt')) == 1
    assert 'private-child-output-control' not in json.dumps(receipt)
    assert store.prs('s') == [] and store.closed_review_owner('s', target.REPOSITORY, 608, args.cwd) == before
    # Deliberate stale API-read control: even an apparent original failure cannot replay the write.
    remote.run.update(run_attempt=1, status='completed', conclusion='failure')
    with pytest.raises(ValueError, match='already attempted'):
        gateway(args, store)
    assert len(remote.writes) == 1


@pytest.mark.parametrize('fault', ['target', 'method', 'body', 'client', 'unowned', 'cwd', 'origin',
    'main', 'merge', 'head', 'workflow', 'project', 'run-sha', 'event', 'attempt', 'passed',
    'job', 'step', 'earlier-failure', 'log', 'not-native', 'executed-success', 'newer', 'active',
    'truncated', 'too-many', 'duplicate', 'ancestry', 'grant', 'superseded'])
def test_changed_or_unproven_prerequisite_refuses_before_request(case, fault, tmp_path, monkeypatch):
    store, args, remote, revision = case
    if fault == 'target': args.target = 'https://github.com/Vaquum/Origo/pull/609'
    elif fault == 'method': args.arguments[-1] = 'DELETE'
    elif fault == 'body': args.arguments += ['--input', '/tmp/deliberate-unadmitted.json']
    elif fault == 'client': args.client = 'claude'
    elif fault == 'unowned': args.session = 'other'
    elif fault == 'cwd': args.cwd = str(tmp_path.parent)
    elif fault == 'origin': git(tmp_path, 'remote', 'set-url', 'origin', 'https://github.com/other/repo.git')
    elif fault == 'grant': monkeypatch.setattr(target, 'GRANT', 'deliberate_unapproved_grant')
    elif fault == 'superseded': args.arguments[1] = 'repos/Vaquum/Origo/actions/runs/39927845492/rerun'
    elif fault == 'ancestry': remote.values[f'repos/Vaquum/Origo/compare/{target.ORIGIN_MERGE}...{revision}']['status'] = 'diverged'
    elif fault == 'main': remote.values['repos/Vaquum/Origo/git/ref/heads/main']['object']['sha'] = 'a' * 40
    elif fault == 'merge': remote.values['repos/Vaquum/Origo/pulls/608']['merge_commit_sha'] = 'a' * 40
    elif fault == 'head': remote.values['repos/Vaquum/Origo/pulls/608']['head']['sha'] = 'a' * 40
    elif fault in {'workflow', 'project'}:
        path = target.WORKFLOW if fault == 'workflow' else 'pyproject.toml'
        remote.values[f'repos/Vaquum/Origo/contents/{path}?ref={revision}']['sha'] = 'a' * 40
    elif fault == 'run-sha': remote.run['head_sha'] = 'a' * 40
    elif fault == 'event': remote.run['event'] = 'workflow_dispatch'
    elif fault == 'attempt': remote.run['run_attempt'] = 2
    elif fault == 'passed': remote.run['conclusion'] = 'success'
    elif fault in {'job', 'step', 'earlier-failure'}:
        job = remote.values[f'repos/Vaquum/Origo/actions/runs/{target.RUN}/attempts/1/jobs?per_page=100']['jobs'][0]
        if fault == 'job': job['id'] += 1
        elif fault == 'step': job['steps'][-1]['conclusion'] = 'success'
        else: job['steps'][1]['conclusion'] = 'failure'
    elif fault == 'log': remote.log = 'deliberate unrelated failure control'
    elif fault == 'not-native': remote.log = remote.log.replace('ORIGO_NATIVE_JOBS_ONLY: true', 'ORIGO_NATIVE_JOBS_ONLY: false')
    elif fault == 'executed-success': remote.log += '2026-10-23T08:55:59Z post-up: deployment verified\n'
    elif fault == 'newer': remote.others = [{'id': target.RUN + 1, 'status': 'completed'}]
    elif fault == 'active': remote.others = [{'id': target.RUN - 1, 'status': 'in_progress'}]
    elif fault == 'truncated': remote.total_delta = 1
    elif fault == 'too-many': remote.total_delta = 200
    elif fault == 'duplicate': remote.others = [deepcopy(remote.run)]
    with pytest.raises(ValueError):
        gateway(args, store)
    assert remote.writes == [] and receipts(store, 'attempt') == []


def test_complete_two_page_inventory_refuses_hidden_older_active_work(case):
    store, args, remote, _revision = case
    remote.others = [{'id': target.RUN - i, 'status': 'completed'} for i in range(1, 198)]
    remote.others[-1]['status'] = 'queued'
    with pytest.raises(ValueError, match='active'):
        gateway(args, store)
    assert remote.writes == [] and any('page=2' in call[2] for call in remote.calls)
    remote.others[-1]['status'] = 'completed'
    assert gateway(args, store)['status'] == 'retry_requested_verified'


@pytest.mark.parametrize('fault', ['timeout', 'interrupt', 'duplicate', 'redirect', 'body', 'malformed',
                                    'http-error', 'main-race', 'owner-race', 'wrong-attempt', 'no-advance'])
def test_http_or_readback_uncertainty_keeps_one_reserved_attempt(case, fault):
    store, args, remote, _revision = case
    remote.fault = fault
    if fault == 'http-error': remote.code = 403
    elif fault == 'main-race': remote.after_http = lambda: remote.values['repos/Vaquum/Origo/git/ref/heads/main']['object'].update(sha='b' * 40)
    elif fault == 'owner-race': remote.after_http = lambda: setattr(args, 'client', 'claude')
    elif fault == 'wrong-attempt': remote.after_http = lambda: remote.run.update(run_attempt=3)
    elif fault == 'no-advance': remote.after_http = lambda: remote.run.update(run_attempt=1)
    with pytest.raises(RuntimeError, match='unverified'):
        gateway(args, store)
    assert len(remote.writes) == 1 and len(receipts(store, 'attempt')) == 1 and len(receipts(store, 'incomplete')) == 1
    assert receipts(store, 'verified') == []


def test_parallel_callers_cannot_duplicate_the_http_request(case):
    store, args, remote, _revision = case
    entered, release = Event(), Event()
    def blocked():
        entered.set()
        assert release.wait(10)
    remote.before_http = blocked
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(gateway, args, store)
        assert entered.wait(10)
        try:
            with pytest.raises(ValueError, match='already attempted'):
                gateway(args, store)
        finally:
            release.set()
        assert first.result(timeout=10)['status'] == 'retry_requested_verified'
    assert len(remote.writes) == 1
