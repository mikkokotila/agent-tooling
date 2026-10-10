import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from caller_pr_gates.cli import gateway, task_gates
from caller_pr_gates.hook import handle
from caller_pr_gates.store import Store

@pytest.fixture
def store(tmp_path):
    value=Store(tmp_path/'state')
    value.record_session('s','codex',str(tmp_path))
    return value

def args(argv, **kw):
    return SimpleNamespace(session='s',client='codex',cwd='/tmp',kind=None,slice_id=None,target=None,arguments=argv,**kw)

def test_api_cannot_create_an_unregistered_pr(store):
    with pytest.raises(ValueError,match='API gateway permits'):
        gateway(SimpleNamespace(session='s',client='codex',cwd='/tmp',kind=None,slice_id=None,target='https://github.com/o/r/pull/1',arguments=['api','repos/o/r/pulls','-X','POST','-f','title=test']),store)

def test_api_cannot_resolve_an_unowned_thread_with_arbitrary_graphql(store):
    a=args(['api','graphql','-f','query=mutation{resolveReviewThread(input:{threadId:"x"}){thread{id}}}'])
    a.target='https://github.com/o/r/pull/1'
    with pytest.raises(ValueError,match='API gateway permits'):gateway(a,store)

def test_mcp_document_creation_requires_controlled_gateway(store):
    result=handle({'hook_event_name':'PreToolUse','session_id':'s','cwd':'/tmp','tool_name':'mcp__github__create_issue','tool_input':{'title':'PRD'}},store)
    assert result['hookSpecificOutput']['permissionDecision']=='deny'

def test_publication_refuses_extra_slice_before_remote_write(store):
    scope={'agreed_scope':'A','acceptance_criteria':['A'],'slice_count':1,'slice_ids':['S1'],'approved_at':1,'approval_evidence':'User approved S1'}
    store.record_scope('s',scope)
    a=args(['issue','create','--title','Slice 2','--body','B']);a.kind='slice';a.slice_id='S2'
    with patch('caller_pr_gates.cli.subprocess.run',side_effect=AssertionError('must not publish')):
        with pytest.raises(ValueError,match='originally approved'):gateway(a,store)

def test_status_lists_due_followup_and_slice_scope_separately(store):
    scope={'agreed_scope':'A','acceptance_criteria':[],'slice_count':0,'slice_ids':[],'approved_at':1}
    store.record_scope('s',scope)
    store.register_followup('s','o/r',1,1,scope,issue_number=1)
    values={g['gate']:g['status'] for g in task_gates(store,'s')}
    assert values=={'PRD_FOLLOWUP':'WAIT','SLICE_SCOPE':'PASS'}

@pytest.mark.parametrize('flag',['--draft','--draft=true','-d'])
def test_draft_flag_variants_are_rejected_before_publication(store,flag):
    with patch('caller_pr_gates.cli.subprocess.run',side_effect=AssertionError('must not publish')):
        with pytest.raises(ValueError,match='drafts are forbidden'):
            gateway(args(['pr','create',flag,'--title','bad']),store)


@pytest.mark.parametrize('argv', [
    ['pr', 'view', 'https://github.com/o/r/pull/77'],
    ['pr', 'diff', 'https://github.com/o/r/pull/77'],
    ['issue', 'view', 'https://github.com/o/r/issues/77'],
    ['api', 'repos/o/r/pulls/77'],
    ['api', 'repos/o/r/pulls/77', '--method', 'GET', '-f', 'reference=https://github.com/o/r/pull/99'],
    ['api', 'repos/o/r/pulls/77', '--method=GET', '-f', 'reference=https://github.com/o/r/pull/99'],
    ['api', 'repos/o/r/pulls/77', '-XGET', '-f', 'reference=https://github.com/o/r/pull/99'],
])
def test_gateway_reads_never_create_pr_ownership(store, argv):
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess(argv, 0)):
        gateway(args(argv), store)
    assert store.prs('s') == []


def test_gateway_comment_owns_exact_mutation_target_not_body_reference(store):
    argv = ['pr', 'comment', 'https://github.com/o/r/pull/77', '--body', 'See https://github.com/other/repo/pull/99']
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess(argv, 0)):
        gateway(args(argv), store)
    assert [(row['repository'], row['number']) for row in store.prs('s')] == [('o/r', 77)]


def test_explicit_gateway_target_is_passed_to_actual_gh_write(store):
    value = args(['pr', 'comment', '--body', 'Actual review reply'])
    value.target = 'https://github.com/o/r/pull/77'
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
        gateway(value, store)
    assert run.call_args.args[0][:4] == ['gh', 'pr', 'comment', value.target]
    assert store.prs('s')[0]['number'] == 77


def test_gateway_creation_uses_unique_actual_url_line(store):
    value = args(['pr', 'create', '--title', 'Actual fix', '--body', 'Closes #76. Related https://github.com/o/r/pull/99'])
    output = 'Created pull request referencing https://github.com/o/r/pull/99\nhttps://github.com/o/r/pull/77\n'
    with patch('caller_pr_gates.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout=output)):
        gateway(value, store)
    assert [row['number'] for row in store.prs('s')] == [77]


def test_write_target_mismatch_is_denied_before_remote_write(store):
    value = args(['pr', 'comment', 'https://github.com/o/r/pull/77', '--body', 'Reply'])
    value.target = 'https://github.com/o/r/pull/99'
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='disagree'):
            gateway(value, store)


def test_attached_post_method_cannot_masquerade_as_read(store):
    with patch('caller_pr_gates.cli.subprocess.run', side_effect=AssertionError('must not write')):
        with pytest.raises(ValueError, match='writes need --target'):
            gateway(args(['api', 'repos/o/r/pulls', '-XPOST']), store)
