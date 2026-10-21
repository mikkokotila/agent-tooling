"""Offline contract tests. No test calls an external provider."""

import gzip
import http.client
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
from urllib.parse import urlsplit


MODULE_PATH = Path(__file__).resolve().parents[1] / "policy_proxy.py"
SPEC = importlib.util.spec_from_file_location("muse_policy_proxy", MODULE_PATH)
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


class Response:
    def __init__(self, body=None, content_type="application/json", status=200, headers=None,
                 chunks=None):
        if body is None:
            body = json.dumps({"model": policy.MODEL, "output": []}).encode()
        self.status = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.body = io.BytesIO(body)
        self.chunks = iter(chunks) if chunks is not None else None

    def getheader(self, key, default=None):
        return next((v for k, v in self.headers.items() if k.lower() == key.lower()), default)

    def getheaders(self):
        return list(self.headers.items())

    def read(self, size=-1):
        return self.body.read(size)

    def read1(self, size=-1):
        if self.chunks is not None:
            return next(self.chunks, b"")
        return self.body.read(size)


class Connection:
    def __init__(self, factory):
        self.factory = factory
        self.sock = None

    def request(self, method, path, body=None, headers=None):
        self.factory.requests.append((method, path, body, headers))

    def getresponse(self):
        self.factory.response.body.seek(0)
        return self.factory.response

    def close(self):
        pass


class Factory:
    def __init__(self, response=None):
        self.response = response or Response()
        self.requests = []
        self.origins = []

    def __call__(self, *args, **kwargs):
        self.origins.append((args, kwargs))
        return Connection(self)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.proxy = policy.PolicyProxy()
        self.factory = Factory()
        self.proxy._connection_factory = self.factory
        self.base = urlsplit(self.proxy.start())
        self.addCleanup(self.proxy.close)

    def request(self, document=None, body=None, headers=None, route="/responses", method="POST"):
        if body is None:
            if document is None:
                document = {"model": policy.MODEL, "reasoning": {"effort": "max"},
                            "input": "PRIVATE PROMPT"}
            body = json.dumps(document).encode()
        supplied = {"Content-Type": "application/json", "Authorization": "Bearer SECRET TOKEN"}
        supplied.update(headers or {})
        connection = http.client.HTTPConnection(self.base.hostname, self.base.port, timeout=3)
        try:
            connection.request(method, self.base.path + route, body=body, headers=supplied)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_exact_contributor_route_without_header_is_forwarded(self):
        self.assertEqual(200, self.request()[0])
        self.assertEqual(1, len(self.factory.requests))
        method, path, body, headers = self.factory.requests[0]
        self.assertEqual(("POST", "/v1/responses"), (method, path))
        self.assertEqual("Bearer SECRET TOKEN", headers["Authorization"])
        self.assertEqual(policy.MODEL, json.loads(body)["model"])
        self.assertEqual(("api.meta.ai", 443), self.factory.origins[0][0])
        self.assertEqual(1, self.proxy.snapshot()["forwarded"])

    def test_codex_gateway_uses_native_auth_and_never_caller_auth(self):
        self.proxy._gateway = {'key': 'private-codex-key', 'port': self.base.port}
        self.request()
        native_base = self.base
        self.base = urlsplit(self.proxy.gateway_url())
        self.assertEqual(200, self.request(headers={'Authorization': 'Bearer UNTRUSTED'})[0])
        self.assertEqual('Bearer SECRET TOKEN', self.factory.requests[-1][3]['authorization'])
        self.assertNotIn('SECRET TOKEN', json.dumps(self.proxy.snapshot()))
        self.assertNotIn('private-codex-key', json.dumps(self.proxy.snapshot()))
        self.base = native_base

    def test_codex_gateway_cold_auth_fails_closed(self):
        self.proxy._gateway = {'key': 'private-codex-key', 'port': self.base.port}
        self.base = urlsplit(self.proxy.gateway_url())
        self.assertEqual(503, self.request()[0])
        self.assertEqual([], self.factory.requests)

    def test_codex_gateway_blocks_low_effort_before_authentication(self):
        self.proxy._gateway = {'key': 'private-codex-key', 'port': self.base.port}
        self.base = urlsplit(self.proxy.gateway_url())
        calls = []
        self.proxy.refresh_credentials = lambda: calls.append(True)
        self.assertEqual(422, self.request({'model': policy.MODEL, 'reasoning': {'effort': 'low'}})[0])
        self.assertEqual([], calls)
        self.assertEqual([], self.factory.requests)

    def test_codex_gateway_native_refresh_and_expiry(self):
        self.proxy._gateway = {'key': 'private-codex-key', 'port': self.base.port}
        self.base = urlsplit(self.proxy.gateway_url())
        def refresh():
            self.proxy._native_headers = {'authorization': 'Bearer NATIVE'}
            self.proxy._auth_ready.set()
        self.proxy.refresh_credentials = refresh
        self.assertEqual(200, self.request()[0])
        self.assertEqual('Bearer NATIVE', self.factory.requests[-1][3]['authorization'])
        self.factory.response = Response(status=401)
        self.assertEqual(401, self.request()[0])
        self.assertFalse(self.proxy.credentials_ready())
        self.assertIsNone(self.proxy._native_headers)

    def test_model_missing_normalized_or_alternate_never_reaches_upstream(self):
        for model in (None, "muse-spark-1.3", "muse-spark-1.2-contributor", "other"):
            document = {"reasoning": {"effort": "max"}}
            if model is not None:
                document["model"] = model
            self.assertEqual(422, self.request(document, headers={"x-tbh-contributor-mode": "true"})[0])
        self.assertEqual([], self.factory.requests)

    def test_effort_missing_lower_invalid_and_conflicting_are_blocked(self):
        cases = [
            {}, {"reasoning": {"effort": "low"}}, {"reasoning_effort": None},
            {"reasoning": "max"}, {"reasoning_effort": "max", "reasoning": {"effort": "minimal"}},
            {"meta.reasoning.effort": "max", "reasoning_effort": "MAX"},
        ]
        for fields in cases:
            self.assertEqual(422, self.request({"model": policy.MODEL, **fields})[0])
        self.assertEqual([], self.factory.requests)

    def test_each_supported_effort_spelling(self):
        for fields in ({"reasoning_effort": "max"}, {"reasoning": {"effort": "max"}},
                       {"reasoning.effort": "max"}, {"meta.reasoning.effort": "max"},
                       {"meta": {"reasoning": {"effort": "max"}}}):
            self.assertEqual(200, self.request({"model": policy.MODEL, **fields})[0])

    def test_negative_or_unknown_contributor_header_is_blocked(self):
        for value in ("false", "off", "0", "yes", ""):
            self.assertEqual(422, self.request(headers={"x-tbh-contributor-mode": value})[0])
        self.assertEqual([], self.factory.requests)

    def test_truthy_contributor_headers_are_allowed(self):
        for value in ("true", "1", "on"):
            self.assertEqual(200, self.request(headers={"x-tbh-contributor-mode": value})[0])

    def test_model_override_is_blocked(self):
        for fields in ({"model_override": "other"}, {"meta": {"model": "other"}},
                       {"providerRequestOptions": {"meta.model": "other"}}):
            document = {"model": policy.MODEL, "reasoning_effort": "max", **fields}
            self.assertEqual(422, self.request(document)[0])
        self.assertEqual([], self.factory.requests)

    def test_duplicate_json_keys_and_invalid_numbers_are_blocked(self):
        bodies = [
            b'{"model":"other","model":"muse-spark-1.3-contributor","reasoning_effort":"max"}',
            b'{"model":"muse-spark-1.3-contributor","reasoning":{"effort":"low","effort":"max"}}',
            b'{"model":"muse-spark-1.3-contributor","reasoning_effort":"max","temperature":NaN}',
        ]
        for body in bodies:
            self.assertEqual(422, self.request(body=body)[0])
        self.assertEqual([], self.factory.requests)

    def test_unknown_routes_methods_and_query_strings_are_blocked(self):
        for route in ("/chat/completions", "/responses?model=other", "/v1/responses", "/models"):
            self.assertEqual(404, self.request(route=route)[0])
        self.assertEqual(405, self.request(method="GET")[0])
        self.assertEqual([], self.factory.requests)

    def test_wrong_secret_never_reaches_upstream(self):
        connection = http.client.HTTPConnection(self.base.hostname, self.base.port, timeout=3)
        try:
            connection.request("POST", "/wrong/responses", body=b"{}")
            self.assertEqual(404, connection.getresponse().status)
        finally:
            connection.close()
        self.assertEqual([], self.factory.requests)

    def test_gzip_is_validated_then_forwarded_without_encoding(self):
        body = gzip.compress(json.dumps({"model": policy.MODEL, "reasoning_effort": "max"}).encode())
        self.assertEqual(200, self.request(body=body, headers={"Content-Encoding": "gzip"})[0])
        headers = self.factory.requests[0][3]
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual("max", json.loads(self.factory.requests[0][2])["reasoning_effort"])

    def test_unknown_encoding_and_non_json_are_blocked(self):
        self.assertEqual(422, self.request(headers={"Content-Encoding": "br"})[0])
        self.assertEqual(422, self.request(headers={"Content-Type": "text/plain"})[0])
        self.assertEqual([], self.factory.requests)

    def test_corrupt_gzip_and_unknown_charset_are_blocked(self):
        self.assertEqual(422, self.request(body=b"not gzip", headers={"Content-Encoding": "gzip"})[0])
        self.assertEqual(422, self.request(headers={"Content-Type": "application/json; charset=latin-1"})[0])
        self.assertEqual([], self.factory.requests)

    def test_chunked_requests_are_blocked_before_body(self):
        connection = http.client.HTTPConnection(self.base.hostname, self.base.port, timeout=3)
        try:
            connection.putrequest("POST", self.base.path + "/responses")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Transfer-Encoding", "chunked")
            connection.endheaders(b"0\r\n\r\n")
            self.assertEqual(422, connection.getresponse().status)
        finally:
            connection.close()
        self.assertEqual([], self.factory.requests)

    def test_duplicate_policy_headers_are_blocked(self):
        connection = http.client.HTTPConnection(self.base.hostname, self.base.port, timeout=3)
        try:
            connection.putrequest("POST", self.base.path + "/responses")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "2")
            connection.putheader("x-tbh-contributor-mode", "true")
            connection.putheader("x-tbh-contributor-mode", "false")
            connection.endheaders(b"{}")
            self.assertEqual(422, connection.getresponse().status)
        finally:
            connection.close()
        self.assertEqual([], self.factory.requests)

    def test_redirects_are_not_followed(self):
        self.factory.response = Response(status=307, headers={"Location": "https://other.invalid/"})
        status, body = self.request()
        self.assertEqual(502, status)
        self.assertIn(b"upstream_redirect_not_allowed", body)
        self.assertEqual(1, len(self.factory.origins))

    def test_response_model_mismatch_is_blocked_before_body(self):
        self.factory.response = Response(body=b'{"model":"other","output":"PRIVATE OUTPUT"}')
        status, body = self.request()
        self.assertEqual(502, status)
        self.assertNotIn(b"PRIVATE OUTPUT", body)
        self.assertEqual(1, self.proxy.snapshot()["response_blocked"])

    def test_unknown_response_encoding_or_content_type_is_blocked(self):
        self.factory.response = Response(headers={"Content-Encoding": "br"})
        self.assertEqual(502, self.request()[0])
        self.factory.response = Response(content_type="text/html")
        self.assertEqual(502, self.request()[0])

    def test_sse_chunks_are_inspected_and_forwarded(self):
        event = ('event: response.created\r\ndata: {"response":{"model":"' + policy.MODEL
                 + '"}}\r\n\r\n').encode()
        self.factory.response = Response(content_type="text/event-stream", chunks=[event[:10], event[10:], b"data: [DONE]\n\n"])
        status, body = self.request()
        self.assertEqual(200, status)
        self.assertEqual(event + b"data: [DONE]\n\n", body)

    def test_sse_unicode_line_separators_inside_json_are_preserved(self):
        event = ('data: ' + json.dumps({'type': 'response.output_text.delta',
                                      'delta': 'before\u2028middle\u2029after'}, ensure_ascii=False)
                 + '\n\n').encode()
        self.factory.response = Response(content_type='text/event-stream', chunks=[event[:25], event[25:]])
        status, body = self.request()
        self.assertEqual(200, status)
        self.assertEqual(event, body)
        self.assertEqual(0, self.proxy.snapshot()['response_blocked'])

    def test_sse_bare_cr_delimiters_preserve_separate_events(self):
        first = b'data: {"type":"response.output_text.delta","delta":"OK"}\r\r'
        second = b'data: {"type":"response.completed"}\r\r'
        self.factory.response = Response(content_type='text/event-stream', chunks=[first, second])
        status, body = self.request()
        self.assertEqual(200, status)
        self.assertEqual(first + second, body)
        self.assertEqual(0, self.proxy.snapshot()['response_blocked'])
        self.assertIsNone(policy._SSE_SEPARATOR.search(b'data: {}\r\n'))

    def test_sse_initial_utf8_bom_is_validated_and_preserved(self):
        event = ('\ufeffdata: ' + json.dumps({'response': {'model': policy.MODEL}}) + '\n\n').encode()
        self.factory.response = Response(content_type='text/event-stream', chunks=[event[:1], event[1:3], event[3:]])
        status, body = self.request()
        self.assertEqual(200, status)
        self.assertEqual(event, body)
        self.assertEqual(0, self.proxy.snapshot()['response_blocked'])

    def test_sse_initial_utf8_bom_cannot_hide_an_alternate_model(self):
        event = '\ufeffdata: {"response":{"model":"other"}}\n\n'.encode()
        self.factory.response = Response(content_type='text/event-stream', chunks=[event[:1], event[1:]])
        status, body = self.request()
        self.assertEqual(200, status)
        self.assertEqual(b'', body)
        self.assertEqual(1, self.proxy.snapshot()['response_blocked'])

    def test_fixed_gateway_port_can_restart_after_streamed_connection(self):
        gateway = {'port': self.base.port, 'key': 'private-codex-key'}
        self.factory.response = Response(content_type='text/event-stream', chunks=[b'data: [DONE]\n\n'])
        self.assertEqual(200, self.request()[0])
        self.proxy.close()
        self.proxy = policy.PolicyProxy(gateway=gateway)
        self.proxy._connection_factory = self.factory
        self.base = urlsplit(self.proxy.start())
        self.addCleanup(self.proxy.close)
        self.assertEqual(gateway['port'], self.base.port)
        self.assertEqual(200, self.request()[0])

    def test_sse_model_mismatch_stops_before_that_event(self):
        good = b'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
        bad = b'data: {"response":{"model":"other","output":"PRIVATE OUTPUT"}}\n\n'
        self.factory.response = Response(content_type="text/event-stream", chunks=[good, bad])
        status, body = self.request()
        self.assertEqual(200, status)
        self.assertEqual(good, body)
        self.assertEqual(1, self.proxy.snapshot()["response_blocked"])

    def test_sse_first_event_is_flushed_before_upstream_finishes(self):
        first = b'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
        release = threading.Event()
        count = [0]

        class SlowResponse(Response):
            def read1(self, _size=-1):
                count[0] += 1
                if count[0] == 1:
                    return first
                release.wait(timeout=5)
                return b""

        self.factory.response = SlowResponse(content_type="text/event-stream")
        connection = http.client.HTTPConnection(self.base.hostname, self.base.port, timeout=2)
        try:
            body = json.dumps({"model": policy.MODEL, "reasoning_effort": "max"})
            connection.request("POST", self.base.path + "/responses", body=body,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(first, response.read(len(first)))
            self.assertFalse(release.is_set())
        finally:
            release.set()
            connection.close()

    def test_audit_does_not_contain_prompt_token_response_or_url_key(self):
        self.request()
        self.request({"model": "other", "reasoning_effort": "max", "input": "PRIVATE PROMPT"})
        audit = json.dumps(self.proxy.snapshot())
        for forbidden in ("PRIVATE PROMPT", "SECRET TOKEN", self.base.path):
            self.assertNotIn(forbidden, audit)

    def test_upstream_cannot_be_reconfigured(self):
        for url in ("http://api.meta.ai/v1", "https://other.invalid/v1", "https://api.meta.ai/v1?x=1",
                    "https://secret@api.meta.ai/v1", "https://api.meta.ai:444/v1"):
            with self.assertRaises(ValueError):
                policy.PolicyProxy(url)

    def test_start_is_idempotent_and_closed_proxy_does_not_restart(self):
        self.assertEqual(self.base.geturl(), self.proxy.start())
        self.proxy.close()
        self.assertFalse(self.proxy.snapshot()["running"])
        with self.assertRaises(RuntimeError):
            self.proxy.start()


if __name__ == "__main__":
    unittest.main()
