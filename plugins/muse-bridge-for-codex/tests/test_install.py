"""Installer lifetime ownership, with no real Codex, Muse or credentials."""
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bridge
SPEC = importlib.util.spec_from_file_location('muse_install', ROOT / 'install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
PROVIDER_SPEC = importlib.util.spec_from_file_location('muse_install_provider', ROOT / 'provider.py')
provider = importlib.util.module_from_spec(PROVIDER_SPEC)
PROVIDER_SPEC.loader.exec_module(provider)


class InstallTests(unittest.TestCase):
    def setUp(self):
        # Darwin's default /var/folders path leaves too little AF_UNIX path space.
        self.directory = tempfile.TemporaryDirectory(prefix='muse-installer-', dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.state = self.home / '.local/state/muse-control'
        self.state.mkdir(parents=True)
        self.runtime = self.home / '.local/share/muse-bridge-for-codex/runtime'
        self.runtime.mkdir(parents=True)
        (self.runtime / 'bridge.py').write_text('raise RuntimeError("OLD RUNTIME")\n')
        self.bin = self.home / '.local/bin'
        self.bin.mkdir(parents=True)
        self.wrapper = self.bin / 'muse-control'
        self.wrapper.write_text('# muse-control old bridge.py wrapper\n')
        self.config = self.home / '.codex/config.toml'
        self.config.parent.mkdir()
        self.config.write_text('web_search="disabled"\n')
        codex = self.bin / 'codex'
        codex.write_text('#!/bin/sh\nexit 0\n')
        codex.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.config.parent),
                        PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''), PYTHONDONTWRITEBYTECODE='1')

    def test_running_controller_refuses_both_modes_without_mutation_or_signal(self):
        code = ('import fcntl,os,pathlib,sys; f=open(sys.argv[1],"a"); '
                'fcntl.flock(f,fcntl.LOCK_EX); ready=pathlib.Path(sys.argv[2]); '
                'temporary=ready.with_suffix(".tmp"); temporary.write_text(str(os.getpid())); '
                'temporary.replace(ready); sys.stdin.read()')
        lock = self.state / 'daemon.lock'
        ready = self.home / 'lock-ready'
        process = subprocess.Popen([sys.executable, '-c', code, str(lock), str(ready)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), 'owned lock holder did not become ready')
            self.assertEqual(str(process.pid), ready.read_text())
            inode = lock.stat().st_ino
            before = {path: path.read_bytes() for path in (self.runtime / 'bridge.py', self.wrapper, self.config)}
            for args in ([], ['--provider']):
                with self.subTest(args=args):
                    result = subprocess.run([sys.executable, str(ROOT / 'install.py'), *args], env=self.env,
                                            capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn('controller is running', result.stderr)
                    self.assertNotIn('Installed', result.stdout)
                    self.assertIsNone(process.poll())
                    self.assertEqual(inode, lock.stat().st_ino)
                    self.assertEqual(before, {path: path.read_bytes() for path in before})
                    self.assertFalse((self.home / 'Library/LaunchAgents').exists())
        finally:
            try:
                process.communicate('', timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    def test_quiet_reinstall_consumes_new_runtime_and_releases_same_lock(self):
        muse = self.bin / 'muse'
        muse.write_text('#!' + sys.executable + '\n' +
            'import json,sys\n'
            'for line in sys.stdin:\n'
            ' request=json.loads(line)\n'
            ' if "id" not in request: continue\n'
            ' method=request["method"]\n'
            f' result={{"schema":{{"fingerprint":{bridge.SCHEMA!r}}},"serverInfo":{{"version":"1.4.11"}}}} if method=="initialize" else '
            f'{{"models":[{{"modelId":{bridge.MODEL!r},"providerId":"meta","variants":["max"]}}]}}\n'
            ' print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}),flush=True)\n')
        muse.chmod(0o700)
        lock = self.state / 'daemon.lock'
        lock.touch()
        inode = lock.stat().st_ino
        result = subprocess.run([sys.executable, str(ROOT / 'install.py')], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('Installed', result.stdout)
        self.assertEqual((ROOT / 'bridge.py').read_bytes(), (self.runtime / 'bridge.py').read_bytes())
        self.assertEqual('web_search="disabled"\n', self.config.read_text())
        self.assertEqual(inode, lock.stat().st_ino)
        with lock.open('a') as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryFile(mode='w+') as daemon_log:
            daemon = subprocess.Popen([sys.executable, str(self.runtime / 'bridge.py'), 'daemon'], env=self.env,
                                      stdout=daemon_log, stderr=daemon_log)
            try:
                deadline = time.monotonic() + 10
                while not (self.state / 'control.sock').exists() and daemon.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                daemon_log.seek(0)
                self.assertTrue((self.state / 'control.sock').exists(), daemon_log.read())
                consumed = subprocess.run([str(self.wrapper), 'call', 'muse_status'], env=self.env,
                                          capture_output=True, text=True, timeout=10)
                self.assertEqual(0, consumed.returncode, consumed.stderr)
                status = json.loads(consumed.stdout)
                self.assertEqual(daemon.pid, status['pid'])
                self.assertEqual(bridge.VERSION, status['bridgeVersion'])
                self.assertTrue(status['running'])
                self.assertEqual(bridge.MODEL, status['model'])
                self.assertEqual('max', status['effort'])
            finally:
                if daemon.poll() is None:
                    daemon.terminate()
                try:
                    daemon.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)
                    self.fail('owned fake daemon required forced termination')
            self.assertFalse((self.state / 'control.sock').exists(), 'owned fake daemon left its socket')

    def test_provider_preparation_holds_lock_and_activation_runs_after_release(self):
        events = []
        state = self.state
        lock = state / 'daemon.lock'
        fake = types.SimpleNamespace(STATE=state, SOCKET=state / 'control.sock', MAX_FRAME=1024)
        def lock_is_held():
            with lock.open('a') as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
            return False
        def run(argv, **_kwargs):
            events.append(argv[1])
            self.assertEqual(argv[1] == 'bootout', lock_is_held())
        def ensure():
            self.assertFalse(lock_is_held())
            events.append('ensure')
        fake.ensure_daemon = ensure
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def settimeout(self, _): pass
            def connect(self, _): pass
            def sendall(self, _): pass
            def makefile(self, _): return io.BytesIO(b'{"result":{"base_url":"http://127.0.0.1:1/test"}}\n')
        env = dict(self.env, MUSE_GUARD_UPSTREAM=str(self.home / 'verified native'),
                   MUSE_GUARD_BYPASS='1', MUSE_GUARD_STATE_DIR=str(self.home / 'other-state'),
                   UNRELATED_INSTALL_ENV='not-a-service-setting')
        with patch.object(Path, 'home', return_value=self.home), patch.dict(os.environ, env), \
                patch.dict(sys.modules, {'bridge': fake}), patch.object(provider.subprocess, 'run', side_effect=run), \
                patch.object(provider.socket, 'socket', return_value=Connection()):
            with installer.controller_lock():
                service = provider.prepare_install(self.runtime)
                self.assertEqual(['bootout'], events)
                specification = plistlib.loads(service[1].read_bytes())
                self.assertEqual({'MUSE_GUARD_UPSTREAM': env['MUSE_GUARD_UPSTREAM']},
                                 specification['EnvironmentVariables'])
            provider.install(self.runtime, service)
        self.assertEqual(['bootout', 'bootstrap', 'ensure'], events)
        self.assertIn('web_search="disabled"', self.config.read_text())

    def test_provider_without_explicit_native_pin_does_not_add_service_environment(self):
        fake = types.SimpleNamespace(STATE=self.state)
        with patch.object(Path, 'home', return_value=self.home), patch.dict(os.environ, {}, clear=True), \
                patch.dict(sys.modules, {'bridge': fake}), patch.object(provider.subprocess, 'run'):
            with installer.controller_lock():
                _domain, agent = provider.prepare_install(self.runtime)
        self.assertNotIn('EnvironmentVariables', plistlib.loads(agent.read_bytes()))


if __name__ == '__main__':
    unittest.main()
