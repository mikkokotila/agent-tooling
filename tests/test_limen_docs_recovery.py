"""Only the reviewed, owned and immutable documentation recovery can dispatch once."""
from copy import deepcopy
import json
import subprocess

import pytest
from caller_pr_gates import cli, limen_docs_recovery as recovery
from test_limen_release_dispatch import case
from test_workflow import local


@pytest.fixture
def repair(case, monkeypatch):
    monkeypatch.setattr(recovery, 'PR', 1196)
    monkeypatch.setattr(recovery, 'HEAD', case['snapshot']['head_sha'])
    monkeypatch.setattr(recovery, 'RELEASE', case['main'])
    case['request'].write_text(json.dumps({'ref': 'main', 'inputs': {'package_publish_run': str(recovery.PACKAGE_RUN)}}))
    case['args'].arguments = ['api', recovery.ENDPOINT, '--method', 'POST', '--input', str(case['request'])]
    target = dict(type='commit', sha=case['main'], url=f'https://api.github.com/repos/{recovery.REPOSITORY}/git/commits/{case["main"]}')
    data = dict(case=case, writes=[], main=deepcopy(target), tag=dict(object=target), blobs=dict(recovery.TRUSTED), fail=False,
                response=dict(workflow_run_id=100, run_url=f'https://api.github.com/repos/{recovery.REPOSITORY}/actions/runs/100', html_url=f'https://github.com/{recovery.REPOSITORY}/actions/runs/100'),
                run=dict(id=100,path=recovery.WORKFLOW,head_sha=case['main'],head_branch='main',event='workflow_dispatch',head_repository=dict(full_name=recovery.REPOSITORY),html_url=f'https://github.com/{recovery.REPOSITORY}/actions/runs/100'),
                runs={number:dict(id=number,path=workflow,head_sha=case['main'],event='workflow_run',status='completed',conclusion=outcome,head_repository=dict(full_name=recovery.REPOSITORY)) for number,workflow,outcome in [(recovery.PACKAGE_RUN,'.github/workflows/pr_publish_pypi.yml','success'),(recovery.FAILED_RUN,recovery.WORKFLOW,'failure')]})
    def run(argv, **kwargs):
        endpoint=argv[2]
        if '--method' in argv:
            assert argv==['gh','api',recovery.ENDPOINT,'--method','POST','--input','-','--header','X-GitHub-Api-Version: 2026-03-10']
            assert json.loads(kwargs['input'])=={'ref':'main','inputs':{'package_publish_run':str(recovery.PACKAGE_RUN)}}
            data['writes'].append(argv)
            return subprocess.CompletedProcess(argv,1 if data['fail'] else 0,json.dumps(data['response']),'')
        if endpoint.endswith('/git/ref/heads/main'):body=dict(object=data['main'])
        elif '/contents/' in endpoint:body=dict(type='file',sha=data['blobs'][endpoint.split('/contents/')[1].split('?')[0]])
        elif endpoint.endswith('/git/ref/tags/v5.46.2'):body=data['tag']
        elif '/git/tags/' in endpoint:body=data['annotation']
        elif endpoint.endswith('/actions/runs/100'):body=data['run']
        else:body=data['runs'][int(endpoint.split('/')[-1])]
        return subprocess.CompletedProcess(argv,0,json.dumps(deepcopy(body)),'')
    monkeypatch.setattr(cli,'github_run',run)
    return data


def test_frozen_body_one_attempt_and_actual_run_identity(repair,capsys):
    c=repair['case'];c['before_post']=lambda:c['request'].write_text('{"ref":"unreviewed"}')
    cli.gateway(c['args'],c['store'])
    result=json.loads(capsys.readouterr().out);assert result['accepted'] is True and result['completion_verified'] is False
    assert result['run_id']==100 and len(repair['writes'])==1
    with pytest.raises(ValueError,match='bounded recovery'):cli.gateway(c['args'],c['store'])
    assert len(repair['writes'])==1


@pytest.mark.parametrize('fault',['owner','client','cwd','dirty','unmerged','approval','self_approval','failed_ci','fork','head','tag','workflow','adapter','package_failed','package_pending','package_head','package_repo','publication_success','payload','method','extra_args','issue'])
def test_wrong_authority_or_source_never_dispatches(repair,fault,tmp_path):
    c=repair['case'];a=c['args'];s=c['snapshot'];package=repair['runs'][recovery.PACKAGE_RUN]
    if fault=='owner':a.session='another'
    elif fault=='client':a.client='claude'
    elif fault=='cwd':a.cwd=str(tmp_path)
    elif fault=='dirty':(c['root']/'src.txt').write_text('changed')
    elif fault=='unmerged':s['merged']=False
    elif fault=='approval':s['review_decision']='CHANGES_REQUESTED'
    elif fault=='self_approval':s['submitted_reviews'][0]['author_login']=s['author_login']
    elif fault=='failed_ci':s['checks'][0]['conclusion']='failure'
    elif fault=='fork':s['head_repository']='other/Limen'
    elif fault=='head':s['head_sha_after']='b'*40
    elif fault=='tag':repair['tag']['object']['sha']='b'*40
    elif fault in ('workflow','adapter'):repair['blobs'][recovery.WORKFLOW if fault=='workflow' else 'docs-site/publisher/prepare-assets.mjs']='b'*40
    elif fault=='package_failed':package['conclusion']='failure'
    elif fault=='package_pending':package['status']='in_progress'
    elif fault=='package_head':package['head_sha']='b'*40
    elif fault=='package_repo':package['head_repository']['full_name']='other/Limen'
    elif fault=='publication_success':repair['runs'][recovery.FAILED_RUN]['conclusion']='success'
    elif fault=='payload':c['request'].write_text('{"ref":"main","inputs":{"package_publish_run":"1"}}')
    elif fault=='method':a.arguments[3]='PATCH'
    elif fault=='extra_args':a.arguments+=['--header','anything=1']
    elif fault=='issue':a.target=a.target.replace('/pull/','/issues/')
    with pytest.raises(ValueError):cli.gateway(a,c['store'])
    assert repair['writes']==[]


@pytest.mark.parametrize('fault',['failed_post','missing_response','wrong_run','wrong_main','wrong_workflow'])
def test_unknown_post_outcome_never_repeats(repair,fault):
    if fault=='failed_post':repair['fail']=True
    elif fault=='missing_response':repair['response']={}
    elif fault=='wrong_run':repair['response']['workflow_run_id']=True
    elif fault=='wrong_main':repair['run']['head_sha']='b'*40
    elif fault=='wrong_workflow':repair['run']['path']='.github/workflows/other.yml'
    c=repair['case']
    with pytest.raises(RuntimeError,match='unverified'):cli.gateway(c['args'],c['store'])
    with pytest.raises(ValueError,match='bounded recovery'):cli.gateway(c['args'],c['store'])
    assert len(repair['writes'])==1


def test_annotated_release_and_owner_recheck(repair):
    target=deepcopy(repair['tag']['object']);repair['tag']['object']=dict(type='tag',sha='c'*40)
    repair['annotation']=dict(sha='c'*40,tag='v5.46.2',object=target)
    c=repair['case'];c['on_second_read']=lambda:(c['root']/'src.txt').write_text('changed')
    with pytest.raises(ValueError,match='checkout changed'):cli.gateway(c['args'],c['store'])
    assert repair['writes']==[]
