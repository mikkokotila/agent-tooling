"""Retain unresolved PR obligations without redispatching unchanged evidence."""
from __future__ import annotations

import hashlib
import json
import re
import time

from .policy import _latest_checks, _outcome
from .copilot import is_service_check


from .errors import error_identity as _error_identity


def branch_identity(snapshot, gates, previous=None):
    """Last authoritative ancestry is only an event identity, never passing proof."""
    gate = next((g for g in gates if g.get('gate') == 'BRANCH_UP_TO_DATE'), None)
    if gate and gate.get('status') in {'PASS', 'FAIL'}:
        return {'head_sha': snapshot.get('head_sha'), 'gate': gate,
                **{key: snapshot.get(key) for key in ('base_ref', 'base_sha', 'branch_comparison')}}
    if isinstance(previous, dict) and previous.get('head_sha') == snapshot.get('head_sha'):
        return previous
    return None


def closure_identity(snapshot, gates, previous=None):
    """A last-known link can deduplicate work during an outage, never prove readiness."""
    gate = next((g for g in gates if g.get('gate') == 'ISSUE_CLOSURE'), None)
    if gate and gate.get('status') in {'PASS', 'FAIL'}:
        return {'head_sha': snapshot.get('head_sha'), 'gate': gate,
                **{key: snapshot.get(key) for key in ('default_branch', 'base_ref', 'closing_issues')}}
    if isinstance(previous, dict) and previous.get('head_sha') == snapshot.get('head_sha'):
        return previous
    return None


def evidence_fingerprint(snapshot, gates, previous_branch=None, previous_closure=None):
    """Identity excludes collection time, request quota and changing error prose."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    gates = [gate for gate in (gates or []) if isinstance(gate, dict)]
    proof = branch_identity(snapshot, gates, previous_branch)
    if proof:
        snapshot = dict(snapshot, **{key: proof.get(key) for key in ('base_ref', 'base_sha', 'branch_comparison')})
        gates = [gate for gate in gates if gate.get('gate') != 'BRANCH_UP_TO_DATE'] + [proof['gate']]
    link = closure_identity(snapshot, gates, previous_closure)
    if link:
        gates = [gate for gate in gates if gate.get('gate') != 'ISSUE_CLOSURE'] + [link['gate']]
    # Adding a non-actionable criterion or losing its API evidence cannot replay old CI work.
    gates = [gate for gate in gates if gate.get('gate') not in {'BRANCH_UP_TO_DATE', 'ISSUE_CLOSURE'} or gate.get('status') == 'FAIL']
    comments = [{"id": str(comment.get("id")), "updated_at": comment.get("updated_at"),
                 "resolved": comment.get("is_resolved"),
                 "body": hashlib.sha256(str(comment.get("body", "")).encode()).hexdigest()}
                for comment in snapshot.get("comments") or [] if isinstance(comment, dict)]
    failures = []
    for check in _latest_checks([c for c in snapshot.get("checks") or [] if not is_service_check(c)]):
        if _outcome(check) == "failed":
            failures.append({key: check.get(key) for key in
                ("kind", "key", "name", "id", "run_attempt", "status", "conclusion")})
    errors = [_error_identity(error, str(component)) for component, error in
              (snapshot.get("component_errors") or {}).items() if component not in {'branch_freshness', 'issue_closure'}]
    if not errors:
        errors = [_error_identity(detail, str(gate.get("gate"))) for gate in gates
                  if gate.get("status") == "UNKNOWN" for detail in gate.get("details") or []]
    value = {"head": snapshot.get("head_sha"), "state": str(snapshot.get("state", "")).lower(),
             "merged": snapshot.get("merged"), "mergeable": snapshot.get('mergeable'),
             "issue_revision": snapshot.get("updated_at") if not snapshot.get("head_sha") else None,
             "comments": sorted(comments, key=lambda item: item["id"]),
             "failed_checks": sorted(failures, key=lambda item: (str(item.get("kind")), str(item.get("id")))),
             "errors": sorted(errors, key=lambda item: json.dumps(item, sort_keys=True)),
             "gates": sorted([{"gate": gate.get("gate"), "status": gate.get("status")}
                               for gate in gates], key=lambda item: str(item["gate"]))}
    if any(gate.get('gate') == 'BRANCH_UP_TO_DATE' for gate in gates):
        value.update({key: snapshot.get(key) for key in ('base_ref', 'base_sha', 'branch_comparison')})
    if link and link['gate']['status'] == 'FAIL':
        value['issue_closure'] = {key: link.get(key) for key in ('default_branch', 'base_ref', 'closing_issues')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def hold_unresolved(store, sid, repository, number, snapshot, gates, *, turn_id=None, reason=None):
    """Record a handoff, never completion; keep existing worker leases intact."""
    old = store.recovery(repository, number, sid) or {}
    fingerprint = evidence_fingerprint(snapshot, gates, old.get('branch_identity'), old.get('closure_identity'))
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    if old.get("state") == "held_unresolved" and old.get("handoff_head_sha") and not snapshot.get("head_sha"):
        return old  # An outage cannot replace a known current-head obligation.
    if old.get("state") == "held_unresolved" and old.get("fingerprint") == fingerprint:
        return old
    return store.record_recovery(repository, number, sid, {
        "state": "held_unresolved", "fingerprint": fingerprint,
        "branch_identity": branch_identity(snapshot, gates, old.get('branch_identity')),
        'closure_identity': closure_identity(snapshot, gates, old.get('closure_identity')),
        "handoff_head_sha": snapshot.get("head_sha"),
        "reported_at": time.time(), "handoff_turn_id": turn_id,
        "handoff_reason": reason or "Reported unresolved PR evidence", "obligation_complete": False})


def current_hold(store, sid, snapshot, gates, *, repository=None, number=None):
    """Release only on changed authoritative identity, preserving a live lease."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    repository = repository or snapshot.get("repository")
    number = number or snapshot.get("number")
    if not repository or not number or not hasattr(store, "recovery"):
        return None
    old = store.recovery(repository, number, sid) or {}
    if old.get("state") != "held_unresolved":
        return None
    if old.get("handoff_head_sha") and not snapshot.get("head_sha"):
        return old  # Missing evidence proves neither a change nor completion.
    fingerprint = evidence_fingerprint(snapshot, gates, old.get('branch_identity'), old.get('closure_identity'))
    if old.get("fingerprint") == fingerprint:
        return old
    store.record_recovery(repository, number, sid, {"state": "pending", "fingerprint": fingerprint,
        "handoff_released_at": time.time(), "handoff_release_reason": "new_authoritative_evidence",
        "next_attempt_at": 0, "attempts": 0, "obligation_complete": False})
    return None
