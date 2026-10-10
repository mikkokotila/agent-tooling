import tempfile
import unittest
from unittest.mock import patch

from caller_pr_gates.store import Store
from caller_pr_gates import hook, turns


class InformationalTurns(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.sid = 'parent'
        self.store.record_session(self.sid, 'codex', '/tmp')
        self.store.register(self.sid, 'codex', 'Owner/Repo', 77, '/tmp')
        self.job = self.store.register_followup(self.sid, 'Owner/Repo', 70, 0, {})

    def start(self, prompt, sid=None, turn_id='turn-1'):
        event = dict(hook_event_name='UserPromptSubmit', prompt=prompt, turn_id=turn_id)
        turns.start_turn(self.store, event, sid or self.sid)
        return event

    def yields(self, text='Press C to hide the overlay.', sid=None, turn_id='turn-1'):
        return turns.informational_yield(self.store, dict(hook_event_name='Stop', turn_id=turn_id),
                                        sid or self.sid, text)

    def test_read_only_answer_yields_without_changing_owned_pr_or_due_followup(self):
        prs, jobs = self.store.prs(self.sid), self.store.followups(self.sid)
        self.start('What is this overlay based on? How do I turn it off?')
        self.assertTrue(self.yields())
        self.assertEqual(self.store.prs(self.sid), prs)
        self.assertEqual(self.store.followups(self.sid), jobs)
        self.assertEqual(self.store.followups(self.sid)[0]['status'], 'pending')

    def test_hook_informational_stop_retains_failed_pr_without_engineering_evaluation(self):
        self.store.set_snapshot('Owner/Repo', 77, {'state': 'OPEN'},
                                [{'gate': 'CI_FAILED', 'status': 'FAIL', 'details': ['build failed']}])
        before = self.store.prs(self.sid), self.store.followups(self.sid), self.store.snapshot('Owner/Repo', 77)
        event = dict(session_id=self.sid, cwd='/tmp', turn_id='question-turn')
        with patch.object(hook, 'evaluate', side_effect=AssertionError('informational Stop must not evaluate engineering work')), \
                patch.object(hook, '_finish_due_followups', side_effect=AssertionError('informational Stop must not perform document work')):
            hook.handle(dict(event, hook_event_name='UserPromptSubmit', prompt='How do I hide the overlay?'), self.store)
            result = hook.handle(dict(event, hook_event_name='Stop',
                                      last_assistant_message='Press C to hide the overlay.'), self.store)
        self.assertNotIn('decision', result)
        self.assertIn('no completion was recorded', result['systemMessage'])
        self.assertEqual(before, (self.store.prs(self.sid), self.store.followups(self.sid),
                                  self.store.snapshot('Owner/Repo', 77)))

    def test_question_forms_and_attachment_envelope(self):
        prompts = ['Why does this happen?', 'Where is the reset option?',
                   'Can you explain how selection works?', 'Explain the control.',
                   'Is PR77 merged?', 'How do I fix the display myself?',
                   '# Files mentioned by the user:\n\n## My request:\nNot sure what this is. What is it based on? How to turn it off?\n<image name="one"></image>']
        for n, prompt in enumerate(prompts):
            with self.subTest(prompt=prompt):
                self.start(prompt, turn_id=str(n))
                self.assertTrue(self.yields(turn_id=str(n)))

    def test_engineering_actions_override_questions(self):
        prompts = ['Fix this overlay.', 'Can you implement the reset?',
                   'Why is CI failing? Fix it.', 'Explain it and add a toggle.',
                   "Let's implement what you proposed.", 'Where should it go? Then make the PR.',
                   'Run tests and explain the failure.', 'Update the docs. How does it work?',
                   'Continue.', 'Looks good.', '']
        for n, prompt in enumerate(prompts):
            with self.subTest(prompt=prompt):
                self.start(prompt, turn_id=str(n))
                self.assertFalse(self.yields(turn_id=str(n)))

    def test_actual_mutation_disables_informational_yield(self):
        self.start('Why is it showing this?')
        turns.note_mutation(self.store, dict(tool_name='apply_patch', turn_id='turn-1'), self.sid)
        self.assertFalse(self.yields())
        self.assertEqual(len(self.store.prs(self.sid)), 1)
        self.assertEqual(self.store.followups(self.sid)[0]['status'], 'pending')

    def test_child_mutation_disables_parent_and_sibling_yield(self):
        self.store.record_session('child', 'codex', '/tmp', parent_session=self.sid)
        self.store.record_session('sibling', 'codex', '/tmp', parent_session=self.sid)
        self.start('What does this do?')
        self.start('Explain the function.', 'sibling', 'sibling-turn')
        turns.note_mutation(self.store, dict(tool_name='apply_patch'), 'child')
        self.assertFalse(self.yields())
        self.assertFalse(self.yields(sid='sibling', turn_id='sibling-turn'))

    def test_child_engineering_request_disables_parent_yield(self):
        self.store.record_session('child', 'codex', '/tmp', parent_session=self.sid)
        self.start('How does the menu work?')
        self.start('Implement a menu toggle.', 'child', 'child-turn')
        self.assertFalse(self.yields())

    def test_read_only_child_inspection_preserves_parent_information_mode(self):
        self.store.record_session('child', 'codex', '/tmp', parent_session=self.sid)
        self.start('How does the menu work?')
        self.start('Inspect source and report the reset location.', 'child', 'child-turn')
        self.assertTrue(self.yields())
        self.assertFalse(self.yields(sid='child', turn_id='child-turn'))
        turns.note_mutation(self.store, dict(tool_name='apply_patch'), 'child')
        self.assertFalse(self.yields())

    def test_other_root_mutation_does_not_change_this_question(self):
        self.store.record_session('other', 'codex', '/tmp')
        self.start('Where is the menu?')
        turns.note_mutation(self.store, dict(tool_name='apply_patch'), 'other')
        self.assertTrue(self.yields())

    def test_replayed_submission_cannot_reset_a_mutation(self):
        event = self.start('Why does it happen?')
        turns.note_mutation(self.store, dict(tool_name='apply_patch'), self.sid)
        turns.start_turn(self.store, event, self.sid)
        self.assertFalse(self.yields())

    def test_new_question_can_yield_after_prior_engineering_turn(self):
        self.start('Implement a reset.', turn_id='old-turn')
        turns.note_mutation(self.store, dict(tool_name='apply_patch'), self.sid)
        self.start('Where is the reset?', turn_id='new-question')
        self.assertTrue(self.yields(turn_id='new-question'))
        self.assertEqual(self.store.followups(self.sid)[0]['status'], 'pending')

    def test_completion_claims_cannot_use_informational_yield(self):
        self.start('How is PR77 doing?')
        for final in ['PR77 is done.', 'PR #77 has been merged.', 'All CI pass.',
                      '**All checks passed**.', 'Every gate is green.', 'Merged.',
                      'I merged PR77.', 'The implementation is complete.', '']:
            with self.subTest(final=final): self.assertFalse(self.yields(final))

    def test_quoted_user_questions_are_not_completion_claims(self):
        self.start('Is PR77 done?')
        self.assertTrue(self.yields('You asked, “Is PR77 done?” The review is still pending.'))
        self.assertTrue(self.yields('> All CI pass?\n\nThe checks are still running.'))
        self.assertFalse(self.yields('You asked, “Is PR77 done?” PR77 is done.'))

    def test_negative_status_is_not_an_affirmative_completion_claim(self):
        self.start('How is PR77 doing?')
        self.assertTrue(self.yields('PR77 is not done. CI is still running.'))

    def test_machine_recovery_heartbeat_and_hook_prompts_are_engineering(self):
        prompts = ['Durable prd review followup abc. Why is CI failing?',
                   '<heartbeat>Explain the current PR status.</heartbeat>',
                   '<hook_prompt>Why does CI fail?</hook_prompt>',
                   'CI_FAILED FAIL: Why is build failing?',
                   'OWNED_PR_PROGRESS WAIT: Explain unfinished work.']
        for n, prompt in enumerate(prompts):
            with self.subTest(prompt=prompt):
                self.start(prompt, turn_id=str(n))
                self.assertFalse(self.yields(turn_id=str(n)))

    def test_unknown_turn_or_mismatched_stop_never_yields(self):
        self.assertFalse(self.yields())
        self.start('How do I hide it?')
        self.assertFalse(self.yields(turn_id='other-turn'))

    def test_claude_without_turn_id_uses_persisted_uuid_at_stop(self):
        self.store.record_session('claude', 'claude', '/tmp')
        self.start('Where is the reset?', 'claude', None)
        restarted = Store(self.directory.name)
        self.assertTrue(turns.informational_yield(restarted, dict(hook_event_name='Stop'),
                                                 'claude', 'The reset icon is in the header.'))
        with restarted._connect() as db:
            turn_id = db.execute('SELECT turn_id FROM turn_modes WHERE session_id=?', ('claude',)).fetchone()[0]
        self.assertEqual(len(turn_id), 36)


if __name__ == '__main__':
    unittest.main()
