import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from caller_pr_gates import hook
from caller_pr_gates.store import Store
from caller_pr_gates.supervisor import scoped_prompt, scope_digest

class Memory:
    def __init__(self): self.registered=[]; self.events=[]
    def record_session(self,*a,**kw): pass
    def get_scope(self,*a): return None
    def followups(self,*a): return []
    def audit(self,*a): self.events.append(a)
    def prs(self,*a): return self.registered
    def register(self,*a): self.registered.append(dict(repository=a[2],number=a[3],session_id=a[0]))

class HookRegression(unittest.TestCase):
    def event(self,name='Stop',**kw): return dict(hook_event_name=name,session_id='s',cwd='/tmp',**kw)
    def test_repeated_stop_never_waives_failed_ci(self):
        async def evaluate(*a): return [({'repository':'o/r','number':1},[{'gate':'CI_FAILED','status':'FAIL','details':['job x failed']}])]
        with patch.object(hook,'discover'),patch.object(hook,'evaluate',evaluate):
            result=hook.handle(self.event(stop_hook_active=True),Memory())
        self.assertNotEqual(result.get('decision'),'block')
        self.assertIn('incomplete or unverified',result['systemMessage'])
        self.assertIn('no obligation completed',result['systemMessage'])
    def test_every_pending_job_blocks_completion(self):
        async def evaluate(*a): return [({'repository':'o/r','number':1},[{'gate':'CI_FAILED','status':'PASS','details':[]},{'gate':'CI_PENDING','status':'WAIT','details':['matrix job 7 running']}])]
        with patch.object(hook,'discover'),patch.object(hook,'evaluate',evaluate):
            result=hook.handle(self.event(last_assistant_message='Finished.'),Memory())
        self.assertIn('matrix job 7 running',result['reason'])
    def test_pr_ownership_survives_non_bash_tool(self):
        store=Memory()
        event=self.event('PostToolUse',tool_name='mcp__github__create_pull_request',tool_input={},tool_response={'html_url':'https://github.com/Owner/Repo/pull/42'})
        with patch.object(hook,'discover'):hook.handle(event,store)
        self.assertEqual(store.registered[0]['number'],42)
    def test_denial_has_required_runtime_discriminator_and_reason(self):
        result=hook.handle(self.event('PreToolUse',tool_name='Bash',tool_input={'command':'gh pr create --draft --title test'}),Memory())
        body=result['hookSpecificOutput']
        self.assertEqual(body['hookEventName'],'PreToolUse')
        self.assertEqual(body['permissionDecision'],'deny')
        self.assertTrue(body['permissionDecisionReason'])
    def test_raw_pr_writes_require_registered_gateway(self):
        result=hook.handle(self.event('PreToolUse',tool_name='Bash',tool_input={'command':'gh pr create --title test'}),Memory())
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')
    def test_editing_documentation_and_hook_source_is_not_document_publication(self):
        event=self.event('PreToolUse',tool_name='functions.exec',tool_input={'code': 'await tools.apply_patch("PRD/Slice instructions explain issue create/post publication");'})
        self.assertIsNone(hook.handle(event,Memory()))
    def test_reading_mcp_review_comments_remains_allowed(self):
        for tool in ['mcp__github__get_pull_request_comments','mcp__github__list_review_threads','github.getIssueComments']:
            result=hook.handle(self.event('PreToolUse',tool_name=tool,tool_input={}),Memory())
            self.assertIsNone(result)
    def test_queued_future_followup_is_owned_but_does_not_block_yield(self):
        store=Memory();store.followups=lambda *a:[dict(id=1,repository='o/r',number=3,due_at=10**12,status='pending')]
        async def evaluate(*a):return []
        with patch.object(hook,'discover'),patch.object(hook,'evaluate',evaluate):result=hook.handle(self.event(),store)
        self.assertIn('not complete',result['systemMessage'])


class OwnershipRegression(unittest.TestCase):
    def event(self, tool='Bash', command=None, **fields):
        arguments = {'command': command} if command is not None else {}
        event = dict(hook_event_name='PostToolUse', session_id='read-session', cwd='/caller',
                     tool_name=tool, tool_input=arguments, tool_response={'exit_code': 0},
                     turn_id='turn-read', tool_call_id='tool-1')
        event.update(fields)
        return event

    def test_repo_file_github_and_attachment_reads_never_discover_ownership(self):
        events = [self.event(command='cat /repo/src.py'), self.event(command='git -C /repo status'),
                  self.event(command='python - <<\'PY\'\nfrom pathlib import Path\nprint(Path("/repo/src.py").read_text())\nPY'),
                  self.event(tool='Read', tool_input={'file_path': '/repo/src.py'}),
                  self.event(tool='mcp__github__get_pull_request', tool_input={'url': 'https://github.com/o/r/pull/77'}, tool_response={'html_url': 'https://github.com/o/r/pull/77'}),
                  self.event(tool='mcp__codex_app__attach_artifact', tool_input={'url': 'https://github.com/o/r/pull/77'}),
                  self.event(command='caller-pr-gates github --session read-session -- pr view https://github.com/o/r/pull/77', tool_response={'output': 'https://github.com/o/r/pull/77', 'exit_code': 0})]
        for event in events:
            with self.subTest(event=event):
                store = Memory()
                with patch.object(hook, '_git_context', side_effect=AssertionError('reads must not reconcile PR ownership')):
                    hook.handle(event, store)
                self.assertEqual(store.registered, [])

    def test_first_failed_cat_does_not_query_git_or_register(self):
        with patch.object(hook, '_git_context', side_effect=AssertionError('failed read must remain read-only')):
            hook.discover(self.event(command='cat missing', hook_event_name='PostToolUseFailure', tool_response={'exit_code': 1}), Memory())

    def test_stop_cannot_create_new_ownership(self):
        store = Memory()
        with patch.object(hook, '_git_context', side_effect=AssertionError('Stop observes registered obligations only')):
            hook.discover(self.event(hook_event_name='Stop'), store)
        self.assertEqual(store.registered, [])

    def test_explicit_successful_edit_and_git_push_establish_ownership_with_evidence(self):
        for event in [self.event(tool='Edit', tool_input={'file_path': '/repo/src.py'}), self.event(command='git -C /repo push origin HEAD')]:
            store = Memory()
            with patch.object(hook, '_git_context', return_value=('/repo', 'o/r', 'fix')), patch.object(hook.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps({'found': True, 'pull_number': 77}))):
                hook.discover(event, store)
            self.assertEqual([row['number'] for row in store.registered], [77])
            evidence = [args[1] for args in store.events if args[0] == 'ownership_evidence'][0]
            self.assertEqual(evidence['target'], 'https://github.com/o/r/pull/77')
            self.assertEqual(evidence['turn_id'], 'turn-read')
            self.assertEqual(evidence['tool_call_id'], 'tool-1')

    def test_nested_read_directory_does_not_get_owned_with_a_write_elsewhere(self):
        event = self.event(tool='functions.exec', tool_input={'code': 'await tools.exec_command({cmd:"cat src.py",workdir:"/read-repo"}); await tools.exec_command({cmd:"git commit -m Fix",workdir:"/write-repo"});'})
        store = Memory()
        with patch.object(hook, '_git_context', return_value=('/write-repo', 'o/r', 'fix')) as context, patch.object(hook.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps({'found': True, 'pull_number': 77}))):
            hook.discover(event, store)
        context.assert_called_once_with('/write-repo')

    def test_later_writes_to_already_owned_pr_still_record_current_turn_evidence(self):
        store = Memory()
        store.registered = [dict(repository='o/r', number=77, session_id='read-session')]
        event = self.event(tool='Edit', tool_input={'file_path': '/repo/src.py'}, turn_id='new-write-turn')
        with patch.object(hook, '_git_context', return_value=('/repo', 'o/r', 'fix')), patch.object(hook.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps({'found': True, 'pull_number': 77}))):
            hook.discover(event, store)
        self.assertEqual(len(store.registered), 1)
        evidence = [args[1] for args in store.events if args[0] == 'ownership_evidence'][0]
        self.assertEqual(evidence['turn_id'], 'new-write-turn')

    def test_creation_owns_output_target_only_not_input_body_references(self):
        store = Memory()
        event = self.event(tool='mcp__github__create_pull_request', tool_input={'body': 'See https://github.com/o/r/pull/99'}, tool_response={'html_url': 'https://github.com/o/r/pull/77', 'body': 'See https://github.com/o/r/pull/88'})
        hook._register_urls(event, store)
        self.assertEqual([row['number'] for row in store.registered], [77])

    def test_gateway_write_output_and_body_urls_are_not_double_harvested(self):
        store = Memory()
        event = self.event(command='caller-pr-gates github --session s -- pr comment https://github.com/o/r/pull/77 --body "see https://github.com/o/r/pull/99"', tool_response={'output': 'https://github.com/o/r/pull/99', 'exit_code': 0})
        hook._register_urls(event, store)
        self.assertEqual(store.registered, [])

    def test_diagnostic_output_writes_outside_repo_do_not_own_caller_repo(self):
        for command in ['cat src.py > /tmp/diagnostic.txt', 'cp src.py /tmp/diagnostic.txt', 'rm /tmp/diagnostic.txt']:
            event = self.event(command=command, cwd='/repo')
            store = Memory()
            with patch.object(hook, '_git_context', return_value=None) as context:
                hook.discover(event, store)
            self.assertEqual(store.registered, [])
            self.assertEqual([call.args[0] for call in context.call_args_list], ['/tmp'])


class FollowupDeliveryRegression(unittest.TestCase):
    sid = '11111111-1111-4111-8111-111111111111'
    foreign = '22222222-2222-4222-8222-222222222222'

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.now = time.time()
        self.scope = dict(agreed_scope='One approved slice', acceptance_criteria=['Existing gates pass'],
                          slice_count=1, slice_ids=['S1'], approved_at=self.now - 5000)
        self.store.record_session(self.sid, 'codex', '/tmp')
        self.store.record_scope(self.sid, self.scope)

    def dispatch(self, kind='prd', cwd='/tmp'):
        job = self.store.register_followup(self.sid, 'Owner/Repo', 70, self.now - 4000,
                                           self.scope, kind=kind, issue_number=70)
        job = self.store.claim_followup(job['id'])
        prompt = scoped_prompt(job, dict(session_id=self.sid, cwd=cwd), self.scope, [])
        self.store.update_followup(job['id'], 'running', fields=dict(
            lease_token=job['lease_token'], delivery='queue_existing_thread', prompt=prompt,
            dispatch_at=self.now - 100, acknowledged_at=self.now - 99,
            queued_prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(), runner_exit=0))
        return job, prompt

    def complete(self, job, **overrides):
        receipt = dict(read_at=self.now - 50, head_sha='revision-70', comment_ids=['issue:1'],
                       scope_digest=scope_digest(self.scope))
        fields = dict(review_read=receipt, verified_at=self.now - 10,
                      verification='live reread, separate gates and unchanged scope verified')
        fields.update(overrides)
        self.assertTrue(self.store.complete_followup(job['id'], job['lease_token'], fields))

    def submit(self, prompt, sid=None):
        return hook.handle(dict(hook_event_name='UserPromptSubmit', session_id=sid or self.sid,
                                cwd='/tmp', turn_id='delivered-turn', prompt=prompt), self.store)

    def assert_context(self, result):
        self.assertNotIn('decision', result)
        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        self.assertIn('Gate errors never waive obligations', result['hookSpecificOutput']['additionalContext'])

    def test_delayed_delivery_after_verified_completion_is_blocked_for_both_kinds(self):
        for kind in ('prd', 'slice'):
            with self.subTest(kind=kind):
                job, prompt = self.dispatch(kind)
                self.complete(job)
                before = next(j for j in self.store.followups(self.sid) if j['id'] == job['id'])
                result = self.submit(prompt)
                self.assertEqual(result['decision'], 'block')
                self.assertIn('already complete after verified live reread', result['reason'])
                self.assertNotIn('Continue', result['reason'])
                after = next(j for j in self.store.followups(self.sid) if j['id'] == job['id'])
                self.assertEqual(before, after)

    def test_historical_dispatched_prompt_is_blocked_after_newer_prompt_replaced_it(self):
        job, older = self.dispatch(cwd='/tmp/previous-checkout')
        newer = older.replace('/tmp/previous-checkout', '/tmp/current-checkout')
        self.store.update_followup(job['id'], 'running', fields=dict(lease_token=job['lease_token'], prompt=newer))
        self.complete(job)
        self.assertEqual(self.submit(older)['decision'], 'block')

    def test_incomplete_delivery_records_consumption_and_due_obligation_still_blocks_stop(self):
        job, prompt = self.dispatch()
        before = next(j for j in self.store.followups(self.sid) if j['id'] == job['id'])
        self.assert_context(self.submit(prompt))
        after = next(j for j in self.store.followups(self.sid) if j['id'] == job['id'])
        self.assertEqual(after['fields']['delivery_receipt']['prompt_hash'], hashlib.sha256(prompt.encode()).hexdigest())
        self.assertEqual(after['fields']['delivery_receipt']['turn_id'], 'delivered-turn')
        for key in ('status', 'attempts', 'lease_until', 'lease_token'):
            self.assertEqual(before[key], after[key])
        async def evaluate(*args): return []
        with patch.object(hook, 'discover'), patch.object(hook, 'evaluate', evaluate):
            result = hook.handle(dict(hook_event_name='Stop', session_id=self.sid, cwd='/tmp'), self.store)
        self.assertEqual(result['decision'], 'block')
        self.assertIn('requires the due review check', result['reason'])

    def test_merged_pr_is_not_completed_document_evidence(self):
        job, prompt = self.dispatch()
        self.store.set_snapshot('Owner/Repo', 70, dict(state='MERGED', merged=True), [])
        self.assert_context(self.submit(prompt))
        self.assertEqual(next(j for j in self.store.followups(self.sid) if j['id'] == job['id'])['status'], 'running')

    def test_completion_between_initial_read_and_consumption_uses_authoritative_receipt(self):
        job, prompt = self.dispatch()
        def complete_before_recording(*args):
            self.complete(job)
            return False
        with patch.object(self.store, 'note_followup_delivery', side_effect=complete_before_recording) as note, \
                patch.object(self.store, 'followups', wraps=self.store.followups) as reads:
            result = self.submit(prompt)
        self.assertEqual(result['decision'], 'block')
        self.assertIn('already complete after verified live reread', result['reason'])
        self.assertEqual(note.call_count, 1)
        self.assertEqual(reads.call_count, 2)

    def test_consumption_rejection_with_incomplete_job_keeps_existing_context(self):
        job, prompt = self.dispatch()
        with patch.object(self.store, 'note_followup_delivery', return_value=False) as note, \
                patch.object(self.store, 'followups', wraps=self.store.followups) as reads:
            result = self.submit(prompt)
        self.assert_context(result)
        self.assertEqual(note.call_count, 1)
        self.assertEqual(reads.call_count, 2)
        self.assertEqual(self.store.followups(self.sid)[0]['status'], 'running')

    def test_complete_without_verified_receipt_is_not_suppressed(self):
        job, prompt = self.dispatch()
        self.assertTrue(self.store.complete_followup(job['id'], job['lease_token']))
        self.assert_context(self.submit(prompt))

    def test_incomplete_or_changed_verification_is_not_suppressed(self):
        cases = [dict(verified_at=float('inf')), dict(verified_at=True), dict(verification='merged'),
                 dict(review_read={}), dict(review_read='invalid'),
                 dict(review_read=dict(read_at=self.now - 3000, head_sha='revision-70', comment_ids=[],
                                       scope_digest=scope_digest(self.scope)))]
        for number, overrides in enumerate(cases):
            with self.subTest(overrides=overrides):
                job = self.store.register_followup(self.sid, 'Owner/Repo', 80 + number,
                                                   self.now - 4000, self.scope, issue_number=80 + number)
                job = self.store.claim_followup(job['id'])
                prompt = scoped_prompt(job, dict(session_id=self.sid, cwd='/tmp'), self.scope, [])
                self.store.update_followup(job['id'], 'running', fields=dict(lease_token=job['lease_token'],
                    delivery='queue_existing_thread', prompt=prompt))
                # SQLite JSON intentionally rejects non-finite data; exercise the hook's defensive check directly.
                if overrides.get('verified_at') == float('inf'):
                    self.complete(job)
                    rows = self.store.followups(self.sid)
                    next(j for j in rows if j['id'] == job['id'])['fields']['verified_at'] = float('inf')
                    with patch.object(self.store, 'followups', return_value=rows):
                        self.assert_context(self.submit(prompt))
                else:
                    self.complete(job, **overrides)
                    self.assert_context(self.submit(prompt))

    def test_normal_quoted_changed_foreign_and_missing_prompts_are_unaffected(self):
        job, prompt = self.dispatch()
        self.complete(job)
        for text in ('Continue the new task.', 'Explain this prompt:\n' + prompt,
                     '> ' + prompt, prompt + '\nExplain why this happened.',
                     prompt.replace(job['id'], '33333333-3333-4333-8333-333333333333')):
            with self.subTest(prompt=text[:70]): self.assert_context(self.submit(text))
        self.assert_context(self.submit(prompt, self.foreign))

    def test_stale_prompt_guard_does_not_bypass_stop_ci_gate(self):
        job, prompt = self.dispatch()
        self.complete(job)
        self.assertEqual(self.submit(prompt)['decision'], 'block')
        async def evaluate(*args):
            return [({'repository': 'Owner/Repo', 'number': 72},
                     [{'gate': 'CI_FAILED', 'status': 'FAIL', 'details': ['build failed']}])]
        with patch.object(hook, 'discover'), patch.object(hook, 'evaluate', evaluate):
            result = hook.handle(dict(hook_event_name='Stop', session_id=self.sid, cwd='/tmp'), self.store)
        self.assertIn('CI_FAILED FAIL', result['reason'])

if __name__=='__main__':unittest.main()
