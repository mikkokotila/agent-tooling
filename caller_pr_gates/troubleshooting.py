"""Offer flexible chat structure once; formatting never blocks work or restarts a turn."""
from __future__ import annotations
from .prompts import render_prompt
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

MARKER = 'TROUBLESHOOTING_REPORT'
RULE_VERSION = 'flexible-chat-guidance-v2'
RULE = (
    render_prompt('communication')
)
SYMPTOM = re.compile(r"\b(?:broken|regression|crash(?:es|ed|ing)?|hang(?:s|ing)?|(?:not|doesn't|isn't|won't|cannot|can't)\s+(?:work(?:ing)?|start|load|run|respond|connect)|(?:failing|failed)\s+(?:tests?|builds?|deploy(?:ment)?|CI|checks?)|(?:CI|tests?|builds?|deployment)\s+(?:(?:is|are|has|have)\s+)?(?:failing|failed|broken)|authentication\s+failed|permission\s+denied|timed\s+out|(?:why|what causes?|what is causing)[\s\S]{0,180}(?:fail|error|timeout|slow|broken|crash|not\s+work)|fix(?:\s+the)?\s+(?:bug|failure|regression|crash|error))\b", re.I)
DIAGNOSTIC = re.compile(r'\b(?:troubleshoot(?:ing)?|debug(?:ging)?|diagnos(?:e|ing|is)|root\s+cause|investigat(?:e|ing)\s+(?:the\s+)?(?:failure|bug|problem|error))\b', re.I)
CONFIGURATION = re.compile(r'\b(?:add|install|create|implement|configure|write)\b[\s\S]{0,240}\b(?:hook|rule|policy|prompt|template|debug\s+logging)\b', re.I)
LABELS = ('Topic', 'Evidence', 'Intervention', 'Input Required')
OPTIONAL_LABELS = ('Questions', 'Options')
HEADING = re.compile(r'^[ ]{0,3}(?:#{1,6}\s*)?(?:[-*]\s+|\d+[.)]\s+)?(?:\*\*)?(Topic|Evidence|Intervention|Input Required|Questions|Options)(?:\*\*)?\s*:(?:\*\*)?\s*(.*)$', re.I)
BOLD_HEADING = re.compile(r'^[ ]{0,3}(?:#{1,6}\s*)?(?:[-*]\s+|\d+[.)]\s+)?\*\*(?:Topic|Evidence|Intervention|Input Required|Questions|Options)(?::\*\*|\*\*\s*:)\s*.*$', re.I)
PLACEHOLDER = re.compile(r'^(?:tbd|todo|n/?a|unknown|pending|\.\.\.|\u2026)[.!\s]*$', re.I)


def is_troubleshooting(text: str) -> bool:
    if SYMPTOM.search(text): return True
    if CONFIGURATION.search(text) or re.search(r'\b(?:troubleshooting|debugging)\s+(?:hook|rule|template|format|reporting)\b',text,re.I): return False
    return bool(DIAGNOSTIC.search(text))


def validate_report(text: str) -> list[str]:
    """Optional lint for a chosen structure; natural replies have no required shape."""
    # Labels inside quoted examples, code or HTML comments are not report structure.
    visible=re.sub(r'(?s)<!--.*?(?:-->|\Z)', lambda match: '\n'*match.group(0).count('\n'), text)
    lines=visible.splitlines(); original_lines=text.splitlines(); headings=[]; fence=None
    for i,line in enumerate(lines):
        marker=re.match(r'^[ ]{0,3}(`{3,}|~{3,})(.*)$',line)
        if fence:
            if marker and marker.group(1)[0]==fence[0] and len(marker.group(1))>=fence[1] and not marker.group(2).strip(): fence=None
            continue
        if marker:
            fence=(marker.group(1)[0],len(marker.group(1))); continue
        match=HEADING.match(line)
        if match: headings.append((i,match.group(1).lower(),match.group(2)))
    if not headings: return []
    errors=[]
    for index,(line,label,inline) in enumerate(headings):
        display=next(item for item in (*LABELS,*OPTIONAL_LABELS) if item.lower()==label)
        if not BOLD_HEADING.fullmatch(lines[line]):
            errors.append(f'Bold the {display} label, for example **{display}:**.')
        if index and original_lines[line-1].strip():
            errors.append(f'Leave a blank line before {display}.')
        end=headings[index+1][0] if index+1<len(headings) else len(lines)
        body=' '.join([inline,*lines[line+1:end]]).strip()
        plain=re.sub(r'[*_`#\s]+',' ',body).strip()
        if not plain or PLACEHOLDER.fullmatch(plain): errors.append(f'{display} needs a concrete explanation; say what is known and unknown.')
        if label in {'topic','evidence','intervention'} and plain.lower().rstrip('.') in {'none','nothing','no decision needed'}:
            errors.append(f'{display} cannot be replaced by None.')
    return errors


class State:
    def __init__(self, state_dir=None):
        root=Path(state_dir or os.getenv('CALLER_PR_GATES_STATE_DIR') or Path.home()/'.local/state/caller-pr-gates')
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=root/'troubleshooting.sqlite3'
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS turns(session_id TEXT PRIMARY KEY, turn_id TEXT, active INTEGER NOT NULL, reason TEXT, block_reason TEXT, updated_at REAL NOT NULL)')
        self.path.chmod(0o600)
    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=2)
        try:
            with db:yield db
        finally:db.close()
    def read(self,sid):
        with self.connection() as db:
            row=db.execute('SELECT turn_id,active,reason,block_reason FROM turns WHERE session_id=?',(sid,)).fetchone()
        return dict(zip(('turn_id','active','reason','block_reason'),row)) if row else {'active':False}
    def write(self,sid,turn_id,active,reason=None,block_reason=None):
        with self.connection() as db:
            db.execute('INSERT INTO turns VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET turn_id=excluded.turn_id,active=excluded.active,reason=excluded.reason,block_reason=excluded.block_reason,updated_at=excluded.updated_at',(sid,turn_id,int(active),reason,block_reason,time.time()))
    def claim_advisory(self,sid,turn_id,prompt,revision=RULE_VERSION):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT turn_id,reason,block_reason FROM turns WHERE session_id=?',(sid,)).fetchone()
            if current and (current[1]==revision or (current[2] and current[2] in prompt)): return False
            turn_id=turn_id or (current[0] if current else None)
            db.execute('INSERT INTO turns VALUES(?,?,0,?,NULL,?) ON CONFLICT(session_id) DO UPDATE SET turn_id=excluded.turn_id,active=0,reason=excluded.reason,block_reason=NULL,updated_at=excluded.updated_at',(sid,turn_id,revision,time.time()))
            return True


def _context(event, rule=None):
    return {'hookSpecificOutput':{'hookEventName':event,'additionalContext':rule if rule is not None else render_prompt('communication')}}


def _continuation(event):
    prompt=str(event.get('prompt') or '')
    return bool(event.get('stop_hook_active') or re.match(
        r'^\s*(?:' + re.escape(MARKER) + r'\b|Durable\b|<heartbeat\b|<hook_prompt\b|\[automated\b|'
        r'(?:PR_NOT_DRAFT|CI_FAILED|CI_PENDING|REVIEW_HANDLED|REVIEW_CONCLUDED|PRD_FOLLOWUP|'
        r'SLICE_SCOPE|PR_GATE|OWNED_PR_PROGRESS)\s+(?:FAIL|WAIT|UNKNOWN)|'
        r'(?:continue|carry on|go ahead|proceed|status\b|how.?s it going)\b)', prompt, re.I))


def handle(event: dict,state_dir=None):
    name=event.get('hook_event_name','')
    # Stop, tool failures and handbacks never validate, inject or reject formatting.
    if name not in {'SessionStart','UserPromptSubmit'}: return None
    if _continuation(event): return None
    sid=str(event.get('session_id') or os.getenv('CODEX_THREAD_ID') or os.getenv('CLAUDE_SESSION_ID') or '')
    if not sid: return None
    from .prompts import baseline_text
    import hashlib
    rule = render_prompt('communication')
    revision = RULE_VERSION if rule == baseline_text('communication') else RULE_VERSION + ':' + hashlib.sha256(rule.encode()).hexdigest()[:16]
    if not State(state_dir).claim_advisory(sid,event.get('turn_id'),str(event.get('prompt') or ''),revision): return None
    return _context(name, rule)


def main():
    event={}
    try:
        event=json.load(sys.stdin)
        if not isinstance(event,dict):raise ValueError('Expected a hook event object')
        result=handle(event)
    except Exception:
        # Advisory availability is never a permission or completion gate.
        return
    if result:print(json.dumps(result))

if __name__=='__main__':main()
