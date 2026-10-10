"""Reload editable prose safely; template errors cannot change execution gates."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import tomllib

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = json.loads((Path(__file__).parent / 'prompt-defaults.json').read_text())
TOKENS = re.compile(r'\$\{([^}]+)\}')
MAX_BYTES = 256 * 1024
_memory = {}


def catalogue_path():
    return Path(os.getenv('CALLER_PR_GATES_PROMPTS_FILE') or ROOT / 'prompts.toml')


def state_directory():
    return Path(os.getenv('CALLER_PR_GATES_PROMPTS_STATE_DIR') or
                os.getenv('CALLER_PR_GATES_STATE_DIR') or
                Path.home() / '.local/state/caller-pr-gates')


def baseline_text(key):
    return DEFAULTS['prompts'][key]['text']


def _validate(document):
    if not isinstance(document, dict) or set(document) != {'schema_version', 'prompts'}:
        raise ValueError('Expected schema_version and prompts only')
    if type(document['schema_version']) is not int or document['schema_version'] != 1:
        raise ValueError('Unsupported prompt schema version')
    rows = document['prompts']
    if not isinstance(rows, dict) or set(rows) != set(DEFAULTS['prompts']):
        raise ValueError('All original prompt section keys must be retained; no unknown sections')
    result = {}
    for key, expected in DEFAULTS['prompts'].items():
        row = rows[key]
        if not isinstance(row, dict) or set(row) != {'text'}:
            raise ValueError(key + ': expected text only')
        text = row['text']
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_BYTES:
            raise ValueError(key + ': text must be a nonempty bounded string')
        if set(TOKENS.findall(text)) != set(expected['parameters']):
            raise ValueError(key + ': retain the exact original ${placeholders}')
        prefix = expected['protected_prefix']
        if prefix and not text.startswith(prefix):
            raise ValueError(key + ': retain the protected routing prefix')
        if (key.startswith('global_') or key == 'communication') and re.search(
                r'<!-- (?:caller-pr-gates|troubleshooting-report|pr-progress):(?:begin|end) -->', text):
            raise ValueError(key + ': global instruction boundary markers are generated separately')
        result[key] = text
    return result


def _document(texts):
    return {'schema_version': 1, 'prompts': {key: {'text': text} for key, text in texts.items()}}


def _atomic(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a+') as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try: yield
        finally: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _stamp(path):
    try:
        value = path.stat()
        return (value.st_ino, value.st_mtime_ns, value.st_size)
    except OSError:
        return None


def _rescue(cache):
    try:
        if cache.stat().st_size > MAX_BYTES * 2: raise ValueError('Rescue data is oversized')
        value = json.loads(cache.read_text())
        return _validate(value['catalogue']), value['revision'], 'last_good'
    except (OSError, ValueError, KeyError, TypeError):
        texts = {key: value['text'] for key, value in DEFAULTS['prompts'].items()}
        digest = hashlib.sha256(json.dumps(_document(texts), sort_keys=True).encode()).hexdigest()
        return texts, digest, 'packaged_defaults'


def _save_health(directory, info):
    try:
        path = directory / 'prompt-catalogue-health.json'
        encoded = (json.dumps(info, sort_keys=True, indent=2) + '\n').encode()
        if not path.exists() or path.read_bytes() != encoded: _atomic(path, encoded)
    except OSError:
        pass  # A local reporting failure cannot block the hook or wake a chat.


def load_catalogue():
    path = catalogue_path()
    directory = state_directory()
    cache = directory / 'prompt-last-good.json'
    stamp = (_stamp(path), _stamp(cache))
    memory_key = (str(path), str(directory))
    cached = _memory.get(memory_key)
    if cached and cached[0] == stamp:
        return dict(cached[1]), dict(cached[2])
    texts, digest, source = _rescue(cache)
    error = None
    valid = False
    try:
        with _lock(directory / 'prompt-catalogue.lock'):
            # Reread under the shared lock so an older callback cannot overwrite
            # the rescue copy after another process accepted a newer catalogue.
            texts, digest, source = _rescue(cache)
            before = _stamp(path)
            if before is None: raise FileNotFoundError(str(path))
            if before[2] > MAX_BYTES: raise ValueError('Prompt file is oversized')
            raw = path.read_bytes()
            if _stamp(path) != before: raise ValueError('Prompt file changed during read')
            candidate = _validate(tomllib.loads(raw.decode('utf-8')))
            revision = hashlib.sha256(raw).hexdigest()
            if revision != digest or source != 'last_good':
                rescue = {'catalogue': _document(candidate), 'revision': revision}
                _atomic(cache, (json.dumps(rescue, sort_keys=True) + '\n').encode())
            texts, digest, source, valid = candidate, revision, 'canonical_file', True
    except (OSError, ValueError, UnicodeError) as exc:
        error = type(exc).__name__ + ': ' + str(exc)[:500]
    info = {'file': str(path), 'source': source, 'valid_current_file': valid,
            'revision': digest, 'prompt_count': len(texts), 'error': error}
    if not error or not error.startswith('BlockingIOError:'):
        _memory[memory_key] = ((_stamp(path), _stamp(cache)), dict(texts), dict(info))
    _save_health(directory, info)
    return dict(texts), dict(info)


def render_prompt(key, **values):
    expected = DEFAULTS['prompts'][key]['parameters']
    if set(values) != set(expected):
        raise ValueError('Internal prompt argument mismatch: ' + key)
    texts, _ = load_catalogue()
    # A single substitution pass: event values are data and are never evaluated
    # or recursively interpreted as further template instructions.
    return TOKENS.sub(lambda match: str(values[match[1]]), texts[key])


def sync_instruction_files(targets=None):
    """Mirror only our three marked blocks, preserving every unrelated byte."""
    if targets is None:
        if ROOT != Path.home() / '.local/share/caller-pr-gates':
            return {'skipped': 'isolated/staged package', 'changed': 0}
        targets = [Path.home() / '.codex/AGENTS.md', Path.home() / '.claude/CLAUDE.md']
    targets = list(map(Path, targets))
    directory = state_directory()
    changed = []
    originals = {}
    updates = {}
    result = {'changed': 0, 'error': None}
    try:
        with _lock(directory / 'prompt-instructions.lock'):
            texts, info = load_catalogue()
            blocks = {'caller-pr-gates': texts['global_pr_obligations'],
                      'pr-progress': texts['global_progress'],
                      'troubleshooting-report': TOKENS.sub(
                          lambda match: texts[match[1]], texts['global_communication'])}
            for path in targets:
                original = path.read_bytes()
                value = original.decode('utf-8')
                for marker, text in blocks.items():
                    begin, end = '<!-- ' + marker + ':begin -->', '<!-- ' + marker + ':end -->'
                    if value.count(begin) != 1 or value.count(end) != 1:
                        raise ValueError(str(path) + ': managed instruction markers missing/duplicated')
                    pattern = re.escape(begin) + r'[\s\S]*?' + re.escape(end)
                    value, count = re.subn(pattern, lambda _match: begin + '\n' + text + end, value)
                    if count != 1: raise ValueError(str(path) + ': managed marker order invalid')
                originals[path] = original
                updates[path] = value.encode()
            # Validate both files before touching either one. Preserve originals
            # outside the registry, and compare immediately before replacement.
            for path, original in originals.items():
                if updates[path] == original: continue
                if path.read_bytes() != original: raise ValueError(str(path) + ': concurrent instruction edit')
                digest = hashlib.sha256(original).hexdigest()
                backup = directory / 'prompt-instruction-backups' / digest / path.name
                if not backup.exists(): _atomic(backup, original)
                _atomic(path, updates[path], path.stat().st_mode & 0o777)
                changed.append(path)
            result.update(changed=len(changed), revision=info['revision'], checked_at=time.time())
    except (OSError, ValueError, UnicodeError) as exc:
        rollback_errors = []
        for path in reversed(changed):
            try:
                if path.read_bytes() == updates[path]:
                    _atomic(path, originals[path], path.stat().st_mode & 0o777)
                else: rollback_errors.append(str(path) + ': concurrent edit retained')
            except OSError as rollback: rollback_errors.append(str(rollback))
        result.update(changed=0, error=type(exc).__name__ + ': ' + str(exc)[:500],
                      rollback_errors=rollback_errors, checked_at=time.time())
    try:
        _atomic(directory / 'prompt-instructions-health.json', (json.dumps(result, indent=2) + '\n').encode())
    except OSError:
        pass
    return result


def refresh():
    """Existing monitor heartbeat refreshes prose; no GitHub or queue operations."""
    load_catalogue()
    return sync_instruction_files()


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check'])
    parser.parse_args()
    _texts, info = load_catalogue()
    path = state_directory() / 'prompt-instructions-health.json'
    try: info['instruction_mirrors'] = json.loads(path.read_text())
    except (OSError, ValueError): info['instruction_mirrors'] = None
    print(json.dumps(info, indent=2))


if __name__ == '__main__': main()
