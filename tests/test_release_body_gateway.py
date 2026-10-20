import hashlib
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from caller_pr_gates.cli import gateway, main
from caller_pr_gates.store import Store


REPOSITORY = 'example/project'
ENDPOINT = f'repos/{REPOSITORY}/releases/17'
TARGET = 'https://api.github.com/' + ENDPOINT
BODY = '## Summary\n\nCorrected text: café.\n\n## Traceability\r\n\r\n' + 'a' * 64 + '  artifact.whl\r\n'


@pytest.fixture
def release(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'remote', 'add', 'origin',
                    f'https://github.com/{REPOSITORY}.git'], check=True)
    store = Store(tmp_path / 'state')
    store.record_session('s', 'codex', str(tmp_path))
    path = tmp_path / 'body.json'
    path.write_text(json.dumps({'body': BODY}), encoding='utf-8')
    args = SimpleNamespace(session='s', client='codex', cwd=str(tmp_path), kind=None,
                           slice_id=None, target=TARGET,
                           arguments=['api', ENDPOINT, '--method', 'PATCH', '--input', str(path)])
    return store, args, path


def response(**changes):
    return subprocess.CompletedProcess([], 0, json.dumps(dict(id=17, url=TARGET, body=BODY, **changes)))


def audits(store):
    with store._connect() as db:
        return [json.loads(row['fields']) for row in db.execute(
            "SELECT fields FROM audit WHERE event='github_release_body_updated'")]


def test_documented_cli_updates_only_body_without_pr_adoption(release, monkeypatch, capsys):
    store, args, _ = release
    monkeypatch.setenv('CALLER_GATE_SESSION', 's')
    monkeypatch.setattr('sys.argv', ['caller-pr-gates', 'github', '--session', 's',
        '--client', 'codex', '--cwd', args.cwd, '--target', TARGET, '--', *args.arguments])
    with patch('caller_pr_gates.cli.Store', return_value=store), \
            patch('caller_pr_gates.cli.github_run', return_value=response()) as run:
        main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt == dict(session_id='s', repository=REPOSITORY, release_id=17,
                          url=TARGET, body_sha256=hashlib.sha256(BODY.encode()).hexdigest())
    run.assert_called_once()
    assert run.call_args.args[0] == ['gh', 'api', ENDPOINT, '--method', 'PATCH', '--input', '-']
    assert json.loads(run.call_args.kwargs['input']) == {'body': BODY}
    assert run.call_args.kwargs['cwd'] == args.cwd
    assert audits(store) == [receipt]
    assert store.prs('s') == [] and store.followups('s') == []
    assert store.validation_plans('s') == []


@pytest.mark.parametrize('origin', [
    'https://github.com/Example/Project.git',
    'git@github.com:example/project.git',
    'ssh://git@github.com/example/project.git',
])
def test_existing_github_origins_and_frozen_relative_input(release, tmp_path, monkeypatch, origin):
    store, args, path = release
    subprocess.run(['git', '-C', args.cwd, 'remote', 'set-url', 'origin', origin], check=True)
    other = tmp_path / 'other'
    other.mkdir()
    (other / path.name).write_text('{"body":"wrong file","name":"wrong title"}')
    monkeypatch.chdir(other)
    args.arguments[-1] = path.name

    def write(argv, **kwargs):
        path.write_text('{"body":"changed after admission","name":"wrong title"}')
        assert argv[-2:] == ['--input', '-']
        assert json.loads(kwargs['input']) == {'body': BODY}
        return response()

    with patch('caller_pr_gates.cli.github_run', side_effect=write) as run:
        assert gateway(args, store)['release_id'] == 17
    run.assert_called_once()


@pytest.mark.parametrize('payload', [
    {}, [], 'text', {'body': None}, {'body': True}, {'body': 17}, {'body': []}, {'body': {}},
    *[{'body': BODY, field: 'not admitted'} for field in (
        'name', 'tag_name', 'target_commitish', 'draft', 'prerelease', 'make_latest',
        'discussion_category_name', 'assets', 'id', 'url', 'unexpected')],
])
def test_only_one_string_body_field_is_admitted(release, payload):
    store, args, path = release
    path.write_text(json.dumps(payload))
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='one string body field'):
            gateway(args, store)
    assert audits(store) == []


@pytest.mark.parametrize('invalid', ['missing', 'malformed', 'stdin'])
def test_input_must_be_a_saved_json_file(release, invalid):
    store, args, path = release
    if invalid == 'missing': path.unlink()
    elif invalid == 'malformed': path.write_text('{broken')
    else:
        (path.parent / '-').write_text(path.read_text())
        args.arguments[-1] = '-'
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError): gateway(args, store)


@pytest.mark.parametrize('extra', [
    ['-f', 'name=changed'], ['-F', 'body=@another-file'], ['--field=tag_name=v9'],
    ['--header', 'Authorization: other'], ['--hostname', 'example.invalid'],
    ['--method', 'DELETE'], ['--input', 'another-file'], ['--paginate'],
])
def test_flags_cannot_extend_or_override_the_admitted_write(release, extra):
    store, args, _ = release
    args.arguments += extra
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError): gateway(args, store)


@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE'])
def test_other_release_methods_are_denied(release, method):
    store, args, _ = release
    args.arguments[3] = method
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError): gateway(args, store)


@pytest.mark.parametrize('target', [
    None, 'https://github.com/example/project/pull/17',
    'https://github.com/example/project/releases/tag/v1',
    'https://api.github.com/repos/example/project/releases/18',
    'https://api.github.com/repos/other/project/releases/17',
    TARGET + '?name=changed',
])
def test_target_must_identify_the_exact_release_not_a_surrogate_pr(release, target):
    store, args, _ = release
    args.target = target
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError): gateway(args, store)


@pytest.mark.parametrize('endpoint', [
    'repos/example/project/releases', 'repos/example/project/releases/17/assets',
    'repos/example/project/releases/assets/21', 'repos/example/project/releases/tags/v1',
    'repos/example/project/releases/0', 'repos/example/project/releases/-1',
    'repos/example/project/releases/17?name=changed', 'repos/example/project/releases/17/',
    'repos/example/project/git/refs/tags/v1', 'repos/example/project/pulls/17/merge',
    'repos/example/project/branches/main/protection', 'repos/example/project/actions/secrets/X',
    'repos/example/project', 'repos/../project/releases/17', 'repos/example/../releases/17',
    'https://uploads.github.com/repos/example/project/releases/17/assets?name=file',
])
def test_unrelated_endpoints_and_path_escapes_remain_denied(release, endpoint):
    store, args, _ = release
    args.arguments[1] = endpoint
    args.target = 'https://api.github.com/' + endpoint
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError): gateway(args, store)


@pytest.mark.parametrize('mismatch', ['session', 'client', 'cwd'])
def test_recorded_session_context_is_required(release, mismatch, tmp_path):
    store, args, _ = release
    if mismatch == 'session': args.session = 'unknown'
    elif mismatch == 'client': args.client = 'claude'
    else:
        args.cwd = str(tmp_path / 'other-checkout')
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='recorded session'):
            gateway(args, store)


@pytest.mark.parametrize('origin', [
    'https://github.com/other/project.git', 'https://example.invalid/example/project.git',
    'https://evilgithub.com/example/project.git', 'ssh://git@github.com.evil/example/project.git',
])
def test_target_must_match_the_actual_github_origin(release, origin):
    store, args, _ = release
    subprocess.run(['git', '-C', args.cwd, 'remote', 'set-url', 'origin', origin], check=True)
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='origin'): gateway(args, store)


def test_missing_origin_fails_before_remote_write(release):
    store, args, _ = release
    subprocess.run(['git', '-C', args.cwd, 'remote', 'remove', 'origin'], check=True)
    with patch('caller_pr_gates.cli.github_run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='origin'): gateway(args, store)


@pytest.mark.parametrize('result', [
    subprocess.CompletedProcess([], 1, '', 'not found'),
    subprocess.CompletedProcess([], 0, '{broken'),
    *[subprocess.CompletedProcess([], 0, json.dumps(payload)) for payload in (
        [], {}, {'id': True, 'url': TARGET, 'body': BODY},
        {'id': 18, 'url': TARGET, 'body': BODY}, {'id': 17, 'url': None, 'body': BODY},
        {'id': 17, 'url': TARGET + '/wrong', 'body': BODY},
        {'id': 17, 'url': TARGET, 'body': 'different body'},
    )],
])
def test_failure_or_unverified_response_never_records_success_or_retries(release, result):
    store, args, _ = release
    with patch('caller_pr_gates.cli.github_run', return_value=result) as run:
        with pytest.raises(RuntimeError, match='read the release before retrying'):
            gateway(args, store)
    run.assert_called_once()
    assert audits(store) == [] and store.prs('s') == []


def test_release_edit_preserves_another_owners_retired_pr(release):
    store, args, _ = release
    store.register('original-owner', 'codex', REPOSITORY, 77, args.cwd)
    with store._connect() as db:
        db.execute("UPDATE pr_watch SET state='closed',delivery_state='cancelled' WHERE number=77")
        before = [dict(row) for row in db.execute('SELECT * FROM pr_watch')]
    registrations = store.prs()
    with patch('caller_pr_gates.cli.github_run', return_value=response()):
        gateway(args, store)
    with store._connect() as db:
        assert [dict(row) for row in db.execute('SELECT * FROM pr_watch')] == before
        assert not db.execute("SELECT 1 FROM audit WHERE event='ownership_evidence'").fetchone()
    assert store.prs() == registrations and store.prs('s') == []
