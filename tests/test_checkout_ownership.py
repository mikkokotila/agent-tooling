"""Scratch writes and historical receipts cannot adopt a different chat's PR."""
import asyncio
from unittest.mock import patch

import pytest

from caller_pr_gates import checklist, cli, hook
from test_quiet_monitor import local, row, SID, OTHER


@pytest.mark.parametrize('command,expected', [
    ("mkdir -p /private/tmp/scratch/mk && cd /private/tmp/scratch/mk && cat > render-sampler.mjs <<'EOF'\nconst text = 'image';\nEOF\nnode render-sampler.mjs", None),
    ('cd /private/tmp && cd scratch && cat > output.txt', None),
    ('cd -- "/private/tmp/scratch space" && cat > output.txt', None),
    ('cd -P /other-checkout && cat > output.txt', None),
    ('cd -L /other-checkout && cat > output.txt', None),
    ('cd -L -P -- /other-checkout && cat > output.txt', None),
    ('cd -- -P && cat > output.txt', None),
    ('cd -s /other-checkout && cat > output.txt', None),
    ('cd -P && cat > output.txt', None),
    ('cd&&cat > output.txt', None),
    ('cd && cat > output.txt', None),
    ("cd '~' && cat > output.txt", None),
    ('cd "~" && cat > output.txt', None),
    (r'cd \~ && cat > output.txt', None),
    ("cd -P -- '~' && cat > output.txt", None),
    ('cd ~/scratch && cat > output.txt', None),
    ('cd ~"user"/scratch && cat > output.txt', None),
    ('command cd /tmp/scratch && cat > output.txt', None),
    ('command -p cd /tmp/scratch && cat > output.txt', None),
    ("builtin 'cd' /tmp/scratch && cat > output.txt", None),
    ('eval "cd /tmp/scratch" && cat > output.txt', None),
    ('cd ~- && cat > relative.txt && cat > /repo/output.txt', '/repo'),
    ('cd ~+ && cat > relative.txt', None),
    ('cd /other-checkout > output.txt', '/repo'),
    ('cd /first && cd /second > output.txt', None),
    ('command cd /other-checkout > output.txt', '/repo'),
    ("'cd' /tmp/scratch && cat > output.txt", None),
    (r'printf x\;; cd /tmp/scratch && cat > output.txt', None),
    ('cd /tmp/scratch-* && cat > output.txt', None),
    ('CDPATH=/tmp && cd scratch && cat > output.txt', None),
    ('cd /other-checkout && touch output.txt', None),
    ('cd /other-checkout && touch /repo/output.txt', '/repo'),
    ('cd /other-checkout && git commit -m fix', None),
    ('git -C /repo commit -m fix', '/repo'),
    ('cat > output.txt', '/repo'),
    ('printf x > >(cat > /tmp/output.txt)', '/tmp'),
    ('cd /tmp/scratch # \\\ncat > output.txt', None),
    (r'git -C /tmp/other\ checkout commit -m fix', '/tmp/other checkout'),
    ('git -C "/tmp/other checkout" commit -m fix', '/tmp/other checkout'),
    ('git -C /tmp/other -C second commit -m fix', None),
    ('printf x # > /repo/output.txt', None),
    ('cat > output#name.txt', None),
    ('printf x >| /tmp/output.txt', '/tmp'),
    ("printf x > '|'", None),
    ("cat > /tmp/script.sh <<'EOF'\ncat > /repo/output.txt\nEOF", '/tmp'),
    (r'cd /private/tmp/scratch\ space && cat > output.txt', None),
    (r'cd /private/tmp/scratch\ space&&cat > output.txt', None),
    (r'cd /private/tmp/scratch\;name && cat > output.txt', None),
    ('cd /private/tmp/scratch" space" && cat > output.txt', None),
    ("printf 'x; cd /tmp/scratch ;' && cat > output.txt", None),
    ('printf "x; cd /tmp/scratch ;" && cat > output.txt', None),
    (r'printf x\; cd /tmp/scratch && cat > output.txt', None),
    ('cd "$SCRATCH" && cat > /repo/output.txt', '/repo'),
    (r'cd "$SCRATCH" && cat > /repo/dir\ name/output.txt', '/repo/dir name'),
    ('cd "$SCRATCH" && cat > /repo/output.txt && cat > relative.txt', '/repo'),
    ('false && cd /tmp/scratch; cat > output.txt', None),
    ('true || cd /tmp/scratch && cat > output.txt', None),
    ('false || cd /tmp/scratch && cat > output.txt', None),
    ('cd /tmp/scratch | cat && cat > output.txt', None),
    ('cd /tmp/scratch && cat > output.txt; true', None),
    ('false && cd /tmp/scratch; cat > /repo/output.txt', '/repo'),
    ('cat > output.txt && cd /private/tmp', '/repo'),
])
def test_shell_write_destination_is_exact_or_unknown(command, expected):
    event = dict(hook_event_name='PostToolUse', session_id=OTHER, cwd='/repo', tool_name='Bash',
                 tool_input={'command': command}, tool_response={'exit_code': 0})
    expected = [expected] if expected is not None else []
    assert hook._mutation_dirs(event) == expected
    with patch.object(hook, '_git_context', return_value=None) as context, patch.object(hook, 'discover_current_pr', side_effect=AssertionError('scratch work must not read GitHub')):
        class NoPRs:
            def prs(self, sid): return []
        hook.discover(event, NoPRs())
    assert [c.args[0] for c in context.call_args_list] == expected


def test_dynamic_cd_does_not_guess_caller_checkout():
    event = dict(hook_event_name='PostToolUse', cwd='/repo', tool_name='Bash',
                 tool_input={'command': 'cd "$SCRATCH" && cat > output.txt'}, tool_response={'exit_code': 0})
    assert hook._mutation_dirs(event) == []


def test_unknown_tilde_user_preserves_later_absolute_write():
    event = dict(hook_event_name='PostToolUse', cwd='/repo', tool_name='Bash',
                 tool_input={'command': 'cd ~missing_user && cat > relative.txt && cat > /repo/output.txt'},
                 tool_response={'exit_code': 0})
    with patch('pwd.getpwnam', side_effect=KeyError('missing_user')):
        assert hook._mutation_dirs(event) == ['/repo']


@pytest.mark.parametrize('option', ['-L', '-P'])
def test_symlink_directory_changes_require_explicit_registration(tmp_path, option):
    logical = tmp_path / 'logical'
    physical = tmp_path / 'physical'
    logical.mkdir(); (physical / 'child').mkdir(parents=True)
    (logical / 'link').symlink_to(physical / 'child', target_is_directory=True)
    command = f'cd {option} {logical}/link/.. && cat > output.txt'
    event = dict(hook_event_name='PostToolUse', cwd='/repo', tool_name='Bash',
                 tool_input={'command': command}, tool_response={'exit_code': 0})
    assert hook._mutation_dirs(event) == []


@pytest.mark.parametrize('client', ['codex', 'claude'])
@pytest.mark.parametrize('command', [
    r'cd /private/tmp/scratch\ space && cat > output.txt',
    "printf 'x; cd /tmp/scratch ;' && cat > output.txt",
    'cd "$SCRATCH" && cat > /repo/output.txt',
    'false && cd /tmp/scratch; cat > output.txt',
    'cd -P /other-checkout && cat > output.txt',
    'cd -s /other-checkout && cat > output.txt',
    'cd&&cat > output.txt',
    "cd '~' && cat > output.txt",
    'command cd /tmp/scratch && cat > output.txt',
    'cd ~- && cat > relative.txt && cat > /repo/output.txt',
    'cd /other-checkout > output.txt',
    "'cd' /tmp/scratch && cat > output.txt",
    r'printf x\;; cd /tmp/scratch && cat > output.txt',
    'cd /tmp/scratch-* && cat > output.txt',
    'CDPATH=/tmp && cd scratch && cat > output.txt',
    'printf x > >(cat > /tmp/output.txt)',
    'cd /tmp/scratch # \\\ncat > output.txt',
    r'git -C /tmp/other\ checkout commit -m fix',
    'printf x >| /tmp/output.txt',
    "cat > /tmp/script.sh <<'EOF'\ncat > /repo/output.txt\nEOF",
])
def test_redirect_completes_native_discovery(local, monkeypatch, client, command):
    store = local[0]
    monkeypatch.setenv('CALLER_HOOK_CLIENT', client)
    event = dict(hook_event_name='PostToolUse', session_id=OTHER, cwd=row(store)['cwd'], tool_name='Bash',
                 tool_input={'command': command},
                 tool_response={'exit_code': 0})
    with patch.object(hook, '_git_context', return_value=None), patch.object(hook, 'discover_current_pr', side_effect=AssertionError('scratch work must not read GitHub')):
        assert checklist.handle(event, store) is None
    assert store.prs(OTHER) == []
    assert row(store)['owner_session'] == SID
    assert len(cli.session_prs(store, SID)) == 1
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event IN ('checklist_discovery_error','checklist_native_error')").fetchone()[0] == 0


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
