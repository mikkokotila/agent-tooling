"""Preserved concurrent operator path never waives other gates."""
from unittest.mock import patch
import pytest
from caller_pr_gates.cli import _nonrequired_quota_merge_allowed
from caller_pr_gates.store import Store
from test_policy import fresh_branch,closing_link
import time

URL='https://github.com/o/r/pull/1';SHA='a'*40
NAMES=['PR_NOT_DRAFT','CI_FAILED','CI_PENDING','REVIEW_HANDLED','REVIEW_CONCLUDED','MERGE_CONFLICT','BRANCH_UP_TO_DATE','ISSUE_CLOSURE']

@pytest.mark.parametrize('blocked',[None,*[n for n in NAMES if n!='CI_FAILED']])
def test_only_verified_nonrequired_failure_with_human_approval_is_eligible(tmp_path,blocked):
    s=Store(tmp_path);s.record_session('s','codex',str(tmp_path));s.register('s','codex','o/r',1,str(tmp_path))
    snapshot=dict(repository='o/r',number=1,head_sha=SHA,head_sha_after=SHA,state='open',merged=False,expected_checks=['CI'],checks=[{'name':'Optional review','kind':'check','key':'check:optional','id':1,'status':'completed','conclusion':'failure'}])
    gates=[{'gate':n,'status':'FAIL' if n in {'CI_FAILED',blocked} else 'PASS'} for n in NAMES]
    s.set_snapshot('o/r',1,snapshot,gates);rows=[{'head_sha':SHA,'gates':gates}]
    argv=['pr','merge',URL,'--merge','--match-head-commit',SHA]
    held={'evidence_url':'https://github.com/o/r/actions/runs/1/job/2','fingerprint':'verified'}
    with patch('caller_pr_gates.external.current_hold',return_value=held),patch('caller_pr_gates.external.publication_ready',return_value=True):
        assert _nonrequired_quota_merge_allowed(s,'s',URL,argv,rows,'actual explicit user authorization') is (blocked is None)
        assert not _nonrequired_quota_merge_allowed(s,'s',URL,argv,rows,'')
        assert not _nonrequired_quota_merge_allowed(s,'s',URL,['pr','merge',URL,'--admin'],rows,'yes')
        snapshot['checks'][0]['name']='CI';s.set_snapshot('o/r',1,snapshot,gates)
        assert not _nonrequired_quota_merge_allowed(s,'s',URL,argv,rows,'yes')
