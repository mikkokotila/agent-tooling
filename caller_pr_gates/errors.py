"""Full, redacted diagnostics and stable causes, independent of display limits."""
from __future__ import annotations
import ast
import json
import re

HEADERS = ('x-ratelimit-resource', 'x-ratelimit-limit', 'x-ratelimit-remaining',
           'x-ratelimit-used', 'x-ratelimit-reset', 'retry-after')

def redact(value):
    text = str(value)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*(?:bearer|token)?\s*)[^\s,}\"]+', r'\1[REDACTED]', text)
    text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b', '[REDACTED]', text)
    return re.sub(r'(?i)([?&](?:access_token|token)=)[^&\s]+', r'\1[REDACTED]', text)

def quota_kind(status, message, headers):
    # A transport's diagnostic label, or a bare 403/429, is not exhaustion.
    text = re.sub(r'(?:rate-limit|rate_limit_headers)\s*:\s*\{[^}]*\}', '', str(message), flags=re.I).lower()
    secondary = 'secondary rate limit' in text or 'abuse detection' in text
    primary = 'rate limit exceeded' in text or 'rate_limit_exceeded' in text
    if status not in {None, 403, 429}: return None
    if secondary: return 'secondary'
    if primary or (status in {403, 429} and headers.get('x-ratelimit-remaining') == '0'): return 'primary'
    if status in {403, 429} and headers.get('retry-after'): return 'secondary'
    return None

def diagnostic(error, path=''):
    if isinstance(error, dict):
        value = dict(error)
        headers = {str(k).lower(): str(v) for k,v in value.get('rate_limit_headers', {}).items() if str(k).lower() in HEADERS}
        body = redact(value.get('response_body', value.get('message', '')))
        message = redact(value.get('message', body)); status = value.get('http_status')
        path = value.get('path') or path
    else:
        text = redact(error); status = None; body = text; headers = {}
        current = error; visited = set()
        while isinstance(current, BaseException) and id(current) not in visited:
            visited.add(id(current)); response = getattr(current, 'response', None)
            if response is not None:
                status = response.status_code; body = redact(response.text)
                headers = {h: str(response.headers[h]) for h in HEADERS if h in response.headers}
                break
            current = current.__cause__ or current.__context__
        if status is None:
            match = re.search(r'\b(?:GitHub|HTTP)\s+(\d{3})\b', text, re.I)
            status = int(match[1]) if match else None
            if 'body=' in text:
                prefix, body = text.split('body=', 1)
                match = re.search(r'(?:rate-limit|rate_limit_headers)\s*:\s*(\{[^}]*\})', prefix, re.I)
                if match:
                    try: headers = {str(k).lower(): str(v) for k,v in ast.literal_eval(match[1]).items() if str(k).lower() in HEADERS}
                    except (ValueError, SyntaxError, AttributeError): pass
        message = body
        try:
            payload = json.loads(body)
            if isinstance(payload, dict): message = str(payload.get('message', body))
        except (ValueError, TypeError): pass
    quota = quota_kind(status, message, headers)
    lower = message.lower()
    if quota: kind = 'rate_limited'
    elif status == 403 and '/rules/branches/' in path and all(s in lower for s in ('upgrade to github', 'make this repository public', 'enable this feature')): kind = 'feature_unavailable'
    elif status in {401,403}: kind = 'permission_denied'
    elif status == 404: kind = 'not_found_or_permission'
    elif status and status >= 500: kind = 'transient'
    elif any(s in type(error).__name__.lower() for s in ('timeout','connect','transport')): kind = 'transient'
    elif type(error).__name__ == 'EvidenceError': kind = 'invalid_evidence'
    else: kind = 'api_error'
    return {'kind':kind,'http_status':status,'message':message,'path':redact(path),
            'response_body':body,'rate_limit_headers':headers,'quota_kind':quota}

def error_identity(value, component=''):
    data = diagnostic(value)
    if isinstance(value, dict) and value.get('kind') == 'rate_limited':
        # Existing structured evidence is already classified at collection.
        data['kind'] = 'rate_limited'
    identity = {'component':component, 'kind':data['kind'], 'path':data['path'], 'http_status':data['http_status']}
    if data['kind'] != 'rate_limited':
        identity['kind'] = value.get('kind',data['kind']) if isinstance(value,dict) else data['kind']
        identity['message'] = ' '.join(re.sub(r'(?:rate-limit|rate_limit_headers)\s*:\s*\{[^}]*\}', '', data['message'], flags=re.I).split())
    return identity
