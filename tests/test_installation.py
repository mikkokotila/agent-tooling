"""Deployment, crash/error rollback, real native callbacks and evidence retention."""
from pathlib import Path
import copy, json, os, shutil, sqlite3, sys, time, zipfile
import pytest

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'tools'))
import manage
NATIVE=manage.native_smoke
RUNTIME_MATCHES=manage.runtime_matches

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
        if name!='.local/share/caller-pr-gates/release.json':assert (home/name).read_bytes()==data,name
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
    text=prompt.read_text();text=text.replace('Completion requires every separate gate to pass:', 'Completion requires every separate gate to pass:')
    # Change a plain fragment while retaining required protected prefixes/tokens.
    text=text.replace('Handle routine engineering and CI work autonomously', 'Handle routine engineering and CI work carefully and autonomously')
    assert text!=prompt.read_text();prompt.write_text(text)
    manage.deploy(home,service)
    assert prompt.read_text()==text
    for p in [home/'.codex/AGENTS.md',home/'.claude/CLAUDE.md']:
        assert 'carefully and autonomously' in p.read_text() and p.read_text().startswith('Personal prefix.') and p.read_text().endswith('Personal suffix.\n')

def test_invalid_local_prompt_is_rejected_without_overwriting(installed):
    home,service=installed;prompt=home/'.local/share/caller-pr-gates/prompts.toml';prompt.write_text('invalid TOML')
    before=original_bytes(home)
    with pytest.raises((RuntimeError,ValueError)):manage.deploy(home,service)
    assert original_bytes(home)==before and service.stops==0

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
