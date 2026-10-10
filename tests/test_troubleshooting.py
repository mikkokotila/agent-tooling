"""Advisory writing guidance cannot become a permission or completion gate."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from caller_pr_gates import troubleshooting


VALID = """**Topic:** Deployment stops before starting the worker.

**Evidence:** The startup log says DATABASE_URL is missing; I believe that prevents worker startup.

**Intervention:** Supply the existing database URL, redeploy, and check worker health.

**Input Required:** None."""


class ReportGuidance(unittest.TestCase):
    def test_new_labels_pass(self):
        self.assertEqual(('Topic', 'Evidence', 'Intervention', 'Input Required'), troubleshooting.LABELS)
        self.assertEqual([], troubleshooting.validate_report(VALID))

    def test_direct_simple_status_and_empty_replies_have_no_required_format(self):
        for answer in ('Press C.', 'No new updates', 'Fixed.', '', 'Why does it fail?',
                       '**Issue found:** The worker stops.\n\n**Cause:** Missing configuration.'):
            with self.subTest(answer=answer):
                self.assertEqual([], troubleshooting.validate_report(answer))

    def test_optional_questions_and_options(self):
        for fields in ('**Questions:** Does the worker receive the variable?',
                       '**Options:** Supply the variable or use the existing environment.',
                       '**Questions:** Which environment is affected?\n\n**Options:** Inspect startup or queue delivery.'):
            with self.subTest(fields=fields):
                report = VALID.replace('\n\n**Intervention:**', '\n\n' + fields + '\n\n**Intervention:**')
                self.assertEqual([], troubleshooting.validate_report(report))
        self.assertEqual([], troubleshooting.validate_report('**Questions:** Which environment?'))

    def test_chosen_structure_has_optional_lint_for_bold_labels_and_blank_lines(self):
        for label in troubleshooting.LABELS:
            with self.subTest(label=label):
                self.assertTrue(troubleshooting.validate_report(VALID.replace('**' + label + ':**', label + ':')))
        self.assertTrue(troubleshooting.validate_report(VALID.replace('\n\n', '\n')))
        self.assertEqual([], troubleshooting.validate_report(VALID.replace(':**', '**:')))

    def test_partial_and_reordered_fields_are_flexible_but_empty_fields_get_lint(self):
        parts = VALID.split('\n\n')
        for index in range(len(parts)):
            with self.subTest(index=index):
                self.assertEqual([], troubleshooting.validate_report('\n\n'.join(parts[:index] + parts[index + 1:])))
                empty = parts.copy()
                empty[index] = empty[index].split(':**', 1)[0] + ':**'
                self.assertTrue(troubleshooting.validate_report('\n\n'.join(empty)))
        self.assertEqual([], troubleshooting.validate_report('\n\n'.join(reversed(parts))))

    def test_quoted_examples_are_not_a_chosen_structure(self):
        for text in ('```text\n' + VALID + '\n```', '<!--\n' + VALID + '\n-->',
                     '\n'.join('> ' + line for line in VALID.splitlines())):
            with self.subTest(text=text):
                self.assertEqual([], troubleshooting.validate_report(text))
        partial = '**Topic:** Startup failure.\n\n```text\n' + VALID + '\n```'
        self.assertEqual([], troubleshooting.validate_report(partial))

    def test_topic_can_be_results_idea_question_or_options_without_other_fields(self):
        for topic in ('Three checks passed; one failed.', 'Add a reset shortcut.',
                      'Which environment is affected?', 'Two storage choices meet the latency target.'):
            with self.subTest(topic=topic):
                self.assertEqual([], troubleshooting.validate_report('**Topic:** ' + topic))

    def test_uncertainty_and_proposed_intervention_are_allowed(self):
        report = VALID.replace('The startup log says DATABASE_URL is missing; I believe that prevents worker startup.',
                               'Cause unconfirmed. Worker startup has not been observed; inspect the log to test the configuration hypothesis.')
        report = report.replace('Supply the existing database URL, redeploy, and check worker health.',
                                'Proposed: inspect startup and queue records before selecting a repair.')
        self.assertEqual([], troubleshooting.validate_report(report))

    def test_rule_is_general_flexible_and_chat_only(self):
        rule = troubleshooting.RULE
        for label in (*troubleshooting.LABELS, *troubleshooting.OPTIONAL_LABELS):
            self.assertIn('**' + label + ':**', rule)
        for guidance in ('substantive explanations or proposals', 'not mandatory for every reply or progress update',
                         'separate observed facts from hypotheses', 'Never repeat the exact same explanation',
                         'No new updates is sufficient', 'unchanged checks stay silent',
                         'invent a human approval requirement', 'does not apply to GitHub PR/issue bodies',
                         'Do not publish chat progress or status reports to GitHub', 'formatting must never cause a Stop block'):
            self.assertIn(guidance, rule)
        for guidance in ('all four fields are not required', 'Omit empty sections', 'not scripted', 'user explicitly asks for status',
                         'support that topic', 'Examples: troubleshooting', 'Run results', 'Idea', 'Decision/options'):
            self.assertIn(guidance, rule)
        for guidance in ('Lead with the useful point', 'exercise judgment', 'current context',
                         'routine engineering and CI work autonomously', 'authorized scope',
                         'Ask only for real decisions or missing inputs', 'do not narrate process'):
            self.assertIn(guidance, rule)
        self.assertRegex(rule, r'only[^.]*\bchat\b')


class AdvisoryLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name)

    def event(self, name, **fields):
        return dict(hook_event_name=name, session_id='owner', turn_id='turn-1', cwd='/tmp', **fields)

    def run_hook(self, event):
        return troubleshooting.handle(event, state_dir=self.state_dir)

    def assert_advisory(self, result, name):
        self.assertEqual({'hookSpecificOutput': {'hookEventName': name,
                                               'additionalContext': troubleshooting.RULE}}, result)

    def test_session_start_then_prompts_supply_one_advisory_total(self):
        self.assert_advisory(self.run_hook(self.event('SessionStart')), 'SessionStart')
        for name, fields in (('UserPromptSubmit', {'prompt': 'Why does it fail?'}),
                             ('UserPromptSubmit', {'prompt': 'Explain the proposed architecture.'}),
                             ('SessionStart', {})):
            with self.subTest(name=name, fields=fields):
                self.assertIsNone(self.run_hook(self.event(name, **fields)))
        self.assertEqual(troubleshooting.RULE_VERSION, troubleshooting.State(self.state_dir).read('owner')['reason'])

    def test_first_user_prompt_can_supply_advisory_without_session_start(self):
        self.assert_advisory(self.run_hook(self.event('UserPromptSubmit', prompt='Where is reset?')), 'UserPromptSubmit')
        self.assertIsNone(self.run_hook(self.event('UserPromptSubmit', prompt='What does reset do?')))

    def test_concurrent_session_and_user_start_cannot_duplicate_context(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            events = [self.event('SessionStart'), self.event('UserPromptSubmit', prompt='Explain the issue.')]
            results = list(workers.map(self.run_hook, events))
        self.assertEqual(1, sum(result is not None for result in results))

    def test_other_session_can_receive_its_own_advisory(self):
        self.run_hook(self.event('SessionStart'))
        event = self.event('SessionStart')
        event['session_id'] = 'other'
        self.assert_advisory(self.run_hook(event), 'SessionStart')

    def test_machine_continuations_never_inject_or_touch_state(self):
        prompts = ['TROUBLESHOOTING_REPORT: Rewrite the report using the four parts.',
                   'CI_PENDING WAIT: Build still running.', 'OWNED_PR_PROGRESS WAIT: Publish work.',
                   'Durable prd review followup abc.', '<heartbeat>Check pending CI.</heartbeat>',
                   '<hook_prompt>Rewrite the answer.</hook_prompt>', '[automated] Resume.',
                   'Continue.', 'Carry on.', 'Go ahead.', 'Proceed.', 'Status?', "How's it going?"]
        with patch.object(troubleshooting, 'State', side_effect=AssertionError('continuation cannot inject guidance')):
            for prompt in prompts:
                with self.subTest(prompt=prompt):
                    self.assertIsNone(self.run_hook(self.event('UserPromptSubmit', prompt=prompt)))
            self.assertIsNone(self.run_hook(self.event('UserPromptSubmit', prompt='Why does it fail?', stop_hook_active=True)))
        self.assert_advisory(self.run_hook(self.event('UserPromptSubmit', prompt='Diagnose startup.')), 'UserPromptSubmit')

    def test_stop_and_subagent_stop_never_validate_or_read_state(self):
        texts = ['Fixed.', '', '**Topic:** Broken.\n**Evidence:** Unknown.', VALID, 'No new updates']
        with patch.object(troubleshooting, 'validate_report', side_effect=AssertionError('Stop cannot validate format')), \
                patch.object(troubleshooting, 'State', side_effect=AssertionError('Stop cannot read format state')):
            for name in ('Stop', 'SubagentStop'):
                for text in texts:
                    with self.subTest(name=name, text=text):
                        self.assertIsNone(self.run_hook(self.event(name, agent_id='child',
                                                                  last_assistant_message=text, stop_hook_active=True)))
                        self.assertIsNone(self.run_hook(self.event(name, lastAssistantMessage=text)))

    def test_prior_mandatory_report_state_cannot_restart_a_chat(self):
        state = troubleshooting.State(self.state_dir)
        reason = 'TROUBLESHOOTING_REPORT: Use all four labels. Rewrite the report.'
        state.write('owner', 'old-turn', True, 'diagnostic request', reason)
        before = state.read('owner')
        for name in ('Stop', 'SubagentStop', 'PostToolUseFailure'):
            self.assertIsNone(self.run_hook(self.event(name, last_assistant_message='Fixed.')))
        self.assertIsNone(self.run_hook(self.event('UserPromptSubmit', prompt=reason)))
        self.assertEqual(before, state.read('owner'))
        self.assert_advisory(self.run_hook(self.event('UserPromptSubmit', prompt='Explain the issue.')), 'UserPromptSubmit')
        self.assertFalse(state.read('owner')['active'])
        self.assertIsNone(state.read('owner')['block_reason'])

    def test_tool_failure_and_handback_never_inject_or_deny(self):
        with patch.object(troubleshooting, 'State', side_effect=AssertionError('tools cannot inject guidance')):
            for name in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure'):
                event = self.event(name, tool_name='SubagentHandback', tool_input={'message': 'Found it.'},
                                   tool_response={'exit_code': 1, 'isError': True}, error='worker failed')
                self.assertIsNone(self.run_hook(event))
            self.assertIsNone(self.run_hook(self.event('SubagentStart', agent_id='child')))

    def test_github_review_payloads_remain_unformatted(self):
        self.run_hook(self.event('UserPromptSubmit', prompt='Why does startup fail?'))
        for name in ('PreToolUse', 'PostToolUse'):
            self.assertIsNone(self.run_hook(self.event(name, tool_name='mcp__github__add_reply_to_pull_request_comment',
                                                      tool_input={'body': 'Fixed in abc1234: supply the existing configuration.'})))
        self.assertIsNone(self.run_hook(self.event('Stop', last_assistant_message='Replied to the review.')))

    def test_missing_session_never_blocks(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.run_hook({'hook_event_name': 'SessionStart'}))
            self.assertIsNone(self.run_hook({'hook_event_name': 'Stop'}))

    def test_unchanged_background_checks_stay_quiet_and_status_may_be_simple(self):
        self.assertIsNone(self.run_hook(self.event('UserPromptSubmit', prompt='<heartbeat>Check unchanged state.</heartbeat>')))
        self.assertIsNone(self.run_hook(self.event('PostToolUse', tool_name='Bash', tool_response={'exit_code': 0})))
        self.assertIsNone(self.run_hook(self.event('Stop', last_assistant_message='')))
        self.assertIsNone(self.run_hook(self.event('UserPromptSubmit', prompt='Status?')))
        self.assertIsNone(self.run_hook(self.event('Stop', last_assistant_message='No new updates')))


class EntryPoint(unittest.TestCase):
    def test_advisory_failures_cannot_emit_denial_or_stop_block(self):
        for name in ('Stop', 'SubagentStop', 'PreToolUse', 'SessionStart', 'UserPromptSubmit'):
            with self.subTest(name=name), patch.object(troubleshooting.sys, 'stdin', io.StringIO(json.dumps({'hook_event_name': name}))), \
                    patch.object(troubleshooting.sys, 'stdout', new_callable=io.StringIO) as output, \
                    patch.object(troubleshooting, 'handle', side_effect=OSError('state unavailable')):
                troubleshooting.main()
                self.assertEqual('', output.getvalue())

    def test_malformed_event_fails_open(self):
        for raw in ('not JSON', '[]'):
            with self.subTest(raw=raw), patch.object(troubleshooting.sys, 'stdin', io.StringIO(raw)), \
                    patch.object(troubleshooting.sys, 'stdout', new_callable=io.StringIO) as output:
                troubleshooting.main()
                self.assertEqual('', output.getvalue())


if __name__ == '__main__':
    unittest.main()
