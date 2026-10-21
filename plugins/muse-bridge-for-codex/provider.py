"""Install a supported Codex custom provider without exporting Meta credentials."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import plistlib
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import tomllib


def atomic(path, body, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.' + secrets.token_hex(8))
    try:
        with temp.open('xb') as stream:
            os.chmod(temp, mode)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def config_text(original, base_url, catalog):
    """Replace only owned settings; preserve all unrelated text and TOML tables."""
    parsed = tomllib.loads(original)
    updates = {'model': 'muse-spark-1.3-contributor', 'model_provider': 'muse',
               'model_reasoning_effort': 'max', 'model_catalog_json': str(catalog)}
    lines = original.splitlines(keepends=True)
    first_table = next((i for i, line in enumerate(lines) if line.lstrip().startswith('[')), len(lines))
    head, rest = ''.join(lines[:first_table]), ''.join(lines[first_table:])
    if head and not head.endswith('\n'):
        head += '\n'
    for key, value in updates.items():
        pattern = re.compile(r'^' + re.escape(key) + r'\s*=.*(?:\n|$)', re.M)
        replacement = key + ' = ' + json.dumps(value) + '\n'
        if pattern.search(head):
            head = pattern.sub(lambda _m: replacement, head)
        else:
            head += replacement
    # Installer owns this provider table; reject pre-existing unrelated names.
    existing = parsed.get('model_providers', {}).get('muse')
    if existing and existing.get('name') != 'Muse Contributor (max only)':
        raise ValueError('An unrelated model_providers.muse table already exists')
    table = re.compile(r'^\[model_providers\.muse\]\s*\n.*?(?=^\[|\Z)', re.M | re.S)
    rest = table.sub('', rest)
    provider = ('\n[model_providers.muse]\nname = "Muse Contributor (max only)"\n'
                'base_url = ' + json.dumps(base_url) + '\nwire_api = "responses"\n'
                'requires_openai_auth = false\nrequest_max_retries = 0\nstream_max_retries = 0\n')
    result = head + rest.rstrip() + '\n' + provider
    tomllib.loads(result)
    return result


def backup_path(config, state):
    # A smoke CODEX_HOME must never occupy the user's real backup slot.
    identity = hashlib.sha256(str(config.resolve()).encode()).hexdigest()
    return state / 'config-backups' / (identity + '.toml')


def install(root):
    """Run after the controller runtime is installed at a stable local path."""
    import bridge
    state = bridge.STATE
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    gateway = state / 'gateway.json'
    if not gateway.exists():
        atomic(gateway, json.dumps({'port': 0, 'key': secrets.token_urlsafe(32)}).encode())
    # Stop only this bridge's daemon, retaining every durable Muse session.
    try:
        status = bridge.call_tool('muse_status', {})
        os.kill(status['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 15
        while bridge.SOCKET.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        if bridge.SOCKET.exists():
            raise RuntimeError('Existing bridge did not stop; provider installation aborted')
    except (ConnectionError, FileNotFoundError):
        pass
    label = 'local.muse-bridge-for-codex'
    agent = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    domain = 'gui/' + str(os.getuid())
    subprocess.run(['launchctl', 'bootout', domain + '/' + label],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    atomic(agent, plistlib.dumps({'Label': label, 'ProgramArguments': [sys.executable, str(root / 'bridge.py'), 'daemon'],
                                 'RunAtLoad': True, 'KeepAlive': True,
                                 'WorkingDirectory': str(root),
                                 'StandardOutPath': str(state / 'daemon.log'),
                                 'StandardErrorPath': str(state / 'daemon.log')}))
    subprocess.run(['launchctl', 'bootstrap', domain, str(agent)], check=True)
    bridge.ensure_daemon()
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(90)
        connection.connect(str(bridge.SOCKET))
        connection.sendall(b'{"action":"provider_info"}\n')
        with connection.makefile('rb') as stream:
            response = json.loads(stream.readline(bridge.MAX_FRAME + 1))
    if 'error' in response:
        raise RuntimeError(response['error'])
    config = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'config.toml'
    original = config.read_text() if config.exists() else ''
    backup = backup_path(config, state)
    if not backup.exists():
        atomic(backup, original.encode())
    body = config_text(original, response['result']['base_url'], root / 'catalog.json')
    atomic(config, body.encode())
    print('Muse Contributor provider configured at max effort. Restart Codex to reload its model catalog.')
