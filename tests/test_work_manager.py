"""Manager deployment must preserve private work and report only proved usage."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import manage
import work_manager

spec = importlib.util.spec_from_file_location('manager_turn_usage', ROOT / 'manager/turn_usage.py')
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)


def event(kind, **payload):
    return {'type': 'event_msg', 'timestamp': '2026-01-01T00:00:00Z',
            'payload': {'type': kind, **payload}}


def count(i, o, cached=0, reasoning=0):
    return event('token_count', info={'total_token_usage': {
        'input_tokens': i, 'cached_input_tokens': cached, 'output_tokens': o,
        'reasoning_output_tokens': reasoning, 'total_tokens': i + o},
        # This single-call value must never be confused with a whole turn.
        'last_token_usage': {'total_tokens': 1}})


def recorded(tmp_path, events):
    path = tmp_path / 'rollout.jsonl'
    path.write_text(''.join(json.dumps(e) + '\n' for e in events))
    return usage.completed_usage(path)


def test_completed_turn_sums_calls_and_does_not_double_count_cached_or_reasoning(tmp_path):
    baseline = count(1000, 100, 800, 20)
    latest = count(1700, 160, 1400, 50)
    result = recorded(tmp_path, [baseline, event('task_started', turn_id='a'),
        count(1400, 130, 1100, 35), latest, latest,
        event('task_complete', turn_id='a'), event('task_started', turn_id='b'),
        count(2000, 200, 1600, 60)])
    assert result['turn_id'] == 'a'
    assert result['total_tokens'] == 760
    assert result['cached_input_tokens'] == 600
    assert result['reasoning_output_tokens'] == 30


@pytest.mark.parametrize('events', [
    [count(20, 3), event('task_started', turn_id='a'), event('task_complete', turn_id='a')],
    [event('task_started', turn_id='a'), count(20, 3), event('task_complete', turn_id='a')],
    [count(20, 3), event('task_started', turn_id='a'), count(10, 2), event('task_complete', turn_id='a')],
    [count(10, 2), event('task_started', turn_id='a'), count(20, 3), event('task_complete', turn_id='other')],
    [count(10, 2), event('task_started', turn_id='a'), count(20, 3)],
    [count(10, 2), event('task_started', turn_id='a'), count(20, 3, cached=30), event('task_complete', turn_id='a')],
])
def test_unproved_usage_remains_unknown(tmp_path, events):
    assert recorded(tmp_path, events) is None


def test_latest_incomplete_accounting_does_not_return_an_older_completed_turn(tmp_path):
    events = [count(10, 1), event('task_started', turn_id='a'), count(20, 2),
              event('task_complete', turn_id='a'), event('task_started', turn_id='b'),
              count(5, 1), event('task_complete', turn_id='b')]
    assert recorded(tmp_path, events) is None


def test_missing_database_is_not_created(tmp_path):
    with pytest.raises(sqlite3.Error):
        usage.report('missing', tmp_path)
    assert not (tmp_path / '.codex/state_5.sqlite').exists()


def test_tail_without_required_boundary_is_unknown(tmp_path, monkeypatch):
    events = [count(10, 2), event('task_started', turn_id='a'),
              {'type': 'response_item', 'padding': 'x' * 4000}, count(20, 3),
              event('task_complete', turn_id='a')]
    monkeypatch.setattr(usage, 'LIMIT', 1500)
    assert recorded(tmp_path, events) is None


def test_global_migration_is_exact_and_preserves_other_human_instructions(tmp_path):
    old = work_manager.render(ROOT, 'legacy-guidance.json', tmp_path)
    legacy = json.loads(old)
    # Old Claude wording, with a synthetic identity, must become dynamic routing.
    worker = legacy['## Request separate work from the local manager'].replace(
        'existing human-appointed manager recorded in `'
        + str(tmp_path) + '/.codex/work-manager/state.json → manager_session`',
        'existing manager chat `00000000-0000-0000-0000-000000000000`')
    original = ('Personal prefix.\n<!-- caller-pr-gates:begin -->\n'
                '<!-- caller-pr-gates:end -->\n' + worker + '\n'
                + legacy['## Manager role and succession'] + '\n## Personal\nKeep this.\n')
    actual = work_manager.guidance(original.encode(), ROOT, tmp_path)
    assert actual.startswith(b'Personal prefix.') and actual.endswith(b'## Personal\nKeep this.\n')
    assert b'00000000' not in actual
    assert actual.count(work_manager.BEGIN.encode()) == 1
    assert work_manager.guidance(actual, ROOT, tmp_path) == actual
    with pytest.raises(RuntimeError, match='Edited legacy'):
        work_manager.guidance(original.replace('Continue other feasible', 'Human edited').encode(), ROOT, tmp_path)


def test_assets_do_not_modify_or_copy_task_state(tmp_path):
    base = tmp_path / '.codex/work-manager'
    base.mkdir(parents=True)
    (base / 'state.json').write_bytes(b'private state sentinel')
    (base / 'requests').mkdir()
    (base / 'requests/unfinished.json').write_bytes(b'private request sentinel')
    assets = work_manager.assets(ROOT, tmp_path)
    assert {Path(k).relative_to(base).as_posix() for k in assets} == {
        'MANAGER.md', 'requests/README.md', 'heartbeat.txt', 'turn_usage.py'}
    assert (base / 'state.json').read_bytes() == b'private state sentinel'
    assert (base / 'requests/unfinished.json').read_bytes() == b'private request sentinel'
    assert all(b'${HOME}' not in v for v in assets.values())


def test_normal_desired_assets_include_manager_and_client_entrypoints(tmp_path):
    assets = manage.desired_assets(tmp_path, None)
    assert str(tmp_path / '.codex/work-manager/MANAGER.md') in assets
    for client, name in [('codex', 'AGENTS.md'), ('claude', 'CLAUDE.md')]:
        assert work_manager.BEGIN.encode() in assets[str(tmp_path / ('.' + client) / name)]
    assert 'you are manager' in (ROOT / 'AGENTS.md').read_text()
