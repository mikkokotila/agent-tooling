"""Explicit, human-evidenced transfer of one existing Codex PR watch."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .publication import exact_source, source


def _identity(value):
    if not isinstance(value, dict) or set(value) != {'session_id', 'client', 'cwd'}:
        raise ValueError('Transfer needs exact session, client and checkout identities')
    uuid.UUID(value['session_id'])
    if value['client'] != 'codex':
        raise ValueError('Ownership transfer currently supports native Codex sessions only')
    path = Path(value['cwd'])
    if not path.is_absolute() or not path.is_dir() or str(path.resolve()) != value['cwd']:
        raise ValueError('Transfer checkout must be an existing canonical absolute path')
    return value


def _native(identity):
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()
    database = home / 'state_5.sqlite'
    if not database.is_file() or database.is_symlink():
        raise ValueError('Native Codex archive evidence is unavailable')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT id,cwd,archived,rollout_path FROM threads WHERE id=?',
                          (identity['session_id'],)).fetchall()
    if len(rows) != 1 or type(rows[0]['archived']) is not int or rows[0]['archived'] != 0:
        raise ValueError('Transfer refuses archived or unverified Codex sessions')
    row = dict(rows[0])
    path = Path(row['rollout_path'])
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('Native Codex transcript is unavailable')
    if not path.resolve().is_relative_to(home / 'sessions'):
        raise ValueError('Native transcript is outside active Codex sessions')
    native = source(row['cwd'])
    checkout = source(identity['cwd'], stable=True)
    if not native or not checkout or native['common'] != checkout['common']:
        raise ValueError('Native session and registered checkout must share a repository')
    return dict(row, rollout_path=str(path.resolve())), checkout


def _human_messages(native, references, approval):
    if not isinstance(references, list) or not 1 <= len(references) <= 4:
        raise ValueError('Transfer needs bounded native human-message references')
    wanted = {}
    quotes = []
    for reference in references:
        if (not isinstance(reference, dict) or set(reference) != {'line_sha256', 'quote'}
                or not isinstance(reference['line_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', reference['line_sha256'])
                or not isinstance(reference['quote'], str) or not reference['quote'].strip()
                or len(reference['quote']) > 8192 or reference['line_sha256'] in wanted):
            raise ValueError('Native human-message reference is malformed or duplicated')
        wanted[reference['line_sha256']] = reference['quote']
        quotes.append(reference['quote'])
    if approval != '\n'.join(quotes):
        raise ValueError('Scope approval must quote the exact referenced human messages')
    found = set()
    with Path(native['rollout_path']).open('rb') as stream:
        for raw in stream:
            digest = hashlib.sha256(raw.rstrip(b'\r\n')).hexdigest()
            if digest not in wanted:
                continue
            record = json.loads(raw)
            payload = record.get('payload') or {}
            content = payload.get('content')
            if (record.get('type') != 'response_item' or payload.get('type') != 'message'
                    or payload.get('role') != 'user' or not isinstance(content, list)):
                raise ValueError('Authorization reference is not a native human user message')
            texts = [item['text'] for item in content if isinstance(item, dict)
                     and item.get('type') == 'input_text' and isinstance(item.get('text'), str)]
            if len(texts) != 1 or texts[0].strip() != wanted[digest]:
                raise ValueError('Native human message does not match the approved quote')
            found.add(digest)
    if found != set(wanted):
        raise ValueError('Native human authorization could not be verified')


def _grant(store, sid, repository, number, client, cwd, work_item):
    scope = store.get_scope(sid, work_item=work_item)
    grant = scope.get('pr_ownership_transfer') if scope else None
    if (not isinstance(grant, dict) or set(grant) != {
            'repository', 'number', 'author', 'head_sha', 'from', 'to', 'human_messages'}
            or grant['repository'] != repository or grant['number'] != number
            or type(grant['number']) is not int
            or not isinstance(grant['author'], str) or not re.fullmatch(r'[A-Za-z0-9-]+', grant['author'])
            or not isinstance(grant['head_sha'], str) or not re.fullmatch(r'[0-9a-f]{40}', grant['head_sha'])):
        raise ValueError('Transfer needs an immutable grant for this exact PR, author and head')
    before, after = _identity(grant['from']), _identity(grant['to'])
    if after != {'session_id': sid, 'client': client, 'cwd': str(Path(cwd).resolve())}:
        raise ValueError('Transfer destination differs from the actual requesting root and checkout')
    if before['session_id'] == sid:
        raise ValueError('Transfer must change the canonical owner')
    for identity in (before, after):
        session = store.session(identity['session_id'])
        if not session or session['root_session'] != identity['session_id'] or session['client'] != identity['client']:
            raise ValueError('Transfer requires two already recorded exact root sessions')
    return scope, grant


def _live(pull, grant):
    repository = grant['repository']
    if (not isinstance(pull, dict) or pull.get('number') != grant['number']
            or pull.get('url') != f'https://api.github.com/repos/{repository}/pulls/{grant["number"]}'
            or pull.get('state') != 'open' or pull.get('merged') is not False or pull.get('draft') is not False
            or (pull.get('head') or {}).get('sha') != grant['head_sha']
            or ((pull.get('head') or {}).get('repo') or {}).get('full_name') != repository
            or ((pull.get('base') or {}).get('repo') or {}).get('full_name') != repository
            or (pull.get('user') or {}).get('login', '').casefold() != grant['author'].casefold()):
        raise ValueError('Live PR identity, author, lifecycle or head differs from the human grant')


def _idle(db, repository, number, watch):
    now = time.time()
    if watch['lease_until'] > now or watch['delivery_state'] in {'reserved', 'queued'}:
        raise ValueError('Transfer refuses an active or queued monitor worker')
    if watch['worker_pid'] is not None:
        try:
            os.kill(watch['worker_pid'], 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError('Transfer refuses a live monitor process')
    if db.execute('''SELECT 1 FROM validation_runs r JOIN validation_plans p ON p.plan_id=r.plan_id
                     WHERE p.repository=? AND p.number=? AND r.status='running' LIMIT 1''',
                  (repository, number)).fetchone():
        raise ValueError('Transfer refuses running validation for this PR')
    for row in db.execute('SELECT fields FROM recovery WHERE repository=? AND number=?', (repository, number)):
        if json.loads(row['fields']).get('lease_until', 0) > now:
            raise ValueError('Transfer refuses an active PR recovery lease')
    for table in ('followups', 'issue_closeouts'):
        if db.execute(f'SELECT 1 FROM {table} WHERE repository=? AND number=? AND lease_until>? LIMIT 1',
                      (repository, number, now)).fetchone():
            raise ValueError('Transfer refuses an active scoped delivery lease')


async def transfer(store, sid, repository, number, client, cwd, work_item, *, reader=None):
    """No remote mutation, chat wakeup or validation credit is created here."""
    scope, grant = _grant(store, sid, repository, number, client, cwd, work_item)
    if reader is None:
        from .transport import GitHubClient
        reader = GitHubClient().get
    path = f'/repos/{repository}/pulls/{number}'
    _live(await reader(path), grant)
    original_native, original_source = _native(grant['from'])
    target_native, target_source = _native(grant['to'])
    _human_messages(target_native, grant['human_messages'], scope.get('approval_evidence'))
    for identity, local in ((grant['from'], original_source), (grant['to'], target_source)):
        if not exact_source(dict(identity, repository=repository), local):
            raise ValueError('Transfer requires clean exact source checkouts in the granted repository')
    if target_source['head'] != grant['head_sha']:
        raise ValueError('Destination checkout does not contain the exact live PR head')
    _live(await reader(path), grant)
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current_scope = store._scope_row(db, sid, work_item)
        if not current_scope or json.loads(current_scope['scope']) != scope:
            raise ValueError('Human transfer grant changed during collection')
        watch = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=?', (repository, number)).fetchone()
        before = grant['from']
        if (not watch or watch['state'] != 'open' or any(watch[key] != before[field]
                for key, field in (('owner_session', 'session_id'), ('client', 'client'), ('cwd', 'cwd')))):
            raise ValueError('Canonical owner or checkout differs from the expected original owner')
        registered = db.execute('SELECT * FROM prs WHERE repository=? AND number=? AND session_id=?',
                                (repository, number, sid)).fetchone()
        if (not registered or registered['status'] != 'active' or registered['client'] != client
                or registered['cwd'] != grant['to']['cwd']):
            raise ValueError('Destination must already register this exact PR and checkout')
        _idle(db, repository, number, watch)
        original_scope = db.execute('SELECT scope FROM pr_scope_bindings WHERE session_id=? AND repository=? AND number=?',
                                    (before['session_id'], repository, number)).fetchone()
        if not original_scope:
            raise ValueError('Original PR scope is unavailable; transfer cannot invent it')
        previous_scope = db.execute('SELECT scope FROM pr_scope_bindings WHERE session_id=? AND repository=? AND number=?',
                                    (sid, repository, number)).fetchone()
        if (_native(before) != (original_native, original_source)
                or _native(grant['to']) != (target_native, target_source)):
            raise ValueError('Native archive or checkout identity changed before transfer')
        _human_messages(target_native, grant['human_messages'], scope.get('approval_evidence'))
        receipt = dict(repository=repository, number=number, head_sha=grant['head_sha'],
                       previous_watch=dict(watch), previous_destination_scope=json.loads(previous_scope['scope']) if previous_scope else None,
                       original_scope=json.loads(original_scope['scope']), grant=grant, work_item=work_item,
                       approval_evidence=scope['approval_evidence'], transferred_at=time.time(), completion_verified=False)
        # A new owner cannot obtain another retry of the same CI run.
        retry_ids = set()
        for row in db.execute('SELECT fields FROM recovery WHERE repository=? AND number=?', (repository, number)):
            retry = json.loads(row['fields']).get('ci_reruns') or {}
            if retry.get('head_sha') == grant['head_sha']:
                retry_ids.update(retry.get('run_ids', []))
        if retry_ids:
            old_recovery = db.execute('SELECT fields FROM recovery WHERE repository=? AND number=? AND session_id=?',
                                      (repository, number, sid)).fetchone()
            fields = json.loads(old_recovery['fields']) if old_recovery else {}
            receipt['previous_destination_recovery'] = fields
            fields = dict(fields, ci_reruns={'head_sha': grant['head_sha'], 'run_ids': sorted(retry_ids)})
            db.execute('''INSERT INTO recovery VALUES(?,?,?,?,?,?) ON CONFLICT(repository,number,session_id)
                          DO UPDATE SET fields=excluded.fields,updated_at=excluded.updated_at''',
                       (repository, number, sid, json.dumps(fields), receipt['transferred_at'], receipt['transferred_at']))
        db.execute('''UPDATE pr_watch SET owner_session=?,client=?,cwd=?,next_check_at=0,
                      fingerprint=NULL,delivered_fingerprint=NULL,event_id=NULL,payload=NULL,delivery_state=NULL,
                      error=NULL,retry_at=0,worker_pid=NULL,lease_until=0,terminal_notice=0,updated_at=?
                      WHERE repository=? AND number=?''',
                   (sid, client, grant['to']['cwd'], receipt['transferred_at'], repository, number))
        db.execute('''INSERT INTO pr_scope_bindings VALUES(?,?,?,?) ON CONFLICT(session_id,repository,number)
                      DO UPDATE SET scope=excluded.scope''', (sid, repository, number, original_scope['scope']))
        store._audit(db, 'pr_owner_transferred', receipt)
    return receipt
