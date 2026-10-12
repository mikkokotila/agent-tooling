"""Text migration parity, live reload, safe fallback and unchanged ownership."""
from pathlib import Path
import asyncio
import copy
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tomllib
from unittest.mock import patch

import pytest

from caller_pr_gates import prompts, monitor, hook, closure, supervisor, troubleshooting
from caller_pr_gates.store import Store

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = (ROOT / 'prompts.toml').read_text()
BASE = tomllib.loads(CATALOGUE)


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    path = tmp_path / 'prompts.toml'
    path.write_text(CATALOGUE)
    monkeypatch.setenv('CALLER_PR_GATES_PROMPTS_FILE', str(path))
    monkeypatch.setenv('CALLER_PR_GATES_PROMPTS_STATE_DIR', str(tmp_path / 'prompt-state'))
    prompts._memory.clear()
    return path


def write_catalogue(path, value):
    lines = ['schema_version = 1\n']
    for key, row in value['prompts'].items():
        lines.append('[prompts.' + key + ']\ntext = ' + json.dumps(row['text'], ensure_ascii=False) + '\n')
    path.write_text('\n'.join(lines))


def edit(path, key, text):
    value = tomllib.loads(path.read_text())
    value['prompts'][key]['text'] = text
    write_catalogue(path, value)


def env_for_expression(client):
    row = {'repository': 'Owner/Repo', 'number': 42, 'branch': 'fix-existing',
           'issue_url': 'https://github.com/Owner/Repo/issues/40', 'state': 'merged'}
    job = {**row, 'kind': 'prd', 'id': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
           'dispatch_id': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'}
    scope = {'agreed_scope': 'The original authorized task', 'acceptance_criteria': ['Existing behavior'],
             'slice_count': 1, 'slice_ids': ['S1'], 'approval_evidence': 'Fix this existing bug'}
    return dict(row=row, r=row, job=job, sid='cccccccc-cccc-4ccc-8ccc-cccccccccccc',
        CLI='/Users/mikkokotila/.local/bin/caller-pr-gates', _client=lambda: client,
        RULE=prompts.baseline_text('pr_rules'), lines=['one PR line', 'Approved work: original'],
        payload={'items': ['CI failing: required-build'], 'agreed_scope': scope['agreed_scope']},
        names=['required-build', 'required-review'], outstanding=['comment:1', 'thread:2'], ref='main',
        session={'session_id': 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', 'cwd': '/tmp/owned-checkout'},
        dispatch='Dispatch ID: bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb.\n',
        target='https://github.com/Owner/Repo/issues/42', scope=scope,
        scope_value=supervisor.scope_value, scope_digest=supervisor.scope_digest,
        failed=[{'gate': 'CI_FAILED', 'status': 'FAIL', 'details': ['required-build']}],
        repository='Owner/Repo', number=42, cwd='/tmp/owned-checkout',
        scope_text=json.dumps(scope, sort_keys=True, ensure_ascii=False),
        blocking=[{'gate': 'CI_FAILED', 'status': 'FAIL'}],
        identifier=job['id'], blocked=['CI_FAILED FAIL: Owner/Repo#42: required-build'],
        instruction=prompts.baseline_text('actionable_stop'), exc=RuntimeError('unavailable evidence'),
        url='https://github.com/Owner/Repo/pull/42', root='/tmp/owned-checkout',
        reason='PR_GATE UNKNOWN: RuntimeError: unavailable evidence. No obligation is waived.',
        json=json, render_prompt=prompts.render_prompt)


@pytest.mark.parametrize('client', ['codex', 'claude'])
def test_every_extracted_runtime_expression_preserves_original_text(catalogue, client):
    # Compare actual old source expressions with the new call sites, rather than
    # asserting that the new template agrees with another copy of itself.
    environment = env_for_expression(client)
    corpus = json.loads((ROOT / 'prompt-extraction.json').read_text())
    for entry in corpus:
        expected = eval(compile('(' + entry['original'] + '\n)', '<original prompt>', 'eval'), environment)
        actual = eval(compile(entry['replacement'], '<new prompt call>', 'eval'), environment)
        if entry['key'] in {'pr_rules','external_account_blocked'}:
            # Preserve immutable extraction history, while explicitly testing
            # the authorized service-availability policy update below.
            assert actual != expected
            assert 'non-blocking' in actual
            continue
        if entry['key'] in {'monitor_issue_closure', 'pr_recovery'}:
            # The original extraction remains immutable; these messages now
            # describe the authorized task-issue path without broadening work.
            assert actual != expected
            assert 'genuine issue' in actual and 'GitHub gateway' in actual
            assert 'scope' in actual if entry['key'] == 'pr_recovery' else 'default branch' in actual
            continue
        if entry['key'] == 'communication':
            # Preserve the original advisory and the already-live human-action
            # guidance now captured in the versioned catalogue.
            assert actual.startswith(expected)
            assert 'Does this really need the human' in actual
            continue
        assert actual == expected, (entry['key'], entry['module'], entry['line'])


def test_one_live_edit_updates_both_native_clients_without_reload(catalogue, tmp_path):
    state = Store(tmp_path / 'registry')
    old = prompts.baseline_text('pr_rules')
    for client in ['codex', 'claude']:
        with patch.dict(os.environ, CALLER_HOOK_CLIENT=client):
            original = hook.handle(dict(hook_event_name='SessionStart', session_id=client, cwd=str(tmp_path)), state)
            assert old in original['hookSpecificOutput']['additionalContext']
    replacement = old + ' Human wording edit applies now.'
    edit(catalogue, 'pr_rules', replacement)
    for client in ['codex', 'claude']:
        with patch.dict(os.environ, CALLER_HOOK_CLIENT=client):
            current = hook.handle(dict(hook_event_name='SessionStart', session_id=client, cwd=str(tmp_path)), state)
            assert replacement in current['hookSpecificOutput']['additionalContext']


def test_live_monitor_and_closeout_templates_refresh_without_module_reload(catalogue):
    row = dict(repository='Owner/Repo', number=42, payload=json.dumps({'items': ['real failure']}))
    before = monitor.message([row])
    edit(catalogue, 'monitor_message', BASE['prompts']['monitor_message']['text'] + ' Edited action.')
    assert monitor.message([row]) == before + ' Edited action.'
    issue = dict(repository='Owner/Repo', number=42, issue_url='https://github.com/Owner/Repo/issues/40')
    before = closure.message([issue])
    edit(catalogue, 'issue_closeout', BASE['prompts']['issue_closeout']['text'] + ' Edited closeout.')
    assert closure.message([issue]) == before + ' Edited closeout.'


@pytest.mark.parametrize('failure', ['syntax', 'missing_section', 'unknown_section', 'missing_token', 'unknown_token', 'routing_prefix', 'empty', 'schema', 'missing_file', 'oversized'])
def test_invalid_edit_uses_whole_last_valid_catalogue(catalogue, failure):
    valid = prompts.baseline_text('pr_rules') + ' Previously accepted human edit.'
    edit(catalogue, 'pr_rules', valid)
    assert prompts.render_prompt('pr_rules') == valid
    value = tomllib.loads(catalogue.read_text())
    if failure == 'syntax': catalogue.write_text('schema_version = [ unfinished')
    elif failure == 'missing_file': catalogue.unlink()
    elif failure == 'oversized': catalogue.write_text('#' + 'x' * (prompts.MAX_BYTES + 1))
    else:
        if failure == 'missing_section': value['prompts'].pop('communication')
        if failure == 'unknown_section': value['prompts']['unexpected'] = {'text': 'Unexpected'}
        if failure == 'missing_token': value['prompts']['session_context']['text'] = 'Lost session identity'
        if failure == 'unknown_token': value['prompts']['session_context']['text'] += ' ${unknown}'
        if failure == 'routing_prefix': value['prompts']['document_followup']['text'] = value['prompts']['document_followup']['text'].replace('Durable', 'Changed', 1)
        if failure == 'empty': value['prompts']['communication']['text'] = ''
        write_catalogue(catalogue, value)
        if failure == 'schema': catalogue.write_text(catalogue.read_text().replace('schema_version = 1', 'schema_version = 2'))
    texts, status = prompts.load_catalogue()
    assert texts['pr_rules'] == valid
    assert texts['communication'] == BASE['prompts']['communication']['text']
    assert status['source'] == 'last_good' and not status['valid_current_file']
    assert status['error']


def test_invalid_first_boot_still_enforces_draft_denial(catalogue, tmp_path):
    catalogue.write_text('invalid TOML')
    state = Store(tmp_path / 'registry')
    result = hook.handle(dict(hook_event_name='PreToolUse', session_id='owner', cwd=str(tmp_path),
        tool_name='Bash', tool_input={'command': 'gh pr create --draft --title example'}), state)
    assert result['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert result['systemMessage'] == prompts.baseline_text('draft_denied')
    assert prompts.load_catalogue()[1]['source'] == 'packaged_defaults'


def test_last_valid_edit_survives_a_new_process_and_broken_file(catalogue):
    valid = prompts.baseline_text('pr_rules') + ' Accepted across processes.'
    edit(catalogue, 'pr_rules', valid)
    prompts.render_prompt('pr_rules')
    catalogue.write_text('invalid TOML')
    completed = subprocess.run([sys.executable, '-c', "from caller_pr_gates.prompts import render_prompt; print(render_prompt('pr_rules'), end='')"],
        cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True, text=True, check=True)
    assert completed.stdout == valid and not completed.stderr


def test_lock_contention_does_not_cache_an_obsolete_rescue(catalogue):
    prompts.render_prompt('pr_rules')
    new = prompts.baseline_text('pr_rules') + ' New valid revision.'
    edit(catalogue, 'pr_rules', new)
    lock = prompts.state_directory() / 'prompt-catalogue.lock'
    with lock.open('a+') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert prompts.render_prompt('pr_rules') != new
    assert prompts.render_prompt('pr_rules') == new


def test_invalid_global_boundary_marker_cannot_break_future_sync(catalogue):
    original = prompts.render_prompt('communication')
    edit(catalogue, 'communication', original + ' <!-- caller-pr-gates:end -->')
    assert prompts.render_prompt('communication') == original
    assert not prompts.load_catalogue()[1]['valid_current_file']


def test_unexpected_prompt_refresh_failure_does_not_stop_pr_polling(catalogue):
    calls = []
    health = []
    async def tick(*args, **kwargs):
        calls.append(True)
        return {'checked': 1, 'errors': 0}
    async def stop(*args):
        raise RuntimeError('test cycle complete')
    with patch.object(prompts, 'refresh', side_effect=RuntimeError('simulated text subsystem error')), \
            patch.object(monitor, 'tick', side_effect=tick), \
            patch.object(monitor, 'write_health', side_effect=lambda store, value: health.append(value)):
        with pytest.raises(RuntimeError, match='test cycle complete'):
            asyncio.run(monitor.serve(None, sleeper=stop, clock=lambda: 0))
    assert calls == [True]
    assert health[0]['checked'] == 1 and health[0]['errors'] == 0
    assert 'simulated text subsystem error' in health[0]['prompt_refresh_error']


def test_placeholders_never_evaluate_code_or_recursively_expand(catalogue):
    value = "Owner/${session_id}/$(touch /tmp/never-run)"
    message = prompts.render_prompt('monitor_scope', approved_scope=value)
    assert value in message


def test_communication_edit_is_delivered_once_and_unrelated_edit_does_not_repeat(catalogue, tmp_path):
    state = tmp_path / 'advisory'
    event = dict(hook_event_name='UserPromptSubmit', session_id='owner', turn_id='one', prompt='Explain the mechanism.')
    assert troubleshooting.handle(event, state_dir=state)
    assert troubleshooting.handle(event, state_dir=state) is None
    text = prompts.baseline_text('communication') + ' Updated human wording.'
    edit(catalogue, 'communication', text)
    assert troubleshooting.handle(event, state_dir=state)['hookSpecificOutput']['additionalContext'] == text
    assert troubleshooting.handle(event, state_dir=state) is None
    edit(catalogue, 'pr_rules', prompts.baseline_text('pr_rules') + ' Other wording change.')
    assert troubleshooting.handle(event, state_dir=state) is None
    catalogue.write_text('invalid TOML')
    assert troubleshooting.handle(event, state_dir=state) is None


def global_targets(tmp_path):
    result = []
    for client, name in [('codex', 'AGENTS.md'), ('claude', 'CLAUDE.md')]:
        target = tmp_path / name
        target.write_bytes((ROOT / 'tests/fixtures' / (client + '-instructions.md')).read_bytes())
        result.append(target)
    return result


def test_policy_update_mirrors_both_clients_and_is_idempotent(catalogue, tmp_path):
    targets = global_targets(tmp_path)
    before = [p.read_bytes() for p in targets]
    result = prompts.sync_instruction_files(targets)
    assert not result['error'] and result['changed'] == 2
    assert [p.read_bytes() for p in targets] != before
    for target in targets:
        assert 'Copilot quota exhaustion, unavailability' in target.read_text()
        assert 'Every actual Copilot review comment or change request' in target.read_text()
        assert 'Report the blocker once and yield.' not in target.read_text()
    assert prompts.sync_instruction_files(targets)['changed']==0


def test_global_edits_mirror_both_clients_and_preserve_personal_text(catalogue, tmp_path):
    targets = global_targets(tmp_path)
    prefix = 'Personal instructions outside the managed blocks.\n'
    suffix = '\nPersonal suffix.\n'
    for p in targets: p.write_text(prefix + p.read_text() + suffix)
    text = prompts.baseline_text('communication') + ' Mirrored human edit.'
    edit(catalogue, 'communication', text)
    result = prompts.sync_instruction_files(targets)
    assert not result['error'] and result['changed'] == 2
    for p in targets:
        assert text in p.read_text()
        assert p.read_text().startswith(prefix) and p.read_text().endswith(suffix)
    assert len(list((prompts.state_directory() / 'prompt-instruction-backups').rglob('*.md'))) == 2
    assert prompts.sync_instruction_files(targets)['changed'] == 0


def test_missing_marker_is_detected_before_either_global_file_changes(catalogue, tmp_path):
    targets = global_targets(tmp_path)
    targets[1].write_text('Missing managed markers; preserve this file.')
    before = [p.read_bytes() for p in targets]
    edit(catalogue, 'communication', prompts.baseline_text('communication') + ' New wording.')
    result = prompts.sync_instruction_files(targets)
    assert result['error'] and result['changed'] == 0
    assert [p.read_bytes() for p in targets] == before


def test_second_global_write_failure_rolls_back_first(catalogue, tmp_path):
    targets = global_targets(tmp_path)
    before = [p.read_bytes() for p in targets]
    edit(catalogue, 'communication', prompts.baseline_text('communication') + ' New wording.')
    original = prompts._atomic
    def fail_second(path, *args, **kwargs):
        if path == targets[1]: raise OSError('simulated second write failure')
        return original(path, *args, **kwargs)
    with patch.object(prompts, '_atomic', side_effect=fail_second):
        result = prompts.sync_instruction_files(targets)
    assert result['error'] and not result['rollback_errors']
    assert [p.read_bytes() for p in targets] == before


def test_prompt_edit_does_not_rearm_evidence_identity_or_write_registry(catalogue, tmp_path):
    state = Store(tmp_path / 'registry')
    state.record_session('owner', 'codex', str(tmp_path))
    state.register('owner', 'codex', 'Owner/Repo', 42, cwd=str(tmp_path))
    with state._connect() as db:
        before = [tuple(row) for row in db.execute('SELECT * FROM pr_watch')]
    snapshot = dict(head_sha='head-42', state='open', merged=False, mergeable=True, comments=[], checks=[])
    digest = monitor.identity(snapshot, [])
    edit(catalogue, 'monitor_ci_failed', 'Current failed checks: ${check_names}')
    prompts.refresh()  # Staged package cannot mirror live instruction files.
    with state._connect() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM pr_watch')] == before
    assert monitor.identity(snapshot, []) == digest


@pytest.mark.parametrize('client,config', [('codex', 'codex-hooks.json'), ('claude', 'claude-hooks.json')])
def test_actual_native_hook_command_uses_one_editable_catalogue(catalogue, tmp_path, client, config):
    data = json.loads((ROOT / 'config' / config).read_text().replace('${HOME}', str(Path.home())))
    command = next(h['command'] for group in data['hooks']['UserPromptSubmit'] for h in group['hooks'] if h['command'].endswith('caller_pr_gates.hook'))
    args = []
    for part in shlex.split(command):
        if part.startswith('PYTHONPATH='): part = 'PYTHONPATH=' + str(ROOT)
        if part.startswith('CALLER_PR_GATES_STATE_DIR='): part = 'CALLER_PR_GATES_STATE_DIR=' + str(tmp_path / 'native-state')
        if part.endswith('/caller-pr-stop-gate/bin/python'): part = sys.executable
        args.append(part)
    edit(catalogue, 'pr_rules', prompts.baseline_text('pr_rules') + ' Native editable catalogue verified.')
    event = dict(hook_event_name='UserPromptSubmit', session_id='dddddddd-dddd-4ddd-8ddd-dddddddddddd', cwd=str(tmp_path), prompt='Explain the current mechanism.')
    process = subprocess.run(args, input=json.dumps(event), text=True, capture_output=True, timeout=15, check=True)
    assert not process.stderr
    result = json.loads(process.stdout)
    assert 'Native editable catalogue verified.' in result['hookSpecificOutput']['additionalContext']
