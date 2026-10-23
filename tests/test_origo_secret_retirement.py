"""Isolated source/HTTP protocol controls; no credential value or live request."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from threading import Event
from types import SimpleNamespace

import pytest

from caller_pr_gates import origo_deploy_retry as continuation
from caller_pr_gates.cli import gateway
from caller_pr_gates.monitor import retire
from caller_pr_gates.origo_secret_retirement import ENDPOINT, EVENT, FIXED, GRANT, PR, WORK_ITEM, source_proof
from caller_pr_gates.store import Store

REPO = 'Vaquum/Origo'
STAMP = '2026-10-23T00:00:00Z'


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


class Remote:
    def __init__(self, revision):
        self.calls, self.writes = [], []
        self.present, self.status, self.fault = True, 204, None
        self.before_http, self.after_http = lambda: None, lambda: None
        branch = {'ref': 'main', 'sha': revision, 'repo': {'id': 17, 'full_name': REPO}}
        self.values = {
            'repos/Vaquum/Origo/pulls/608': {'number': 608, 'url': 'https://api.github.com/repos/Vaquum/Origo/pulls/608',
                'state': 'closed', 'merged': True, 'draft': False, 'merged_at': STAMP,
                'head': deepcopy(branch), 'base': deepcopy(branch), 'merge_commit_sha': revision},
            'repos/Vaquum/Origo/git/ref/heads/main': {'object': {'sha': revision}},
            'repos/Vaquum/Origo/actions/workflows/deploy_on_merge.yml': {
                'id': 23, 'name': 'Deploy On Merge', 'path': '.github/workflows/deploy_on_merge.yml', 'state': 'active'},
            'repos/Vaquum/Origo/actions/workflows/deploy_on_merge.yml/runs'
                f'?branch=main&event=push&head_sha={revision}&per_page=100': {
                    'total_count': 1, 'workflow_runs': [{'id': 31, 'head_sha': revision}]},
            'repos/Vaquum/Origo/actions/runs/31': {'id': 31, 'head_sha': revision, 'head_branch': 'main',
                'event': 'push', 'workflow_id': 23, 'path': '.github/workflows/deploy_on_merge.yml',
                'repository': {'id': 17, 'full_name': REPO}, 'status': 'completed', 'conclusion': 'success', 'run_attempt': 1},
            'repos/Vaquum/Origo/actions/runs/31/attempts/1/jobs?per_page=100': {'total_count': 1, 'jobs': [{
                'id': 41, 'run_id': 31, 'name': 'Build Images And Deploy', 'status': 'completed',
                'conclusion': 'success', 'completed_at': STAMP, 'steps': [{
                    'number': 12, 'name': 'Deploy updated images', 'status': 'completed',
                    'conclusion': 'success', 'completed_at': STAMP}]}]},
        }

    def http(self, method, url, **kwargs):
        self.before_http()
        self.writes.append((method, url, kwargs['content']))
        if self.status == 204:
            self.present = False
        self.after_http()
        if self.fault == 'timeout':
            raise subprocess.TimeoutExpired('deliberate HTTP outcome control', 90)
        if self.fault == 'interrupt':
            raise KeyboardInterrupt('deliberate interrupted outcome control')
        return SimpleNamespace(status_code=self.status)

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert argv[:2] == ['gh', 'api'] and argv[3] == '--include'
        endpoint = argv[2]
        if argv[4:]:
            assert argv[4:] == ['--method', 'DELETE'] and endpoint == ENDPOINT
            upstream = kwargs['upstream']
            response = upstream('DELETE', 'https://api.github.com/' + endpoint,
                                content=b'', follow_redirects=False, timeout=15)
            if self.fault in {'duplicate', 'redirect'}:
                upstream('DELETE', 'https://api.github.com/' + (endpoint if self.fault == 'duplicate' else endpoint + '/other'),
                         content=b'', follow_redirects=False, timeout=15)
            if self.fault == 'malformed-response':
                return subprocess.CompletedProcess(argv, 0, 'deliberate malformed HTTP response', '')
            code, value = response.status_code, None
        elif endpoint == ENDPOINT:
            code = 200 if self.present else 404
            value = ({'name': 'CRYPTOHFTDATA_API_KEY', 'created_at': STAMP, 'updated_at': STAMP,
                      'unexpected': 'deliberate-response-field-control'} if self.present else {'message': 'Not Found'})
        else:
            code, value = 200, self.values[endpoint]
        body = json.dumps(value) if value is not None else ''
        return subprocess.CompletedProcess(argv, int(code >= 400), f'HTTP/2.0 {code}\n\n{body}', 'private-child-output-control')


@pytest.fixture
def case(tmp_path, monkeypatch):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    git(tmp_path, 'config', 'user.name', 'Deliberate protocol fixture')
    git(tmp_path, 'config', 'user.email', 'protocol@example.invalid')
    git(tmp_path, 'config', 'commit.gpgsign', 'false')
    git(tmp_path, 'remote', 'add', 'origin', 'https://github.com/Vaquum/Origo.git')
    (tmp_path / '.gitignore').write_text('state/\ndifferent-state/\n')
    for name in FIXED | set(continuation.BOUNDARIES) | {'origo/source.py'}:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Explicit source-presence protocol control; no deployed application.\n')
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion="3.34.56"\n[tool.origo.release]\nnative_jobs_only_version="3.34.56"\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'test: establish immutable source protocol control')
    origin = git(tmp_path, 'rev-parse', 'HEAD')
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion="3.34.57"\n[tool.origo.release]\nnative_jobs_only_version="3.34.57"\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'test: establish exact deployment descendant control')
    revision = git(tmp_path, 'rev-parse', 'HEAD')
    boundaries = {path: git(tmp_path, 'rev-parse', f'{origin}:{path}') for path in continuation.BOUNDARIES}
    for name, value in (('ORIGIN_MERGE', origin), ('MERGE', revision), ('RUN', 31),
                        ('WORKFLOW_ID', 23), ('BOUNDARIES', boundaries),
                        ('PROJECT_BLOB', git(tmp_path, 'rev-parse', 'HEAD:pyproject.toml'))):
        monkeypatch.setattr(continuation, name, value)
    store = Store(tmp_path / 'state')
    store.register('s', 'codex', REPO, 608, str(tmp_path))
    scope = {'agreed_scope': 'Retire only the obsolete CryptoHFTData Actions secret after verified deployment.',
        'approval_evidence': 'Keep all live feeds; remove only CryptoHFTData and its imported data.',
        'acceptance_criteria': ['One exact deletion with successful deployment and absent metadata.'],
        'slice_count': 0, 'slice_ids': [], 'approved_at': 1,
        GRANT: {'repository': REPO, 'authorizing_pr': PR, 'approval_evidence': 'Original human provider-removal authorization.'}}
    store.record_scope('s', scope, work_item=WORK_ITEM)
    store.record_scope('s', {**scope, continuation.GRANT: {
        'repository': REPO, 'authorizing_pr': PR, 'source_merge': origin, 'deployment_sha': revision,
        'run_id': 31, 'version': continuation.VERSION, 'approval_evidence': 'Exact latest continuation control.'}},
        work_item=continuation.WORK_ITEM)
    retire(store, REPO, 608, {'state': 'closed', 'merged': True, 'head': {'sha': origin, 'ref': 'topic'}})
    args = SimpleNamespace(session='s', client='codex', cwd=str(tmp_path), target=PR,
                           arguments=['api', ENDPOINT, '--method', 'DELETE'])
    remote = Remote(revision)
    remote.values['repos/Vaquum/Origo/pulls/608']['merge_commit_sha'] = origin
    remote.values['repos/Vaquum/Origo/pulls/608']['head']['sha'] = origin
    remote.values[f'repos/Vaquum/Origo/compare/{origin}...{revision}'] = {
        'status': 'ahead', 'merge_base_commit': {'sha': origin}}
    remote.values.update({f'repos/Vaquum/Origo/contents/{path}?ref={revision}': {
        'path': path, 'type': 'file', 'sha': blob}
        for path, blob in {**boundaries, 'pyproject.toml': continuation.PROJECT_BLOB}.items()})
    monkeypatch.setattr('caller_pr_gates.cli.github_run', remote)
    monkeypatch.setattr('caller_pr_gates.origo_secret_retirement.httpx.request', remote.http)
    return store, args, remote, revision, scope


def receipts(store, kind):
    with store._connect() as db:
        return [json.loads(row[0]) for row in db.execute('SELECT fields FROM audit WHERE event=?', (EVENT + kind,))]


def test_merged_owner_deletes_only_exact_secret_after_deployment_and_preserves_retirement(case):
    store, args, remote, _revision, _scope = case
    before = store.closed_review_owner('s', REPO, 608, args.cwd)
    receipt = gateway(args, store)
    assert receipt['stage'] == 'verified' and receipt['delete_http_status'] == 204
    assert receipt['after_secret_http_status'] == 404 and receipt['write_requests'] == 1
    assert remote.writes == [('DELETE', 'https://api.github.com/' + ENDPOINT, b'')]
    assert receipts(store, 'verified') == [receipt] and len(receipts(store, 'attempt')) == 1
    assert 'deliberate-response-field-control' not in json.dumps(receipt)
    assert 'private-child-output-control' not in json.dumps(receipt)
    assert store.prs('s') == [] and store.closed_review_owner('s', REPO, 608, args.cwd) == before
    with pytest.raises(ValueError):
        gateway(args, store)
    assert len(remote.writes) == 1


@pytest.mark.parametrize('fault', ['other-key', 'other-repo', 'other-target', 'body', 'header', 'method',
                                 'owner', 'client', 'checkout', 'grant'])
def test_unrelated_or_unowned_secret_operation_never_reaches_http(case, fault, tmp_path):
    store, args, remote, _revision, scope = case
    if fault == 'other-key': args.arguments[1] = ENDPOINT.replace('CRYPTOHFTDATA_API_KEY', 'OTHER')
    elif fault == 'other-repo': args.arguments[1] = ENDPOINT.replace('Vaquum/Origo', 'other/repository')
    elif fault == 'other-target': args.target = PR.replace('608', '609')
    elif fault == 'body': args.arguments += ['--input', 'request.json']
    elif fault == 'header': args.arguments += ['--header', 'deliberate-header-control']
    elif fault == 'method': args.arguments[-1] = 'PUT'
    elif fault == 'owner': args.session = 'another'
    elif fault == 'client': args.client = 'claude'
    elif fault == 'checkout': args.cwd = str(tmp_path / 'other')
    else:
        # Original immutable work item cannot be changed; another selected scope
        # cannot substitute for the exact named retirement grant.
        scope.pop(GRANT)
        other = Store(tmp_path / 'different-state')
        other.record_session('s', 'codex', str(tmp_path))
        other.record_scope('s', scope, work_item=WORK_ITEM)
        store = other
    with pytest.raises((ValueError, RuntimeError)):
        gateway(args, store)
    assert remote.calls == [] and remote.writes == []


@pytest.mark.parametrize('fault', ['unmerged', 'head', 'base', 'run-sha', 'run-failed', 'run-attempt',
                                 'run-id', 'truncated', 'job-skipped', 'step-skipped', 'pre404',
                                 'merge-time', 'job-time', 'step-time', 'superseded-run', 'continuation-grant', 'boundary'])
def test_missing_exact_merge_or_actual_deployment_blocks_secret_delete(case, fault, monkeypatch):
    store, args, remote, revision, _scope = case
    pr, run = remote.values['repos/Vaquum/Origo/pulls/608'], remote.values['repos/Vaquum/Origo/actions/runs/31']
    job = remote.values['repos/Vaquum/Origo/actions/runs/31/attempts/1/jobs?per_page=100']['jobs'][0]
    if fault == 'unmerged': pr['merged'] = False
    elif fault == 'head': pr['head']['sha'] = 'a' * 40
    elif fault == 'base': pr['base']['ref'] = 'other'
    elif fault == 'run-sha': run['head_sha'] = 'a' * 40
    elif fault == 'run-failed': run['conclusion'] = 'failure'
    elif fault == 'run-attempt': run['run_attempt'] = 0
    elif fault == 'run-id': run['id'] = 32
    elif fault == 'truncated':
        remote.values['repos/Vaquum/Origo/actions/workflows/deploy_on_merge.yml/runs'
                      f'?branch=main&event=push&head_sha={revision}&per_page=100']['total_count'] = 101
    elif fault == 'job-skipped': job['conclusion'] = 'skipped'
    elif fault == 'step-skipped': job['steps'][0]['conclusion'] = 'skipped'
    elif fault == 'superseded-run':
        remote.values['repos/Vaquum/Origo/actions/workflows/deploy_on_merge.yml/runs'
                      f'?branch=main&event=push&head_sha={revision}&per_page=100']['workflow_runs'][0]['id'] = 30
    elif fault == 'continuation-grant': monkeypatch.setattr(continuation, 'GRANT', 'deliberate_missing_grant')
    elif fault == 'boundary':
        remote.values[f'repos/Vaquum/Origo/contents/{continuation.WORKFLOW}?ref={revision}']['sha'] = 'b' * 40
    elif fault == 'merge-time': pr['merged_at'] = 123
    elif fault == 'job-time': job['completed_at'] = 'deliberate invalid timestamp'
    elif fault == 'step-time': job['steps'][0]['completed_at'] = '2026-10-23T00:00:00+02:00'
    else: remote.present = False
    with pytest.raises(ValueError):
        gateway(args, store)
    assert remote.writes == [] and receipts(store, 'attempt') == []


@pytest.mark.parametrize('fault', ['reference', 'removed-provider-file', 'oversize', 'missing-workflow'])
def test_source_proof_reads_exact_bounded_git_objects_not_mutable_checkout(case, fault):
    _store, args, _remote, revision, _scope = case
    root = Path(args.cwd)
    (root / 'origo/source.py').write_text('CRYPTOHFTDATA_API_KEY\n')
    assert source_proof(args.cwd, revision)['files'] == 5
    if fault == 'removed-provider-file':
        path = root / 'origo/sources/adapters/book_vendor.py'
        path.parent.mkdir(parents=True)
        path.write_text('# Deliberate forbidden-path control.\n')
    elif fault == 'oversize': (root / 'origo/source.py').write_bytes(b'x' * (2 * 1024**2 + 1))
    elif fault == 'missing-workflow': (root / '.github/workflows/deploy_on_merge.yml').unlink()
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'test: establish deliberate invalid source control')
    with pytest.raises(ValueError):
        source_proof(args.cwd, git(root, 'rev-parse', 'HEAD'))


@pytest.mark.parametrize('state', ['unreviewed-descendant', 'unrelated', 'reintroduced'])
def test_current_main_must_retain_merge_and_credential_absence(case, state):
    store, args, remote, revision, _scope = case
    root = Path(args.cwd)
    (root / 'origo/source.py').write_text('CRYPTOHFTDATA_API_KEY\n' if state == 'reintroduced'
                                        else '# Deliberate safe descendant control.\n')
    git(root, 'add', 'origo/source.py')
    git(root, 'commit', '-qm', 'test: establish current main protocol control')
    current = git(root, 'rev-parse', 'HEAD')
    remote.values['repos/Vaquum/Origo/git/ref/heads/main']['object']['sha'] = current
    remote.values[f'repos/Vaquum/Origo/compare/{revision}...{current}'] = {
        'status': 'diverged' if state == 'unrelated' else 'ahead',
        'merge_base_commit': {'sha': 'a' * 40 if state == 'unrelated' else revision},
    }
    with pytest.raises(ValueError):
        gateway(args, store)
    assert remote.writes == []


@pytest.mark.parametrize('fault', ['timeout', 'interrupt', 'duplicate', 'redirect', 'non204', 'post200',
                                 'identity-drift', 'malformed-response'])
def test_uncertain_or_changed_delete_outcome_is_incomplete_and_never_replayed(case, fault, capsys):
    store, args, remote, _revision, _scope = case
    if fault == 'non204': remote.status = 500
    elif fault == 'post200': remote.after_http = lambda: setattr(remote, 'present', True)
    elif fault == 'identity-drift':
        remote.after_http = lambda: remote.values['repos/Vaquum/Origo/pulls/608']['head'].update(sha='b' * 40)
    else:
        remote.fault = fault
        if fault == 'redirect': remote.status = 307
    with pytest.raises(RuntimeError, match='incomplete'):
        gateway(args, store)
    assert len(remote.writes) == 1 and len(receipts(store, 'incomplete')) == 1
    with pytest.raises((ValueError, RuntimeError)):
        gateway(args, store)
    assert len(remote.writes) == 1 and len(receipts(store, 'attempt')) == 1
    assert 'private-child-output-control' not in capsys.readouterr().out


def test_atomic_retirement_reservation_admits_only_one_concurrent_writer(case):
    store, args, remote, _revision, _scope = case
    started, release = Event(), Event()

    def hold_http():
        started.set()
        assert release.wait(10), 'Deliberate first-writer control was not released'

    remote.before_http = hold_http
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(gateway, args, store)
        assert started.wait(10), 'First writer did not reach the isolated HTTP boundary'
        try:
            with pytest.raises(ValueError, match='already attempted'):
                pool.submit(gateway, args, store).result(timeout=10)
        finally:
            release.set()
        assert first.result(timeout=10)['stage'] == 'verified'
    assert len(remote.writes) == 1 and len(receipts(store, 'attempt')) == 1
