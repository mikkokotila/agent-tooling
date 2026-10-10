import json
import subprocess
from unittest.mock import patch
import pytest
from caller_pr_gates.cli import gateway
from test_cli import args, store


@pytest.mark.parametrize('argv', [
    ['api', 'repos/o/r/pulls/77/comments/123/replies', '--method', 'POST', '-f', 'body=Fixed'],
    ['api', '--method', 'POST', 'repos/o/r/pulls/77/comments/123/replies', '-f', 'body=Fixed'],
    ['api', '--method', 'POST', 'repos/o/r/pulls/77/comments', '-F', 'in_reply_to=123', '-f', 'body=Fixed'],
    ['api', 'repos/o/r/pulls/77/comments', '-Fin_reply_to=123', '-fbody=Fixed'],
    ['api', '--method=POST', 'repos/o/r/pulls/77/comments', '--field=in_reply_to=123', '--raw-field=body=Fixed'],
    ['api', '-XPOST', '-Fin_reply_to=123', 'repos/o/r/pulls/77/comments', '-fbody=Fixed'],
])
def test_documented_reply_routes_record_the_exact_pr_and_preserve_arguments(store, argv):
    value = args(argv); value.target = 'https://github.com/o/r/pull/77'
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(value, store)
    assert run.call_args.args[0] == ['gh', *argv]
    assert [(p['repository'], p['number']) for p in store.prs('s')] == [('o/r', 77)]


@pytest.mark.parametrize('endpoint,extra', [
    ('repos/o/r/pulls/78/comments/123/replies', ['-f', 'body=Fixed']),
    ('repos/other/repo/pulls/77/comments/123/replies', ['-f', 'body=Fixed']),
    ('repos/o/r/pulls/comments/123/replies', ['-f', 'body=Fixed']),
    ('repos/o/r/pulls/77/comments', ['-f', 'body=New comment']),
    ('repos/o/r/pulls/77/comments', ['-F', 'in_reply_to=0']),
    ('repos/o/r/pulls/77/comments', ['-F', 'in_reply_to=true']),
    ('repos/o/r/pulls/77/comments', ['-F', 'in_reply_to=123', '-F', 'in_reply_to=456']),
])
def test_reply_routes_cannot_escape_target_or_publish_a_new_inline_comment(store, endpoint, extra):
    value = args(['api', '--method', 'POST', endpoint, *extra]); value.target = 'https://github.com/o/r/pull/77'
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(value, store)


def test_json_input_reply_is_bound_to_actual_body_not_query_fields(store, tmp_path):
    body = tmp_path / 'reply.json'
    body.write_text(json.dumps({'in_reply_to': 123, 'body': 'Fixed'}))
    value = args(['api', '--method', 'POST', 'repos/o/r/pulls/77/comments', '--input', str(body)])
    value.target = 'https://github.com/o/r/pull/77'
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
        gateway(value, store)
    body.write_text(json.dumps({'body': 'New comment', 'path': 'src.py', 'line': 1, 'commit_id': 'a' * 40}))
    value.arguments.extend(['-F', 'in_reply_to=123'])
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('query fields cannot turn input into a reply')):
        with pytest.raises(ValueError, match='API gateway permits'):
            gateway(value, store)


@pytest.mark.parametrize('fields', [['-Fin_reply_to=123'], ['--field=in_reply_to=123'], ['--input=reply.json']])
def test_attached_field_post_cannot_masquerade_as_a_read(store, fields):
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='writes need --target'):
            gateway(args(['api', 'repos/o/r/pulls/77/comments', *fields]), store)


def test_flags_first_explicit_get_with_fields_remains_a_read(store):
    argv = ['api', '--method', 'GET', 'repos/o/r/pulls/77/comments', '-f', 'reference=123']
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
        gateway(args(argv), store)
    assert store.prs('s') == []
