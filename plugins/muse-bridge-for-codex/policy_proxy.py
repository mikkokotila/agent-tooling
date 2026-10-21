"""Local, fail-closed inference policy for the Muse control host.

Only the verified contributor route and max effort may reach Meta.  The
listener is an in-process transport, not an authenticated public service.
It never records request bodies, response bodies, credentials, or its URL key.
"""

from __future__ import annotations

import gzip
import http.client
import io
import json
import re
import secrets
import threading
import time
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


MODEL = "muse-spark-1.3-contributor"
EFFORT = "max"
UPSTREAM = "https://api.meta.ai/v1"
_MAX_BODY = 64 * 1024 * 1024
_MAX_EVENT = 4 * 1024 * 1024
_UPSTREAM_TIMEOUT = 600
_SSE_SEPARATOR = re.compile(rb"\r?\n\r?\n")
_TRUTHY = frozenset(("true", "1", "on"))
_INFERENCE_PATHS = frozenset(("/responses",))
_HOP_HEADERS = frozenset((
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
))
_FORWARD_HEADERS = frozenset((
    "authorization", "accept", "user-agent", "x-api-version",
    "x-tbh-contributor-mode", "x-meta-ai-gateway-session-id", "x-client-id",
    "traceparent", "x-fb-routing-control",
))
_MODEL_OVERRIDES = frozenset((
    "model_id", "modelId", "model_override", "modelOverride", "meta.model",
    "provider_request_options", "providerRequestOptions", "base_url", "baseUrl",
))


class _PolicyError(ValueError):
    """A safe, fixed policy reason, never provider- or caller-supplied text."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _PolicyError("duplicate_json_key")
        result[key] = value
    return result


def _parse_json(body):
    def invalid_constant(_value):
        raise _PolicyError("invalid_json_constant")

    try:
        return json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=invalid_constant,
        )
    except _PolicyError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise _PolicyError("invalid_json") from None


def _efforts(document):
    values = []
    for key in ("reasoning_effort", "reasoning.effort", "meta.reasoning.effort"):
        if key in document:
            values.append(document[key])
    if "reasoning" in document:
        reasoning = document["reasoning"]
        if not isinstance(reasoning, dict):
            raise _PolicyError("invalid_reasoning")
        if "effort" in reasoning:
            values.append(reasoning["effort"])
    if "meta" in document:
        meta = document["meta"]
        if not isinstance(meta, dict):
            raise _PolicyError("invalid_meta")
        if "model" in meta or _MODEL_OVERRIDES.intersection(meta):
            raise _PolicyError("model_override")
        if "reasoning.effort" in meta:
            values.append(meta["reasoning.effort"])
        if "reasoning" in meta:
            reasoning = meta["reasoning"]
            if not isinstance(reasoning, dict):
                raise _PolicyError("invalid_reasoning")
            if "effort" in reasoning:
                values.append(reasoning["effort"])
    return values


def _validate_request(document, contributor_header):
    if not isinstance(document, dict):
        raise _PolicyError("json_object_required")
    if document.get("model") != MODEL:
        raise _PolicyError("model_not_allowed")
    if _MODEL_OVERRIDES.intersection(document):
        raise _PolicyError("model_override")
    # Installed Muse 1.4.11 uses the explicit contributor model without a
    # contributor header.  A supplied negative/unknown header is contradictory.
    if contributor_header is not None and contributor_header.strip().lower() not in _TRUTHY:
        raise _PolicyError("contributor_mode_not_allowed")
    efforts = _efforts(document)
    if not efforts:
        raise _PolicyError("reasoning_effort_missing")
    if any(not isinstance(value, str) or value != EFFORT for value in efforts):
        raise _PolicyError("reasoning_effort_not_allowed")


def _validate_response(document):
    """Check provider model declarations, never fields inside generated content."""
    if not isinstance(document, dict):
        return
    candidates = [document]
    if isinstance(document.get("response"), dict):
        candidates.append(document["response"])
    for candidate in candidates:
        if "model" in candidate and candidate["model"] != MODEL:
            raise _PolicyError("response_model_not_allowed")


def _validate_sse_event(event):
    try:
        lines = event.decode("utf-8").splitlines()
    except UnicodeError:
        raise _PolicyError("invalid_response_encoding") from None
    data = []
    for line in lines:
        if line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        payload = "\n".join(data)
        if payload.strip() and payload.strip() != "[DONE]":
            _validate_response(_parse_json(payload.encode("utf-8")))


class _LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False


class PolicyProxy:
    """Pin all accepted inference requests to one contributor model and max.

    ``start`` returns a private base URL suitable for Muse ``--base-url``.
    Policy constants and the sanctioned HTTPS origin are not configurable.
    Tests can replace the private ``_connection_factory`` before starting.
    """

    def __init__(self, upstream=UPSTREAM):
        parsed = urlsplit(upstream)
        if (parsed.scheme != "https" or parsed.hostname != "api.meta.ai"
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.path.rstrip("/") != "/v1"
                or parsed.query or parsed.fragment):
            raise ValueError("Only the sanctioned Meta HTTPS /v1 upstream is allowed")
        self._connection_factory = http.client.HTTPSConnection
        self._lock = threading.Lock()
        self._counts = Counter()
        self._audit = deque(maxlen=32)
        self._connections = set()
        self._server = None
        self._thread = None
        self._base_url = None
        self._prefix = "/" + secrets.token_urlsafe(32)
        self._stopping = threading.Event()

    def start(self):
        with self._lock:
            if self._server is not None:
                return self._base_url
            if self._stopping.is_set():
                raise RuntimeError("A closed policy proxy cannot be restarted")
            owner = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def setup(self):
                    super().setup()
                    self.connection.settimeout(30)

                def log_message(self, _format, *_args):
                    pass

                def do_POST(self):
                    owner._handle(self)

                def do_GET(self):
                    owner._reject(self, "method_not_allowed", 405)

                do_PUT = do_GET
                do_PATCH = do_GET
                do_DELETE = do_GET
                do_OPTIONS = do_GET
                do_HEAD = do_GET
                do_CONNECT = do_GET
                do_TRACE = do_GET

            self._server = _LocalServer(("127.0.0.1", 0), Handler)
            port = self._server.server_address[1]
            self._base_url = f"http://127.0.0.1:{port}{self._prefix}"
            self._thread = threading.Thread(
                target=self._server.serve_forever, kwargs={"poll_interval": 0.1},
                name="muse-policy-proxy", daemon=True,
            )
            self._thread.start()
            return self._base_url

    def close(self):
        self._stopping.set()
        with self._lock:
            server, thread = self._server, self._thread
            connections = list(self._connections)
            self._server = None
            self._thread = None
        for connection in connections:
            try:
                if getattr(connection, "sock", None) is not None:
                    connection.sock.shutdown(2)
                connection.close()
            except OSError:
                pass
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def snapshot(self):
        with self._lock:
            counts = dict(self._counts)
            return {
                "running": self._server is not None and not self._stopping.is_set(),
                "upstream": UPSTREAM, "model": MODEL, "reasoning_effort": EFFORT,
                "contributor_mode": True,
                "requests": counts.get("requests", 0),
                "forwarded": counts.get("forwarded", 0),
                "blocked": counts.get("blocked", 0),
                "response_blocked": counts.get("response_blocked", 0),
                "upstream_errors": counts.get("upstream_errors", 0),
                "completed": counts.get("completed", 0),
                "inflight": len(self._connections),
                "audit": [dict(entry) for entry in self._audit],
            }

    def _record(self, counter, reason, status=None):
        with self._lock:
            self._counts[counter] += 1
            entry = {"time": int(time.time()), "result": counter, "reason": reason}
            if status is not None:
                entry["status"] = int(status)
            self._audit.append(entry)

    def _reject(self, handler, reason, status=422, counter="blocked"):
        self._record(counter, reason, status)
        payload = json.dumps({"error": {"type": "muse_policy", "code": reason}}).encode()
        handler.close_connection = True
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            if handler.command != "HEAD":
                handler.wfile.write(payload)
                handler.wfile.flush()
        except (OSError, ValueError):
            pass

    def _body(self, handler):
        for name in ("content-length", "content-type", "content-encoding", "authorization",
                     "x-tbh-contributor-mode", "host"):
            if len(handler.headers.get_all(name, [])) > 1:
                raise _PolicyError("duplicate_header")
        if handler.headers.get("Transfer-Encoding"):
            raise _PolicyError("transfer_encoding_not_allowed")
        content_type = handler.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise _PolicyError("content_type_not_allowed")
        if ";" in content_type:
            parameters = content_type.split(";")[1:]
            if any(p.strip().lower().replace('"', '') not in ("charset=utf-8", "charset=utf8")
                   for p in parameters):
                raise _PolicyError("content_type_parameters_not_allowed")
        length = handler.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]+", length) or not 0 < int(length) <= _MAX_BODY:
            raise _PolicyError("content_length_not_allowed")
        body = handler.rfile.read(int(length))
        if len(body) != int(length):
            raise _PolicyError("incomplete_body")
        encoding = handler.headers.get("Content-Encoding", "identity").strip().lower()
        if encoding == "gzip":
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as zipped:
                    body = zipped.read(_MAX_BODY + 1)
            except (OSError, EOFError, ValueError):
                raise _PolicyError("invalid_gzip") from None
            if len(body) > _MAX_BODY:
                raise _PolicyError("body_too_large")
        elif encoding != "identity":
            raise _PolicyError("content_encoding_not_allowed")
        return body

    def _handle(self, handler):
        self._record("requests", "received")
        expected_prefix = self._prefix + "/"
        if not handler.path.startswith(expected_prefix):
            self._reject(handler, "unknown_endpoint", 404)
            return
        route = handler.path[len(self._prefix):]
        if route not in _INFERENCE_PATHS:
            self._reject(handler, "endpoint_not_allowed", 404)
            return
        if self._stopping.is_set():
            self._reject(handler, "proxy_closed", 503)
            return
        try:
            body = self._body(handler)
            document = _parse_json(body)
            _validate_request(document, handler.headers.get("x-tbh-contributor-mode"))
        except _PolicyError as error:
            self._reject(handler, str(error))
            return
        except (OSError, ValueError, OverflowError):
            self._reject(handler, "request_read_failed", 400)
            return
        headers = {
            name: value for name, value in handler.headers.items()
            if name.lower() in _FORWARD_HEADERS
        }
        headers.update({"Content-Type": "application/json", "Content-Length": str(len(body)),
                        "Accept-Encoding": "identity", "Connection": "close"})
        connection = None
        response_started = False
        try:
            connection = self._connection_factory("api.meta.ai", 443, timeout=_UPSTREAM_TIMEOUT)
            with self._lock:
                if self._stopping.is_set():
                    raise _PolicyError("proxy_closed")
                self._connections.add(connection)
            self._record("forwarded", "policy_passed")
            connection.request("POST", "/v1" + route, body=body, headers=headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise _PolicyError("upstream_redirect_not_allowed")
            encoding = response.getheader("Content-Encoding", "identity").lower().strip()
            if encoding != "identity":
                raise _PolicyError("upstream_encoding_not_allowed")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type == "text/event-stream":
                self._response_headers(handler, response, streaming=True)
                response_started = True
                self._stream(handler, response)
            elif content_type == "application/json":
                response_body = response.read(_MAX_BODY + 1)
                if len(response_body) > _MAX_BODY:
                    raise _PolicyError("response_too_large")
                _validate_response(_parse_json(response_body))
                self._response_headers(handler, response, length=len(response_body))
                response_started = True
                handler.wfile.write(response_body)
                handler.wfile.flush()
            else:
                raise _PolicyError("upstream_content_type_not_allowed")
            self._record("completed", "response_completed", response.status)
        except _PolicyError as error:
            if response_started:
                self._record("response_blocked", str(error), 502)
                handler.close_connection = True
            else:
                self._reject(handler, str(error), 502, "response_blocked")
        except (OSError, ValueError, http.client.HTTPException):
            if response_started:
                self._record("upstream_errors", "stream_disconnected", 502)
                handler.close_connection = True
            else:
                self._reject(handler, "upstream_unavailable", 502, "upstream_errors")
        finally:
            if connection is not None:
                with self._lock:
                    self._connections.discard(connection)
                connection.close()

    @staticmethod
    def _response_headers(handler, response, length=None, streaming=False):
        handler.send_response(response.status)
        for name, value in response.getheaders():
            if name.lower() not in _HOP_HEADERS and name.lower() not in (
                    "content-encoding", "set-cookie", "www-authenticate"):
                handler.send_header(name, value)
        if length is not None:
            handler.send_header("Content-Length", str(length))
        if streaming:
            handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True

    def _stream(self, handler, response):
        pending = bytearray()
        while not self._stopping.is_set():
            chunk = response.read1(64 * 1024)
            if not chunk:
                if pending:
                    _validate_sse_event(bytes(pending))
                    handler.wfile.write(pending)
                    handler.wfile.flush()
                return
            pending.extend(chunk)
            while True:
                match = _SSE_SEPARATOR.search(pending)
                if match is None:
                    if len(pending) > _MAX_EVENT:
                        raise _PolicyError("response_event_too_large")
                    break
                end = match.end()
                if end > _MAX_EVENT:
                    raise _PolicyError("response_event_too_large")
                event = bytes(pending[:end])
                del pending[:end]
                _validate_sse_event(event)
                handler.wfile.write(event)
                handler.wfile.flush()
        raise _PolicyError("proxy_closed")
