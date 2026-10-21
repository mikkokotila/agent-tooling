"""Install a supported Codex custom provider without exporting Meta credentials."""
from __future__ import annotations

import json
import hashlib
import math
import datetime
import os
from pathlib import Path
import plistlib
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


def _statements(source):
    """TOML statement spans, keeping multiline strings/arrays opaque.

    tomllib validates the syntax; this scanner only locates edit boundaries.
    Every edit is also checked for semantic preservation before writing.
    """
    start = index = depth = 0
    quote = None
    triple = comment = False
    while index < len(source):
        char = source[index]
        if comment:
            if char == '\n':
                comment = False
                if depth == 0:
                    yield source[start:index + 1]
                    start = index + 1
        elif quote:
            if quote == '"' and char == '\\':
                index += 2
                continue
            if triple and source.startswith(quote * 3, index):
                end = index + 3
                while end < len(source) and source[end] == quote:
                    end += 1
                index = end
                quote = None
                continue
            if not triple and char == quote:
                quote = None
        elif char == '#':
            comment = True
        elif char in ('"', "'"):
            quote = char
            triple = source.startswith(char * 3, index)
            if triple:
                index += 3
                continue
        elif char in '[{':
            depth += 1
        elif char in ']}':
            depth -= 1
        elif char == '\n' and depth == 0:
            yield source[start:index + 1]
            start = index + 1
        index += 1
    if start < len(source):
        yield source[start:]


def _table_path(statement):
    value = tomllib.loads(statement + '\n__muse_table_probe__ = true\n')
    def find(node, path=()):
        if isinstance(node, list):
            return find(node[-1], path)
        if '__muse_table_probe__' in node:
            return path
        for key, child in node.items():
            found = find(child, path + (key,))
            if found is not None:
                return found
        return None
    return find(value)


def _unrelated(config):
    # Deep-copy parsed values, including TOML dates, without serialization.
    import copy
    value = copy.deepcopy(config)
    for key in ('model', 'model_provider', 'model_reasoning_effort', 'model_catalog_json'):
        value.pop(key, None)
    if 'model_providers' in value:
        value['model_providers'].pop('muse', None)
        if not value['model_providers']:
            value.pop('model_providers')
    return value


def _same_values(left, right):
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
        return True
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same_values(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same_values(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def _toml_value(value):
    """Encode only the inline provider map; all other statements stay intact."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace('\x7f', '\\u007f')
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return '[' + ', '.join(_toml_value(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(_toml_value(k) + ' = ' + _toml_value(v) for k, v in value.items()) + '}'
    raise ValueError('Unsupported TOML value; refusing to change configuration')


def _direct_provider_assignment(statement):
    quote = None
    index = 0
    while index < len(statement):
        char = statement[index]
        if quote:
            if quote == '"' and char == '\\':
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == '=':
            return tomllib.loads(statement[:index] + ' = {}') == {'model_providers': {}}
        index += 1
    return False


def config_text(original, base_url, catalog):
    """Edit owned settings, then prove unrelated TOML values are unchanged."""
    parsed = tomllib.loads(original)
    updates = {'model': 'muse-spark-1.3-contributor', 'model_provider': 'muse',
               'model_reasoning_effort': 'max', 'model_catalog_json': str(catalog)}
    owned = {'name': 'Muse Contributor (max only)', 'base_url': base_url, 'wire_api': 'responses',
             'requires_openai_auth': False, 'request_max_retries': 0, 'stream_max_retries': 0}
    # Installer owns this provider table; reject pre-existing unrelated names.
    existing = parsed.get('model_providers', {}).get('muse')
    if existing and (not isinstance(existing, dict) or existing.get('name') != 'Muse Contributor (max only)'):
        raise ValueError('An unrelated model_providers.muse table already exists')
    kept = []
    section = ()
    inline = False
    for statement in _statements(original):
        if statement.lstrip().startswith('['):
            section = _table_path(statement)
            if section[:2] != ('model_providers', 'muse'):
                kept.append(statement)
            continue
        if section[:2] == ('model_providers', 'muse'):
            continue
        fields = tomllib.loads(statement)
        if section == () and 'model_providers' in fields and _direct_provider_assignment(statement):
            providers = dict(fields['model_providers'])
            providers['muse'] = owned
            kept.append('model_providers = ' + _toml_value(providers) + '\n')
            inline = True
            continue
        if section == () and set(fields).intersection(updates):
            continue
        if section == ('model_providers',) and 'muse' in fields:
            continue
        kept.append(statement)
    head = ''.join(key + ' = ' + _toml_value(value) + '\n' for key, value in updates.items())
    provider = '' if inline else ('\n[model_providers.muse]\n' + ''.join(
        key + ' = ' + _toml_value(value) + '\n' for key, value in owned.items()))
    result = head + ''.join(kept).strip('\n') + '\n' + provider
    if not _same_values(_unrelated(tomllib.loads(result)), _unrelated(parsed)):
        raise ValueError('Provider configuration would change unrelated settings; refusing to write')
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
