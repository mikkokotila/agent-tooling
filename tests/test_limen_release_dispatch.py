"""The release repair cannot dispatch another workflow, source, owner or version."""
from copy import deepcopy
import json
import subprocess

import pytest

from caller_pr_gates import cli, limen_release as release, monitor
from test_cli import args
from test_policy import snapshot, closing_link
from test_workflow import local, git


def version_files(root):
    (root / 'pyproject.toml').write_text('[project]\nversion = "5.46.2"\n')
    (root / 'CITATION.cff').write_text('version: "5.46.2"\n')
    (root / 'CHANGELOG.md').write_text('## [5.46.2] - 2026-10-23\n\n- Complete the book.\n')
    for name in ('limen/yaml/templates/base.yaml', 'docs/examples/factorized_shuffle.yaml', 'docs/examples/logreg_return.yaml'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('metadata:\n  limen_version: "5.46.2"\n')


@pytest.fixture
def case(local, tmp_path, monkeypatch):
    store, root, _, _ = local
    git(root, 'remote', 'set-url', 'origin', 'https://github.com/Vaquum/Limen.git')
    version_files(root)
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Release candidate')
    merged = git(root, 'rev-parse', 'HEAD')
    store.register('s', 'codex', release.REPOSITORY, 1196, str(root))
    snap = dict(snapshot(), repository=release.REPOSITORY, number=1196, state='closed', merged=True,
                merge_commit_sha=merged, head_repository=release.REPOSITORY, base_repository=release.REPOSITORY,
                head_repository_id=1, base_repository_id=1, review_decision='APPROVED',
                submitted_reviews=[dict(state='APPROVED', commit_sha='a' * 40, author_login='reviewer')],
                **closing_link('a' * 40, release.REPOSITORY, 1195, 'CLOSED'))
    monitor.retire(store, release.REPOSITORY, 1196,
                   dict(state='closed', merged=True, head=dict(sha=snap['head_sha'])), checklist=True)
    request = tmp_path / 'dispatch.json'
    request.write_text(json.dumps({'ref': 'main'}))
    value = args(['api', release.ENDPOINT, '--method', 'POST', '--input', str(request)])
    value.target, value.cwd = 'https://github.com/Vaquum/Limen/pull/1196', str(root)
    data = dict(store=store, root=root, request=request, args=value, snapshot=snap, writes=[], reads=[],
                blobs={}, main=merged, prior='v5.46.1', tags=[], runs=[], before_post=None,
                collections=0, on_second_read=None,
                response=dict(workflow_run_id=100, run_url='https://api.github.com/repos/Vaquum/Limen/actions/runs/100',
                              html_url='https://github.com/Vaquum/Limen/actions/runs/100'),
                run=dict(id=100, head_sha=merged, head_branch='main', path=release.WORKFLOW,
                         event='workflow_dispatch', html_url='https://github.com/Vaquum/Limen/actions/runs/100',
                         head_repository=dict(full_name=release.REPOSITORY)))

    async def collect(*_args):
        data['collections'] += 1
        if data['collections'] == 2 and data['on_second_read']:
            data['on_second_read']()
        if data['before_post']:
            action, data['before_post'] = data['before_post'], None
            action()
        return deepcopy(data['snapshot'])

    def run(argv, **kwargs):
        target = argv[2]
        if '--method' in argv:
            assert argv == ['gh', 'api', release.ENDPOINT, '--method', 'POST', '--input', '-',
                            '--header', 'X-GitHub-Api-Version: 2026-03-10']
            data['writes'].append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(data['response']))
        data['reads'].append(target)
        if target.endswith('/git/ref/heads/main'): result = dict(object=dict(sha=data['main']))
        elif target.endswith('/releases/latest'): result = dict(tag_name=data['prior'])
        elif '/git/matching-refs/' in target: result = data['tags']
        elif '/contents/' in target:
            name = target.split('/contents/', 1)[1].split('?')[0]
            result = dict(path=name, type='file', sha=data['blobs'].get(name, release.TRUSTED[name]))
        elif '/runs?' in target: result = dict(workflow_runs=data['runs'])
        elif target.endswith('/actions/runs/100'): result = data['run']
        else: raise AssertionError('Unexpected GitHub read: ' + target)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(result))

    monkeypatch.setattr('caller_pr_gates.github.collect_pr', collect)
    monkeypatch.setattr(cli, 'github_run', run)
    return data


def test_release_dispatch_freezes_request_and_proves_actual_run(case, capsys):
    from caller_pr_gates.policy import evaluate_pr
    assert next(g['status'] for g in evaluate_pr(case['snapshot'], []) if g['gate'] == 'MERGE_READY') == 'WAIT'
    case['before_post'] = lambda: case['request'].write_text('{"ref":"unreviewed"}')
    cli.gateway(case['args'], case['store'])
    assert len(case['writes']) == 1
    assert json.loads(case['writes'][0][1]['input']) == {'ref': 'main'}
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['merge_commit'] == case['main'] and receipt['run_id'] == 100
    assert receipt['candidate_head'] == case['snapshot']['head_sha']
    assert not any(p['repository'] == release.REPOSITORY for p in case['store'].prs('s'))
    with case['store']._connect() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='github_limen_release_dispatch_verified'").fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM audit WHERE event='ownership_evidence'").fetchone()


@pytest.mark.parametrize('fault', ['owner', 'cwd', 'client', 'unmerged', 'head', 'merge', 'fork',
    'repository_id', 'approval', 'self_approval', 'approval_head', 'missing_checks', 'failed_ci',
    'review_finding', 'main', 'prior', 'tag', 'run', 'workflow', 'script', 'dirty', 'origin',
    'payload', 'extra_input', 'header', 'stdin', 'method', 'endpoint', 'issue'])
def test_release_dispatch_refuses_wrong_authority_before_any_write(case, fault, tmp_path):
    value, snap = case['args'], case['snapshot']
    if fault == 'owner': value.session = 'another'
    elif fault == 'cwd': value.cwd = str(tmp_path)
    elif fault == 'client': value.client = 'claude'
    elif fault == 'unmerged': snap['merged'] = False
    elif fault == 'head': snap['head_sha_after'] = 'b' * 40
    elif fault == 'merge': snap['merge_commit_sha'] = 'b' * 40
    elif fault == 'fork': snap['head_repository'] = 'another/Limen'
    elif fault == 'repository_id': snap['head_repository_id'] = 2
    elif fault == 'approval': snap['review_decision'] = 'REVIEW_REQUIRED'
    elif fault == 'self_approval': snap['submitted_reviews'][0]['author_login'] = snap['author_login']
    elif fault == 'approval_head': snap['submitted_reviews'][0]['commit_sha'] = 'b' * 40
    elif fault == 'missing_checks': snap['expected_checks'] = []
    elif fault == 'failed_ci': snap['checks'][0]['conclusion'] = 'failure'
    elif fault == 'review_finding':
        from test_policy import comment
        snap['comments'] = [comment()]
    elif fault == 'main': case['main'] = 'b' * 40
    elif fault == 'prior': case['prior'] = 'v5.47.0'
    elif fault == 'tag': case['tags'] = [dict(ref='refs/tags/v5.46.2')]
    elif fault == 'run': case['runs'] = [dict(head_sha=case['main'])]
    elif fault in ('workflow', 'script'):
        case['blobs'][release.WORKFLOW if fault == 'workflow' else 'scripts/create_release.py'] = 'b' * 40
    elif fault == 'dirty': (case['root'] / 'src.txt').write_text('changed')
    elif fault == 'origin': git(case['root'], 'remote', 'set-url', 'origin', 'https://github.com/other/repo.git')
    elif fault == 'payload': case['request'].write_text('{"ref":"feature"}')
    elif fault == 'extra_input': case['request'].write_text('{"ref":"main","inputs":{"version":"6.0.0"}}')
    elif fault == 'header': value.arguments += ['--header', 'X-GitHub-Api-Version: 2022-11-28']
    elif fault == 'stdin': value.arguments[-1] = '-'
    elif fault == 'method': value.arguments[3] = 'PUT'
    elif fault == 'endpoint': value.arguments[1] = release.ENDPOINT.replace('pr_post_release.yml', 'other.yml')
    elif fault == 'issue': value.target = value.target.replace('/pull/', '/issues/')
    with pytest.raises(ValueError): cli.gateway(value, case['store'])
    assert case['writes'] == []


@pytest.mark.parametrize('fault', ['empty', 'run_id', 'api_url', 'html_url', 'run_head', 'run_path', 'run_event', 'run_repo'])
def test_release_dispatch_never_repeats_an_unverified_post(case, fault):
    if fault == 'empty': case['response'] = {}
    elif fault == 'run_id': case['response']['workflow_run_id'] = True
    elif fault == 'api_url': case['response']['run_url'] = 'https://api.github.com/other'
    elif fault == 'html_url': case['response']['html_url'] = 'https://github.com/other'
    elif fault == 'run_head': case['run']['head_sha'] = 'b' * 40
    elif fault == 'run_path': case['run']['path'] = '.github/workflows/other.yml'
    elif fault == 'run_event': case['run']['event'] = 'push'
    elif fault == 'run_repo': case['run']['head_repository']['full_name'] = 'other/repo'
    with pytest.raises(RuntimeError, match='outcome is unverified'): cli.gateway(case['args'], case['store'])
    assert len(case['writes']) == 1
    with case['store']._connect() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='github_limen_release_dispatch_incomplete'").fetchone()[0] == 1


@pytest.mark.parametrize('race', ['owner', 'checkout', 'main'])
def test_release_dispatch_rechecks_authority_immediately_before_post(case, race):
    def change():
        if race == 'owner':
            with case['store']._connect() as db:
                db.execute("UPDATE pr_watch SET owner_session='another' WHERE repository=? AND number=1196", (release.REPOSITORY,))
        elif race == 'checkout': (case['root'] / 'src.txt').write_text('changed')
        else: case['main'] = 'b' * 40
    case['on_second_read'] = change
    with pytest.raises(ValueError): cli.gateway(case['args'], case['store'])
    assert case['writes'] == []


def test_unverified_release_attempt_blocks_an_immediate_second_post(case):
    case['response'] = {}
    with pytest.raises(RuntimeError, match='outcome is unverified'): cli.gateway(case['args'], case['store'])
    with pytest.raises(ValueError, match='earlier release dispatch attempt'): cli.gateway(case['args'], case['store'])
    assert len(case['writes']) == 1


def test_concurrent_release_requests_make_only_one_post(case):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(cli.gateway, case['args'], case['store']) for _ in range(2)]
        successes, refusals = 0, 0
        for result in results:
            try: result.result(); successes += 1
            except ValueError as error:
                assert 'earlier release dispatch attempt' in str(error)
                refusals += 1
    assert successes == refusals == len(case['writes']) == 1


@pytest.mark.parametrize('file,match', [('pyproject.toml', '5.46.2 release'), ('CITATION.cff', 'citation'),
    ('docs/examples/logreg_return.yaml', 'version differs'), ('limen/yaml/templates/base.yaml', 'version differs'),
    ('CHANGELOG.md', 'changelog')])
def test_release_metadata_rejects_incoherent_or_other_versions(tmp_path, file, match):
    version_files(tmp_path)
    release.metadata(tmp_path)
    path = tmp_path / file
    path.write_text(path.read_text().replace('5.46.2', '5.46.1'))
    with pytest.raises(ValueError, match=match): release.metadata(tmp_path)
