import json
import unittest
from caller_pr_gates import hook
from test_hook import Memory


class PatchDraftDataRegression(unittest.TestCase):
    def event(self, tool, arguments):
        return {'hook_event_name': 'PreToolUse', 'session_id': 'patch-session', 'cwd': '/tmp', 'tool_name': tool, 'tool_input': arguments}

    def test_native_file_edits_can_contain_draft_command_and_api_fixtures(self):
        fixture = 'gh pr create --draft; gh api graphql -f query="mutation { convertPullRequestToDraft }"; {"isDraft":true}'
        for tool in ['Write', 'Edit', 'apply_patch', 'functions.apply_patch']:
            with self.subTest(tool=tool):
                arguments = {'code': '*** Begin Patch\n*** Add File: /tmp/test.py\n+' + fixture + '\n*** End Patch'}
                self.assertIsNone(hook.handle(self.event(tool, arguments), Memory()))

    def test_nested_patch_content_does_not_execute_quoted_shell_calls(self):
        fixture = 'gh pr create --draft; convertPullRequestToDraft; {"draft":true}; await tools.exec_command({cmd:"gh pr ready --undo"});'
        code = 'await tools.apply_patch(' + json.dumps(fixture) + ');'
        self.assertIsNone(hook.handle(self.event('functions.exec', {'code': code}), Memory()))

    def test_nested_real_draft_commands_still_denied_alongside_patch_data(self):
        for command in ['gh pr create --draft --title test', 'gh pr ready --undo']:
            with self.subTest(command=command):
                code = 'await tools.apply_patch("source text"); await tools.exec_command({cmd:' + json.dumps(command) + '});'
                result = hook.handle(self.event('functions.exec', {'code': code}), Memory())
                self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
                self.assertIn('PR_NOT_DRAFT', result['hookSpecificOutput']['permissionDecisionReason'])

    def test_nested_real_api_conversion_still_denied(self):
        command = 'gh api graphql -f query="mutation { convertPullRequestToDraft(input:{pullRequestId:\"PR_1\"}) { clientMutationId } }"'
        code = 'await tools.exec_command({cmd:' + json.dumps(command) + '});'
        result = hook.handle(self.event('functions.exec', {'code': code}), Memory())
        self.assertIn('PR_NOT_DRAFT', result['hookSpecificOutput']['permissionDecisionReason'])
