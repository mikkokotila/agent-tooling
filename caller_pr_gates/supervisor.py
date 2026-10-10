"""Durable review followups; a subprocess acknowledgement never proves completion.

The native Codex heartbeat invokes ``caller-pr-gates supervise --execute``. Codex work is queued into
its existing thread; Claude work resumes only an idle (or detached) session.
No scheduling service or model-specific session mapping is created here.
"""
from __future__ import annotations
from .prompts import render_prompt

import asyncio
from datetime import datetime, timezone
import hashlib
import fcntl
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any, Callable
import uuid

MAX_ATTEMPTS = 3
LEASE_SECONDS = 900
WORKER_TIMEOUT = 600
RETRY_SECONDS = 300
RECOVERY_COOLDOWN = 600
SESSION_INACTIVITY = 180


def scope_value(scope: dict | None) -> dict | None:
    if not isinstance(scope, dict):
        return None
    value = scope.get("scope", scope)
    if not isinstance(value, dict):
        return None
    return {k: v for k, v in value.items() if k not in {"session_id", "created_at", "updated_at"}}


def scope_digest(scope: dict | None) -> str:
    value = scope_value(scope)
    if value is None:
        raise ValueError("approved scope is missing")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def recorded_job_scope(store, job):
    return store.followup_scope(job) if hasattr(store, 'followup_scope') else store.get_scope(job['session_id'])


def _epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            pass
    raise ValueError("missing or invalid timestamp")


def _client(value: str) -> str:
    value = value.lower()
    if value in {"codex", "codex-cli"}:
        return "codex"
    if value in {"claude", "claude-code", "claude-code-plugin"}:
        return "claude"
    raise ValueError(f"unsupported owning client: {value}")


def scoped_prompt(job: dict, session: dict, scope: dict, gates: list[dict]) -> str:
    issue = job.get("issue_number")
    target = f"https://github.com/{job['repository']}/{'issues' if issue else 'pull'}/{issue or job['number']}"
    failed = [g for g in gates if g.get("status") != "PASS"]
    dispatch = render_prompt('followup_dispatch_line', dispatch_id=job['dispatch_id']) if job.get("dispatch_id") else ""
    return (
        render_prompt('document_followup', kind=job.get('kind', 'prd'), followup_id=job['id'], dispatch_line=dispatch, session_id=session['session_id'], owner_cwd=session['cwd'], target_url=target, scope_json=json.dumps(scope_value(scope), sort_keys=True, ensure_ascii=False), scope_digest=scope_digest(scope), gate_json=json.dumps(failed, sort_keys=True, ensure_ascii=False))
    )


def resume_command(client: str, session_id: str, prompt: str) -> tuple[list[str], str | None]:
    """Use exact existing IDs, never --last, fork-session, new session or fallback."""
    uuid.UUID(session_id)
    if _client(client) == "codex":
        return [os.getenv("CODEX_CLI", "codex"), "queue", "--thread", session_id, "--message", prompt], None
    return [os.getenv("CLAUDE_CLI", "claude"), "--print", "--resume", session_id, "--output-format", "json"], prompt


def run_process(args: list[str], *, cwd: str, input_text: str | None = None, timeout: float = WORKER_TIMEOUT, capture_limit: int | None = 2000) -> dict:
    """Bound the whole process group, including hook/agent grandchildren."""
    proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
        raise TimeoutError(f"worker timeout after {timeout:g}s")
    return {"returncode": proc.returncode, "stdout": stdout[-capture_limit:] if capture_limit else stdout, "stderr": stderr[-2000:]}


def claude_availability(session_id: str, cwd: str) -> str:
    """Use Claude's session inventory; unknown state fails closed."""
    try:
        done = run_process([os.getenv("CLAUDE_CLI", "claude"), "agents", "--json"], cwd=cwd, timeout=20, capture_limit=None)
        if done["returncode"]:
            return "unknown"
        rows = json.loads(done["stdout"])
        if not isinstance(rows, list):
            return "unknown"
        for row in rows:
            if row.get("sessionId", row.get("session_id")) == session_id:
                if os.path.realpath(row.get("cwd", "")) != os.path.realpath(cwd):
                    return "unknown"
                return "idle" if row.get("status") in {"idle", "completed", "exited"} else "busy"
        return "idle"  # Detached transcript; resume errors remain durable failures.
    except (OSError, ValueError, TimeoutError):
        return "unknown"


async def _call(func: Callable, *args: Any, **kwargs: Any) -> Any:
    value = func(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _blocking(gates: list[dict]) -> bool:
    return not gates or any(g.get("status") != "PASS" for g in gates)


def _read_verified(job: dict, snapshot: dict, current_scope: dict | None) -> tuple[bool, str]:
    fields = job.get("fields") or {}
    receipt = fields.get("review_read", fields)
    try:
        digest = scope_digest(job.get("scope"))
        if scope_digest(current_scope) != digest:
            return False, "approved scope or slice count changed"
        if receipt.get("scope_digest") != digest:
            return False, "review receipt does not match approved scope"
        read_at = _epoch(receipt.get("read_at"))
        if read_at < _epoch(job["due_at"]) or read_at > _epoch(snapshot.get("collected_at")):
            return False, "review reread was before the due time or is unverifiable"
        expected_head = snapshot.get("head_sha", snapshot.get("updated_at"))
        if not expected_head or receipt.get("head_sha") != expected_head:
            return False, "review reread no longer matches current content"
        comments = snapshot.get("comments")
        if not isinstance(comments, list):
            return False, "comment inventory is unavailable"
        ids = set(map(str, receipt.get("comment_ids", [])))
        if not {str(c["id"]) for c in comments}.issubset(ids):
            return False, "new comments appeared after the review reread"
        for comment in comments:
            if _epoch(comment.get("updated_at")) > read_at:
                return False, "a review comment changed after the review reread"
    except (ValueError, TypeError, KeyError):
        return False, "review reread evidence is incomplete"
    return True, "live due reread and unchanged scope verified"


def _verified(job: dict, snapshot: dict, gates: list[dict], current_scope: dict | None) -> tuple[bool, str]:
    verified, reason = _read_verified(job, snapshot, current_scope)
    if not verified:
        return False, reason
    if _blocking(gates):
        return False, "separate review/PR gates remain unfinished"
    return True, "live reread, separate gates and unchanged scope verified"


def complete_verified_followup(store: Any, job: dict, snapshot: dict, gates: list[dict], *, now: float | None = None) -> tuple[bool, str]:
    """Stop hooks can complete immediately using their own freshly collected data.

    This helper performs no network or model work. Its caller must pass a fresh,
    fully collected PR/issue snapshot and every applicable independent gate.
    """
    now = time.time() if now is None else now
    verified, reason = _verified(job, snapshot, gates, recorded_job_scope(store, job))
    if not verified:
        return False, reason
    claimed = job if job.get("status") == "running" and float(job.get("lease_until") or 0) > now else store.claim_followup(job["id"], lease_seconds=LEASE_SECONDS)
    if claimed and store.complete_followup(job["id"], claimed["lease_token"], fields={"verification": reason, "verified_at": now}):
        return True, reason
    return False, "another worker owns the completion lease"


async def _tick(store: Any = None, *, execute: bool = False, collect_pr: Callable | None = None,
               collect_issue: Callable | None = None, evaluate_pr: Callable | None = None,
               evaluate_review: Callable | None = None, runner: Callable | None = None,
               availability: Callable | None = None, clock: Callable = time.time,
               max_attempts: int = MAX_ATTEMPTS, lease_seconds: int = LEASE_SECONDS,
               worker_timeout: int = WORKER_TIMEOUT) -> dict:
    """Refresh PR evidence and advance due jobs. Tests inject collectors/runners.

    Dry-run ticks retain obligations without dispatching. Execute mode only queues
    existing Codex IDs or resumes verified idle Claude IDs. No API writes occur
    inside this supervisor; resumed scoped agents handle already-authorized work.
    """
    if store is None:
        from .store import Store
        store = Store()
    if collect_pr is None or collect_issue is None or evaluate_pr is None or evaluate_review is None:
        from . import github, policy
        collect_pr = collect_pr or github.collect_pr
        collect_issue = collect_issue or github.collect_issue
        evaluate_pr = evaluate_pr or policy.evaluate_pr
        evaluate_review = evaluate_review or policy.evaluate_review
    now = clock()
    result: dict[str, Any] = {"at": now, "execute": execute, "refreshed": [], "actions": [], "completed": [], "deferred": [], "errors": []}
    cache: dict[tuple[str, int, bool], tuple[dict, list[dict]]] = {}

    async def refresh(repository: str, number: int, issue: bool = False) -> tuple[dict, list[dict]]:
        key = (repository, number, issue)
        if key in cache:
            return cache[key]
        snapshot = await _call(collect_issue if issue else collect_pr, repository, number)
        gates = await _call(evaluate_review if issue else evaluate_pr, snapshot, store.dispositions(repository, number))
        store.set_snapshot(repository, number, snapshot, gates)
        cache[key] = snapshot, gates
        result["refreshed"].append({"repository": repository, "number": number, "issue": issue, "gates": gates})
        return snapshot, gates

    due_targets = {(j["repository"], int(j["number"])) for j in store.followups() if not j.get("issue_number") and j.get("status") != "complete" and _epoch(j["due_at"]) <= now}
    for pr in store.prs():
        if pr.get("status", "active") != "active":
            continue
        repository, number = pr["repository"], int(pr["number"])
        try:
            snap, gates = await refresh(repository, number)
            blocking = [g for g in gates if g.get("status") != "PASS"]
            session = store.session(pr["session_id"])
            if session and session.get("root_session") != pr["session_id"]:
                session = store.session(session["root_session"])
            sid = session["session_id"] if session else pr["session_id"]
            recovery = store.recovery(repository, number, sid) or {}
            from .handoff import current_hold as unresolved_hold, evidence_fingerprint
            if unresolved_hold(store, sid, snap, gates, repository=repository, number=number):
                result["deferred"].append({"repository": repository, "number": number, "session_id": sid,
                                          "reason": "reported unresolved evidence unchanged; no redispatch"})
                continue
            recovery = store.recovery(repository, number, sid) or {}
            from .external import current_hold, publication_ready
            if current_hold(store, sid, snap, gates) and publication_ready(store, sid, repository, number):
                result["deferred"].append({"repository": repository, "number": number, "session_id": sid,
                                          "reason": "verified external account failure held unresolved; no unchanged retry"})
                continue
            if not blocking:
                if recovery:
                    store.record_recovery(repository, number, sid, {"state": "verified", "verified_at": now, "head_sha": snap.get("head_sha"), "lease_until": 0, "attempts": 0})
                continue
            if snap.get("merged") is True or str(snap.get("state", "")).lower() in {"closed", "merged"}:
                result["deferred"].append({"repository": repository, "number": number, "session_id": sid,
                                          "reason": "closed or merged PR retains unresolved gates; no source-repair redispatch"})
                continue
            # Merely running checks require polling; an agent cannot fix elapsed time.
            actionable = [g for g in blocking if g.get("status") == "FAIL" and g.get("gate") in {"PR_NOT_DRAFT", "CI_FAILED", "REVIEW_HANDLED", "REVIEW_CONCLUDED", "MERGE_CONFLICT", "BRANCH_UP_TO_DATE", "ISSUE_CLOSURE"}]
            if (repository, number) in due_targets:
                continue  # The scoped due followup also owns this PR remediation.
            if not actionable:
                result["deferred"].append({"repository": repository, "number": number, "reason": "pending checks or unavailable evidence; continue polling"})
                continue
            fingerprint = evidence_fingerprint(snap, gates)
            same = recovery.get("fingerprint") == fingerprint
            attempts = int(recovery.get("attempts", 0)) if same else 0
            ident = {"kind": "pr_recovery", "repository": repository, "number": number, "session_id": sid}
            if float(recovery.get("lease_until") or 0) > now:
                result["deferred"].append(dict(ident, reason="durable PR recovery lease remains active"))
                continue
            if attempts >= max_attempts:
                result["errors"].append(dict(ident, error=recovery.get("error") or "bounded PR recovery attempts exhausted", required_action="repair the failure and explicitly retry the recovery"))
                continue
            if same and float(recovery.get("next_attempt_at") or 0) > now:
                result["deferred"].append(dict(ident, reason="durable PR recovery cooldown"))
                continue
            if not session:
                raise ValueError("owning PR session metadata is missing")
            if now - float(session.get("updated_at", now)) < SESSION_INACTIVITY:
                result["deferred"].append(dict(ident, reason="owning session has recent activity"))
                continue
            client, cwd = _client(session["client"]), session.get("cwd")
            if not cwd or not Path(cwd).is_dir():
                raise ValueError("owning PR checkout/cwd is missing")
            uuid.UUID(sid)
            baseline = store.pr_scope(sid, repository, number) if hasattr(store, 'pr_scope') else store.get_scope(sid)
            scope_text = json.dumps(scope_value(baseline), sort_keys=True, ensure_ascii=False) if baseline else render_prompt('recovery_scope_unknown')
            prompt = (
                render_prompt('pr_recovery', repository=repository, number=number, session_id=sid, owner_cwd=cwd, scope_json=scope_text, gate_json=json.dumps(blocking, sort_keys=True, ensure_ascii=False))
            )
            args, input_text = resume_command(client, sid, prompt)
            action = dict(ident, id=f"pr:{repository}:{number}:{sid}", client=client, cwd=cwd, argv=args, input=input_text, prompt=prompt,
                          delivery="queue_existing_thread" if client == "codex" else "resume_idle_session")
            if not execute:
                result["actions"].append(action)
                continue
            if client == "claude" and await _call(availability or claude_availability, sid, cwd) != "idle":
                store.record_recovery(repository, number, sid, {"fingerprint": fingerprint, "attempts": attempts, "error": "owning Claude session is busy or unknown", "next_attempt_at": now + RETRY_SECONDS})
                result["deferred"].append(dict(ident, reason="owning Claude session is busy or unknown; no interruption"))
                continue
            # tick's file lock serializes reservation and dispatch across processes.
            store.record_recovery(repository, number, sid, {"fingerprint": fingerprint, "attempts": attempts + 1, "state": "dispatching", "dispatch_at": now,
                                                          "lease_until": now + max(lease_seconds, worker_timeout + 60), "next_attempt_at": now + RECOVERY_COOLDOWN})
            try:
                if client == "claude" and await _call(availability or claude_availability, sid, cwd) != "idle":
                    store.record_recovery(repository, number, sid, {"state": "pending", "lease_until": 0, "attempts": attempts, "error": "owning Claude session became busy", "next_attempt_at": now + RETRY_SECONDS})
                    result["deferred"].append(dict(ident, reason="owning Claude session became busy"))
                    continue
                if runner is not None:
                    done = await _call(runner, args, cwd=cwd, input_text=input_text, timeout=20 if client == "codex" else worker_timeout)
                else:
                    done = await asyncio.to_thread(run_process, args, cwd=cwd, input_text=input_text, timeout=20 if client == "codex" else worker_timeout)
                if int(done.get("returncode", 1)) != 0:
                    raise RuntimeError(f"{client} PR recovery worker failed: {done.get('stderr') or done.get('stdout') or 'no diagnostics'}")
                latest = store.recovery(repository, number, sid) or {}
                fields = {"runner_exit": 0, "error": None, "acknowledged_at": clock()}
                if latest.get("state") != "held_unresolved":
                    fields["state"] = "awaiting_verification"
                store.record_recovery(repository, number, sid, fields)
                result["actions"].append(dict(action, dispatched=True))
            except Exception as exc:
                store.record_recovery(repository, number, sid, {"state": "failed" if attempts + 1 >= max_attempts else "pending", "lease_until": 0, "error": str(exc)})
                result["errors"].append(dict(ident, error=str(exc)))
        except Exception as exc:
            error = {"repository": repository, "number": number, "error": str(exc)}
            result["errors"].append(error)
            if 'session metadata' in str(exc) or 'cwd' in str(exc) or 'checkout' in str(exc):
                store.record_recovery(repository, number, pr["session_id"], {"state": "failed", "error": str(exc), "next_attempt_at": now + RECOVERY_COOLDOWN})
            store.audit("supervisor_refresh_failed", error)

    for job in store.followups():
        if job.get("status") == "complete" or _epoch(job["due_at"]) > now:
            continue
        jid = job["id"]
        try:
            snapshot, gates = await refresh(job["repository"], int(job.get("issue_number") or job["number"]), bool(job.get("issue_number")))
            current_scope = recorded_job_scope(store, job)
            verified, reason = _verified(job, snapshot, gates, current_scope)
            if verified:
                complete, reason = complete_verified_followup(store, job, snapshot, gates, now=now)
                if complete:
                    result["completed"].append(jid)
                else:
                    result["deferred"].append({"id": jid, "reason": reason})
                continue
            read_verified, _ = _read_verified(job, snapshot, current_scope)
            if not job.get("issue_number") and read_verified:
                from .handoff import current_hold as unresolved_hold
                held = unresolved_hold(store, job['session_id'], snapshot, gates,
                                       repository=job['repository'], number=job['number'])
                if held or snapshot.get("merged") is True or str(snapshot.get("state", "")).lower() in {"closed", "merged"}:
                    result["deferred"].append({"id": jid, "reason": "due reread verified; unresolved PR handoff retained without redispatch"})
                    continue
            from .external import held_followup
            if held_followup(store, job['session_id'], job) and publication_ready(store, job['session_id'], job['repository'], job['number']):
                result["deferred"].append({"id": jid, "reason": "due reread and reviews verified; external account failure held unresolved"})
                continue
            fields = job.get("fields") or {}
            acknowledged_queue = fields.get("delivery") == "queue_existing_thread" and fields.get("acknowledged_at") is not None
            if acknowledged_queue:
                receipt = fields.get("delivery_receipt") or {}
                queued_hash = fields.get("queued_prompt_hash")
                if not queued_hash and isinstance(fields.get("prompt"), str):
                    queued_hash = hashlib.sha256(fields["prompt"].encode()).hexdigest()
                consumed = (queued_hash and receipt.get("prompt_hash") == queued_hash
                            and receipt.get("session_id") == job["session_id"]
                            and receipt.get("dispatch_at") == fields.get("dispatch_at"))
                if not consumed:
                    result["deferred"].append({"id": jid, "reason": "queued Codex followup awaits owner consumption", "verification": reason})
                    continue
                owner = store.session(job["session_id"])
                last_activity = max(float((owner or {}).get("updated_at") or 0), float(receipt.get("consumed_at") or 0))
                if now - last_activity < SESSION_INACTIVITY:
                    result["deferred"].append({"id": jid, "reason": "owning Codex session has recent activity after prompt consumption", "verification": reason})
                    continue
            if job.get("status") == "running" and float(job.get("lease_until") or 0) > now:
                result["deferred"].append({"id": jid, "reason": "worker lease remains active", "verification": reason})
                continue
            if job.get("status") == "failed" or int(job.get("attempts", 0)) >= max_attempts:
                result["errors"].append({"id": jid, "error": fields.get("error") or "bounded followup attempts exhausted", "required_action": "repair the failure and explicitly retry the durable job"})
                continue
            if float(fields.get("next_attempt_at") or 0) > now:
                result["deferred"].append({"id": jid, "reason": "durable retry backoff"})
                continue
            session = store.session(job["session_id"])
            if not session:
                raise ValueError("owning session metadata is missing")
            session = dict(session, session_id=job["session_id"])
            client = _client(session.get("client", ""))
            cwd = session.get("cwd")
            if not cwd or not Path(cwd).is_dir():
                raise ValueError("owning checkout/cwd is missing")
            uuid.UUID(job["session_id"])
            if scope_digest(current_scope) != scope_digest(job.get("scope")):
                raise ValueError("approved scope or slice count changed; user action required")
            prompt = scoped_prompt(job, session, current_scope, gates)
            args, input_text = resume_command(client, job["session_id"], prompt)
            action = {"id": jid, "session_id": job["session_id"], "client": client, "cwd": cwd,
                      "repository": job["repository"], "number": job["number"], "issue_number": job.get("issue_number"),
                      "prompt": prompt, "argv": args, "input": input_text, "reason": reason,
                      "delivery": "queue_existing_thread" if client == "codex" else "resume_idle_session"}
            if not execute:
                result["actions"].append(action)
                continue
            if client == "claude":
                status = await _call(availability or claude_availability, job["session_id"], cwd)
                if status != "idle":
                    store.update_followup(jid, "pending", fields=dict(fields, error=f"owning Claude session is {status}", next_attempt_at=now + RETRY_SECONDS, lease_token=job.get("lease_token")))
                    result["deferred"].append({"id": jid, "reason": f"owning Claude session is {status}; no interruption"})
                    continue
            claimed = store.claim_followup(jid, lease_seconds=max(lease_seconds, worker_timeout + 60))
            if not claimed:
                result["deferred"].append({"id": jid, "reason": "another worker owns the dispatch lease"})
                continue
            token = claimed["lease_token"]
            if client == "codex":
                prompt = scoped_prompt(dict(job, dispatch_id=token), session, current_scope, gates)
                args, input_text = resume_command(client, job["session_id"], prompt)
                action.update(prompt=prompt, argv=args, input=input_text)
            store.update_followup(jid, "running", fields={"lease_token": token, "dispatch_at": now, "delivery": action["delivery"], "prompt": prompt,
                                                          "queued_prompt_hash": hashlib.sha256(prompt.encode()).hexdigest() if client == "codex" else None,
                                                          "acknowledged_at": None, "delivery_receipt": None, "runner_exit": None})
            # Recheck Claude immediately before launch. Never interrupt a busy thread.
            if client == "claude" and await _call(availability or claude_availability, job["session_id"], cwd) != "idle":
                store.update_followup(jid, "pending", fields={"lease_token": token, "error": "owning Claude session became busy", "next_attempt_at": now + RETRY_SECONDS})
                result["deferred"].append({"id": jid, "reason": "owning Claude session became busy"})
                continue
            try:
                if runner is not None:
                    done = await _call(runner, args, cwd=cwd, input_text=input_text, timeout=20 if client == "codex" else worker_timeout)
                else:
                    done = await asyncio.to_thread(run_process, args, cwd=cwd, input_text=input_text, timeout=20 if client == "codex" else worker_timeout)
                if int(done.get("returncode", 1)) != 0:
                    raise RuntimeError(f"{client} worker failed: {done.get('stderr') or done.get('stdout') or 'no diagnostics'}")
                store.update_followup(jid, "running", fields={"lease_token": token, "runner_exit": 0, "error": None, "acknowledged_at": clock()})
                result["actions"].append(dict(action, dispatched=True))
                # Even Claude exit zero stays running until a subsequent live tick
                # verifies a receipt written by review-read and every separate gate.
                store.audit("supervisor_dispatched", {"id": jid, "client": client, "session_id": job["session_id"]})
            except Exception as exc:
                state = "failed" if int(claimed.get("attempts", 0)) >= max_attempts else "pending"
                store.update_followup(jid, state, fields={"lease_token": token, "error": str(exc), "next_attempt_at": clock() + RETRY_SECONDS})
                result["errors"].append({"id": jid, "error": str(exc)})
        except Exception as exc:
            fields = dict(job.get("fields") or {}, error=str(exc), next_attempt_at=now + RETRY_SECONDS, lease_token=job.get("lease_token"))
            store.update_followup(jid, "failed" if any(word in str(exc) for word in ("scope", "metadata", "cwd", "checkout", "unsupported", "UUID")) else "pending", fields=fields)
            error = {"id": jid, "error": str(exc)}
            result["errors"].append(error)
            store.audit("supervisor_followup_failed", error)
    store.audit("supervisor_tick", {"refreshed": len(result["refreshed"]), "completed": result["completed"], "errors": len(result["errors"]), "execute": execute})
    return result


async def tick(store: Any = None, **kwargs: Any) -> dict:
    """Only one local heartbeat tick may reserve/dispatch PR recovery workers."""
    if store is None:
        from .store import Store
        store = Store()
    lock_path = Path(store.state_dir) / "supervisor.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"at": kwargs.get("clock", time.time)(), "execute": kwargs.get("execute", False), "refreshed": [], "actions": [], "completed": [],
                    "deferred": [{"reason": "another supervisor tick owns the local dispatch lock"}], "errors": []}
        try:
            return await _tick(store, **kwargs)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
