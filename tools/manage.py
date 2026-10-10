"""Offline build and verification; state-preserving transactional macOS deployment."""
from pathlib import Path
import argparse, contextlib, ctypes, fcntl, hashlib, json, os, platform, re, shutil, shlex
import sqlite3, subprocess, sys, tempfile, time, tomllib, uuid, xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / '.build'
CAPTURE = json.loads((ROOT / 'capture/operational-baseline.json').read_text())
VERSION = (ROOT / 'VERSION').read_text().strip()
RELEASE = json.loads((ROOT/'release/runtime-manifest.json').read_text())
EXCLUDED = {'.git', '.build', '.venv', 'dist', '__pycache__', '.pytest_cache'}

def sha(data): return hashlib.sha256(data).hexdigest()
def source_files():
    result=[]
    for directory, folders, names in os.walk(ROOT):
        folders[:]=[name for name in folders if name not in EXCLUDED and not name.endswith('.egg-info')]
        result.extend(Path(directory)/name for name in names if not name.endswith('.pyc') and name!='.DS_Store')
    return sorted(result)

def fingerprint():
    return sha(json.dumps({str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in source_files()}, sort_keys=True).encode())

def atomic(path, data, mode=None):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    mode = mode if mode is not None else (path.stat().st_mode & 0o777 if path.exists() else 0o644)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, mode); os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

def dump(path, data): atomic(path, (json.dumps(data, indent=2) + '\n').encode())

@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        try: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('Another build or deployment is running')
        yield

def run(argv, **kwargs):
    return subprocess.run(list(map(str, argv)), check=True, timeout=kwargs.pop('timeout', 180), **kwargs)

def assert_capture():
    if tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']!=VERSION: raise RuntimeError('VERSION and project metadata disagree')
    if RELEASE['version'] != VERSION: raise RuntimeError('Release manifest version disagrees')
    actual={str(p.relative_to(ROOT)) for p in (ROOT/'caller_pr_gates').iterdir() if p.is_file() and p.suffix in {'.py','.json'}}
    if actual != set(RELEASE['gate_files']): raise RuntimeError('Release runtime inventory differs')
    for name, expected in RELEASE['gate_files'].items():
        if sha((ROOT / name).read_bytes()) != expected:
            raise RuntimeError('Runtime differs from committed release manifest: ' + name)
    for group in [CAPTURE['runtime_wheels'], json.loads((ROOT / 'wheels/tools/manifest.json').read_text())]:
        for row in group:
            path = ROOT / row['file'] if row['file'].startswith('wheels/') else ROOT / 'wheels/tools' / row['file']
            if sha(path.read_bytes()) != row['sha256']: raise RuntimeError('Dependency wheel changed: ' + str(path))

def tool_environment():
    if sys.version_info[:2] != (3, 13) or sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise RuntimeError('v0.1.0 reproduces the captured Python 3.13 macOS arm64 environment')
    manifest = json.loads((ROOT / 'wheels/tools/manifest.json').read_text())
    revision = sha(json.dumps([CAPTURE['runtime_wheels'], manifest], sort_keys=True).encode())
    target = BUILD / ('tools-' + revision[:16]); python = target / 'bin/python'
    ready = target / '.ready'
    if ready.exists() and ready.read_text() == revision: return python
    if target.exists(): shutil.rmtree(target)
    run([sys.executable, '-m', 'venv', target])
    wheels = [ROOT / r['file'] for r in CAPTURE['runtime_wheels']]
    wheels += [ROOT / 'wheels/tools' / r['file'] for r in manifest]
    run([python, '-m', 'pip', 'install', '--no-index', '--no-deps', *wheels], stdout=subprocess.DEVNULL)
    atomic(ready, revision.encode())
    return python

def build():
    assert_capture(); python = tool_environment()
    run([python, '-c', "import sys; sys.path.insert(0,'tools'); import backend; print(backend.build_wheel('.build/dist'))"], cwd=ROOT)
    return python

def verify():
    revision = fingerprint()
    report = BUILD / 'verified.json'
    if report.exists() and json.loads(report.read_text()).get('source_fingerprint') == revision:
        assert_capture(); return json.loads(report.read_text())
    python = build()
    with tempfile.TemporaryDirectory(prefix='agent-tooling-verification-') as state:
        env = dict(os.environ, PYTHONPATH=str(ROOT), CALLER_PR_GATES_STATE_DIR=state,
                   CALLER_PR_GATES_PROMPTS_STATE_DIR=state, CALLER_PR_GATES_PROMPTS_FILE=str(ROOT / 'prompts.toml'),
                   CODEX_CLI='codex',CLAUDE_CLI='claude')
        run([python, '-m', 'pytest', '-q', '--junitxml=.build/verification.xml'], cwd=ROOT, env=env)
    xml = ET.parse(BUILD / 'verification.xml')
    expected = [case for case in xml.iter('testcase') if any(s.get('type') == 'pytest.xfail' for s in case.findall('skipped'))]
    if len(expected) != len(RELEASE['known_defects']): raise RuntimeError('Known defect inventory changed')
    if fingerprint() != revision: raise RuntimeError('Checkout changed during verification; publish and verify the new revision')
    value = {'source_fingerprint':revision, 'verified_at':time.time(), 'version':VERSION,
             'known_expected_failures':RELEASE['known_defects'], 'testcases':len(list(xml.iter('testcase')))}
    dump(report, value); return value

def exchange(first, second):
    """Atomic directory/symlink exchange; never expose a missing live path."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin': result = libc.renameatx_np(-2, os.fsencode(first), -2, os.fsencode(second), 2)
    else: result = libc.renameat2(-100, os.fsencode(first), -100, os.fsencode(second), 2)
    if result: raise OSError(ctypes.get_errno(), 'Atomic activation failed')

def catalogue_texts(data, defaults):
    document = tomllib.loads(data.decode())
    if set(document) != {'schema_version','prompts'} or type(document['schema_version']) is not int or document['schema_version'] != 1:
        raise RuntimeError('Invalid prompt catalogue schema')
    if len(data) > 256 * 1024 or set(document['prompts']) != set(defaults['prompts']): raise RuntimeError('Invalid prompt sections')
    result = {}
    for key, spec in defaults['prompts'].items():
        row = document['prompts'][key]; text = row.get('text')
        if set(row) != {'text'} or not isinstance(text,str) or not text.strip(): raise RuntimeError('Invalid prompt text: ' + key)
        if set(re.findall(r'\$\{([a-zA-Z_][a-zA-Z_0-9]*)\}', text)) != set(spec['parameters']): raise RuntimeError('Invalid placeholders: ' + key)
        if spec['protected_prefix'] and not text.startswith(spec['protected_prefix']): raise RuntimeError('Invalid routing prefix: ' + key)
        if (key.startswith('global_') or key == 'communication') and re.search(r'<!-- (?:caller-pr-gates|troubleshooting-report|pr-progress):(?:begin|end) -->',text):
            raise RuntimeError('Invalid managed instruction marker: ' + key)
        result[key] = text
    return result

def merge_instructions(original, texts):
    value = original.decode()
    blocks = {'caller-pr-gates':texts['global_pr_obligations'], 'troubleshooting-report':texts['global_communication'].replace('${communication}',texts['communication']), 'pr-progress':texts['global_progress']}
    for marker, text in blocks.items():
        begin, end = '<!-- '+marker+':begin -->', '<!-- '+marker+':end -->'
        if begin not in value and end not in value:
            value += ('\n\n' if value and not value.endswith('\n\n') else '') + begin+'\n'+text+end+'\n'
        elif value.count(begin) != 1 or value.count(end) != 1 or value.index(begin) > value.index(end):
            raise RuntimeError('Invalid managed instruction boundaries: '+marker)
        else: value = re.sub(re.escape(begin)+r'[\s\S]*?'+re.escape(end), lambda _:begin+'\n'+text+end, value)
    return value.encode()

def merge_hooks(original, desired):
    document = json.loads(original) if original else {}
    # v0.1.0 owns its captured callback definitions; preserve unrelated hooks.
    hooks = document.setdefault('hooks', {})
    for event, wanted in desired['hooks'].items():
        groups = hooks.get(event, [])
        foreign = []
        for group in groups:
            other = [h for h in group.get('hooks',[]) if not any(s in h.get('command','') for s in ('caller_pr_gates.hook','caller_pr_gates.troubleshooting'))]
            if other: foreign.append({**group,'hooks':other})
        hooks[event] = [*wanted, *foreign]
    if original and json.loads(original) == document: return original
    return (json.dumps(document, indent=2)+'\n').encode()

def render(path, home): return (ROOT / 'config' / path).read_bytes().replace(b'${HOME}',os.fsencode(home))

def native_smoke(python, home):
    """Exercise installed native definitions using isolated state and no PRs."""
    with tempfile.TemporaryDirectory(prefix='agent-tooling-native-') as state:
        for client in ['codex','claude']:
            config=home/('.'+client)/('hooks.json' if client=='codex' else 'settings.json')
            data=json.loads(config.read_text())
            for event in ['SessionStart','PreToolUse']:
                command=next(h['command'] for group in data['hooks'][event] for h in group['hooks'] if h['command'].endswith('caller_pr_gates.hook'))
                argv=[]
                for part in shlex.split(command):
                    if part.startswith('CALLER_PR_GATES_STATE_DIR='): part='CALLER_PR_GATES_STATE_DIR='+state
                    if part.endswith('/caller-pr-stop-gate/bin/python'): part=str(python)
                    argv.append(part)
                payload={'hook_event_name':event,'session_id':str(uuid.uuid4()),'cwd':state}
                if event=='PreToolUse': payload.update(tool_name='Bash',tool_input={'command':'gh pr create --draft --title example'})
                env=dict(os.environ,CALLER_PR_GATES_PROMPTS_STATE_DIR=state,CALLER_PR_GATES_PROMPTS_FILE=str(home/'.local/share/caller-pr-gates/prompts.toml'))
                result=run(argv,input=json.dumps(payload),capture_output=True,text=True,env=env,timeout=15)
                if result.stderr: raise RuntimeError('Native hook produced stderr: '+result.stderr)
                output=json.loads(result.stdout)
                if event=='SessionStart' and not output.get('hookSpecificOutput',{}).get('additionalContext'): raise RuntimeError('Missing native context')
                if event=='PreToolUse' and output.get('hookSpecificOutput',{}).get('permissionDecision')!='deny': raise RuntimeError('Native draft denial was lost')

def process_alive(pid):
    if type(pid) is not int or pid <= 0: return True
    try: os.kill(pid,0)
    except ProcessLookupError: return False
    except (PermissionError,OSError): return True
    return True

def orphaned_claude_delivery(db,row):
    # A consumed deadline is not an active worker when both independent owner
    # inventories prove it finished. Preserve the row and every task receipt.
    if not {'delivery_state','client','worker_pid','owner_session'}.issubset(row.keys()): return False
    if row['delivery_state']!='consumed' or row['client']!='claude' or process_alive(row['worker_pid']): return False
    try: owner=db.execute('SELECT cwd,client FROM sessions WHERE session_id=?',(row['owner_session'],)).fetchone()
    except sqlite3.Error: return False
    if not owner or owner['client']!='claude' or not owner['cwd']: return False
    from caller_pr_gates.supervisor import claude_availability
    return claude_availability(row['owner_session'],owner['cwd'])=='idle'

def live_leases(state):
    path = state / 'registry.sqlite3'
    if not path.exists(): return []
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=3) as db:
        db.row_factory=sqlite3.Row
        result = []
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            columns = {r[1] for r in db.execute('PRAGMA table_info("'+table+'")')}
            if 'lease_until' in columns:
                rows=db.execute('SELECT * FROM "'+table+'" WHERE lease_until>?',(time.time(),)).fetchall()
                count=sum(not (table=='pr_watch' and orphaned_claude_delivery(db,row)) for row in rows)
                if count: result.append({'table':table,'count':count})
            if table == 'recovery':
                for (raw,) in db.execute('SELECT fields FROM recovery'):
                    fields = json.loads(raw)
                    if fields.get('lease_until',0) > time.time(): result.append({'table':table,'count':1})
        return result

def wait_for_workers(state, seconds):
    """Wait before deployment; admission is still rechecked by deploy itself."""
    deadline=time.monotonic()+seconds
    while live_leases(state):
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise RuntimeError('A live PR/document worker still holds a lease; deployment deferred without changing state')
        time.sleep(min(5,remaining))

class Service:
    def __init__(self, home):
        self.target=f'gui/{os.getuid()}/local.caller-pr-monitor'
        self.plist=home/'Library/LaunchAgents/local.caller-pr-monitor.plist'
    def running(self):
        p=subprocess.run(['launchctl','print',self.target],capture_output=True,text=True,timeout=10)
        return p.returncode == 0 and bool(re.search(r'\bpid = \d+',p.stdout))
    def stop(self): run(['launchctl','bootout',self.target],capture_output=True,timeout=10)
    def start(self):
        p=subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',self.plist],capture_output=True,text=True,timeout=10)
        if p.returncode and not self.running(): raise RuntimeError('Monitor activation failed: '+p.stderr.strip())
        run(['launchctl','kickstart','-p',self.target],capture_output=True,timeout=10)
        if not self.running(): raise RuntimeError('Monitor did not start')

def runtime_matches(python):
    if not python.exists(): return False
    program = "import importlib.metadata as m,json,pathlib; print(json.dumps({'versions':{d.metadata['Name']:d.version for d in m.distributions()},'site':str(pathlib.Path(m.distribution('httpx')._path).parent)}))"
    p=subprocess.run([python,'-c',program],capture_output=True,text=True,timeout=10)
    if p.returncode: return False
    info=json.loads(p.stdout)
    expected={r['name']:r['version'] for r in CAPTURE['runtime_wheels']}
    if info['versions'] != expected: return False
    site=Path(info['site'])
    return all((site/name).is_file() and sha((site/name).read_bytes()) == expected for name,expected in CAPTURE['runtime_file_hashes'].items())

def prepare_runtime(base):
    revision=sha(json.dumps(CAPTURE['runtime_wheels'],sort_keys=True).encode())
    target=base/'runtimes'/revision;python=target/'bin/python'
    if not runtime_matches(python):
        if target.exists(): raise RuntimeError('Previously prepared runtime drifted; refusing to overwrite it')
        run([sys.executable,'-m','venv',target])
        run([python,'-m','pip','install','--no-index','--no-deps',*[ROOT/r['file'] for r in CAPTURE['runtime_wheels']]],stdout=subprocess.DEVNULL)
        if not runtime_matches(python): raise RuntimeError('Rebuilt environment differs from captured dependency files')
    return target

def desired_assets(home, current):
    live=home/'.local/share/caller-pr-gates'
    path=live/'prompts.toml';incoming=(ROOT/'prompts.toml').read_bytes();existing=path.read_bytes() if path.exists() else None
    prior=(current or {}).get('source_prompt_hash',CAPTURE['prompt_sha256'])
    if existing is not None and sha(existing) != prior and sha(incoming) != prior and existing != incoming:
        raise RuntimeError('Local prompts and repository prompts both changed. Reconcile them before installation; live text was preserved.')
    data=existing if existing is not None and sha(existing) != prior else incoming
    texts=catalogue_texts(data,json.loads((ROOT/'caller_pr_gates/prompt-defaults.json').read_text()))
    assets={str(path):data, str(home/'.local/bin/caller-pr-gates'):render('launcher.sh',home),
            str(home/'Library/LaunchAgents/local.caller-pr-monitor.plist'):render('monitor.plist',home)}
    for client,name in [('codex','AGENTS.md'),('claude','CLAUDE.md')]:
        target=home/('.'+client)/name
        assets[str(target)]=merge_instructions(target.read_bytes() if target.exists() else b'',texts)
        config=home/('.'+client)/('hooks.json' if client=='codex' else 'settings.json')
        assets[str(config)]=merge_hooks(config.read_bytes() if config.exists() else b'',json.loads(render(client+'-hooks.json',home)))
    target=home/'.codex/config.toml';original=target.read_bytes() if target.exists() else b''
    value=original.decode(); parsed=tomllib.loads(value)
    if parsed.get('features',{}).get('hooks') is False: raise RuntimeError('Codex hooks were explicitly disabled; refusing to overwrite that preference')
    if parsed.get('features',{}).get('hooks') is not True:
        if re.search(r'^\[features\]\s*$',value,re.M): value=re.sub(r'(^\[features\]\s*$)',r'\1\nhooks = true',value,count=1,flags=re.M)
        else: value+='\n[features]\nhooks = true\n'
    # Only reuse captured trust for the exact captured home/definitions.
    if str(home)==CAPTURE['captured_home']:
        trusted=json.loads((ROOT/'capture/codex-trust.json').read_text())
        current_trust=parsed.get('hooks',{}).get('state',{})
        for key, row in trusted.items():
            if key in current_trust and current_trust[key] != row: raise RuntimeError('Codex hook trust changed locally: '+key)
            if key not in current_trust: value+='\n[hooks.state.'+json.dumps(key)+']\ntrusted_hash = '+json.dumps(row['trusted_hash'])+'\n'
    assets[str(target)]=value.encode()
    return assets

def restore(journal):
    for name,row in reversed(list(journal['files'].items())):
        path=Path(name)
        current=sha(path.read_bytes()) if path.exists() else None
        if current==row['before']: continue
        if current != row['after']: raise RuntimeError('Concurrent edit blocks rollback: '+name)
        if row['before'] is None: path.unlink(missing_ok=True)
        else: atomic(path,Path(row['backup']).read_bytes(),row['mode'])
    for row in reversed(journal.get('exchanges',[])):
        path,prior=Path(row['live']),Path(row['prior'])
        if row.get('created'):
            if path.is_symlink(): path.unlink()
            elif path.exists(): shutil.rmtree(path)
        elif row.get('after_inode'):
            if path.lstat().st_ino==row['before_inode']: continue
            if path.lstat().st_ino!=row['after_inode']: raise RuntimeError('Concurrent activation blocks rollback: '+str(path))
            exchange(path,prior)
        else: exchange(path,prior)

def deploy(home=None, service=None, verified_fingerprint=None):
    if verified_fingerprint is not None and fingerprint()!=verified_fingerprint:
        raise RuntimeError('Checkout changed after verification; publish and verify the current revision')
    home=Path(home or Path.home());base=home/'.local/share/agent-tooling';live=home/'.local/share/caller-pr-gates'
    state=home/'.local/state/caller-pr-gates';service=service or Service(home)
    base.mkdir(parents=True,exist_ok=True)
    with locked(base/'deployment.lock'):
        pending=base/'pending.json'
        if pending.exists():
            previous=json.loads(Path(json.loads(pending.read_text())['journal']).read_text())
            if not previous.get('committed'):
                if live_leases(state): raise RuntimeError('Interrupted deployment recovery awaits a live worker lease')
                if service.running(): service.stop()
                restore(previous)
                prior=previous.get('previous_pointer')
                if prior is None: (base/'current.json').unlink(missing_ok=True)
                else: dump(base/'current.json',prior)
                if previous.get('was_running'): service.start()
            pending.unlink()
        pointer=base/'current.json';current=json.loads(pointer.read_text()) if pointer.exists() else None
        if current:
            before_version=current.get('version','')
            if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',before_version) and tuple(map(int,VERSION.split('.')))<tuple(map(int,before_version.split('.'))):
                raise RuntimeError('Stale installer cannot downgrade an installed release; use make rollback')
        assets=desired_assets(home,current)
        asset_before={name:(Path(name).read_bytes() if Path(name).exists() else None) for name in assets}
        package=live/'caller_pr_gates'
        incoming={p.name:p.read_bytes() for p in (ROOT/'caller_pr_gates').iterdir() if p.is_file() and p.suffix in {'.py','.json'}}
        originals={p.name:p.read_bytes() for p in package.iterdir() if p.is_file() and p.suffix in {'.py','.json'}} if package.exists() else {}
        expected=(current or {}).get('runtime_inventory',(current or {}).get('gate_hashes',{Path(n).name:v for n,v in RELEASE['gate_files'].items()}))
        actual_hashes={n:sha(v) for n,v in originals.items()}
        if originals and actual_hashes != expected:
            # Only a committed, reviewed complete baseline may cross the drift guard.
            accepted=json.loads((ROOT/'release/upgrade-baselines.json').read_text())
            live_release=json.loads((live/'release.json').read_text())
            if not current or not any(
                current.get('commit')==b['previous_commit'] and current.get('version')==b['previous_version']
                and live_release.get('version')==b['runtime_version'] and actual_hashes==b['gate_hashes']
                for b in accepted):
                raise RuntimeError('Installed gate code changed locally; reconcile before installation')
        code_changed=incoming != originals
        runtime_changed=not runtime_matches(home/'.local/share/caller-pr-stop-gate/bin/python')
        revision=fingerprint()
        if verified_fingerprint is not None and revision!=verified_fingerprint:
            raise RuntimeError('Checkout changed while preparing deployment')
        if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip(): raise RuntimeError('Commit and publish checkout changes before installation')
        commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        release_path=live/'release.json'
        asset_before[str(release_path)]=release_path.read_bytes() if release_path.exists() else None
        release=json.loads(release_path.read_text()) if release_path.exists() else {'version':CAPTURE['captured_gate_version']}
        release['source']=str(ROOT)
        release['version']=RELEASE['runtime_version']
        release['agent_tooling']={'version':VERSION,'commit':commit,'source_fingerprint':revision,'captured_gate_version':CAPTURE['captured_gate_version'], 'known_defects':RELEASE['known_defects']}
        assets[str(release_path)]=(json.dumps(release,indent=2)+'\n').encode()
        changes={name:data for name,data in assets.items() if not Path(name).exists() or Path(name).read_bytes()!=data}
        if not changes and not code_changed and not runtime_changed and service.running():
            return {'status':'already_installed','version':VERSION,'commit':commit,'runtime_unchanged':True}
        operational_change=code_changed or runtime_changed or any(name!=str(release_path) for name in changes)
        if operational_change and live_leases(state): raise RuntimeError('A live PR/document worker holds a lease; deployment deferred without changing state')
        prepared=prepare_runtime(base) if runtime_changed else None
        deployment=base/'deployments'/(str(time.time_ns())+'-'+uuid.uuid4().hex[:8]);deployment.mkdir(parents=True,mode=0o700)
        journal={'version':VERSION,'commit':commit,'files':{},'exchanges':[], 'state_restored':False,'previous_pointer':current}
        was_running=service.running()
        journal['was_running']=was_running
        restart=code_changed or runtime_changed or str(service.plist) in changes
        stopped=False
        try:
            # Last compare before activation catches human edits during preparation.
            if verified_fingerprint is not None and fingerprint()!=verified_fingerprint:
                raise RuntimeError('Checkout changed before activation; no activation performed')
            if operational_change and live_leases(state): raise RuntimeError('A worker acquired a lease; no activation performed')
            if any((Path(name).read_bytes() if Path(name).exists() else None)!=before for name,before in asset_before.items()): raise RuntimeError('Concurrent configuration edit; no activation performed')
            if package.exists() and {p.name:sha(p.read_bytes()) for p in package.iterdir() if p.is_file() and p.suffix in {'.py','.json'}}!={n:sha(v) for n,v in originals.items()}: raise RuntimeError('Concurrent code edit; no activation performed')
            dump(deployment/'journal.json',journal);dump(pending,{'journal':str(deployment/'journal.json')})
            if was_running and restart: service.stop();stopped=True
            if operational_change and live_leases(state): raise RuntimeError('A worker acquired a lease before service stop; no activation performed')
            if code_changed:
                stage=deployment/'package';stage.mkdir()
                for name,data in incoming.items(): atomic(stage/name,data)
                if package.exists():
                    journal['exchanges'].append({'live':str(package),'prior':str(stage),'before_inode':package.lstat().st_ino,'after_inode':stage.lstat().st_ino})
                    dump(deployment/'journal.json',journal);exchange(package,stage)
                else:
                    package.parent.mkdir(parents=True,exist_ok=True)
                    journal['exchanges'].append({'live':str(package),'prior':str(stage),'created':True})
                    dump(deployment/'journal.json',journal);os.rename(stage,package)
            if runtime_changed:
                target=home/'.local/share/caller-pr-stop-gate';stage=deployment/'runtime';stage.symlink_to(prepared,target_is_directory=True)
                if target.exists():
                    journal['exchanges'].append({'live':str(target),'prior':str(stage),'before_inode':target.lstat().st_ino,'after_inode':stage.lstat().st_ino})
                    dump(deployment/'journal.json',journal);exchange(target,stage)
                else:
                    journal['exchanges'].append({'live':str(target),'prior':str(stage),'created':True})
                    dump(deployment/'journal.json',journal);os.rename(stage,target)
            for index,(name,data) in enumerate(changes.items()):
                path=Path(name);before=path.read_bytes() if path.exists() else None
                if before!=asset_before.get(name): raise RuntimeError('Concurrent edit: '+name)
                backup=deployment/('file-'+str(index));mode=path.stat().st_mode&0o777 if path.exists() else (0o755 if name.endswith('/caller-pr-gates') else 0o644)
                if before is not None: atomic(backup,before,mode)
                journal['files'][name]={'before':sha(before) if before is not None else None,'after':sha(data),'backup':str(backup),'mode':mode}
                dump(deployment/'journal.json',journal)
                atomic(path,data,mode)
            if stopped or not was_running: service.start();stopped=False
            if not service.running(): raise RuntimeError('Monitor is not running after installation')
            if not runtime_matches(home/'.local/share/caller-pr-stop-gate/bin/python'): raise RuntimeError('Post-install runtime differs')
            native_smoke(home/'.local/share/caller-pr-stop-gate/bin/python',home)
            pointer_value={'schema':2,'version':VERSION,'commit':commit,'source_fingerprint':revision,'source_prompt_hash':sha((ROOT/'prompts.toml').read_bytes()),
                'runtime_inventory':{n:sha(v) for n,v in incoming.items()},'deployment':str(deployment),'previous_pointer':current}
            dump(pointer,pointer_value);journal['committed']=True;dump(deployment/'journal.json',journal)
            pending.unlink()
            return {'status':'installed','version':VERSION,'commit':commit,'deployment':str(deployment),'code_unchanged':not code_changed,
                    'runtime_unchanged':not runtime_changed,'service_restarted':restart or not was_running,'changed_files':list(changes),'state_restored':False}
        except Exception:
            if (code_changed or runtime_changed) and service.running(): service.stop()
            restore(journal)
            if current is None: pointer.unlink(missing_ok=True)
            else: dump(pointer,current)
            if was_running and not service.running(): service.start()
            pending.unlink(missing_ok=True)
            raise

def rollback(home=None,service=None):
    home=Path(home or Path.home());base=home/'.local/share/agent-tooling';service=service or Service(home)
    with locked(base/'deployment.lock'):
        pointer=base/'current.json'
        if not pointer.exists(): raise RuntimeError('No agent-tooling deployment to roll back')
        current=json.loads(pointer.read_text());journal=json.loads((Path(current['deployment'])/'journal.json').read_text())
        restart=bool(journal['exchanges']) or str(service.plist) in journal['files']
        if restart and live_leases(home/'.local/state/caller-pr-gates'): raise RuntimeError('A live worker holds a lease; rollback deferred')
        # Validate every affected file before changing any.
        for name,row in journal['files'].items():
            if (sha(Path(name).read_bytes()) if Path(name).exists() else None)!=row['after']: raise RuntimeError('Local edit blocks rollback: '+name)
        package=home/'.local/share/caller-pr-gates/caller_pr_gates'
        hashes={p.name:sha(p.read_bytes()) for p in package.iterdir() if p.is_file() and p.suffix in {'.py','.json'}}
        if hashes!=current.get('runtime_inventory',current.get('gate_hashes')): raise RuntimeError('Local source edit blocks rollback')
        if any(row['live'].endswith('/caller-pr-stop-gate') for row in journal['exchanges']) and not runtime_matches(home/'.local/share/caller-pr-stop-gate/bin/python'):
            raise RuntimeError('Local dependency edit blocks rollback')
        was_running=service.running()
        if restart and was_running: service.stop()
        restore(journal)
        if current['previous_pointer'] is None: pointer.unlink()
        else: dump(pointer,current['previous_pointer'])
        if restart and was_running: service.start()
        return {'status':'rolled_back','state_restored':False,'service_restarted':restart and was_running}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['build','verify','install','rollback','status'])
    parser.add_argument('--wait-for-workers',type=float,default=None,help='Bounded seconds to wait for worker admission before install (default 600); 0 fails immediately, never bypasses a lease')
    args=parser.parse_args()
    if args.wait_for_workers is None:
        args.wait_for_workers=600 if args.command=='install' else 0
    if not 0<=args.wait_for_workers<=3600 or args.wait_for_workers and args.command!='install':
        parser.error('--wait-for-workers requires install and a finite value from 0 to 3600')
    BUILD.mkdir(exist_ok=True)
    if args.command=='status':
        target=Path.home()/'.local/share/agent-tooling/current.json'
        print(target.read_text() if target.exists() else 'No agent-tooling deployment recorded');return
    if args.command=='rollback': print(json.dumps(rollback(),indent=2));return
    with locked(BUILD/'build.lock'):
        if args.command=='build': build()
        else:
            report=verify()
            if args.command=='install' and args.wait_for_workers:
                wait_for_workers(Path.home()/'.local/state/caller-pr-gates',args.wait_for_workers)
            print(json.dumps(deploy(verified_fingerprint=report['source_fingerprint']) if args.command=='install' else report,indent=2))

if __name__=='__main__': main()
