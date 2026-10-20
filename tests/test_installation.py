"""Deployment, crash/error rollback, real native callbacks and evidence retention."""
from pathlib import Path
import copy, json, os, shutil, sqlite3, subprocess, sys, time, zipfile
import pytest

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'tools'))
import manage
NATIVE=manage.native_smoke
RUNTIME_MATCHES=manage.runtime_matches

@pytest.mark.parametrize('recovers,already_running', [(True,False),(False,False),(True,True)])
def test_monitor_activation_retries_are_bounded(tmp_path,monkeypatch,recovers,already_running):
    from types import SimpleNamespace
    service=manage.Service(tmp_path);calls=[];pauses=[]
    def launch(argv,**kwargs):
        calls.append(argv[1])
        if argv[1]=='bootstrap':
            success=recovers and calls.count('bootstrap')==3
            return SimpleNamespace(returncode=0 if success else 5,stderr='Bootstrap failed: 5: Input/output error')
        return SimpleNamespace(returncode=0,stderr='')
    monkeypatch.setattr(manage.subprocess,'run',launch)
    monkeypatch.setattr(manage.time,'sleep',pauses.append)
    monkeypatch.setattr(service,'running',lambda: already_running or 'kickstart' in calls)
    if recovers: service.start()
    else:
        with pytest.raises(RuntimeError,match='Monitor activation failed'): service.start()
    assert calls.count('bootstrap')==(1 if already_running else 3)
    assert calls.count('kickstart')==(1 if recovers else 0)
    assert len(pauses)==(0 if already_running else 2) and sum(pauses)<=1

class FakeService:
    def __init__(self,home):
        self.plist=home/'Library/LaunchAgents/local.caller-pr-monitor.plist'
        self.active=True;self.stops=0;self.starts=0;self.fail_next_start=False;self.on_stop=None
    def running(self): return self.active
    def stop(self):
        self.stops+=1;self.active=False
        if self.on_stop:self.on_stop()
    def start(self):
        self.starts+=1
        if self.fail_next_start:self.fail_next_start=False;raise RuntimeError('activation failed')
        self.active=True

@pytest.fixture
def installed(tmp_path,monkeypatch):
    home=tmp_path/'home';live=home/'.local/share/caller-pr-gates';live.mkdir(parents=True)
    shutil.copytree(ROOT/'caller_pr_gates',live/'caller_pr_gates',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    (live/'prompts.toml').write_bytes((ROOT/'prompts.toml').read_bytes())
    (live/'release.json').write_text(json.dumps({'version':'1.7.0','source':'original-captured-source','keep':'historical provenance'}))
    for client,name in [('codex','AGENTS.md'),('claude','CLAUDE.md')]:
        target=home/('.'+client)/name;target.parent.mkdir()
        target.write_bytes(b'Personal prefix.\n\n'+(ROOT/'tests/fixtures'/f'{client}-instructions.md').read_bytes()+b'\nPersonal suffix.\n')
        config=target.parent/('hooks.json' if client=='codex' else 'settings.json')
        obj=json.loads(manage.render(client+'-hooks.json',home));obj['personal_setting']='keep'
        config.write_text(json.dumps(obj,indent=2)+'\n')
    (home/'.codex/config.toml').write_text('model = "personal-model"\n[features]\nhooks = true\n[personal]\nvalue = "keep"\n')
    for file,target in [('launcher.sh',home/'.local/bin/caller-pr-gates'),('monitor.plist',home/'Library/LaunchAgents/local.caller-pr-monitor.plist')]:
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(manage.render(file,home))
    state=home/'.local/state/caller-pr-gates';state.mkdir(parents=True)
    with sqlite3.connect(state/'registry.sqlite3') as db:
        db.execute('CREATE TABLE evidence(value TEXT)');db.execute('INSERT INTO evidence VALUES("original")')
    (state/'monitor-health.json').write_text('unchanged health sentinel')
    monkeypatch.setattr(manage,'runtime_matches',lambda p:True)
    monkeypatch.setattr(manage,'native_smoke',lambda *args:None)
    return home,FakeService(home)

def original_bytes(home):
    return {str(p.relative_to(home)):p.read_bytes() for p in home.rglob('*') if p.is_file() and '/agent-tooling/' not in str(p)}

def alter_owned_code(home):
    path=home/'.local/share/caller-pr-gates/caller_pr_gates/hook.py';path.write_bytes(path.read_bytes()+b'\n# Older code snapshot\n')
    package=path.parent;base=home/'.local/share/agent-tooling';base.mkdir(parents=True)
    manage.dump(base/'current.json',{'gate_hashes':{p.name:manage.sha(p.read_bytes()) for p in package.iterdir() if p.is_file() and p.suffix in {'.py','.json'}},'source_prompt_hash':manage.CAPTURE['prompt_sha256'],'deployment':'older','version':'prior'})
    return path.read_bytes()

def test_capture_code_and_wheel_assets_are_byte_identical():
    manage.assert_capture()
    wheel=ROOT/'.build/dist'/f'agent_tooling-{manage.VERSION}-py3-none-any.whl'
    with zipfile.ZipFile(wheel) as z:
        for name,digest in manage.RELEASE['gate_files'].items():assert manage.sha(z.read(name))==digest
        assert z.read('agent_tooling_assets/prompts.toml')==(ROOT/'prompts.toml').read_bytes()
        for p in (ROOT/'config').iterdir():assert z.read('agent_tooling_assets/config/'+p.name)==p.read_bytes()

def test_install_preserves_operational_assets_and_state(installed):
    home,service=installed;before=original_bytes(home)
    result=manage.deploy(home,service)
    assert result['code_unchanged'] and result['runtime_unchanged'] and not result['service_restarted']
    assert service.stops==service.starts==0
    for name,data in before.items():
        if name not in {'.local/share/caller-pr-gates/release.json','.codex/AGENTS.md','.claude/CLAUDE.md'}:
            assert (home/name).read_bytes()==data,name
    for name in ['.codex/AGENTS.md','.claude/CLAUDE.md']:
        text=(home/name).read_text()
        assert 'Copilot quota exhaustion, unavailability' in text
        assert 'Every actual Copilot review comment or change request' in text
        assert text.startswith('Personal prefix.') and text.endswith('Personal suffix.\n')
    assert json.loads((home/'.local/share/caller-pr-gates/release.json').read_text())['version']==manage.RELEASE['runtime_version']

def test_repeated_install_is_a_true_noop(installed):
    home,service=installed;manage.deploy(home,service);before=original_bytes(home)
    pointer=home/'.local/share/agent-tooling/current.json';mtime=pointer.stat().st_mtime_ns
    assert manage.deploy(home,service)['status']=='already_installed'
    assert pointer.stat().st_mtime_ns==mtime and original_bytes(home)==before
    assert service.stops==service.starts==0

def test_rollback_preserves_newer_pr_state(installed):
    home,service=installed;before=original_bytes(home);manage.deploy(home,service)
    state=home/'.local/state/caller-pr-gates/registry.sqlite3'
    with sqlite3.connect(state) as db:db.execute('UPDATE evidence SET value="newer work"')
    assert not manage.rollback(home,service)['state_restored']
    with sqlite3.connect(state) as db:assert db.execute('SELECT value FROM evidence').fetchone()[0]=='newer work'
    assert (home/'.local/share/caller-pr-gates/release.json').read_bytes()==before['.local/share/caller-pr-gates/release.json']

def test_valid_live_prompt_edit_is_preserved_and_mirrored(installed):
    home,service=installed;prompt=home/'.local/share/caller-pr-gates/prompts.toml'
    # Establish the installed release baseline before applying a human edit.
    # Concurrent changes against the older capture must still be rejected.
    manage.deploy(home,service)
    text=prompt.read_text()
    # Change a plain fragment while retaining required protected prefixes/tokens.
    text=text.replace('Can you verify yes to every question?', 'Can you verify yes to every question? Human edit.')
    assert text!=prompt.read_text();prompt.write_text(text)
    manage.deploy(home,service)
    assert prompt.read_text()==text
    for p in [home/'.codex/AGENTS.md',home/'.claude/CLAUDE.md']:
        assert 'Human edit.' in p.read_text() and p.read_text().startswith('Personal prefix.') and p.read_text().endswith('Personal suffix.\n')

def test_invalid_local_prompt_is_rejected_without_overwriting(installed):
    home,service=installed;prompt=home/'.local/share/caller-pr-gates/prompts.toml';prompt.write_text('invalid TOML')
    before=original_bytes(home)
    with pytest.raises((RuntimeError,ValueError)):manage.deploy(home,service)
    assert original_bytes(home)==before and service.stops==0

def test_pristine_legacy_catalogue_migrates_to_exact_source_bytes(installed):
    home,service=installed
    prompt=home/'.local/share/caller-pr-gates/prompts.toml'
    legacy=(ROOT/'prompts.toml').read_bytes().split(b'[prompts.pr_checklist]')[0]
    prompt.write_bytes(legacy)
    current={'source_prompt_hash':manage.sha(legacy)}
    assets=manage.desired_assets(home,current)
    assert assets[str(prompt)]==(ROOT/'prompts.toml').read_bytes()

@pytest.mark.parametrize('human_edit',[False,True])
def test_old_machine_migration_is_pristine_but_human_edits_still_conflict(installed,monkeypatch,human_edit):
    home,service=installed
    prompt=home/'.local/share/caller-pr-gates/prompts.toml'
    old=(ROOT/'prompts.toml').read_bytes()
    generated=b'# Prior machine migration\n'+old
    prompt.write_bytes(generated+(b'\n# Human edit' if human_edit else b''))
    deployment=home/'previous-deployment';deployment.mkdir()
    prior_hash=manage.sha(b'old pristine 58-section catalogue')
    manage.dump(deployment/'journal.json',{'files':{str(prompt):{'before':prior_hash,'after':manage.sha(generated)}}})
    current={'version':'0.1.8','source_prompt_hash':manage.sha(old),'previous_pointer':{'source_prompt_hash':prior_hash},'deployment':str(deployment)}
    new=old.replace(b'Can you verify yes to every question?',b'Can you verify yes to every question? Updated wording.')
    original=Path.read_bytes
    monkeypatch.setattr(Path,'read_bytes',lambda p: new if p==ROOT/'prompts.toml' else original(p))
    if human_edit:
        with pytest.raises(RuntimeError,match='both changed'):manage.desired_assets(home,current)
    else:
        assert manage.desired_assets(home,current)[str(prompt)]==new

@pytest.mark.parametrize('catalogue_replaced',[False,True])
@pytest.mark.parametrize('human_edit',[False,True])
def test_reinstalled_machine_migration_retains_proven_pristine_baseline(installed,monkeypatch,catalogue_replaced,human_edit):
    home,_=installed;prompt=home/'.local/share/caller-pr-gates/prompts.toml'
    old=(ROOT/'prompts.toml').read_bytes();generated=b'# Prior machine migration\n'+old
    prompt.write_bytes(generated+(b'\n# Human edit' if human_edit else b''))
    first=home/'first-deployment';first.mkdir()
    prior_hash=manage.sha(b'old pristine 58-section catalogue')
    manage.dump(first/'journal.json',{'files':{str(prompt):{'before':prior_hash,'after':manage.sha(generated)}}})
    previous={'version':'0.1.8','source_prompt_hash':manage.sha(old),'previous_pointer':{'source_prompt_hash':prior_hash},'deployment':str(first)}
    second=home/'second-deployment';second.mkdir()
    rows={str(prompt):{'before':manage.sha(generated),'after':manage.sha(generated)}} if catalogue_replaced else {}
    manage.dump(second/'journal.json',{'files':rows})
    current={'version':'0.1.8','source_prompt_hash':manage.sha(old),'previous_pointer':previous,'deployment':str(second)}
    new=old.replace(b'Can you verify yes to every question?',b'Can you verify yes to every question? Updated wording.')
    read_bytes=Path.read_bytes
    monkeypatch.setattr(Path,'read_bytes',lambda p:new if p==ROOT/'prompts.toml' else read_bytes(p))
    if human_edit:
        with pytest.raises(RuntimeError,match='both changed'):manage.desired_assets(home,current)
    else:
        assert manage.desired_assets(home,current)[str(prompt)]==new


def test_old_catalogue_upgrade_keeps_human_edits_and_removes_legacy_callbacks(installed):
    home,service=installed
    prompt=home/'.local/share/caller-pr-gates/prompts.toml'
    text=prompt.read_text().split('[prompts.pr_checklist]')[0]
    text=text.replace('Handle routine engineering and CI work autonomously', 'Handle routine engineering and CI work precisely and autonomously')
    prompt.write_text(text)
    for client,name in [('codex','hooks.json'),('claude','settings.json')]:
        config=home/('.'+client)/name
        data=json.loads(config.read_text())
        data['hooks']['SessionStart'].append({'hooks':[{'type':'command','command':'python -m caller_pr_gates.troubleshooting'}]})
        data['hooks']['SessionStart'].append({'hooks':[{'type':'command','command':'foreign-callback'}]})
        config.write_text(json.dumps(data))
    manage.deploy(home,service)
    assert text in prompt.read_text() and '[prompts.pr_checklist]' in prompt.read_text()
    for client,name in [('codex','hooks.json'),('claude','settings.json')]:
        data=json.loads((home/('.'+client)/name).read_text())
        commands=[h['command'] for groups in data['hooks'].values() for g in groups for h in g['hooks']]
        assert 'foreign-callback' in commands
        assert not any('caller_pr_gates.troubleshooting' in c for c in commands)
        value=(home/('.'+client)/('AGENTS.md' if client=='codex' else 'CLAUDE.md')).read_text()
        assert '<!-- troubleshooting-report:' not in value and '<!-- pr-progress:' not in value
    assert not manage.rollback(home,service)['state_restored']
    assert prompt.read_text()==text


def test_prd_prompt_addition_preserves_human_pr_checklist(installed):
    home,service=installed
    prompt=home/'.local/share/caller-pr-gates/prompts.toml'
    old=(ROOT/'prompts.toml').read_bytes().split(b'[prompts.prd_review_checklist]')[0]
    edited=old.replace(b'Can you verify yes to every question?', b'Can you verify yes to every question? Human PR edit.')
    prompt.write_bytes(edited)
    assets=manage.desired_assets(home,{'source_prompt_hash':manage.sha(old)})
    value=assets[str(prompt)]
    assert value.startswith(edited)
    assert b'[prompts.prd_review_checklist]' in value
    assert 'Human PR edit.' in assets[str(home/'.codex/AGENTS.md')].decode()
    assert 'Human PR edit.' in assets[str(home/'.claude/CLAUDE.md')].decode()

def test_local_code_drift_refuses_deployment(installed):
    home,service=installed;path=home/'.local/share/caller-pr-gates/caller_pr_gates/hook.py';path.write_text('Local human edit')
    before=original_bytes(home)
    with pytest.raises(RuntimeError,match='changed locally'):manage.deploy(home,service)
    assert original_bytes(home)==before and service.stops==0

def test_live_lease_refuses_deployment(installed):
    home,service=installed
    alter_owned_code(home)
    with sqlite3.connect(home/'.local/state/caller-pr-gates/registry.sqlite3') as db:
        db.execute('CREATE TABLE pr_watch(lease_until REAL)');db.execute('INSERT INTO pr_watch VALUES(?)',(time.time()+300,))
    before=original_bytes(home)
    with pytest.raises(RuntimeError,match='lease'):manage.deploy(home,service)
    assert original_bytes(home)==before and service.stops==0

def test_activation_failure_restores_prior_code_and_service(installed):
    home,service=installed;old=alter_owned_code(home);before=original_bytes(home);service.fail_next_start=True
    with pytest.raises(RuntimeError,match='activation failed'):manage.deploy(home,service)
    assert original_bytes(home)==before
    assert service.active and service.stops==1 and service.starts==2

def test_lease_race_after_service_stop_preserves_code(installed):
    home,service=installed;alter_owned_code(home)
    def acquire():
        with sqlite3.connect(home/'.local/state/caller-pr-gates/registry.sqlite3') as db:
            db.execute('CREATE TABLE pr_watch(lease_until REAL)');db.execute('INSERT INTO pr_watch VALUES(?)',(time.time()+300,))
    service.on_stop=acquire
    old=(home/'.local/share/caller-pr-gates/caller_pr_gates/hook.py').read_bytes()
    with pytest.raises(RuntimeError,match='lease'):manage.deploy(home,service)
    assert (home/'.local/share/caller-pr-gates/caller_pr_gates/hook.py').read_bytes()==old and service.active

def test_partial_file_failure_restores_prior_assets(installed,monkeypatch):
    home,service=installed;launcher=home/'.local/bin/caller-pr-gates';launcher.write_text('older launcher')
    before=original_bytes(home);original=manage.atomic
    def fail(path,*args,**kwargs):
        if Path(path)==home/'.local/share/caller-pr-gates/release.json':raise OSError('simulated release write failure')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(manage,'atomic',fail)
    with pytest.raises(OSError,match='simulated'):manage.deploy(home,service)
    assert original_bytes(home)==before and service.active

def test_native_callback_failure_rolls_back_provenance(installed,monkeypatch):
    home,service=installed;before=original_bytes(home)
    def fail(*args):raise RuntimeError('native callback failed')
    monkeypatch.setattr(manage,'native_smoke',fail)
    with pytest.raises(RuntimeError,match='native callback failed'):manage.deploy(home,service)
    assert original_bytes(home)==before and service.active

def test_rollback_refuses_human_edits_before_any_write(installed):
    home,service=installed;manage.deploy(home,service)
    release=home/'.local/share/caller-pr-gates/release.json';release.write_text('manual edit')
    before=original_bytes(home)
    with pytest.raises(RuntimeError,match='Local edit'):manage.rollback(home,service)
    assert original_bytes(home)==before

def test_real_native_commands_keep_context_and_draft_denial(installed):
    home,service=installed;NATIVE(Path(sys.executable),home)

def test_atomic_directory_and_symlink_exchange(tmp_path):
    first,second=tmp_path/'one',tmp_path/'two';first.mkdir();second.mkdir()
    (first/'value').write_text('one');(second/'value').write_text('two')
    manage.exchange(first,second);assert (first/'value').read_text()=='two'
    link=tmp_path/'pointer';link.symlink_to(second,target_is_directory=True)
    manage.exchange(first,link);assert first.is_symlink() and not link.is_symlink()
    manage.exchange(first,link);assert not first.is_symlink() and link.is_symlink()

def test_unrelated_native_hooks_and_settings_are_retained(installed):
    home,service=installed;path=home/'.claude/settings.json';data=json.loads(path.read_text())
    foreign={'hooks':[{'type':'command','command':'echo personal hook','timeout':2}]}
    data['hooks']['Stop'].append(foreign);path.write_text(json.dumps(data)+'\n');before=path.read_bytes()
    manage.deploy(home,service)
    assert path.read_bytes()==before and json.loads(path.read_text())['personal_setting']=='keep'

def test_missing_managed_blocks_are_added_without_replacing_personal_text(installed):
    home,service=installed;path=home/'.claude/CLAUDE.md';path.write_text('Personal text only.\n')
    manage.deploy(home,service)
    assert path.read_text().startswith('Personal text only.\n') and path.read_text().count('<!-- caller-pr-gates:begin -->')==1

def test_explicitly_disabled_hooks_are_preserved(installed):
    home,service=installed;path=home/'.codex/config.toml';path.write_text('[features]\nhooks = false\n');before=original_bytes(home)
    with pytest.raises(RuntimeError,match='explicitly disabled'):manage.deploy(home,service)
    assert original_bytes(home)==before

def test_deployment_lock_refuses_another_installer(installed):
    home,service=installed;base=home/'.local/share/agent-tooling'
    with manage.locked(base/'deployment.lock'):
        with pytest.raises(RuntimeError,match='Another'):manage.deploy(home,service)

def test_rollback_refuses_later_source_edits(installed):
    home,service=installed;alter_owned_code(home);manage.deploy(home,service)
    path=home/'.local/share/caller-pr-gates/caller_pr_gates/hook.py';path.write_bytes(path.read_bytes()+b'\n# Human edit after deployment\n')
    before=original_bytes(home)
    with pytest.raises(RuntimeError,match='Local source edit'):manage.rollback(home,service)
    assert original_bytes(home)==before and service.active

def test_interrupted_file_activation_recovers_without_state_rewind(installed,monkeypatch):
    home,service=installed;original=manage.atomic;release=home/'.local/share/caller-pr-gates/release.json'
    def interrupted(path,*args,**kwargs):
        result=original(path,*args,**kwargs)
        if Path(path)==release:raise KeyboardInterrupt('simulated process death after write')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(manage,'atomic',interrupted)
        with pytest.raises(KeyboardInterrupt):manage.deploy(home,service)
    assert (home/'.local/share/agent-tooling/pending.json').exists()
    with sqlite3.connect(home/'.local/state/caller-pr-gates/registry.sqlite3') as db:db.execute('UPDATE evidence SET value="post-crash work"')
    manage.deploy(home,service)
    assert not (home/'.local/share/agent-tooling/pending.json').exists()
    with sqlite3.connect(home/'.local/state/caller-pr-gates/registry.sqlite3') as db:assert db.execute('SELECT value FROM evidence').fetchone()[0]=='post-crash work'

def test_completed_journal_is_not_rolled_back_on_pending_cleanup(installed):
    home,service=installed;manage.deploy(home,service)
    base=home/'.local/share/agent-tooling';current=json.loads((base/'current.json').read_text())
    manage.dump(base/'pending.json',{'journal':str(Path(current['deployment'])/'journal.json')})
    before=original_bytes(home)
    assert manage.deploy(home,service)['status']=='already_installed'
    assert original_bytes(home)==before and service.stops==0

def test_missing_dependency_runtime_is_rebuilt_offline(installed,monkeypatch):
    home,service=installed
    monkeypatch.setattr(manage,'runtime_matches',RUNTIME_MATCHES)
    result=manage.deploy(home,service)
    runtime=home/'.local/share/caller-pr-stop-gate'
    assert runtime.is_symlink() and RUNTIME_MATCHES(runtime/'bin/python')
    assert not result['runtime_unchanged'] and service.active
    manage.rollback(home,service)
    assert not runtime.exists()

def test_stale_verification_cannot_activate(installed):
    home,service=installed;before=original_bytes(home)
    with pytest.raises(RuntimeError,match='after verification'):
        manage.deploy(home,service,verified_fingerprint='not-the-verified-source')
    assert original_bytes(home)==before and service.stops==0

def test_source_race_before_activation_is_rejected(installed,monkeypatch):
    home,service=installed;before=original_bytes(home)
    values=iter(['verified','verified','changed'])
    monkeypatch.setattr(manage,'fingerprint',lambda:next(values))
    with pytest.raises(RuntimeError,match='before activation'):
        manage.deploy(home,service,verified_fingerprint='verified')
    assert original_bytes(home)==before and service.stops==0


def test_reviewed_live_baseline_is_exact_and_rollback_retains_it(installed,monkeypatch):
    home,service=installed
    package=home/'.local/share/caller-pr-gates/caller_pr_gates'
    path=package/'workflow.py';path.write_bytes(path.read_bytes()+b'\n# Concurrent reviewed maintenance\n')
    base=home/'.local/share/agent-tooling';base.mkdir(parents=True)
    pointer={'version':'0.1.1','commit':'reviewed','gate_hashes':{'wrong':'hash'}}
    manage.dump(base/'current.json',pointer)
    row={'previous_version':'0.1.1','previous_commit':'reviewed','runtime_version':'1.7.0',
         'gate_hashes':{p.name:manage.sha(p.read_bytes()) for p in package.iterdir() if p.is_file() and p.suffix in {'.py','.json'}}}
    original=Path.read_text
    def read(p,*a,**k):
        if p==manage.ROOT/'release/upgrade-baselines.json':return json.dumps([row])
        return original(p,*a,**k)
    monkeypatch.setattr(Path,'read_text',read)
    before=path.read_bytes()
    manage.deploy(home,service);manage.rollback(home,service)
    assert path.read_bytes()==before
    path.write_bytes(before+b'# Additional unreviewed edit\n')
    with pytest.raises(RuntimeError,match='changed locally'):manage.deploy(home,service)


def test_stale_installer_refuses_downgrade_before_writes(installed,monkeypatch):
    home,service=installed;manage.deploy(home,service);before=original_bytes(home)
    monkeypatch.setattr(manage,'VERSION','0.0.1')
    with pytest.raises(RuntimeError,match='Stale installer'):manage.deploy(home,service)
    assert original_bytes(home)==before and service.stops==0


def test_actual_capture_installer_cannot_replace_new_release(installed):
    home,service=installed;manage.deploy(home,service);before=original_bytes(home)
    namespace={'__file__':str(manage.ROOT/'tools/manage.py'),'__name__':'captured_installer'}
    exec(compile((ROOT/'tests/fixtures/captured-installer-v0.1.0.py').read_text(),'captured_installer','exec'),namespace)
    namespace['runtime_matches']=lambda p:True
    namespace['native_smoke']=lambda *a:None
    with pytest.raises(RuntimeError,match='changed locally'):namespace['deploy'](home,service)
    assert original_bytes(home)==before and service.stops==0


def test_coordination_receipts_survive_upgrade_and_rollback(installed):
    from caller_pr_gates.quota import Coordinator
    home,service=installed;state=home/'.local/state/caller-pr-gates';co=Coordinator(state)
    permit=co.admit('account','core');co.throttle(permit,{'http_status':403,'message':'API rate limit exceeded'})
    with co.db() as db:before=[dict(r) for r in db.execute('SELECT * FROM cooldowns')]
    manage.deploy(home,service);manage.rollback(home,service)
    with co.db() as db:assert [dict(r) for r in db.execute('SELECT * FROM cooldowns')]==before


def launcher_child(tmp_path, program, inherited_path):
    """Exercise the generated shell launcher with an isolated CLI probe."""
    home=tmp_path/'launcher-home'
    package=home/'.local/share/caller-pr-gates/caller_pr_gates'
    package.mkdir(parents=True)
    (package/'__init__.py').write_text('')
    (package/'cli.py').write_text(program)
    python=home/'.local/share/caller-pr-stop-gate/bin/python'
    python.parent.mkdir(parents=True);python.symlink_to(sys.executable)
    launcher=home/'.local/bin/caller-pr-gates'
    launcher.parent.mkdir(parents=True);launcher.write_bytes(manage.render('launcher.sh',home))
    result=subprocess.run(['/bin/sh',str(launcher)],cwd=tmp_path,
        env=dict(os.environ,HOME=str(home),PATH=inherited_path),
        capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert result.stderr==''
    return home,json.loads(result.stdout)


def test_generated_launcher_keeps_path_controlled(tmp_path):
    foreign=tmp_path/'inherited-bin';foreign.mkdir()
    probe=foreign/'ambient-only-path-probe'
    probe.write_text('#!/bin/sh\nexit 0\n');probe.chmod(0o755)
    home,result=launcher_child(tmp_path,
        'import json,os,shutil\n'
        'print(json.dumps({"path":os.environ["PATH"].split(os.pathsep),'
        '"foreign":shutil.which("ambient-only-path-probe")}))\n',
        str(foreign)+':/sbin')
    assert result['path']==['/opt/homebrew/bin','/usr/local/bin','/usr/bin','/bin',
                            '/usr/sbin',str(home/'.local/bin')]
    assert result['foreign'] is None


@pytest.mark.skipif(sys.platform!='darwin',reason='Requires the actual macOS system sysctl')
def test_generated_launcher_child_discovers_actual_physical_cores(tmp_path):
    _,result=launcher_child(tmp_path,
        'import json,shutil,subprocess,warnings\n'
        'warnings.simplefilter("error")\n'
        'resolved=shutil.which("sysctl")\n'
        'child=subprocess.run(["sysctl","-n","hw.physicalcpu"],'
        'check=True,capture_output=True,text=True,timeout=10)\n'
        'print(json.dumps({"resolved":resolved,"cores":int(child.stdout),'
        '"stderr":child.stderr}))\n',
        '')
    assert result['resolved']=='/usr/sbin/sysctl'
    assert result['cores']>0
    assert result['stderr']==''


@pytest.mark.parametrize('active_worker',[False,True])
def test_launcher_only_upgrade_preserves_worker_admission_and_rollback(installed,active_worker):
    home,service=installed;launcher=home/'.local/bin/caller-pr-gates'
    # Normalize the fixture's legacy instructions before isolating this asset.
    manage.deploy(home,service)
    desired=manage.render('launcher.sh',home)
    previous=desired.replace(b':/usr/sbin:',b':')
    assert previous!=desired
    launcher.write_bytes(previous)
    current=json.loads((home/'.local/share/agent-tooling/current.json').read_text())
    pending=manage.desired_assets(home,current)
    assert {name for name,data in pending.items() if Path(name).read_bytes()!=data}=={str(launcher)}
    state=home/'.local/state/caller-pr-gates/registry.sqlite3'
    if active_worker:
        with sqlite3.connect(state) as db:
            db.execute('CREATE TABLE pr_watch(lease_until REAL)')
            db.execute('INSERT INTO pr_watch VALUES(?)',(time.time()+300,))
        before=original_bytes(home)
        with pytest.raises(RuntimeError,match='lease'):manage.deploy(home,service)
        assert original_bytes(home)==before and service.stops==service.starts==0
        return
    result=manage.deploy(home,service)
    assert result['code_unchanged'] and result['runtime_unchanged']
    assert not result['service_restarted'] and launcher.read_bytes()==desired
    with sqlite3.connect(state) as db:db.execute('UPDATE evidence SET value="post-upgrade work"')
    assert not manage.rollback(home,service)['state_restored']
    assert launcher.read_bytes()==previous and service.stops==service.starts==0
    with sqlite3.connect(state) as db:
        assert db.execute('SELECT value FROM evidence').fetchone()[0]=='post-upgrade work'
