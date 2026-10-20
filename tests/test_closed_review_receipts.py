"""Closed receipt admission preserves retirement, review proof and execution history."""
import asyncio
from copy import deepcopy
import json
import sys
import time

import pytest

from caller_pr_gates import cli, github, monitor, progress, workflow
from caller_pr_gates.policy import evaluate_pr, evaluate_review
from test_github import Client
from test_workflow import local, git

URL = 'https://github.com/Owner/Repo/pull/1'
REASON = 'These reviews report no blocking findings and request no source changes.'
STAMP = '2026-10-20T08:00:00Z'
REPLIED = '2026-10-20T08:02:00Z'


def review_snapshot(local, *, retired=True, merged=True):
    store, root, snap, _ = local
    reply = dict(id='issue:12', kind='issue', url=URL + '#issuecomment-12',
                 body=REASON, author_login='agent', created_at=REPLIED, updated_at=REPLIED)
    reviews = [dict(id=f'review:{number}', kind='review', url=URL + f'#pullrequestreview-{number}',
                    body='Reviewed: no blocking findings.', author_login='reviewer',
                    created_at=STAMP, updated_at=STAMP, review_state='COMMENTED')
               for number in (10, 11)]
    result = dict(deepcopy(snap), profile='review', comments=[*reviews, reply],
                  state='closed' if retired else 'open', merged=merged if retired else False,
                  head_ref='feature', head_repository='Owner/Repo', base_repository='Owner/Repo',
                  head_repository_id=1, base_repository_id=1, collected_at=time.time())
    if retired:
        monitor.retire(store, 'Owner/Repo', 1, dict(state='closed', merged=merged,
                       head={'sha': snap['head_sha'], 'ref': 'feature'}), checklist=True)
    return result


def inventory(snapshot):
    result = deepcopy(snapshot)
    replies = [c for c in result['comments'] if c.get('kind') == 'issue']
    for comment in result['comments']:
        if comment.get('kind') != 'inline':
            comment['replies'] = [deepcopy(reply) for reply in replies
                                  if reply['id'] != comment['id']
                                  and reply['created_at'] > comment['updated_at']]
    return result


def address(local, monkeypatch, capsys, snapshot, *, sid='s', cwd=None, head=True,
            comment='review:10', disposition='nonfix', reason=REASON, commit=None):
    store, root, original, _ = local
    calls = []

    async def collect(repo, number):
        calls.append((repo, number))
        return inventory(snapshot)

    monkeypatch.setattr(cli, 'Store', lambda: store)
    monkeypatch.setattr(github, 'collect_review', collect)
    argv = ['caller-pr-gates', 'address', '--session', sid, '--pr', URL,
            '--cwd', str(cwd or root), '--comment', comment,
            '--disposition', disposition, '--reason', reason,
            '--reply-url', URL + '#issuecomment-12']
    if head is not None:
        argv += ['--closed-head', original['head_sha'] if head is True else head]
    if commit is not None:
        argv += ['--commit', commit]
    monkeypatch.setattr(sys, 'argv', argv)
    cli.main()
    return json.loads(capsys.readouterr().out), calls


@pytest.mark.parametrize('merged', [False, True])
def test_closed_owner_records_existing_reviews_and_conclusion_without_rearming(local, monkeypatch, capsys, merged):
    store, root, snap, _ = local
    review = review_snapshot(local, merged=merged)
    with store._connect() as db:
        before_watch = dict(db.execute('SELECT * FROM pr_watch').fetchone())
        before_prs = [dict(row) for row in db.execute('SELECT * FROM prs')]
    assert not store.prs('s')
    with pytest.raises(ValueError, match='not registered'):
        workflow.owned_pr(store, 's', 'Owner/Repo', 1)

    first, calls = address(local, monkeypatch, capsys, review)
    assert calls == [('Owner/Repo', 1)]
    assert {g['status'] for g in first['review_gates']} == {'FAIL'}
    second, calls = address(local, monkeypatch, capsys, review, comment='review:11')
    assert calls == [('Owner/Repo', 1)]
    assert {g['status'] for g in second['review_gates']} == {'PASS'}
    assert second['completion_verified'] is False
    assert second['receipt']['closed_head'] == snap['head_sha']
    assert second['receipt']['cwd'] == str(root)
    assert second['review_gates'][0]['evidence']['verified_conclusion_replies'] == [URL + '#issuecomment-12']
    assert {r['comment_id'] for r in store.dispositions('Owner/Repo', 1)} == {'review:10', 'review:11'}
    with store._connect() as db:
        assert dict(db.execute('SELECT * FROM pr_watch').fetchone()) == before_watch
        assert [dict(row) for row in db.execute('SELECT * FROM prs')] == before_prs
        events = [row[0] for row in db.execute('SELECT event FROM audit')]
    assert events.count('pr_registered') == 1 and events.count('review_disposition') == 2
    assert not store.prs('s')
    ready = next(g for g in evaluate_pr(review, store.dispositions('Owner/Repo', 1)) if g['gate'] == 'MERGE_READY')
    assert ready['status'] == 'WAIT' and ready['evidence']['completion_verified'] is False


def test_receipt_does_not_change_real_pass_failure_pending_or_source(local, monkeypatch, capsys):
    store, root, snap, _ = local
    plan = {'checks': [
        dict(id='passing', argv=[sys.executable, '-c', "from pathlib import Path; assert Path('src.txt').read_text() == 'old'"], reason='Existing source assertion'),
        dict(id='failing', argv=[sys.executable, 'check.py'], reason='Retain the actual failed assertion'),
        dict(id='pending', argv=[sys.executable, 'check.py', 'pending'], reason='Still required and unexecuted')]}
    row = workflow.save_plan(store, 's', 'Owner/Repo', 1, root, plan)['plan']
    assert workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'passing')['run']['exit_code'] == 0
    assert workflow.run_check(store, 's', 'Owner/Repo', 1, root, 'failing')['run']['exit_code'] == 1
    review = review_snapshot(local)
    before_runs = store.validation_runs(row['plan_id'])
    before_plans = store.validation_plans('s')
    before_digest = progress.fingerprint(root, row['plan'])
    before_snapshot = store.snapshot('Owner/Repo', 1)
    address(local, monkeypatch, capsys, review)
    address(local, monkeypatch, capsys, review, comment='review:11')
    assert store.validation_runs(row['plan_id']) == before_runs
    assert store.validation_plans('s') == before_plans
    assert {r['status'] for r in before_runs} == {'pass', 'fail'}
    assert not any(r['check_id'] == 'pending' for r in before_runs)
    assert progress.fingerprint(root, row['plan']) == before_digest
    assert store.snapshot('Owner/Repo', 1) == before_snapshot
    assert workflow.task_progress(store, 's')[0]['status'] != 'PASS'


def test_original_closed_refusal_remains_without_explicit_continuation(local, monkeypatch, capsys):
    store, _, _, _ = local
    review = review_snapshot(local)
    with pytest.raises(SystemExit):
        address(local, monkeypatch, capsys, review, head=None)
    assert 'not registered' in capsys.readouterr().err
    with pytest.raises(ValueError, match='not registered'):
        store.disposition('s', 'Owner/Repo', 1, dict(comment_id='review:10', comment_updated_at=STAMP,
                          disposition='nonfix', reason=REASON))
    assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('sid', ['other', 'child', 'legacy'])
def test_closed_receipt_rejects_foreign_or_child_owner(local, monkeypatch, capsys, sid):
    store, root, _, _ = local
    store.record_session(sid, 'codex', root, parent_session='s' if sid == 'child' else None)
    if sid == 'legacy':
        store.register(sid, 'codex', 'Owner/Repo', 1, root)
    review = review_snapshot(local)
    with pytest.raises(SystemExit):
        address(local, monkeypatch, capsys, review, sid=sid)
    assert 'exact retained owner' in capsys.readouterr().err
    assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('takeover', ['other', 's'])
def test_reregistration_cannot_adopt_closed_receipt_authority(local, tmp_path, monkeypatch, capsys, takeover):
    store, root, snap, _ = local
    review = review_snapshot(local)
    clone = tmp_path / 'clone'
    git(root, 'clone', '-q', str(root), str(clone))
    git(clone, 'remote', 'set-url', 'origin', 'https://github.com/Owner/Repo.git')
    store.register(takeover, 'codex', 'Owner/Repo', 1, clone)
    monitor.retire(store, 'Owner/Repo', 1, dict(state='closed', merged=True,
                   head={'sha': snap['head_sha']}), checklist=True)
    with pytest.raises(SystemExit):
        address(local, monkeypatch, capsys, review, sid=takeover, cwd=clone)
    assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('change', ['clone', 'subdirectory', 'missing', 'origin', 'dirty', 'head'])
def test_closed_receipt_requires_original_clean_checkout(local, tmp_path, monkeypatch, capsys, change):
    store, root, _, _ = local
    review = review_snapshot(local)
    cwd = root
    if change == 'clone':
        cwd = tmp_path / 'clone'
        git(root, 'clone', '-q', str(root), str(cwd))
        git(cwd, 'remote', 'set-url', 'origin', 'https://github.com/Owner/Repo.git')
    elif change == 'subdirectory':
        cwd = root / 'sub'
        cwd.mkdir()
    elif change == 'missing':
        cwd = tmp_path / 'missing'
    elif change == 'origin':
        git(root, 'remote', 'set-url', 'origin', 'https://github.com/Other/Repo.git')
    elif change == 'dirty':
        (root / 'src.txt').write_text('unpublished')
    else:
        git(root, 'commit', '--allow-empty', '-qm', 'Different head')
    with pytest.raises(SystemExit):
        address(local, monkeypatch, capsys, review, cwd=cwd)
    assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('change', [
    {'profile': 'validation'}, {'state': 'open'}, {'merged': False}, {'merged': None},
    {'repository': 'Other/Repo'}, {'number': 2}, {'head_sha': 'b' * 40},
    {'head_sha_after': 'b' * 40}, {'head_repository': 'Other/Repo'},
    {'base_repository': 'Other/Repo'}, {'head_repository_id': None},
    {'base_repository_id': 2}, {'base_repository_id': True}, {'head_ref': ''},
    {'commit_shas': []}, {'collected_at': 1}, {'collected_at': float('inf')}
])
def test_closed_receipt_refuses_incoherent_or_stale_live_proof(local, monkeypatch, capsys, change):
    store, _, _, _ = local
    review = review_snapshot(local)
    review.update(change)
    with pytest.raises(SystemExit):
        address(local, monkeypatch, capsys, review)
    assert 'fresh coherent proof' in capsys.readouterr().err
    assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('head', ['abc1234', 'b' * 40, 'A' * 40])
def test_closed_receipt_requires_full_exact_pinned_head(local, monkeypatch, capsys, head):
    store, _, _, _ = local
    with pytest.raises(SystemExit):
        address(local, monkeypatch, capsys, review_snapshot(local), head=head)
    assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('change', ['missing', 'actor', 'body', 'time', 'edited', 'unresolved', 'unknown_actor'])
def test_closed_receipt_requires_current_real_public_conclusion(local, monkeypatch, capsys, change):
    store, _, _, _ = local
    review = review_snapshot(local)
    if change == 'missing':
        review['comments'].pop()
    elif change == 'actor':
        review['comments'][-1]['author_login'] = 'someone-else'
    elif change == 'body':
        review['comments'][-1]['body'] = 'Acknowledged.'
    elif change == 'time':
        review['comments'][-1]['created_at'] = '2026-10-20T07:59:00Z'
    elif change == 'edited':
        review['comments'][0]['updated_at'] = '2026-10-20T08:03:00Z'
    elif change == 'unresolved':
        review['comments'][0].update(thread_id='T1', is_resolved=False)
    else:
        review['actor_login'] = None
    with pytest.raises(SystemExit):
        address(local, monkeypatch, capsys, review)
    assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('valid', [True, False])
def test_fixed_receipt_requires_this_pr_commit_not_successor_credit(local, monkeypatch, capsys, valid):
    store, _, snap, _ = local
    review = review_snapshot(local)
    review['comments'][0]['body'] = 'Fix the boundary.'
    commit = snap['head_sha'] if valid else 'b' * 40
    reason = 'The boundary fix is in this PR.'
    review['commit_dates'] = {snap['head_sha']: '2026-10-20T08:01:00Z'}
    review['comments'][-1]['body'] = reason
    if valid:
        result, _ = address(local, monkeypatch, capsys, review, disposition='fixed', reason=reason, commit=commit)
        assert result['receipt']['commit_sha'] == commit
    else:
        with pytest.raises(SystemExit):
            address(local, monkeypatch, capsys, review, disposition='fixed', reason=reason, commit=commit)
        assert 'absent from this PR' in capsys.readouterr().err
        assert store.dispositions('Owner/Repo', 1) == []


def test_other_real_finding_and_later_review_edit_remain_required(local, monkeypatch, capsys):
    store, _, _, _ = local
    review = review_snapshot(local)
    finding = dict(review['comments'][0], id='review:13', url=URL + '#pullrequestreview-13',
                   body='Fix the remaining data loss.', review_state='CHANGES_REQUESTED')
    review['comments'].insert(2, finding)
    address(local, monkeypatch, capsys, review)
    result, _ = address(local, monkeypatch, capsys, review, comment='review:11')
    obligations = {r['id']: r for r in result['review_gates'][0]['evidence']['obligations']}
    assert not obligations['review:13']['handled']
    assert {g['status'] for g in result['review_gates']} == {'FAIL'}
    review['comments'][0].update(body='A new finding after the reply.', updated_at='2026-10-20T08:03:00Z')
    gates = evaluate_review(inventory(review), store.dispositions('Owner/Repo', 1))
    assert 'edited after its disposition' in ' '.join(gates[0]['details'])
    before = store.dispositions('Owner/Repo', 1)
    with pytest.raises(SystemExit):
        address(local, monkeypatch, capsys, review)
    assert store.dispositions('Owner/Repo', 1) == before


@pytest.mark.parametrize('race', ['owner', 'source', 'unavailable'])
def test_collection_change_cannot_record_closed_receipt(local, monkeypatch, capsys, race):
    store, root, snap, _ = local
    review = review_snapshot(local)

    async def collect(repo, number):
        if race == 'owner':
            store.register('other', 'codex', repo, number, root)
        elif race == 'source':
            (root / 'src.txt').write_text('changed during review')
        else:
            raise github.EvidenceError('Review unavailable')
        return inventory(review)

    monkeypatch.setattr(cli, 'Store', lambda: store)
    monkeypatch.setattr(github, 'collect_review', collect)
    monkeypatch.setattr(sys, 'argv', ['caller-pr-gates', 'address', '--session', 's', '--pr', URL,
        '--cwd', str(root), '--closed-head', snap['head_sha'], '--comment', 'review:10',
        '--disposition', 'nonfix', '--reason', REASON, '--reply-url', URL + '#issuecomment-12'])
    with pytest.raises(SystemExit):
        cli.main()
    assert store.dispositions('Owner/Repo', 1) == []


def test_receipt_transaction_rechecks_owner_after_preflight(local, monkeypatch, capsys):
    store, root, _, _ = local
    review = review_snapshot(local)
    original = store.disposition

    def race(*args, **kwargs):
        store.register('other', 'codex', 'Owner/Repo', 1, root)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'disposition', race)
    with pytest.raises(SystemExit):
        address(local, monkeypatch, capsys, review)
    assert store.dispositions('Owner/Repo', 1) == []


@pytest.mark.parametrize('change', ['dirty', 'head'])
def test_receipt_transaction_rechecks_real_source_after_preflight(local, monkeypatch, capsys, change):
    store, root, _, _ = local
    review = review_snapshot(local)
    original = store.disposition

    def race(*args, **kwargs):
        if change == 'dirty':
            (root / 'src.txt').write_text('changed after the final preflight')
        else:
            git(root, 'commit', '--allow-empty', '-qm', 'Head moved after final preflight')
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'disposition', race)
    with pytest.raises(SystemExit):
        address(local, monkeypatch, capsys, review)
    assert 'source changed before recording' in capsys.readouterr().err
    assert store.dispositions('Owner/Repo', 1) == []
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='review_disposition'").fetchone()[0] == 0


def test_open_address_behavior_and_default_receipt_shape_remain(local, monkeypatch, capsys):
    store, _, _, _ = local
    review = review_snapshot(local, retired=False)
    result, calls = address(local, monkeypatch, capsys, review, head=None)
    assert calls == [('Owner/Repo', 1)]
    assert result['comment_id'] == 'review:10' and 'closed_head' not in result
    assert store.prs('s')
    with pytest.raises(SystemExit):
        address(local, monkeypatch, capsys, review)
    assert len(store.dispositions('Owner/Repo', 1)) == 1


@pytest.mark.parametrize('race', [None, 'ref', 'repository', 'repository_id', 'head', 'lifecycle', 'revision', 'base'])
def test_review_collection_reuses_two_reads_and_rejects_identity_races(race):
    class ReviewClient(Client):
        async def get(self, path, params=None):
            value = await super().get(path, params)
            if path.endswith('/pulls/50'):
                value['head'].update(ref='feature', repo={'full_name': 'mikkokotila/caller', 'id': 1})
                value['base']['repo'] = {'full_name': 'mikkokotila/caller', 'id': 1}
                if self.reads == 2:
                    if race == 'ref': value['head']['ref'] = 'another'
                    elif race == 'repository': value['head']['repo']['full_name'] = 'other/caller'
                    elif race == 'repository_id': value['head']['repo']['id'] = 2
                    elif race == 'head': value['head']['sha'] = 'b' * 40
                    elif race == 'lifecycle': value['state'] = 'open'
                    elif race == 'revision': value['updated_at'] = REPLIED
                    elif race == 'base': value['base']['ref'] = 'another'
            return value

    client = ReviewClient()
    if race:
        with pytest.raises(github.EvidenceError, match='PR changed'):
            asyncio.run(github.collect_review_with_client(client, 'mikkokotila/caller', 50))
    else:
        result = asyncio.run(github.collect_review_with_client(client, 'mikkokotila/caller', 50))
        assert result['head_repository_id'] == result['base_repository_id'] == 1
        assert result['head_repository'] == result['base_repository'] == 'mikkokotila/caller'
    assert client.reads == 2 and len(client.paths) == 4

