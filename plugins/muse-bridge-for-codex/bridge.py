#!/usr/bin/env python3
"""Local Muse controller. The provider route and effort are not caller options."""
from __future__ import annotations

import argparse
import atexit
from collections import deque
import fcntl
import json
import os
from pathlib import Path
import queue
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid

from policy_proxy import PolicyProxy

MODEL = 'muse-spark-1.3-contributor'
EFFORT = 'max'
PROVIDER = 'meta'
VERSION = '0.2.0'
SCHEMA = 'sha256:f282ad16e21520d0cda68cb190149b86e2b062eef1dd282749360c05aa3d1082'
MUSE = Path.home() / '.local/bin/muse'
STATE = Path.home() / '.local/state/muse-control'
SOCKET = STATE / 'control.sock'
MAX_FRAME = 16 * 1024 * 1024


def uid7() -> str:
    value = (int(time.time() * 1000) << 80) | (7 << 76) | (uuid.uuid4().int & ((1 << 76) - 1))
    return str(uuid.UUID(int=(value & ~(3 << 62)) | (2 << 62)))


class BridgeError(RuntimeError):
    pass


def schema(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {}, 'required': list(required),
            'additionalProperties': False}


TEXT = {'type': 'string', 'minLength': 1}
SID = {'type': 'string', 'format': 'uuid'}
TOOLS = [
    ('muse_status', 'Verify the live Muse host and immutable contributor/max provider policy.', schema()),
    ('muse_launch', 'Launch a Muse agent, always muse-spark-1.3-contributor at max. Returns its session ID.',
     schema({'workspace': TEXT, 'name': TEXT, 'prompt': TEXT, 'command_id': SID}, ('workspace',))),
    ('muse_list', 'List retained Muse sessions and identify sessions controlled by this bridge.',
     schema({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}, 'cursor': TEXT,
             'workspace': TEXT})),
    ('muse_read', 'Read session metadata and a page of durable transcript events, including agent messages.',
     schema({'session_id': SID, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000},
             'cursor': TEXT, 'direction': {'type': 'string', 'enum': ['forward', 'backward']}}, ('session_id',))),
    ('muse_send', 'Send a prompt to a controlled agent, queueing it if busy. Contributor/max are fixed.',
     schema({'session_id': SID, 'message': TEXT, 'command_id': SID}, ('session_id', 'message'))),
    ('muse_steer', 'Inject guidance into the exact running turn. Contributor/max are fixed.',
     schema({'session_id': SID, 'turn_id': SID, 'message': TEXT, 'command_id': SID},
            ('session_id', 'turn_id', 'message'))),
    ('muse_interrupt', 'Interrupt a controlled agent, optionally targeting the exact turn.',
     schema({'session_id': SID, 'turn_id': SID, 'command_id': SID}, ('session_id',))),
    ('muse_resume', 'Resume a stopped contributor session and enforce max before admitting prompts. Refuses other models.',
     schema({'session_id': SID, 'command_id': SID}, ('session_id',))),
    ('muse_events', 'Poll live events and agent replies from controlled sessions. Cursors survive client reconnects within this daemon.',
     schema({'after': {'type': 'integer', 'minimum': 0}, 'session_id': SID,
             'wait_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 50},
             'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000}})),
    ('muse_pending', 'Read pending tool approvals for a controlled agent.',
     schema({'session_id': SID}, ('session_id',))),
    ('muse_approve', 'Answer an existing approval using the requirement and choice returned by muse_pending.',
     schema({'session_id': SID, 'approval_id': TEXT, 'requirement_id': TEXT,
             'choice_id': TEXT, 'feedback': {'type': 'string'}, 'command_id': SID},
            ('session_id', 'approval_id', 'requirement_id', 'choice_id'))),
]
TOOL_MAP = {name: (description, spec) for name, description, spec in TOOLS}


def validate(name, args):
    if name not in TOOL_MAP:
        raise BridgeError('Unknown tool; arbitrary Muse RPC is not exposed.')
    if not isinstance(args, dict):
        raise BridgeError('Arguments must be an object.')
    spec = TOOL_MAP[name][1]
    extra = set(args) - set(spec['properties'])
    if extra:
        raise BridgeError('Unsupported arguments: ' + ', '.join(sorted(extra)))
    for key in spec['required']:
        if key not in args:
            raise BridgeError('Missing argument: ' + key)
    for key, value in args.items():
        field = spec['properties'][key]
        if field['type'] == 'string':
            if not isinstance(value, str) or (field.get('minLength') and not value.strip()):
                raise BridgeError('Invalid string: ' + key)
            if field.get('format') == 'uuid':
                try:
                    parsed = uuid.UUID(value)
                except (ValueError, AttributeError):
                    raise BridgeError('Invalid UUID: ' + key) from None
                if key == 'command_id' and parsed.version != 7:
                    raise BridgeError('command_id must be a UUIDv7.')
        if field['type'] == 'integer':
            if type(value) is not int or not field.get('minimum', float('-inf')) <= value <= field.get('maximum', float('inf')):
                raise BridgeError('Invalid integer: ' + key)
        if 'enum' in field and value not in field['enum']:
            raise BridgeError('Invalid value: ' + key)


class Host:
    def __init__(self):
        gateway_file = STATE / 'gateway.json'
        gateway = json.loads(gateway_file.read_text()) if gateway_file.exists() else None
        self.proxy = PolicyProxy(gateway=gateway)
        base = self.proxy.start()
        if gateway and not gateway['port']:
            gateway['port'] = self.proxy._server.server_address[1]
            temporary = gateway_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(gateway))
            temporary.chmod(0o600)
            temporary.replace(gateway_file)
        self.auth_lock = threading.Lock()
        self.auth_session = None
        self.events = deque(maxlen=20000)
        self.event_seq = 0
        self.event_condition = threading.Condition()
        self.requests = {}
        self.requests_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.operation_lock = threading.RLock()
        self.next_id = 0
        self.managed = set()
        self.stderr = deque(maxlen=20)
        self.policy_broken = None
        self.closed = False
        self.started = time.time()
        env = os.environ.copy()
        env.pop('MUSE_GUARD_BYPASS', None)
        env.update(MUSE_NO_AUTO_UPDATE='1', MUSE_CONTRIBUTOR_MODE='on')
        self.command = [str(MUSE), 'serve', '--provider', PROVIDER, '--model', MODEL,
                        '--contributor-mode', 'on', '--disable-request-user-input',
                        '-c', 'reasoning_effort="max"',
                        '-c', 'first_turn_minimal_effort_regex=' + json.dumps(r'\b\B'),
                        '-c', 'provider_retry.max_retries=0',
                        '-c', 'endpoint_transport=' + json.dumps({'base_url': base, 'auth': 'bearer'})]
        try:
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
                                            start_new_session=True)
            threading.Thread(target=self._reader, daemon=True).start()
            threading.Thread(target=self._stderr, daemon=True).start()
            self.info = self.rpc('initialize', {'clientInfo': {'name': 'muse_control', 'version': VERSION},
                                             'capabilities': {'requestedCapabilities': ['sessionRename'],
                                                              'userInputDialogs': False}}, timeout=20)
            if self.info.get('schema', {}).get('fingerprint') != SCHEMA:
                raise BridgeError('Muse protocol changed; refusing all inference until the bridge is reverified.')
            if self.info.get('serverInfo', {}).get('version') != '1.4.11':
                raise BridgeError('Unverified Muse version; refusing all inference.')
            self._write({'jsonrpc': '2.0', 'method': 'initialized'})
            catalog = self.rpc('model/list', {}, timeout=20)
            matches = [m for m in catalog.get('models', []) if m.get('modelId') == MODEL and m.get('providerId') == PROVIDER]
            if len(matches) != 1 or EFFORT not in matches[0].get('variants', []):
                raise BridgeError('Pinned contributor model with max is unavailable; no fallback is allowed.')
            self.catalog_source = catalog.get('source')
            self.model_entry = {k: matches[0].get(k) for k in ['modelId', 'providerId', 'variants', 'displayLabel']}
            self.proxy.refresh_credentials = self._refresh_credentials
        except BaseException:
            self.close()
            raise

    def _refresh_credentials(self):
        """Let native Muse obtain/refresh its own Keychain credential.

        Only the existing private provider transport sees the bearer, in RAM.
        No Keychain export, credential file, or credential-returning RPC exists.
        """
        with self.auth_lock:
            if self.proxy.credentials_ready():
                return
            with self.operation_lock:
                if self.auth_session is None:
                    workspace = STATE / 'provider-auth'
                    workspace.mkdir(exist_ok=True, mode=0o700)
                    result = self._dispatch('muse_launch', {'workspace': str(workspace),
                                                         'name': 'Codex Muse provider authentication'})
                    self.auth_session = result['session']['sessionId']
                self._dispatch('muse_send', {'session_id': self.auth_session,
                                            'message': 'Reply READY. Do not use tools.'})
            if not self.proxy._auth_ready.wait(45):
                raise BridgeError('Native Muse authentication did not become available.')

    def _write(self, msg):
        with self.write_lock:
            self.process.stdin.write(json.dumps(msg, separators=(',', ':')) + '\n')
            self.process.stdin.flush()

    def _stderr(self):
        for line in self.process.stderr:
            # Kept in memory for diagnosis; never copied to MCP protocol output or state files.
            self.stderr.append(line.rstrip()[:2000])

    def _reader(self):
        try:
            for line in self.process.stdout:
                if len(line) > MAX_FRAME:
                    raise BridgeError('Muse emitted an oversized protocol frame.')
                msg = json.loads(line)
                if 'id' in msg and 'method' not in msg:
                    with self.requests_lock:
                        waiting = self.requests.get(msg['id'])
                    if waiting:
                        waiting.put(msg)
                    continue
                if 'method' in msg:
                    # Presentation receipt only. Decisions require an explicit muse_approve call.
                    if 'id' in msg:
                        self._write({'jsonrpc': '2.0', 'id': msg['id'], 'result': {}})
                    with self.event_condition:
                        self.event_seq += 1
                        self.events.append({'seq': self.event_seq, **msg})
                        self.event_condition.notify_all()
                    params = msg.get('params', {})
                    if msg['method'] == 'session/modelChanged':
                        model = params.get('model', {})
                        selected = params.get('modelId', model.get('modelId'))
                        if selected and selected != MODEL:
                            self.policy_broken = 'Muse changed a controlled model; no further prompts are allowed.'
        except (ValueError, OSError, BridgeError) as exc:
            self.policy_broken = 'Muse protocol failure: ' + type(exc).__name__
        finally:
            with self.requests_lock:
                for waiting in self.requests.values():
                    waiting.put({'error': {'message': 'Muse host disconnected; result may be uncertain. Read the session before retrying.'}})
            with self.event_condition:
                self.event_condition.notify_all()

    def rpc(self, method, params, timeout=30):
        if self.process.poll() is not None:
            raise BridgeError('Muse host is not running; no fallback is allowed.')
        waiting = queue.Queue()
        with self.requests_lock:
            self.next_id += 1
            request_id = self.next_id
            self.requests[request_id] = waiting
        try:
            self._write({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
            try:
                result = waiting.get(timeout=timeout)
            except queue.Empty:
                raise BridgeError('Muse request timed out. Its outcome may be uncertain; inspect before retrying.') from None
            if 'error' in result:
                error = result['error']
                raise BridgeError(json.dumps(error))
            return result['result']
        finally:
            with self.requests_lock:
                self.requests.pop(request_id, None)

    def _record(self):
        target = STATE / 'managed.json'
        temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps({'sessions': sorted(self.managed), 'model': MODEL, 'effort': EFFORT}) + '\n')
        temp.chmod(0o600)
        temp.replace(target)

    def _session(self, sid):
        return self.rpc('session/read', {'sessionId': sid, 'excludeItems': True})

    def _verify(self, sid):
        if self.policy_broken:
            raise BridgeError(self.policy_broken)
        if sid not in self.managed:
            raise BridgeError('Session is not controlled by this host. Use muse_resume; an active foreign CLI lease cannot be taken over.')
        result = self._session(sid)
        session = result['session']
        if session.get('modelId') != MODEL or session.get('providerId') != PROVIDER:
            raise BridgeError('Session route violates the pinned contributor policy; prompt refused.')
        return result

    def _set_max(self, sid):
        self.rpc('session/setReasoningEffort', {'sessionId': sid, 'commandId': uid7(), 'reasoningEffort': EFFORT})

    def dispatch(self, name, args):
        validate(name, args)
        if name == 'muse_events':
            return self._events(args)
        with self.operation_lock:
            return self._dispatch(name, args)

    def _dispatch(self, name, a):
        sid = a.get('session_id')
        command = a.get('command_id') or uid7()
        if name == 'muse_status':
            return {'bridgeVersion': VERSION, 'pid': os.getpid(), 'musePid': self.process.pid,
                    'running': self.process.poll() is None, 'host': self.info,
                    'model': MODEL, 'provider': PROVIDER, 'effort': EFFORT, 'contributorMode': True,
                    'policy': 'strict provider request validation; no alternate model, effort, or fallback',
                    'policyBroken': self.policy_broken, 'providerGuard': self.proxy.snapshot(),
                    'managedSessions': sorted(self.managed), 'catalogSource': self.catalog_source,
                    'catalogModel': self.model_entry, 'startedAt': self.started}
        if name == 'muse_launch':
            workspace = Path(a['workspace']).expanduser()
            if not workspace.is_absolute() or not workspace.is_dir():
                raise BridgeError('workspace must be an existing absolute directory.')
            result = self.rpc('session/start', {'commandId': command, 'workspaceRoot': str(workspace.resolve()),
                                               'providerId': PROVIDER, 'modelId': MODEL, 'approvalMode': 'onRequest'})
            sid = result['session']['sessionId']
            self.managed.add(sid)
            self._record()
            self._set_max(sid)
            self._verify(sid)
            if a.get('name'):
                result['rename'] = self.rpc('session/rename', {'sessionId': sid, 'commandId': uid7(), 'name': a['name']})
            result['policy'] = {'model': MODEL, 'effort': EFFORT, 'contributorMode': True}
            if a.get('prompt'):
                prompt_id = str(uuid.UUID(int=uuid.UUID(command).int ^ 1))
                result['turn'] = self._dispatch('muse_send', {'session_id': sid, 'message': a['prompt'],
                                                           'command_id': prompt_id})
            return result
        if name == 'muse_list':
            params = {'limit': a.get('limit', 50)}
            if a.get('cursor'):
                params['cursor'] = a['cursor']
            if a.get('workspace'):
                params['workspaceRoot'] = str(Path(a['workspace']).expanduser().resolve())
            result = self.rpc('session/list', params)
            for session in result['sessions']:
                session['bridgeControlled'] = session['sessionId'] in self.managed
                session['matchesContributorPolicy'] = session.get('modelId') == MODEL and session.get('providerId') == PROVIDER
            return result
        if name == 'muse_read':
            result = self._session(sid)
            params = {'sessionId': sid, 'limit': a.get('limit', 100), 'direction': a.get('direction', 'backward')}
            if a.get('cursor'):
                params['cursor'] = a['cursor']
            result['transcriptPage'] = self.rpc('view/page', params)
            return result
        if name == 'muse_resume':
            prior = self._session(sid)['session']
            if prior.get('modelId') != MODEL or prior.get('providerId') != PROVIDER:
                raise BridgeError('Refusing to resume a session with a different or unknown model/provider.')
            result = self.rpc('session/resume', {'commandId': command, 'sessionId': sid, 'excludeItems': True})
            self.managed.add(sid)
            self._record()
            self._set_max(sid)
            self._verify(sid)
            result['policy'] = {'model': MODEL, 'effort': EFFORT, 'contributorMode': True}
            return result
        if name in ('muse_send', 'muse_steer'):
            self._verify(sid)
            params = {'commandId': command, 'sessionId': sid,
                      'input': [{'type': 'text', 'text': a['message']}], 'reasoningEffort': EFFORT}
            method = 'turn/start'
            if name == 'muse_steer':
                method = 'turn/steer'
                params['expectedTurnId'] = a['turn_id']
            else:
                params['ifBusy'] = 'queue'
            return self.rpc(method, params)
        if name == 'muse_interrupt':
            if sid not in self.managed:
                raise BridgeError('Only this host\'s controlled sessions can be interrupted.')
            params = {'commandId': command, 'sessionId': sid}
            if a.get('turn_id'):
                params['turnId'] = a['turn_id']
            return self.rpc('turn/interrupt', params)
        if name == 'muse_pending':
            self._verify(sid)
            return self.rpc('approval/listPending', {'sessionId': sid})
        if name == 'muse_approve':
            self._verify(sid)
            params = {'commandId': command, 'sessionId': sid, 'approvalId': a['approval_id'],
                      'requirementId': a['requirement_id'], 'choiceId': a['choice_id']}
            if 'feedback' in a:
                params['feedback'] = a['feedback']
            return self.rpc('approval/decide', params)
        raise BridgeError('Unhandled tool.')

    def _events(self, a):
        after = a.get('after', 0)
        limit = a.get('limit', 100)
        sid = a.get('session_id')
        deadline = time.monotonic() + a.get('wait_seconds', 0)
        with self.event_condition:
            while True:
                rows = [e for e in self.events if e['seq'] > after and (not sid or e.get('params', {}).get('sessionId') == sid)]
                if rows or time.monotonic() >= deadline or self.process.poll() is not None:
                    page = rows[:limit]
                    return {'events': page, 'next': page[-1]['seq'] if page else self.event_seq,
                            'gap': bool(self.events and after and after < self.events[0]['seq'] - 1),
                            'running': self.process.poll() is None}
                self.event_condition.wait(min(1, deadline - time.monotonic()))

    def close(self):
        if self.closed:
            return
        self.closed = True
        process = getattr(self, 'process', None)
        if process and process.poll() is None:
            # Every owned Muse process has its own process group.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        self.proxy.close()

def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def load_json(line):
    return json.loads(line, object_pairs_hook=no_duplicates)


class LocalHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(MAX_FRAME + 1)
            if len(line) > MAX_FRAME:
                raise BridgeError('Request is too large.')
            message = load_json(line)
            if message.get('action') == 'provider_info':
                result = {'base_url': self.server.host.proxy.gateway_url()}
            else:
                result = self.server.host.dispatch(message['tool'], message.get('arguments', {}))
            response = {'result': result}
        except Exception as exc:
            response = {'error': str(exc)}
        try:
            self.wfile.write((json.dumps(response) + '\n').encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


class LocalServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def run_daemon():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    lock = open(STATE / 'daemon.lock', 'a')
    os.chmod(lock.name, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    SOCKET.unlink(missing_ok=True)
    host = Host()
    server = LocalServer(str(SOCKET), LocalHandler)
    SOCKET.chmod(0o600)
    server.host = host
    atexit.register(host.close)
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        server.server_close()
        host.close()
        SOCKET.unlink(missing_ok=True)


def ensure_daemon():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(.5)
            connection.connect(str(SOCKET))
            return
    except OSError:
        pass
    log = open(STATE / 'daemon.log', 'ab', buffering=0)
    os.chmod(log.name, 0o600)
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'daemon'],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(.2)
                connection.connect(str(SOCKET))
                return
        except OSError:
            time.sleep(.1)
    raise BridgeError('Muse bridge did not start. Read ~/.local/state/muse-control/daemon.log; no fallback was launched.')


def call_tool(name, args):
    validate(name, args)
    ensure_daemon()
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(90)
        connection.connect(str(SOCKET))
        connection.sendall((json.dumps({'tool': name, 'arguments': args}) + '\n').encode())
        with connection.makefile('rb') as stream:
            raw = stream.readline(MAX_FRAME + 1)
        if len(raw) > MAX_FRAME:
            raise BridgeError('Bridge response exceeds the frame limit; request a smaller transcript page.')
        response = load_json(raw)
    if 'error' in response:
        raise BridgeError(response['error'])
    return response['result']


def run_mcp():
    write_lock = threading.Lock()
    def respond(message):
        with write_lock:
            print(json.dumps(message, separators=(',', ':')), flush=True)
    def handle(message):
        request_id = message.get('id')
        method = message.get('method')
        if request_id is None:
            return
        try:
            if method == 'initialize':
                result = {'protocolVersion': message.get('params', {}).get('protocolVersion', '2024-11-05'),
                          'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'muse-control', 'version': VERSION},
                          'instructions': 'All managed Muse inference is pinned to muse-spark-1.3-contributor with max effort. No model/effort override or generic RPC exists.'}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': [{'name': name, 'description': description, 'inputSchema': spec,
                                     'annotations': {'readOnlyHint': name in ('muse_status', 'muse_list', 'muse_read', 'muse_events', 'muse_pending'),
                                                     'openWorldHint': True}}
                                    for name, description, spec in TOOLS]}
            elif method == 'tools/call':
                params = message.get('params', {})
                try:
                    data = call_tool(params['name'], params.get('arguments', {}))
                    result = {'content': [{'type': 'text', 'text': json.dumps(data)}], 'isError': False}
                except Exception as exc:
                    result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
            else:
                respond({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32601, 'message': 'Method not found'}})
                return
            respond({'jsonrpc': '2.0', 'id': request_id, 'result': result})
        except Exception as exc:
            respond({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32603, 'message': str(exc)}})
    workers = []
    for line in sys.stdin:
        if len(line) > MAX_FRAME:
            continue
        try:
            message = load_json(line)
        except ValueError:
            respond({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}})
            continue
        # Long event polls must not block interrupt or send requests.
        worker = threading.Thread(target=handle, args=(message,))
        worker.start()
        workers = [w for w in workers if w.is_alive()]
        workers.append(worker)
    for worker in workers:
        worker.join(timeout=90)


def main():
    parser = argparse.ArgumentParser(description='Muse control: contributor model and max effort are mandatory.')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('daemon')
    sub.add_parser('mcp')
    invoke = sub.add_parser('call')
    invoke.add_argument('tool', choices=TOOL_MAP)
    invoke.add_argument('--args', default='{}', help='JSON arguments; no model or effort override is accepted')
    args = parser.parse_args()
    if args.command == 'daemon':
        run_daemon()
    elif args.command == 'mcp':
        run_mcp()
    else:
        try:
            print(json.dumps(call_tool(args.tool, load_json(args.args)), indent=2))
        except (BridgeError, ValueError) as exc:
            print(json.dumps({'error': str(exc)}), file=sys.stderr)
            raise SystemExit(1)


if __name__ == '__main__':
    main()
