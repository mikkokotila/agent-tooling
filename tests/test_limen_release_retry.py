"""Historical release recovery retains authority, real validation and one attempt."""
from copy import deepcopy
import json
import subprocess

import pytest
from caller_pr_gates import cli, limen_release_retry as recovery
from test_limen_release_dispatch import case, version_files


@pytest.fixture
def repair(case, monkeypatch):
    monkeypatch.setattr(recovery, 'MERGE', case['main'])
    endpoint=f'repos/{recovery.REPOSITORY}/actions/runs/39955803473/rerun'
    case['args'].arguments=['api',endpoint,'--method','POST']
    data=dict(case=case, writes=[], fail=False, before_write=None,
              tag=dict(object=dict(sha=case['main'],type='commit')),
              qualified=dict(id=recovery.QUALIFIED_RUN,head_sha=recovery.QUALIFIED_HEAD,
                  path=recovery.RUNS[39955803473][0],event='pull_request',status='completed',conclusion='success',
                  head_repository=dict(full_name=recovery.REPOSITORY)),
              blob=dict(sha=recovery.FIXED_TEST,type='file'),
              runs={key:dict(id=key,head_sha=case['main'],head_repository=dict(full_name=recovery.REPOSITORY),
                  path=value[0],event=value[1],status='completed',conclusion='failure',run_attempt=1)
                  for key,value in recovery.RUNS.items()},
              jobs=[dict(name=name,conclusion='failure' if name=='Resolve Release Tag' else 'skipped')
                  for name in ('Resolve Release Tag','Build Distribution','Publish Distribution to PyPI','Publish Release Assets')])
    def run(argv,**kwargs):
        target=argv[2]
        if '--method' in argv:
            data['writes'].append(argv)
            return subprocess.CompletedProcess(argv,1 if data['fail'] else 0,'','')
        if target.endswith('/git/ref/tags/v5.46.2'):body=data['tag']
        elif target.endswith('/actions/runs/'+str(recovery.QUALIFIED_RUN)):body=data['qualified']
        elif '/contents/' in target:body=data['blob']
        elif '/jobs?' in target:body=dict(jobs=data['jobs'])
        else:body=data['runs'][int(target.split('/')[-1])]
        if data['before_write']:
            callback,data['before_write']=data['before_write'],None;callback()
        return subprocess.CompletedProcess(argv,0,json.dumps(deepcopy(body)),'')
    monkeypatch.setattr(cli,'github_run',run)
    return data


def test_actual_corrected_browser_ci_precedes_one_immutable_release_requalification(repair,capsys):
    case=repair['case'];cli.gateway(case['args'],case['store'])
    assert len(repair['writes'])==1
    receipt=json.loads(capsys.readouterr().out);assert receipt['accepted'] is True and receipt['completion_verified'] is False
    with pytest.raises(ValueError,match='bounded recovery'):cli.gateway(case['args'],case['store'])
    assert len(repair['writes'])==1
    assert not case['store'].prs('s')


@pytest.mark.parametrize('fault',['owner','client','cwd','dirty','unmerged','approval','failed_candidate',
    'wrong_merge','wrong_tag','tag_type','fix_pending','fix_failed','fix_head','fix_workflow','fix_repo','fix_blob',
    'original_head','original_path','original_event','original_repo','original_pending','original_success',
    'second_attempt','extra_args','method','issue','fork'])
def test_no_wrong_authority_source_or_unqualified_retry(repair,fault,tmp_path):
    c=repair['case'];value=c['args'];run=repair['runs'][39955803473]
    if fault=='owner':value.session='other'
    elif fault=='client':value.client='claude'
    elif fault=='cwd':value.cwd=str(tmp_path)
    elif fault=='dirty':(c['root']/'src.txt').write_text('changed')
    elif fault=='unmerged':c['snapshot']['merged']=False
    elif fault=='approval':c['snapshot']['review_decision']='CHANGES_REQUESTED'
    elif fault=='failed_candidate':c['snapshot']['checks'][0]['conclusion']='failure'
    elif fault=='wrong_merge':c['snapshot']['merge_commit_sha']='b'*40
    elif fault=='wrong_tag':repair['tag']['object']['sha']='b'*40
    elif fault=='tag_type':repair['tag']['object']['type']='tag'
    elif fault=='fix_pending':repair['qualified']['status']='in_progress'
    elif fault=='fix_failed':repair['qualified']['conclusion']='failure'
    elif fault=='fix_head':repair['qualified']['head_sha']='b'*40
    elif fault=='fix_workflow':repair['qualified']['path']='.github/workflows/other.yml'
    elif fault=='fix_repo':repair['qualified']['head_repository']['full_name']='other/repo'
    elif fault=='fix_blob':repair['blob']['sha']='b'*40
    elif fault=='original_head':run['head_sha']='b'*40
    elif fault=='original_path':run['path']='.github/workflows/other.yml'
    elif fault=='original_event':run['event']='pull_request'
    elif fault=='original_repo':run['head_repository']['full_name']='other/repo'
    elif fault=='original_pending':run['status']='in_progress'
    elif fault=='original_success':run['conclusion']='success'
    elif fault=='second_attempt':run['run_attempt']=2
    elif fault=='extra_args':value.arguments+=['-f','anything=1']
    elif fault=='method':value.arguments[3]='PATCH'
    elif fault=='issue':value.target=value.target.replace('/pull/','/issues/')
    elif fault=='fork':c['snapshot']['head_repository']='other/Limen'
    with pytest.raises(ValueError):cli.gateway(value,c['store'])
    assert repair['writes']==[]


def test_publication_waits_for_actual_historical_validation_and_no_prior_upload(repair):
    c=repair['case'];c['args'].arguments[1]=f'repos/{recovery.REPOSITORY}/actions/runs/39957123635/rerun'
    with pytest.raises(ValueError,match='actual validation'):cli.gateway(c['args'],c['store'])
    repair['runs'][39955803473]['conclusion']='success'
    repair['jobs'][1]['conclusion']='success'
    with pytest.raises(ValueError,match='pre-build failure'):cli.gateway(c['args'],c['store'])
    repair['jobs'][1]['conclusion']='skipped';cli.gateway(c['args'],c['store']);assert len(repair['writes'])==1


def test_unknown_post_outcome_never_gets_an_automatic_second_attempt(repair):
    repair['fail']=True;c=repair['case']
    with pytest.raises(RuntimeError,match='unverified'):cli.gateway(c['args'],c['store'])
    with pytest.raises(ValueError,match='bounded recovery'):cli.gateway(c['args'],c['store'])
    assert len(repair['writes'])==1


def test_owner_and_checkout_are_rechecked_before_request(repair):
    repair['before_write']=lambda:(repair['case']['root']/'src.txt').write_text('changed')
    with pytest.raises(ValueError,match='checkout changed'):cli.gateway(repair['case']['args'],repair['case']['store'])
    assert repair['writes']==[]
