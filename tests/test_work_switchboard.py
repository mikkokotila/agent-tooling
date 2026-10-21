"""Switchboard installs with the existing role path without starting an observer."""
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import manage
import work_manager
import work_switchboard


def test_client_routes_preserve_personal_manager_and_private_state(tmp_path):
    private = tmp_path / '.codex/switchboard/sessions/stopped/state.json'
    private.parent.mkdir(parents=True)
    private.write_bytes(b'{"stopped":true,"pending":"private answer"}')
    for client, filename in [('codex', 'AGENTS.md'), ('claude', 'CLAUDE.md')]:
        target = tmp_path / ('.' + client) / filename
        target.parent.mkdir(exist_ok=True)
        original = work_manager.guidance(b'Personal prefix.\n<!-- caller-pr-gates:begin -->\n<!-- caller-pr-gates:end -->\n## Personal\nKeep this.\n', ROOT, tmp_path)
        target.write_bytes(original)
        assets = manage.desired_assets(tmp_path, None)
        updated = assets[str(target)]
        assert updated.startswith(b'Personal prefix.')
        assert updated.endswith(b'## Personal\nKeep this.\n')
        assert updated.count(work_manager.BEGIN.encode()) == 1
        assert updated.count(work_switchboard.BEGIN.encode()) == 1
        assert str(tmp_path / '.codex/switchboard/SWITCHBOARD.md').encode() in updated
        assert work_switchboard.guidance(updated, ROOT, tmp_path) == updated
        assert b'${HOME}' not in updated
    assert private.read_bytes() == b'{"stopped":true,"pending":"private answer"}'
    installed = work_switchboard.assets(ROOT, tmp_path)
    assert set(installed) == {str(tmp_path / '.codex/switchboard/SWITCHBOARD.md')}
    assert b'${HOME}' not in next(iter(installed.values()))
    assert not any('sessions/' in path or 'config.json' in path for path in assets)


@pytest.mark.parametrize('text', [work_switchboard.BEGIN, work_switchboard.END,
    work_switchboard.BEGIN + work_switchboard.BEGIN + work_switchboard.END,
    work_switchboard.END + work_switchboard.BEGIN])
def test_ambiguous_or_reversed_markers_refuse_before_writes(tmp_path, text):
    with pytest.raises(RuntimeError, match='Switchboard guidance markers'):
        work_switchboard.guidance(text.encode(), ROOT, tmp_path)
    assert not list(tmp_path.iterdir())


def test_wheel_includes_exact_canonical_role():
    wheel = ROOT / '.build/dist' / f'agent_tooling-{manage.VERSION}-py3-none-any.whl'
    with zipfile.ZipFile(wheel) as archive:
        for name in ['SWITCHBOARD.md', 'global-guidance.md']:
            assert archive.read('agent_tooling_assets/switchboard/' + name) == (ROOT / 'switchboard' / name).read_bytes()


def test_role_host_matrix_and_browser_isolation_contract():
    role = (ROOT / 'switchboard/SWITCHBOARD.md').read_text()
    for host in ['Codex', 'Claude']:
        assert f'| {host} | All | All | All | Yes, dedicated separate browser window | Never |' in role
    assert '| Muse | All | All | All | Not required | Never |' in role
    assert 'A tab or group in the user' in role
    assert 'no supported separate-window capability' in role
    assert 'you are switchboard' in (ROOT / 'AGENTS.md').read_text()
