import json
import subprocess
from unittest.mock import patch

import pytest

from caller_pr_gates import cli
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
                writes=[], reads=[], posted=False, blobs={}, after_post=None,
                before_write=None, post_error=False, response=dict(
                    workflow_run_id=100, run_url=f'https://api.github.com/repos/{REPO}/actions/runs/100',
                    html_url=run['html_url']), empty_response=False, missing_run=False)

    async def validation(*_args):
        return snap

    def github(argv, **kwargs):
        target = argv[2]
        if '--method' in argv:
            assert argv == ['gh', 'api', ENDPOINT, '--method', 'POST', '--input', '-',
                            '--header', 'X-GitHub-Api-Version: 2026-03-10']
            case['writes'].append((argv, kwargs))
            case['posted'] = True
            if case['after_post']:
                case['after_post']()
            return subprocess.CompletedProcess(argv, int(case['post_error']), stdout=(
                '' if case['empty_response'] else json.dumps(case['response'])))
        case['reads'].append(target)
        if '/contents/' in target:
            ref = target.rsplit('=', 1)[1]
            result = dict(path=WORKFLOW, sha=case['blobs'].get(ref, BLOB), type='file')
        elif target == f'repos/{REPO}/actions/runs/100':
            if case['missing_run']:
                raise subprocess.CalledProcessError(1, argv)
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
    'extra_inputs', 'extra_flags', 'caller_header', 'stdin', 'method', 'workflow', 'repository', 'issue',
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
    elif fault == 'caller_header': value.arguments += ['--header', 'X-GitHub-Api-Version: 2022-11-28']
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
                                  'missing_id', 'empty_response', 'api_url', 'html_url', 'boolean_id',
                                  'zero_id', 'missing_run', 'head_after', 'owner_after', 'checkout_after', 'post_error'])
def test_dispatched_run_requires_exact_head_readback_and_never_retries(store, dispatch_case, fault):
    case = dispatch_case
    fields = {'head': 'head_sha', 'branch': 'head_branch', 'workflow': 'path',
              'event': 'event', 'id': 'id', 'url': 'html_url'}
    if fault in fields: case['run'][fields[fault]] = 'different'
    elif fault == 'repository': case['run']['head_repository']['full_name'] = 'fork/Origo'
    elif fault == 'missing_id': case['response'].pop('workflow_run_id')
    elif fault == 'empty_response': case['empty_response'] = True
    elif fault == 'api_url': case['response']['run_url'] = 'https://api.github.com/repos/other/repo/actions/runs/100'
    elif fault == 'html_url': case['response']['html_url'] = 'https://github.com/other/repo/actions/runs/100'
    elif fault == 'boolean_id': case['response']['workflow_run_id'] = True
    elif fault == 'zero_id': case['response']['workflow_run_id'] = 0
    elif fault == 'missing_run': case['missing_run'] = True
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


def test_dispatch_reads_only_its_returned_id_even_with_a_concurrent_run(store, dispatch_case):
    case = dispatch_case
    # A concurrent server dispatch has the same branch/head and appears first.
    # Only this POST's returned ID can prove attribution to runner=github.
    other = dict(case['run'], id=101, html_url=f'https://github.com/{REPO}/actions/runs/101')
    original = cli.github_run

    def github(argv, **kwargs):
        if '/workflows/pr_checks_tests.yml/runs?' in argv[2]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({'workflow_runs': [other]}))
        if argv[2] == f'repos/{REPO}/actions/runs/101':
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(other))
        return original(argv, **kwargs)

    with patch('caller_pr_gates.cli.github_run', github):
        gateway(case['args'], store)
    assert case['reads'][-2:] == [f'repos/{REPO}/actions/runs/100', f'repos/{REPO}/pulls/608']
