"""Explicit authenticated smoke check. Prints evidence, never credentials or URLs."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

import bridge


def safe(text):
    return re.sub(r'http://127\.0\.0\.1:\d+/[^\s"\x27]+', '[private Muse gateway]', text)


def main():
    env = dict(os.environ)
    home = Path(env['CODEX_HOME'])
    assert (home / 'config.toml').exists(), 'Install provider in the isolated CODEX_HOME first'
    command = ['codex']
    diagnostic = tempfile.TemporaryFile(mode='w+t')
    process = subprocess.Popen(command + ['app-server'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=diagnostic,
                               text=True, env=env)
    timer = threading.Timer(30, process.kill)
    timer.start()
    try:
        process.stdin.write(json.dumps({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'muse_bridge_verification', 'version': bridge.VERSION},
            'capabilities': {}}}) + '\n')
        process.stdin.flush()
        for line in process.stdout:
            message = json.loads(line)
            if message.get('id') == 1:
                assert 'error' not in message, safe(str(message))
                process.stdin.write('{"method":"initialized"}\n')
                process.stdin.write('{"id":2,"method":"model/list","params":{}}\n')
                process.stdin.flush()
            if message.get('id') == 2:
                assert 'error' not in message, safe(str(message))
                models = message['result']['data']
                muse = next(m for m in models if m['model'] == bridge.MODEL)
                assert muse['displayName'] == 'Muse Contributor'
                assert muse['defaultReasoningEffort'] == 'max'
                assert [e['reasoningEffort'] for e in muse['supportedReasoningEfforts']] == ['max']
                print('Codex model/list: Muse Contributor, max only', flush=True)
                break
        else:
            diagnostic.seek(0)
            raise RuntimeError('Codex model/list did not return: ' + safe(diagnostic.read()[-3000:]))
    finally:
        timer.cancel()
        process.terminate()
        process.wait(timeout=10)
        diagnostic.close()
    before = bridge.call_tool('muse_status', {})['providerGuard']
    with tempfile.TemporaryDirectory(prefix='muse-codex-live-') as directory:
        workspace = Path(directory)
        (workspace / 'read-token.txt').write_text('MUSE_CODEX_TOOL_OK\n')
        for prompt, expected, tool in [
            ('Reply exactly MUSE_CODEX_STREAM_OK. Do not use tools.', 'MUSE_CODEX_STREAM_OK', False),
            ('Use your shell tool to read read-token.txt in the current directory. '
             'Then reply with exactly its contents. You must actually use the tool.', 'MUSE_CODEX_TOOL_OK', True),
        ]:
            result = subprocess.run(command + ['exec', '--json', '--ephemeral', '--skip-git-repo-check',
                '--sandbox', 'workspace-write', '-C', directory, prompt], env=env,
                capture_output=True, text=True, timeout=240)
            if result.returncode:
                raise RuntimeError(safe((result.stderr + '\n' + result.stdout)[-3500:]))
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            messages = [e.get('item', {}).get('text', '') for e in events
                        if e.get('type') == 'item.completed' and e.get('item', {}).get('type') == 'agent_message']
            assert any(expected in message for message in messages), safe(str(events))
            assert any(e.get('type') == 'turn.completed' for e in events), 'Missing completed stream'
            if tool:
                assert any(e.get('type') == 'item.completed' and e.get('item', {}).get('type') == 'command_execution'
                           for e in events), 'No actual shell tool round trip'
            print('Codex live ' + ('tool continuation' if tool else 'streaming text') + ': PASS', flush=True)
    after = bridge.call_tool('muse_status', {})['providerGuard']
    assert after['forwarded'] >= before['forwarded'] + 3, 'Missing provider inference evidence'
    assert after['response_blocked'] == before['response_blocked'], 'Model mismatch or bad stream'
    print('Meta route: contributor/max; authenticated requests forwarded; no response policy mismatch', flush=True)


if __name__ == '__main__':
    main()
