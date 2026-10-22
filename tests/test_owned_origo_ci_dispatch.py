import json
import subprocess
from types import SimpleNamespace
import pytest

from caller_pr_gates.cli import gateway
from test_cli import args, store
from test_policy import snapshot

REPO = 'Vaquum/Origo'
WORKFLOW = '.github/workflows/pr_checks_tests.yml'
BLOB = '6bf211fb8bbc65040bd5848040e36f2c2d463134'
ENDPOINT = f'repos/{REPO}/actions/workflows/pr_checks_tests.yml/dispatches'


@pytest.fixture
def dispatch_case(store, tmp_path, monkeypatch):
    store.register('s', 'codex', REPO, 608, str(tmp_path))
    request = tmp_path / 'dispatch.json'
    request.write_text(json.dumps({'ref': 'codex/source-fix', 'inputs': {'runner': 'github'}}))
    value = args(['api', ENDPOINT, '--method', 'POST', '--input', str(request)])
    value.cwd, value.target = str(tmp_path), f'https://github.com/{REPO}/pull/608'
    snap = dict(snapshot(), repository=REPO, number=608,
                head_repository=REPO, base_repository=REPO)
    local = dict(root=str(tmp_path), common=str(tmp_path / '.git'), clean=True,
                 head=snap['head_sha'], branch='codex/source-fix', origin=f'https://github.com/{REPO}.git')
    pull = dict(state='open', merged=False, draft=False,
                head=dict(ref=local['branch'], sha=local['head'], repo=dict(full_name=REPO, id=1)),
                base=dict(ref='main', sha=snap['base_sha'], repo=dict(full_name=REPO, id=1)))
    run = dict(id=100, path=WORKFLOW, head_sha=local['head'], head_branch=local['branch'],
               event='workflow_dispatch', html_url=f'https://github.com/{REPO}/actions/runs/100',
               head_repository=dict(full_name=REPO))
    case = dict(args=value, snapshot=snap, local=local, pull=pull, run=run, request=request,
                writes=[], posted=False, before=[], after=[dict(run)], blobs={}, after_post=None,
                before_write=None, post_error=False)

    async def validation(*_args):
        return snap

    def github(argv, **kwargs):
        target = argv[2]
        if '--method' in argv:
            assert argv == ['gh', 'api', ENDPOINT, '--method', 'POST', '--input', '-']
            case['writes'].append((argv, kwargs))
            case['posted'] = True
            if case['after_post']:
                case['after_post']()
            return subprocess.CompletedProcess(argv, int(case['post_error']), stdout='')
        if '/contents/' in target:
            ref = target.rsplit('=', 1)[1]
            result = dict(path=WORKFLOW, sha=case['blobs'].get(ref, BLOB), type='file')
        elif '/workflows/pr_checks_tests.yml/runs?' in target:
            result = {'workflow_runs': case['after'] if case['posted'] else case['before']}
        elif target == f'repos/{REPO}/actions/runs/100':
            result = case['run']
        elif target == f'repos/{REPO}/pulls/608':
            if not case['posted'] and case['before_write']:
                case['before_write']()
            result = case['pull']
        else:
            raise AssertionError(f'Unexpected GitHub request: {argv}')
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(result))

    monkeypatch.setattr('caller_pr_gates.github.collect_validation', validation)
    monkeypatch.setattr('caller_pr_gates.publication.source', lambda *_args, **_kwargs: dict(case['local']))
    monkeypatch.setattr('caller_pr_gates.cli.github_run', github)
    return case


def test_owned_origo_dispatch_freezes_payload_and_verifies_actual_run(store, dispatch_case, monkeypatch, capsys):
    case = dispatch_case

    async def validation(*_args):
        case['request'].write_text(json.dumps({'ref': 'another-branch', 'inputs': {'runner': 'self-hosted'}}))
        return case['snapshot']

    monkeypatch.setattr('caller_pr_gates.github.collect_validation', validation)
    gateway(case['args'], store)
    assert len(case['writes']) == 1
    assert json.loads(case['writes'][0][1]['input']) == {
        'ref': 'codex/source-fix', 'inputs': {'runner': 'github'},
    }
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['head_sha'] == case['local']['head']
    assert receipt['run_id'] == 100 and receipt['workflow_blob'] == BLOB
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='github_origo_ci_dispatch_verified'").fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM audit WHERE event='ownership_evidence'").fetchone()


@pytest.mark.parametrize('fault', [
    'unowned', 'foreign_receipt', 'wrong_cwd', 'wrong_client', 'closed', 'merged',
    'unknown_merged', 'head_race', 'behind', 'base_race', 'fork', 'dirty', 'origin',
    'local_head', 'local_branch', 'main_workflow', 'head_workflow', 'bad_ref', 'bad_runner',
    'extra_inputs', 'extra_flags', 'stdin', 'method', 'workflow', 'repository', 'issue',
    'pull_head', 'pull_base', 'pull_repo_id', 'owner_race', 'checkout_race',
])
def test_origo_dispatch_refuses_unowned_or_changed_inputs_before_post(store, dispatch_case, fault, tmp_path):
    case, value = dispatch_case, dispatch_case['args']
    snap, local = case['snapshot'], case['local']
    if fault == 'unowned': value.session = 'other'
    elif fault == 'foreign_receipt':
        store.register('other', 'claude', REPO, 608, value.cwd)
        value.session = 'other'
    elif fault == 'wrong_cwd': value.cwd = str(tmp_path / 'other')
    elif fault == 'wrong_client': value.client = 'claude'
    elif fault == 'closed': snap['state'] = 'closed'
    elif fault == 'merged': snap['merged'] = True
    elif fault == 'unknown_merged': snap.pop('merged')
    elif fault == 'head_race': snap['head_sha_after'] = 'b' * 40
    elif fault == 'behind': snap['branch_comparison']['merge_base_sha'] = 'd' * 40
    elif fault == 'base_race': snap['base_sha_after'] = 'b' * 40
    elif fault == 'fork': snap['head_repository'] = 'fork/Origo'
    elif fault == 'dirty': local['clean'] = False
    elif fault == 'origin': local['origin'] = 'https://github.com/other/repo'
    elif fault == 'local_head': local['head'] = 'b' * 40
    elif fault == 'local_branch': local['branch'] = 'other-branch'
    elif fault == 'main_workflow': case['blobs'][snap['base_sha']] = 'b' * 40
    elif fault == 'head_workflow': case['blobs'][snap['head_sha']] = 'b' * 40
    elif fault == 'bad_ref': case['request'].write_text(json.dumps({'ref': 'main', 'inputs': {'runner': 'github'}}))
    elif fault == 'bad_runner': case['request'].write_text(json.dumps({'ref': local['branch'], 'inputs': {'runner': 'self-hosted'}}))
    elif fault == 'extra_inputs': case['request'].write_text(json.dumps({'ref': local['branch'], 'inputs': {'runner': 'github', 'extra': True}}))
    elif fault == 'extra_flags': value.arguments += ['--hostname', 'other.example']
    elif fault == 'stdin': value.arguments[-1] = '-'
    elif fault == 'method': value.arguments[3] = 'PUT'
    elif fault == 'workflow': value.arguments[1] = ENDPOINT.replace('pr_checks_tests.yml', 'deploy.yml')
    elif fault == 'repository': value.arguments[1] = ENDPOINT.replace(REPO, 'other/repo')
    elif fault == 'issue': value.target = f'https://github.com/{REPO}/issues/608'
    elif fault == 'pull_head': case['pull']['head']['sha'] = 'b' * 40
    elif fault == 'pull_base': case['pull']['base']['sha'] = 'b' * 40
    elif fault == 'pull_repo_id': case['pull']['head']['repo']['id'] = 2
    elif fault == 'owner_race':
        def change_owner():
            with store._connect() as db:
                db.execute("UPDATE pr_watch SET owner_session='other' WHERE repository=? AND number=608", (REPO,))
        case['before_write'] = change_owner
    elif fault == 'checkout_race': case['before_write'] = lambda: local.update(clean=False)
    with pytest.raises(ValueError):
        gateway(value, store)
    assert case['writes'] == []


@pytest.mark.parametrize('fault', ['head', 'branch', 'workflow', 'repository', 'event', 'id', 'url',
                                  'ambiguous', 'missing', 'head_after', 'owner_after', 'checkout_after', 'post_error'])
def test_dispatched_run_requires_exact_head_readback_and_never_retries(store, dispatch_case, monkeypatch, fault):
    case = dispatch_case
    fields = {'head': 'head_sha', 'branch': 'head_branch', 'workflow': 'path',
              'event': 'event', 'id': 'id', 'url': 'html_url'}
    if fault in fields: case['run'][fields[fault]] = 'different'
    elif fault == 'repository': case['run']['head_repository']['full_name'] = 'fork/Origo'
    elif fault == 'ambiguous': case['after'].append(dict(case['run'], id=101))
    elif fault == 'missing':
        case['after'] = []
        clock = [0]
        monkeypatch.setattr('caller_pr_gates.cli.time', SimpleNamespace(
            monotonic=lambda: clock[0], sleep=lambda _seconds: clock.__setitem__(0, 30),
        ))
    elif fault == 'head_after': case['after_post'] = lambda: case['pull']['head'].update(sha='b' * 40)
    elif fault == 'owner_after':
        def change_owner():
            with store._connect() as db:
                db.execute("UPDATE pr_watch SET owner_session='other' WHERE repository=? AND number=608", (REPO,))
        case['after_post'] = change_owner
    elif fault == 'checkout_after': case['after_post'] = lambda: case['local'].update(clean=False)
    elif fault == 'post_error': case['post_error'] = True
    with pytest.raises(RuntimeError, match='outcome is unverified'):
        gateway(case['args'], store)
    assert len(case['writes']) == 1
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='github_origo_ci_dispatch_incomplete'").fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM audit WHERE event='github_origo_ci_dispatch_verified'").fetchone()


def test_relative_dispatch_input_is_bound_to_owned_checkout(store, dispatch_case, tmp_path, monkeypatch):
    case = dispatch_case
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'dispatch.json').write_text(json.dumps({'ref': 'main'}))
    monkeypatch.chdir(elsewhere)
    case['args'].arguments[-1] = 'dispatch.json'
    gateway(case['args'], store)
    assert json.loads(case['writes'][0][1]['input'])['ref'] == case['local']['branch']
