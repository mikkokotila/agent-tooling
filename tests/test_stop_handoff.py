import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from caller_pr_gates import hook
from caller_pr_gates.policy import evaluate_pr
from caller_pr_gates.store import Store
from test_handoff import quota_exhausted
from test_policy import snapshot, check


class StopHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.register('s', 'codex', 'owner/repo', 1, self.tmp.name)
        self.data = snapshot()

    def stop(self, text, **fields):
        event = dict(hook_event_name='Stop', session_id='s', cwd=self.tmp.name,
                     turn_id='turn-1', last_assistant_message=text, **fields)
        async def collector(*args):
            return self.data
        with patch('caller_pr_gates.github.collect_pr', collector):
            return hook.handle(event, self.store)

    def test_exact_13_denials_end_without_restarts_and_keep_unknown(self):
        self.data.update(state='closed', merged=True, expected_checks=None,
                         known_expected_checks=['test'], component_errors={'rules': {
                             'kind': 'feature_unavailable', 'http_status': 403,
                             'path': '/repos/owner/repo/rules/branches/main',
                             'message': 'Upgrade to GitHub Pro or make this repository public to enable this feature.',
                             'rate_limit_headers': {'x-ratelimit-remaining': '4725'}}})
        for client in ('codex', 'claude'):
            with self.subTest(client=client), patch.dict(os.environ, CALLER_HOOK_CLIENT=client):
                for n in range(13):
                    self.data['component_errors']['rules']['rate_limit_headers']['x-ratelimit-remaining'] = str(4725-n)
                    result = self.stop('The feature is deployed. Completion verification is blocked by unavailable repository rules.', stop_hook_active=bool(n))
                    self.assertNotEqual('block', (result or {}).get('decision'))
                saved = self.store.snapshot('owner/repo', 1)
                self.assertEqual('closed', saved['snapshot']['state'])
                self.assertEqual({'UNKNOWN'}, {g['status'] for g in saved['gates'] if g['gate'].startswith('CI_')})
                self.assertEqual('held_unresolved', self.store.recovery('owner/repo', 1, 's')['state'])
                self.assertEqual('active', self.store.prs('s')[0]['status'])

    def test_actionable_ci_gets_one_repair_instruction_then_control_returns(self):
        self.data['checks'] = [check(conclusion='failure')]
        first = self.stop('I will fix the failing browser test.')
        self.assertEqual('block', first['decision'])
        self.assertIn('actual failing CI', first['reason'])
        second = self.stop('Blocked: the fix needs an unavailable account service.', stop_hook_active=True)
        self.assertIsNone(second)
        self.assertEqual('FAIL', self.store.snapshot('owner/repo', 1)['gates'][1]['status'])

    def test_pending_only_yields_without_completing_or_repeating(self):
        self.data['checks'] = [check(status='in_progress', conclusion=None)]
        first = self.stop('CI is pending; no new source correction is needed.')
        self.assertNotEqual('block', (first or {}).get('decision'))
        self.assertIsNone(self.stop('CI is pending; no new source correction is needed.', stop_hook_active=True))
        self.assertEqual('WAIT', self.store.snapshot('owner/repo', 1)['gates'][2]['status'])

    def test_false_completion_corrected_once_but_never_green(self):
        self.data['checks'] = [check(status='in_progress', conclusion=None)]
        self.assertEqual('block', self.stop('All checks passed. Task is complete.')['decision'])
        repeated = self.stop('All checks passed. Task is complete.', stop_hook_active=True)
        self.assertNotEqual('block', repeated.get('decision'))
        self.assertIn('incomplete or unverified', repeated['systemMessage'])
        self.assertEqual('WAIT', self.store.snapshot('owner/repo', 1)['gates'][2]['status'])

    def test_new_failed_attempt_allows_one_new_action(self):
        self.data['checks'] = [check(conclusion='failure')]
        self.stop('Investigating the browser failure.')
        self.stop('Blocked: current service unavailable.', stop_hook_active=True)
        self.data['checks'] = [check(id='11', run_attempt=2, conclusion='failure')]
        self.assertEqual('block', self.stop('Investigating the new failure.', stop_hook_active=True)['decision'])

    def test_collector_exception_replaces_stale_green_and_yields(self):
        self.store.set_snapshot('owner/repo', 1, self.data, evaluate_pr(self.data, []))
        async def fail(*args):
            raise RuntimeError('GitHub 403: permission denied')
        with patch('caller_pr_gates.github.collect_pr', fail):
            result = hook.handle(dict(hook_event_name='Stop', session_id='s', cwd=self.tmp.name,
                                      last_assistant_message='Verification is blocked by GitHub access.'), self.store)
        self.assertNotEqual('block', (result or {}).get('decision'))
        self.assertEqual('UNKNOWN', self.store.snapshot('owner/repo', 1)['gates'][0]['status'])

    def test_unchanged_quota_outage_is_reported_once_then_yields(self):
        # An unpublished branch is actionable; the rate-limited PR data is not.
        with self.store._connect() as db:
            db.execute('INSERT INTO unpublished_work VALUES(?,?,?,?,?)', ('s', 'owner/repo', 'fix', self.tmp.name, time.time()))
        def outage_stop(error, text, **fields):
            async def collector(*args):
                raise error
            event = dict(hook_event_name='Stop', session_id='s', cwd=self.tmp.name, turn_id='turn-1',
                         last_assistant_message=text, **fields)
            with patch('caller_pr_gates.github.collect_pr', collector):
                result = hook.handle(event, self.store)
            with self.store._connect() as db:
                digest = db.execute('SELECT fingerprint FROM stop_feedback WHERE session_id=?', ('s',)).fetchone()[0]
            return result, digest, self.store.snapshot('owner/repo', 1)['gates'][0]['details'][0]
        first, first_digest, first_detail = outage_stop(
            quota_exhausted('32CC:2453F2:2CE3791:42ECEE3:6AC9436A', '2026-10-09 19:41:30', used='5004'),
            'Publishing the branch next.')
        self.assertEqual('block', first['decision'])
        second, second_digest, second_detail = outage_stop(
            quota_exhausted('3320:9F412:2F4352D:46AAF19:6AC943D0', '2026-10-09 19:43:12'),
            'Blocked: the GitHub API quota is exhausted until it resets, so the branch cannot be published yet.',
            stop_hook_active=True)
        self.assertNotEqual(first_detail, second_detail)
        self.assertEqual(first_digest, second_digest)
        self.assertIsNone(second)
        self.assertEqual('held_unresolved', self.store.recovery('owner/repo', 1, 's')['state'])
