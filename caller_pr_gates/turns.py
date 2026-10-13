"""Informational turns may yield without completing or changing durable obligations."""
from __future__ import annotations

import hashlib
import re
import time
import uuid


_MACHINE = re.compile(r'^\s*(?:<!-- pr-work-event:|Automated PR hook\b|Durable\b|<heartbeat\b|<hook_prompt\b|\[automated\b|'
                      r'(?:PR_NOT_DRAFT|CI_FAILED|CI_PENDING|REVIEW_HANDLED|REVIEW_CONCLUDED|'
                      r'PRD_FOLLOWUP|SLICE_SCOPE|PR_GATE|OWNED_PR_PROGRESS)\b)', re.I)
_ACTION_WORDS = r'(?:fix|repair|implement|build|create|make|add|edit|change|update|modify|remove|delete|'
_ACTION_WORDS += r'refactor|commit|push|publish|deploy|merge|close|reopen|resolve|address|run|test|'
_ACTION_WORDS += r'validate|install|configure|continue|resume|proceed)'
_ACTION = re.compile(r'(?:^|[\n.!?;]\s*)(?:please\s+)?' + _ACTION_WORDS + r'\b|'
                     r'\b(?:can|could|would|will)\s+you\s+(?:please\s+)?' + _ACTION_WORDS + r'\b|'
                     r'\b(?:then|and|also)\s+(?:please\s+)?' + _ACTION_WORDS + r'\b|'
                     r'\blet(?:\x27s| us)\s+' + _ACTION_WORDS + r'\b', re.I)
_QUESTION = re.compile(r'\b(?:what|how|why|where)\b[^?]*\?|'
                       r'^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?'
                       r'(?:explain|describe|help me understand|tell me (?:what|how|why|where))\b|'
                       r'^\s*(?:is|are|does|do|can|could|would|will)\b[^?]*\?', re.I)
_COMPLETE = re.compile(
    r'\b(?:pr(?=\s|#?\d|$)|pull request\b|work\b|task\b|job\b|obligation\b|review\b|implementation\b|fix\b)'
    r'(?:\s*#?\d+)?\s+(?:(?:is|are|was|were|has been|have been|now|already|fully)\s+)*'
    r'(?:done|complete(?:d)?|finished|merged)\b|'
    r'\b(?:all|every)\s+(?:ci|checks?|tests?|gates?|reviews?)'
    r'(?:\s+(?:checks?|jobs?|gates?))?\s+'
    r'(?:(?:has|have|is|are|now|already)\s+)*(?:pass(?:ed|es|ing)?|green|complete(?:d)?|succeeded)\b|'
    r'\b(?:I|we)\s+(?:(?:have|successfully|now|just|already)\s+)*(?:merged|completed|finished|closed|resolved)\b|'
    r'^\s*(?:done|finished|complete(?:d)?|merged)\s*[.!]?\s*$', re.I)


def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS turn_modes (
        session_id TEXT PRIMARY KEY, root_session TEXT NOT NULL, turn_id TEXT NOT NULL,
        prompt_hash TEXT NOT NULL, mode TEXT NOT NULL, mutated INTEGER NOT NULL DEFAULT 0,
        started_at REAL NOT NULL, updated_at REAL NOT NULL
    )""")


def _root(store, db, sid):
    return store._root(sid, store._parents(db))


def _informational(prompt):
    if not isinstance(prompt, str) or not prompt.strip() or _MACHINE.search(prompt):
        return False
    if re.search(r'^\s*#{1,3}\s+My request:\s*$', prompt, re.M):
        prompt = re.split(r'^\s*#{1,3}\s+My request:\s*$', prompt, maxsplit=1, flags=re.M)[1]
        prompt = prompt.split('<image ', 1)[0]
    explanation = re.search(r'(?:^|[.!?]\s*)(?:please\s+)?(?:explain|describe|help me understand|tell me)\b', prompt, re.I)
    return not _MACHINE.search(prompt) and not _ACTION.search(prompt) and bool(_QUESTION.search(prompt) or explanation)


def _completion_claim(text):
    if not isinstance(text, str) or not text.strip():
        return True  # An absent answer cannot establish an informational yield.
    # A quoted user question is source material, not an assistant completion claim.
    text = re.sub(r'(?m)^\s*>[^\n]*\?[^\n]*(?:\n|$)', '', text)
    text = re.sub(r'["“\x27‘][^"”\x27’\n]*\?[^"”\x27’\n]*["”\x27’]', '', text)
    return bool(_COMPLETE.search(re.sub(r'[*_`]', '', text)))


def start_turn(store, event, sid) -> None:
    """Record user-turn intent; any engineering request invalidates family info yield."""
    prompt = event.get('prompt')
    digest = hashlib.sha256((prompt if isinstance(prompt, str) else '').encode()).hexdigest()
    incoming = event.get('turn_id')
    turn_id = incoming if isinstance(incoming, str) and incoming else str(uuid.uuid4())
    mode = 'informational' if _informational(prompt) else 'engineering'
    now = time.time()
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _schema(db)
        root = _root(store, db, sid)
        previous = db.execute('SELECT turn_id,prompt_hash FROM turn_modes WHERE session_id=?', (sid,)).fetchone()
        if previous and previous['turn_id'] == turn_id and previous['prompt_hash'] == digest:
            return  # Replayed submission cannot reset a mutation already recorded.
        db.execute("""INSERT INTO turn_modes VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET root_session=excluded.root_session,
            turn_id=excluded.turn_id,prompt_hash=excluded.prompt_hash,mode=excluded.mode,
            mutated=0,started_at=excluded.started_at,updated_at=excluded.updated_at""",
            (sid, root, turn_id, digest, mode, now, now))
        # Unknown child instructions stay conservative for the child without
        # converting the parent's read-only diagnostic delegation into new work.
        explicit_action = isinstance(prompt, str) and bool(_ACTION.search(prompt))
        if mode == 'engineering' and (sid == root or explicit_action):
            db.execute("UPDATE turn_modes SET mode='engineering',updated_at=? WHERE root_session=?", (now, root))
        store._audit(db, 'turn_mode_recorded', {'session_id': sid, 'root_session': root,
                                               'turn_id': turn_id, 'mode': mode})


def note_mutation(store, event, sid) -> None:
    """Caller invokes only after a confirmed successful engineering mutation."""
    now = time.time()
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _schema(db)
        root = _root(store, db, sid)
        db.execute("UPDATE turn_modes SET mode='engineering',mutated=1,updated_at=? WHERE root_session=?", (now, root))
        store._audit(db, 'turn_mutation_recorded', {'session_id': sid, 'root_session': root,
                                                   'turn_id': event.get('turn_id'), 'tool_name': event.get('tool_name')})


def informational_yield(store, event, sid, final_text) -> bool:
    """Permit an answer to yield; PR/followup state remains untouched and owned."""
    if _completion_claim(final_text):
        return False
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _schema(db)
        row = db.execute('SELECT * FROM turn_modes WHERE session_id=?', (sid,)).fetchone()
        if row is None or row['mode'] != 'informational' or row['mutated']:
            return False
        incoming = event.get('turn_id')
        if incoming and incoming != row['turn_id']:
            return False
        if row['root_session'] != _root(store, db, sid):
            return False
        store._audit(db, 'informational_turn_yield', {'session_id': sid, 'root_session': row['root_session'],
                                                    'turn_id': row['turn_id'], 'obligations_retained': True})
        return True
