"""Confirmed publication, native owner isolation and once-only delivery."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest

from caller_pr_gates import checklist, hook, monitor, prompts
from caller_pr_gates.cli import gateway
from caller_pr_gates.store import Store
from test_cli import args
from test_prompt_catalogue import catalogue, edit

ROOT = Path(__file__).resolve().parent.parent
SID = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
MINIMAL = ('Minimal, clinical interventions all the way. Get in, get out, do the thing that actually has to be done, '
    'never expand it beyond that. Keep asking "is this truly the cleanest and simplest and least intrusive way '
    'to get this done" and do it that way and only that way. Don\'t let others come expand it, let them come make it cleaner.')


def setup_store(tmp_path, client='codex'):
    store = Store(tmp_path / 'state')
    store.record_session(SID, client, str(tmp_path))
    scope = dict(agreed_scope='Agreed work', acceptance_criteria=['Requested outcome'],
        slice_count=1, slice_ids=['S1'], approved_at=1)
    store.record_scope(SID, scope)
    return store, scope


@pytest.fixture
def published(tmp_path):
    return setup_store(tmp_path)


def publish(store, *, client='codex', kind='prd', number=42, failure=None, readback_failure=False):
    value = args(['issue', 'create', '--title', 'Agreed design', '--body', 'Authorized document'])
    value.session, value.client, value.kind = SID, client, kind
    if kind == 'slice': value.slice_id = 'S1'
    result = subprocess.CompletedProcess([], 1 if failure else 0,
        stdout=f'https://github.com/owner/repo/issues/{number}\n', stderr=failure or '')
    collector = AsyncMock(side_effect=RuntimeError('Readback unavailable')) if readback_failure else AsyncMock(return_value={'created_at': 100})
    with patch('caller_pr_gates.cli.github_run', return_value=result), patch('caller_pr_gates.github.collect_issue', collector):
        gateway(value, store)


def callback(store, tmp_path, client='codex', sid=SID):
    with patch.dict(os.environ, CALLER_HOOK_CLIENT=client), patch.object(hook, 'discover'), patch.object(hook, '_register_urls'), patch.object(monitor, 'consume', return_value=None):
        return checklist.handle(dict(hook_event_name='PostToolUse', session_id=sid, cwd=str(tmp_path),
            tool_name='Bash', tool_input={'command':'echo published'}), store)


@pytest.mark.parametrize('client', ['codex', 'claude'])
def test_confirmed_creation_emits_editable_prompt_once_in_owner_chat(tmp_path, catalogue, client):
    store, _ = setup_store(tmp_path, client)
    text = prompts.baseline_text('prd_review_checklist') + ' Human wording.'
    edit(catalogue, 'prd_review_checklist', text)
    publish(store)  # Omitted --client defaults to codex even in a Claude chat.
    assert store.followups(SID)[0]['fields']['prd_review_notice']['client'] == client
    assert callback(store, tmp_path, client, 'other-chat') is None
    assert callback(store, tmp_path, 'claude' if client == 'codex' else 'codex') is None
    store.record_session('child', client, str(tmp_path), SID)
    assert callback(store, tmp_path, client, 'child') is None
    output = callback(store, tmp_path, client)['hookSpecificOutput']
    assert output == {'hookEventName':'PostToolUse', 'additionalContext':text.replace('${prd_url}', 'https://github.com/owner/repo/issues/42')}
    assert MINIMAL in output['additionalContext']
    assert callback(store, tmp_path, client) is None


def test_slices_legacy_records_and_reregistering_cannot_create_notice(published, tmp_path):
    store, scope = published
    store.register_followup(SID, 'owner/repo', 42, 100, scope, issue_number=42)
    # An existing record never acquires a creation receipt on registration replay.
    publish(store)
    publish(store, kind='slice', number=43)
    assert callback(store, tmp_path) is None


@pytest.mark.parametrize('failure,readback_failure', [('GitHub refused creation', False), (None, True)])
def test_failed_creation_or_readback_never_emits(published, tmp_path, failure, readback_failure):
    store, _ = published
    with pytest.raises(RuntimeError):
        publish(store, failure=failure, readback_failure=readback_failure)
    assert store.followups(SID) == []
    assert callback(store, tmp_path) is None


def test_reads_edits_and_references_do_not_signal_publication(published, tmp_path):
    store, _ = published
    for argv in [['issue','view','https://github.com/owner/repo/issues/42'],
                 ['issue','edit','https://github.com/owner/repo/issues/42','--title','PRD'],
                 ['issue','comment','https://github.com/owner/repo/issues/42','--body','PRD https://github.com/owner/repo/issues/99']]:
        value = args(argv); value.session = SID; value.kind = 'prd'
        with patch('caller_pr_gates.cli.github_run', return_value=subprocess.CompletedProcess([], 0)):
            gateway(value, store)
    assert store.followups(SID) == []
    assert callback(store, tmp_path) is None


def test_title_matching_alone_cannot_emit_a_document_instruction(published, tmp_path):
    store, _ = published
    value = args(['issue','create','--title','Fix PRD publication detection','--body','Engineering tracking issue'])
    value.session = SID
    with patch('caller_pr_gates.cli.github_run', return_value=subprocess.CompletedProcess([],0,stdout='https://github.com/owner/repo/issues/42\n')), patch('caller_pr_gates.github.collect_issue', AsyncMock(return_value={'created_at':100})):
        gateway(value, store)
    assert callback(store, tmp_path) is None


def test_concurrent_callbacks_and_legacy_retry_cannot_repeat_notice(published):
    store, _ = published
    publish(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.consume_prd_notices(SID, 'codex'), range(2)))
    assert sorted(map(len, results)) == [0, 1]
    job = store.followups(SID)[0]
    assert store.retry_followup(job['id'])
    assert store.consume_prd_notices(SID, 'codex') == []
    publish(store, number=44)
    assert [r['number'] for r in store.consume_prd_notices(SID, 'codex')] == [44]


@pytest.mark.parametrize('client', ['codex', 'claude'])
def test_actual_native_posttool_command_delivers_once(tmp_path, catalogue, client):
    store, _ = setup_store(tmp_path, client)
    publish(store)
    data = json.loads((ROOT / 'config' / (client+'-hooks.json')).read_text().replace('${HOME}', str(Path.home())))
    command = data['hooks']['PostToolUse'][0]['hooks'][0]['command']
    argv = []
    for part in shlex.split(command):
        if part.startswith('PYTHONPATH='): part = 'PYTHONPATH=' + str(ROOT)
        if part.startswith('CALLER_PR_GATES_STATE_DIR='): part = 'CALLER_PR_GATES_STATE_DIR=' + str(tmp_path/'state')
        if part.endswith('/caller-pr-stop-gate/bin/python'): part = sys.executable
        argv.append(part)
    event = dict(hook_event_name='PostToolUse', session_id=SID, cwd=str(tmp_path),
        tool_name='Bash', tool_input={'command':'echo published'})
    outputs = [subprocess.run(argv, input=json.dumps(event), text=True, capture_output=True, check=True, timeout=15) for _ in range(2)]
    assert all(not p.stderr for p in outputs)
    text = json.loads(outputs[0].stdout)['hookSpecificOutput']['additionalContext']
    assert text.startswith('Automated PRD review hook — https://github.com/owner/repo/issues/42.')
    assert MINIMAL in text and not outputs[1].stdout.strip()


def test_root_client_repairs_only_pending_old_flag_receipts(tmp_path):
    store, scope = setup_store(tmp_path, 'claude')
    store.record_session('child', 'codex', str(tmp_path), SID)
    job = store.register_followup('child', 'owner/repo', 42, 100, scope, issue_number=42, publication_notice=True)
    assert job['fields']['prd_review_notice']['client'] == 'claude'
    # Reproduce an older pending receipt; the immutable recorded owner wins.
    store.update_followup(job['id'], job['status'], {'prd_review_notice':{'client':'codex','state':'pending'}})
    assert store.consume_prd_notices('child', 'codex') == []
    assert store.consume_prd_notices(SID, 'codex') == []
    assert len(store.consume_prd_notices(SID, 'claude')) == 1
    assert store.consume_prd_notices(SID, 'claude') == []
