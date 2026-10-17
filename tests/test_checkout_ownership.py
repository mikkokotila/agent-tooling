"""Scratch writes and historical receipts cannot adopt a different chat's PR."""
import asyncio
from unittest.mock import patch

import pytest

from caller_pr_gates import checklist, cli, hook
from test_quiet_monitor import local, row, SID, OTHER


@pytest.mark.parametrize('command,expected', [
    ("mkdir -p /private/tmp/scratch/mk && cd /private/tmp/scratch/mk && cat > render-sampler.mjs <<'EOF'\nconst text = 'image';\nEOF\nnode render-sampler.mjs", '/private/tmp/scratch/mk'),
    ('cd /private/tmp && cd scratch && cat > output.txt', '/private/tmp/scratch'),
    ('cd -- "/private/tmp/scratch space" && cat > output.txt', '/private/tmp/scratch space'),
    ('cat > output.txt && cd /private/tmp', '/repo'),
])
def test_redirect_uses_directory_when_write_occurs(command, expected):
    event = dict(hook_event_name='PostToolUse', session_id=OTHER, cwd='/repo', tool_name='Bash',
                 tool_input={'command': command}, tool_response={'exit_code': 0})
    assert hook._mutation_dirs(event) == [expected]
    with patch.object(hook, '_git_context', return_value=None) as context, patch.object(hook, 'discover_current_pr', side_effect=AssertionError('scratch work must not read GitHub')):
        class NoPRs:
            def prs(self, sid): return []
        hook.discover(event, NoPRs())
    assert [c.args[0] for c in context.call_args_list] == [expected]


def test_dynamic_cd_does_not_guess_caller_checkout():
    event = dict(hook_event_name='PostToolUse', cwd='/repo', tool_name='Bash',
                 tool_input={'command': 'cd "$SCRATCH" && cat > output.txt'}, tool_response={'exit_code': 0})
    assert hook._mutation_dirs(event) == []


@pytest.mark.parametrize('client', ['codex', 'claude'])
def test_shared_checkout_mutation_cannot_adopt_existing_owner(local, monkeypatch, client):
    store = local[0]
    monkeypatch.setenv('CALLER_HOOK_CLIENT', client)
    event = dict(hook_event_name='PostToolUse', session_id=OTHER, cwd=row(store)['cwd'], tool_name='Write',
                 tool_input={'file_path': row(store)['cwd'] + '/unrelated.txt'}, tool_response={'exit_code': 0})
    with patch.object(hook, '_git_context', return_value=(row(store)['cwd'], 'owner/repo', 'feature')), patch.object(hook, 'discover_current_pr', return_value={'found': True, 'pull_number': 1}):
        assert checklist.handle(event, store) is None
    assert store.prs(OTHER) == []
    assert row(store)['owner_session'] == SID
    assert cli.session_prs(store, OTHER) == []


def test_legacy_foreign_receipt_is_retained_but_not_reported_as_owned(local):
    store = local[0]
    store.register(OTHER, 'claude', 'owner/repo', 1, row(store)['cwd'])
    assert len(store.prs(OTHER)) == 1  # Preserve the historical receipt.
    assert cli.session_prs(store, OTHER) == []
    assert len(cli.session_prs(store, SID)) == 1
    with patch('caller_pr_gates.github.collect_pr', side_effect=AssertionError('foreign receipt cannot cause GitHub reads')):
        assert asyncio.run(cli.inspect(store, OTHER)) == []
