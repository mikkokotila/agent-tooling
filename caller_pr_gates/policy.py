"""Independent PR gates. Evidence gaps and changed heads never pass silently."""
from __future__ import annotations

import re
import hashlib
from datetime import datetime
from typing import Any

GATES = ("PR_NOT_DRAFT", "CI_FAILED", "CI_PENDING", "REVIEW_HANDLED", "REVIEW_CONCLUDED", "MERGE_CONFLICT", "BRANCH_UP_TO_DATE", "ISSUE_CLOSURE")
FAILURES = {"failure", "failed", "error", "cancelled", "canceled", "timed_out", "action_required", "stale", "startup_failure"}
SUCCESSES = {"success", "neutral", "skipped"}
PENDING = {"queued", "pending", "in_progress", "requested", "waiting", "waiting_for_review", "waiting_for_resource", "waiting_for_approval"}


def _gate(name: str, status: str, details: list[str], evidence: dict | None = None) -> dict:
    return {"gate": name, "status": status, "details": details, "evidence": evidence or {}}


def unknown_gates(reason: str) -> list[dict]:
    return [_gate(name, "UNKNOWN", [reason]) for name in GATES]


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _latest_checks(checks: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for check in checks:
        key = str(check.get("key") or f"{check.get('kind')}:{check.get('name')}")
        # Attempts on separate workflow runs cannot be compared numerically: newest
        # creation wins, then attempt. Normal collector output already selects runs.
        def order(item: dict) -> tuple:
            raw_id = str(item.get("id") or "")
            number = int(raw_id) if raw_id.isdigit() else 0
            return (str(item.get("created_at") or item.get("updated_at") or ""),
                    int(item.get("run_attempt") or 1), str(item.get("updated_at") or ""), number)
        if key not in latest or order(check) > order(latest[key]):
            latest[key] = check
    return list(latest.values())


def _outcome(check: dict) -> str:
    status = str(check.get("status") or "").lower()
    conclusion = str(check.get("conclusion") or "").lower()
    if status in PENDING:
        return "pending"
    if status in FAILURES or conclusion in FAILURES:
        return "failed"
    if status == "completed" and conclusion in SUCCESSES:
        return "success"
    if status in SUCCESSES and (not conclusion or conclusion in SUCCESSES):
        return "success"
    return "unknown"


def _ci(snapshot: dict, expected_override: list[str] | None) -> list[dict]:
    checks = snapshot.get("checks")
    expected = snapshot.get("expected_checks") if expected_override is None else expected_override
    if not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks):
        return [_gate(name, "UNKNOWN", ["Individual CI evidence is missing or malformed"])
                for name in ("CI_FAILED", "CI_PENDING")]
    discovery_unknown = not isinstance(expected, list) or any(not isinstance(name, str) for name in expected)
    if discovery_unknown:
        expected = snapshot.get("known_expected_checks") or []
        if not isinstance(expected, list) or any(not isinstance(name, str) for name in expected):
            expected = []
    head = str(snapshot["head_sha"])
    if any(check.get("head_sha") and check["head_sha"] != head for check in checks):
        return [_gate(name, "UNKNOWN", ["CI evidence belongs to a different commit"])
                for name in ("CI_FAILED", "CI_PENDING")]
    try:
        checks = _latest_checks(checks)
    except (TypeError, ValueError):
        return [_gate(name, "UNKNOWN", ["CI attempt evidence is malformed"])
                for name in ("CI_FAILED", "CI_PENDING")]
    failed, pending, unknown = [], [], []
    if discovery_unknown:
        errors = snapshot.get("component_errors") or {}
        rules = errors.get("rules") or {}
        explanation = rules.get("message") or "Required check discovery is unavailable"
        unknown.append(str(explanation))
    for check in checks:
        if not check.get("name") or not check.get("id") or not check.get("kind"):
            unknown.append("Missing CI identity")
            continue
        outcome = _outcome(check)
        label = f"{check['kind']} {check['name']} ({check.get('conclusion') or check.get('status')})"
        if outcome == "failed":
            failed.append(label)
        elif outcome == "pending":
            pending.append(label)
        elif outcome == "unknown":
            unknown.append(label)
    present = {check.get("name") for check in checks}
    missing = sorted(set(expected) - present)
    pending.extend(f"Required check missing: {name}" for name in missing)
    evidence = {"head_sha": head, "checks": checks, "expected_checks": None if discovery_unknown else expected,
                "known_expected_checks": expected, "missing_checks": missing,
                "component_errors": snapshot.get("component_errors") or {}}
    failed_status = "FAIL" if failed else ("UNKNOWN" if unknown else "PASS")
    pending_status = "WAIT" if pending else ("UNKNOWN" if unknown else "PASS")
    return [_gate("CI_FAILED", failed_status, failed + unknown, evidence),
            _gate("CI_PENDING", pending_status, pending + unknown, evidence)]


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())


def _receipt(comment: dict, dispositions: list[dict]) -> tuple[dict | None, str | None]:
    records = [record for record in dispositions if isinstance(record, dict)
               and str(record.get("comment_id") or "") == str(comment["id"])]
    current = [record for record in records if record.get("comment_updated_at") == comment["updated_at"]]
    if not current:
        return None, "Comment was edited after its disposition" if records else "No recorded disposition"
    return current[-1], None


def _handled(snapshot: dict, comment: dict, receipt: dict | None, error: str | None) -> tuple[bool, str]:
    if error or receipt is None:
        return False, error or "No recorded disposition"
    reason = receipt.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return False, "Disposition needs a substantive reason"
    disposition = receipt.get("disposition")
    if disposition == "nonfix":
        return True, "Recorded non-fix reason"
    if disposition != "fixed":
        return False, "Disposition must be fixed or nonfix"
    if snapshot.get("kind") == "issue":
        body = snapshot.get("body")
        if not isinstance(body, str):
            return False, "Issue document content is unavailable"
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if receipt.get("document_revision") != snapshot.get("document_edited_at") or receipt.get("content_sha256") != content_hash:
            return False, "Issue fix receipt does not match the current document revision/content"
        edited, commented = _date(snapshot.get("document_edited_at")), _date(comment.get("created_at") or comment["updated_at"])
        if edited is None or commented is None or edited <= commented:
            return False, "Issue document was not verifiably edited after the comment"
        return True, "Current issue document edit and content hash verified"
    commit = str(receipt.get("commit_sha") or "").lower()
    commits = {str(sha).lower() for sha in snapshot.get("commit_shas", [])}
    if not commit or commit not in commits:
        return False, "Fix commit is absent from this PR's current commit history"
    dates = snapshot.get("commit_dates") or {}
    if commit in dates:
        committed, commented = _date(dates[commit]), _date(comment.get("created_at") or comment["updated_at"])
        if committed is None or commented is None or committed < commented:
            return False, "Fix commit predates the comment or has unverifiable chronology"
    return True, "Fix commit remains in this PR"


def _concluded(snapshot: dict, comment: dict, receipt: dict | None, *, reply_only=False) -> tuple[bool, str]:
    if not reply_only and comment.get("thread_id") and comment.get("is_resolved") is True:
        return True, "GitHub review thread is resolved"
    if not reply_only and comment.get('thread_id'):
        return False, 'Close the review thread after its evidenced fix/non-fix; a reply alone does not close it'
    if receipt is None or not receipt.get("reply_url"):
        return False, "Needs a disposition reply or GitHub thread resolution"
    actor = str(snapshot.get("actor_login") or "").lower()
    for reply in comment.get("replies") or []:
        if reply.get("url") != receipt["reply_url"]:
            continue
        if str(reply.get("author_login") or "").lower() != actor:
            return False, "Reply was not posted by the authenticated actor"
        replied, updated = _date(reply.get("created_at")), _date(comment.get("updated_at"))
        if replied is None or updated is None or replied < updated:
            return False, "Reply predates the comment's latest edit"
        body = _normalize(str(reply.get("body") or ""))
        reason = _normalize(str(receipt.get("reason") or ""))
        commit = str(receipt.get("commit_sha") or "").lower()
        reference = bool(len(commit) >= 7 and re.search(r"(?<![a-f0-9])" + re.escape(commit[:7]) + r"[a-f0-9]*(?![a-f0-9])", body))
        if not body or not ((reason and reason in body) or (receipt.get("disposition") == "fixed" and reference)):
            return False, "Reply does not explain this recorded fix/non-fix"
        return True, "Disposition reply verified on GitHub"
    return False, "Recorded reply URL was not found in this conversation"


def _verified_conclusion_replies(snapshot: dict, comments: list[dict], dispositions: list[dict]) -> set[str]:
    """An account identity never distinguishes human feedback from agent replies."""
    actor = str(snapshot.get("actor_login") or "").lower()
    verified: set[str] = set()
    for candidate in comments:
        if str(candidate.get("author_login") or "").lower() != actor:
            continue
        url = candidate.get("url")
        if not isinstance(url, str) or not url or not candidate.get("id") or not candidate.get("updated_at"):
            continue
        if not isinstance(candidate.get("body"), str) or not candidate["body"].strip():
            continue
        for parent in comments:
            if parent.get("id") == candidate["id"] or parent.get("url") == url:
                continue
            if not parent.get("id") or not parent.get("updated_at") or not parent.get("url"):
                continue
            if not isinstance(parent.get("body"), str) or not parent["body"].strip():
                continue
            record, error = _receipt(parent, dispositions)
            if record is None or record.get("reply_url") != url:
                continue
            if not _handled(snapshot, parent, record, error)[0]:
                continue
            if not any(reply.get("url") == url for reply in parent.get("replies") or []):
                continue
            # Resolution can independently conclude a parent, but cannot authenticate
            # a reply. Validate the actual current inventory body, author and time.
            reply_parent = {**parent, "is_resolved": False, "replies": [candidate]}
            if _concluded(snapshot, reply_parent, record, reply_only=True)[0]:
                verified.add(url)
                break
    return verified


def evaluate_review(snapshot: dict, dispositions: list[dict]) -> list[dict]:
    """Review-only gates also apply to PRD/Slice issues."""
    comments = snapshot.get("comments")
    if not isinstance(comments, list) or any(not isinstance(item, dict) for item in comments):
        return [_gate(name, "UNKNOWN", ["Review comment evidence is missing or malformed"])
                for name in ("REVIEW_HANDLED", "REVIEW_CONCLUDED")]
    actor, author = snapshot.get("actor_login"), snapshot.get("author_login")
    if not isinstance(actor, str) or not actor or not isinstance(author, str) or not author:
        return [_gate(name, "UNKNOWN", ["Review author/authenticated actor identity is missing"])
                for name in ("REVIEW_HANDLED", "REVIEW_CONCLUDED")]
    verified_replies = _verified_conclusion_replies(snapshot, comments, dispositions)
    failed_handled, failed_concluded, unknown = [], [], []
    obligations = []
    for comment in comments:
        if comment.get("url") in verified_replies:
            continue
        if not isinstance(comment.get("body"), str):
            unknown.append("Comment body is unavailable")
            continue
        if not comment["body"].strip():
            continue
        if not comment.get("id") or not comment.get("updated_at") or not comment.get("url") or not comment.get("author_login"):
            unknown.append("Comment identity/author/revision is incomplete")
            continue
        if comment.get("kind") == "inline" and (not isinstance(comment.get("is_resolved"), bool) or not isinstance(comment.get("is_outdated"), bool)):
            unknown.append(f"Comment {comment['id']}: resolution/outdated state is unavailable")
            continue
        receipt, error = _receipt(comment, dispositions)
        handled, handling_reason = _handled(snapshot, comment, receipt, error)
        concluded, conclusion_reason = _concluded(snapshot, comment, receipt)
        obligations.append({"id": comment["id"], "url": comment["url"], "kind": comment.get("kind"),
                            "is_outdated": comment.get("is_outdated", False), "handled": handled,
                            "concluded": concluded, "handling_evidence": handling_reason,
                            "conclusion_evidence": conclusion_reason})
        label = f"Comment {comment['id']} ({comment['url']})"
        if not handled:
            failed_handled.append(f"{label}: {handling_reason}")
        if not concluded:
            failed_concluded.append(f"{label}: {conclusion_reason}")
    evidence = {"obligations": obligations, "comment_count": len(comments),
                "verified_conclusion_replies": sorted(verified_replies)}
    return [_gate("REVIEW_HANDLED", "FAIL" if failed_handled else ("UNKNOWN" if unknown else "PASS"), failed_handled + unknown, evidence),
            _gate("REVIEW_CONCLUDED", "FAIL" if failed_concluded else ("UNKNOWN" if unknown else "PASS"), failed_concluded + unknown, evidence)]


def branch_freshness_gate(snapshot: dict) -> dict:
    if snapshot.get('state') == 'closed':
        return _gate('BRANCH_UP_TO_DATE', 'PASS', [], {'source_closed': True, 'applicable': False})
    base, after, ref = snapshot.get('base_sha'), snapshot.get('base_sha_after'), snapshot.get('base_ref')
    comparison = snapshot.get('branch_comparison') or {}
    evidence = {'base_ref': ref, 'base_sha': base, 'base_sha_after': after,
                'head_sha': snapshot.get('head_sha'), 'comparison': comparison}
    def unknown(reason):
        return _gate('BRANCH_UP_TO_DATE', 'UNKNOWN', [reason], evidence)
    if (not isinstance(base, str) or not re.fullmatch(r'[a-fA-F0-9]{40}', base)
            or not isinstance(ref, str) or not ref or not isinstance(comparison, dict)):
        return unknown('Current target-branch ancestry evidence is unavailable; reread before completion')
    if base != after or ref != snapshot.get('base_ref_after'):
        return unknown('The target branch changed during collection; reread its current tip')
    if (comparison.get('head_sha') != snapshot.get('head_sha') or comparison.get('base_sha') != base
            or (snapshot.get('component_errors') or {}).get('branch_freshness')):
        return unknown('Branch comparison is unavailable or belongs to different commits; reread')
    behind, ahead = comparison.get('behind_by'), comparison.get('ahead_by')
    status, ancestor = comparison.get('status'), comparison.get('merge_base_sha')
    if (type(behind) is not int or behind < 0 or type(ahead) is not int or ahead < 0
            or not isinstance(ancestor, str) or not re.fullmatch(r'[a-fA-F0-9]{40}', ancestor)):
        return unknown('Branch comparison lacks verified commit counts or ancestry')
    expected = 'identical' if behind == ahead == 0 else 'ahead' if behind == 0 else 'behind' if ahead == 0 else 'diverged'
    if (status != expected or (behind == 0) != (ancestor == base)
            or (status == 'identical') != (snapshot.get('head_sha') == base)):
        return unknown('Branch comparison has contradictory ancestry evidence')
    if behind:
        return _gate('BRANCH_UP_TO_DATE', 'FAIL',
                     [f'Branch lacks {behind} commit(s) from current {ref}; fetch, integrate, resolve conflicts and push before local validation or completion'], evidence)
    return _gate('BRANCH_UP_TO_DATE', 'PASS', [], evidence)


def evaluate_pr(snapshot: dict, dispositions: list[dict], expected_checks: list[str] | None = None) -> list[dict]:
    if not isinstance(snapshot, dict):
        return unknown_gates("PR snapshot is unavailable")
    head, after = snapshot.get("head_sha"), snapshot.get("head_sha_after")
    if not isinstance(head, str) or not head or not isinstance(after, str) or not after:
        return unknown_gates("Current-head verification is missing")
    if head != after:
        return unknown_gates("PR head changed during evidence collection; reread required")
    draft = snapshot.get("draft")
    if not isinstance(draft, bool) or snapshot.get("state") not in {"open", "closed"} or not isinstance(snapshot.get("merged"), bool):
        draft_gate = _gate("PR_NOT_DRAFT", "UNKNOWN", ["Draft/lifecycle evidence is unavailable"])
    else:
        draft_gate = _gate("PR_NOT_DRAFT", "FAIL" if draft else "PASS", ["PR is draft; mark it ready for review"] if draft else [],
                           {"draft": draft, "state": snapshot["state"], "merged": snapshot["merged"]})
    mergeable = snapshot.get('mergeable')
    if snapshot.get('state') == 'closed':
        merge_gate = _gate('MERGE_CONFLICT', 'PASS', [], {'source_closed': True})
    elif isinstance(mergeable, bool):
        merge_gate = _gate('MERGE_CONFLICT', 'PASS' if mergeable else 'FAIL',
                           [] if mergeable else ['Resolve the conflict with the base branch and push'],
                           {'mergeable': mergeable, 'head_sha': head})
    else:
        merge_gate = _gate('MERGE_CONFLICT', 'UNKNOWN', ['GitHub has not established mergeability; reread without claiming completion'])
    from .closure import gate as closure_gate
    return [draft_gate, *_ci(snapshot, expected_checks), *evaluate_review(snapshot, dispositions), merge_gate, branch_freshness_gate(snapshot), closure_gate(snapshot)]
