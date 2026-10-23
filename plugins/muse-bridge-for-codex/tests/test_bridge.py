import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bridge


class BridgePolicyTests(unittest.TestCase):
    def test_no_tool_accepts_model_or_effort(self):
        for name, _, spec in bridge.TOOLS:
            valid = {}
            for key in spec['required']:
                field = spec['properties'][key]
                valid[key] = ('00000000-0000-4000-8000-000000000001'
                              if field.get('format') == 'uuid' else 'valid')
            bridge.validate(name, valid)
            for key in ('model', 'model_id', 'modelId', 'provider', 'providerId',
                        'reasoning_effort', 'reasoningEffort', 'contributor_mode',
                        'base_url', 'config', 'rpc'):
                with self.subTest(tool=name, key=key):
                    self.assertNotIn(key, spec['properties'])
                    with self.assertRaisesRegex(bridge.BridgeError, 'Unsupported arguments'):
                        bridge.validate(name, {**valid, key: 'other'})

    def test_actual_runtime_validation_rejects_bad_types(self):
        for args in ({'wait_seconds': True}, {'wait_seconds': 51}, {'session_id': 'not-a-uuid'},
                     {'limit': 0}, {'after': -1}):
            with self.assertRaises(bridge.BridgeError):
                bridge.validate('muse_events', args)
        bridge.validate('muse_events', {'after': 0, 'wait_seconds': 0, 'limit': 1})

    def fake_host(self):
        h = bridge.Host.__new__(bridge.Host)
        h.managed = {'00000000-0000-4000-8000-000000000001'}
        h.policy_broken = None
        h.operation_lock = threading.RLock()
        self.calls = []
        h._verify = lambda sid: {'session': {'modelId': bridge.MODEL, 'providerId': 'meta'}}
        h.rpc = lambda method, params, **kw: self.calls.append((method, params)) or {'status': 'accepted'}
        return h

    def test_send_pins_max_and_queues(self):
        h = self.fake_host()
        h.dispatch('muse_send', {'session_id': next(iter(h.managed)), 'message': 'hello'})
        method, args = self.calls[-1]
        self.assertEqual(method, 'turn/start')
        self.assertEqual(args['reasoningEffort'], 'max')
        self.assertEqual(args['ifBusy'], 'queue')
        self.assertEqual(args['input'], [{'type': 'text', 'text': 'hello'}])

    def test_steer_pins_max_and_exact_turn(self):
        h = self.fake_host()
        turn = '00000000-0000-4000-8000-000000000002'
        h.dispatch('muse_steer', {'session_id': next(iter(h.managed)), 'turn_id': turn, 'message': 'change'})
        method, args = self.calls[-1]
        self.assertEqual(method, 'turn/steer')
        self.assertEqual(args['reasoningEffort'], 'max')
        self.assertEqual(args['expectedTurnId'], turn)

    def test_noncontributor_resume_rejected_before_load(self):
        h = self.fake_host()
        h._session = lambda sid: {'session': {'modelId': 'other-model', 'providerId': 'meta'}}
        with self.assertRaises(bridge.BridgeError):
            h.dispatch('muse_resume', {'session_id': next(iter(h.managed))})
        self.assertEqual([], self.calls)

    def test_foreign_session_cannot_be_interrupted(self):
        h = self.fake_host()
        with self.assertRaises(bridge.BridgeError):
            h.dispatch('muse_interrupt', {'session_id': '00000000-0000-4000-8000-000000000009'})
        self.assertEqual([], self.calls)

    def test_duplicate_json_is_rejected(self):
        with self.assertRaises(ValueError):
            bridge.load_json('{"model":"a","model":"b"}')

    def test_uuid7_generation_and_retry_identity(self):
        value = bridge.uid7()
        import uuid
        self.assertEqual(7, uuid.UUID(value).version)
        for key in ('muse_send', 'muse_interrupt', 'muse_resume'):
            args = {'session_id': '00000000-0000-4000-8000-000000000001', 'command_id': value}
            if key == 'muse_send':
                args['message'] = 'hello'
            bridge.validate(key, args)

    def test_mcp_discovery_and_rejection_need_no_host(self):
        frames = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'muse_send', 'arguments': {'model': 'other'}}},
        ]
        p = subprocess.run([sys.executable, str(ROOT/'bridge.py'), 'mcp'],
                           input=''.join(json.dumps(x)+'\n' for x in frames), text=True,
                           capture_output=True, timeout=10, check=True)
        replies = {r['id']: r for r in map(json.loads, p.stdout.splitlines())}
        self.assertEqual('muse-control', replies[1]['result']['serverInfo']['name'])
        self.assertEqual(len(bridge.TOOLS), len(replies[2]['result']['tools']))
        self.assertTrue(replies[3]['result']['isError'])
        self.assertEqual('', p.stderr)


if __name__ == '__main__':
    unittest.main()
