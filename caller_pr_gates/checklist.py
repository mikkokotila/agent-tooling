"""One PR-only native message; verification and durable receipts stay in the CLI."""
from __future__ import annotations

import json
from pathlib import Path
import time

from .prompts import render_prompt
from .store import Store


def message(rows):
    urls = list(dict.fromkeys(f"https://github.com/{r['repository']}/pull/{r['number']}" for r in rows))
    return render_prompt('pr_checklist', pr_urls='\n'.join(urls) or 'publication in this chat')


def instructions(texts):
    """One managed block, without resurrecting the retired instruction injectors."""
    return (
        '## PR checklist (Codex and Claude)\n'
        'Ownership is the exact recorded chat and PR checkout; references and repository reads do not adopt work. '
        'Register created/modified PRs by explicit URL with caller-pr-gates register URL --session SESSION --client CLIENT --cwd CHECKOUT. '
        'Use caller-pr-gates github --session SESSION --client CLIENT -- ... for GitHub writes. '
        'Publish useful commits and a non-draft PR before local checks. Keep the agreed scope and genuine closing issue reference. '
        'Record required checks with validation-plan and run them with validate --background; actual results are required. '
        'Inspect evidence with status --session SESSION --fresh. CLI help describes evidenced review dispositions and resolution. '
        'The PR monitor checks one owned open PR every five minutes, or several every ten minutes. '
        'Unchanged results, pending CI and API outages do not restart chats. Merged/closed PRs cancel delivery without passing unresolved gates. '
        'Existing issue/document evidence remains recorded; their automatic chat prompts are retired.\n'
        'Copilot quota exhaustion, unavailability and review execution status never block work, completion or merge. '
        'Every actual Copilot review comment or change request requires the same evidenced disposition and public reply as other feedback.\n\n'
        + texts['pr_checklist'].replace('${pr_urls}', 'owned PRs in this chat') + '\n'
    )


def owned_rows(store, sid):
    with store._connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM pr_watch WHERE owner_session=? AND state='open'", (sid,))]


def handle(event, store=None):
    from . import hook, monitor, turns
    store = store or Store()
    name = str(event.get('hook_event_name') or '')
    sid = hook._session(event)
    if not sid:
        return None
    parent = event.get('parent_session_id') or event.get('parent_thread_id')
    store.record_session(sid, hook._client(), hook._cwd(event), parent)
    session = store.session(sid)
    root = session['root_session']
    prompt = str(event.get('prompt') or '')

    # Already queued legacy document/issue prompts cannot become human authority.
    if name == 'UserPromptSubmit' and (prompt.startswith('<!-- issue-closeout:') or hook.DURABLE_FOLLOWUP.match(prompt)):
        store.audit('retired_native_message_rejected', {'session_id': sid})
        return {'continue': False, 'stopReason': ''}

    if name == 'UserPromptSubmit' and prompt.startswith(monitor.MARKER):
        token = prompt[len(monitor.MARKER):].split('-->', 1)[0].strip()
        rows = [r for r in monitor.pending(store, root) if r['event_id'] == token]
        if root != sid or not rows or prompt.split('-->\n', 1)[-1] != message(rows):
            return {'continue': False, 'stopReason': ''}
        text = monitor.consume(store, sid, event_id=token, full=False, checklist=True)
        if not text:
            with store._connect() as db:
                db.execute('UPDATE watch_sessions SET busy=0 WHERE session_id=?', (sid,))
            return {'continue': False, 'stopReason': ''}
        # The queue already contains the one checklist. Do not inject it twice.
        return None

    if name == 'UserPromptSubmit':
        turns.start_turn(store, event, sid)
    if name in {'PreToolUse', 'PostToolUse', 'UserPromptSubmit', 'Stop', 'Interrupt', 'SessionEnd', 'StopFailure'}:
        busy = name in {'PreToolUse', 'PostToolUse', 'UserPromptSubmit'}
        with store._connect() as db:
            db.execute('INSERT INTO watch_sessions VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET busy=excluded.busy,updated_at=excluded.updated_at',
                       (root, int(busy), time.time()))
    if name == 'PreToolUse':
        # Keep existing mutation/validation guards, but retire their prompt zoo.
        result = hook.handle(dict(event, checklist_only=True), store)
        if result:
            if result.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny':
                store.audit('checklist_tool_guard', {'session_id': sid, 'guard': result})
                return hook._deny(message(owned_rows(store, root)))
        return None
    if name == 'PostToolUse':
        hook._register_urls(event, store)
        hook.discover(event, store)
        if hook._local_mutation(event):
            turns.note_mutation(store, event, sid)
        if sid != root:
            return None
        text = monitor.consume(store, sid, full=False, checklist=True)
        if text:
            return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': text}}
    # Stop is not a second poller and cannot initiate recursive continuations.
    return None


def failure(event, error, store=None):
    store = store or Store()
    sid = str(event.get('session_id') or '')
    store.audit('checklist_native_error', {'session_id': sid, 'error': f'{type(error).__name__}: {str(error)[:300]}'})
    if event.get('hook_event_name') == 'PreToolUse':
        from .hook import _deny
        return _deny(message(owned_rows(store, sid)))
    return None
