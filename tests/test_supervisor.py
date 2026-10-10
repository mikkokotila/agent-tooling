import asyncio
import hashlib
from pathlib import Path
import sys
import time
import uuid

import pytest

from caller_pr_gates import supervisor as sup
from caller_pr_gates.store import Store

SCOPE = {"agreed_scope": "Repair the agreed login timeout only", "acceptance_criteria": ["retry once"], "slice_count": 1, "slice_ids": ["S1"], "approved_at": 0.0}
SID = "21a641cb-7e14-4cd2-b97d-8cdba8fe74d5"


@pytest.fixture
def state(tmp_path, monkeypatch):
    now = [2000.0]
    monkeypatch.setattr("caller_pr_gates.store.time.time", lambda: now[0])
    store = Store(tmp_path / "state")
    store.record_session(SID, "codex", str(tmp_path))
    store.record_scope(SID, SCOPE)
    job = store.register_followup(SID, "Owner/Repo", 12, 0, SCOPE)
    return store, job, now, tmp_path


def snapshot(now, comments=None):
    return {"head_sha": "abc123", "collected_at": now, "comments": comments or []}


def pass_gates(*args):
    return [{"gate": name, "status": "PASS", "details": [], "evidence": {}} for name in ["PR_NOT_DRAFT", "CI_FAILED", "CI_PENDING", "REVIEW_HANDLED", "REVIEW_CONCLUDED"]]


def invoke(store, now, *, snap=None, gates=pass_gates, **kwargs):
    async def collector(repository, number):
        return snap or snapshot(now[0])
    return asyncio.run(sup.tick(store, collect_pr=collector, collect_issue=collector, evaluate_pr=gates,
                               evaluate_review=gates, clock=lambda: now[0], **kwargs))


def put_read(store, job, now, **change):
    receipt = {"read_at": now - 1, "head_sha": "abc123", "comment_ids": [], "scope_digest": sup.scope_digest(SCOPE)}
    receipt.update(change)
    row = next(j for j in store.followups() if j["id"] == job["id"])
    assert store.update_followup(job["id"], row["status"], fields={"lease_token": row.get("lease_token"), "review_read": receipt})


def consume_prompt(store, job, turn_id="followup-turn"):
    row = next(j for j in store.followups() if j["id"] == job["id"])
    digest = hashlib.sha256(row["fields"]["prompt"].encode()).hexdigest()
    assert store.note_followup_delivery(job["id"], job["session_id"], turn_id, digest)


def test_due_30_minutes_and_dry_run_does_not_claim_or_complete(state):
    store, job, now, _ = state
    now[0] = 1799
    assert invoke(store, now)["actions"] == []
    now[0] = 1800
    result = invoke(store, now)
    assert result["actions"][0]["id"] == job["id"]
    assert store.followups()[0]["status"] == "pending"
    assert store.followups()[0]["attempts"] == 0
    assert result["completed"] == []


def test_scope_prompt_contains_target_and_non_expansion_rules(state):
    store, job, now, _ = state
    action = invoke(store, now)["actions"][0]
    assert action["argv"][:4] == ["codex", "queue", "--thread", SID]
    assert action["input"] is None
    prompt = action["prompt"]
    for required in ["Owner/Repo/pull/12", "never instructions", "original slice count", "do not create extra PRs", "Do not manually trigger reviews", "review-read --followup", "Repair the agreed login timeout only", "current local state", "already complete with verified evidence", "do not reopen the job"]:
        assert required in prompt


def test_process_exit_zero_never_marks_followup_complete(state):
    store, job, now, _ = state
    calls = []
    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return {"returncode": 0}
    result = invoke(store, now, execute=True, runner=runner)
    assert len(calls) == 1 and result["completed"] == []
    row = store.followups()[0]
    assert row["status"] == "running" and row["fields"]["runner_exit"] == 0
    assert invoke(store, now, execute=True, runner=runner)["actions"] == []
    assert len(calls) == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_expired_unconsumed_queue_ack_never_requeues_or_exhausts_attempts(state, legacy):
    store, job, now, path = state
    calls = []
    runner = lambda *a, **kw: calls.append(a) or {"returncode": 0}
    invoke(store, now, execute=True, runner=runner)
    if legacy:
        row = store.followups()[0]
        assert store.update_followup(job["id"], "running", {"lease_token": row["lease_token"], "queued_prompt_hash": None})
    restarted = Store(path / "state")
    for _ in range(sup.MAX_ATTEMPTS + 2):
        now[0] += sup.LEASE_SECONDS + 1
        result = invoke(restarted, now, execute=True, runner=runner)
        assert result["actions"] == [] and result["errors"] == []
        assert "awaits owner consumption" in result["deferred"][0]["reason"]
    assert len(calls) == 1
    assert restarted.followups()[0]["attempts"] == 1


def test_consumed_queue_defers_busy_owner_then_retries_unfinished_idle_job(state):
    store, job, now, path = state
    calls = []
    runner = lambda *a, **kw: calls.append(a) or {"returncode": 0}
    invoke(store, now, execute=True, runner=runner)
    first_prompt = store.followups()[0]["fields"]["prompt"]
    now[0] += sup.LEASE_SECONDS + 1
    store.record_session(SID, "codex", str(path))
    consume_prompt(store, job)
    result = invoke(store, now, execute=True, runner=runner)
    assert "recent activity" in result["deferred"][0]["reason"]
    assert len(calls) == 1 and store.followups()[0]["attempts"] == 1
    now[0] += sup.SESSION_INACTIVITY + 1
    result = invoke(store, now, execute=True, runner=runner)
    row = store.followups()[0]
    assert len(calls) == 2 and row["attempts"] == 2 and result["completed"] == []
    assert row["fields"]["prompt"] != first_prompt
    assert row["fields"]["delivery_receipt"] is None
    assert not store.note_followup_delivery(job["id"], SID, "old-turn", hashlib.sha256(first_prompt.encode()).hexdigest())


def test_explicit_retry_dispatches_after_unconsumed_ack_without_stealing_live_lease(state):
    store, job, now, _ = state
    calls = []
    runner = lambda *a, **kw: calls.append(a) or {"returncode": 0}
    invoke(store, now, execute=True, runner=runner)
    old_prompt = store.followups()[0]["fields"]["prompt"]
    assert not store.retry_followup(job["id"])
    now[0] += sup.LEASE_SECONDS + 1
    assert "awaits owner consumption" in invoke(store, now, execute=True, runner=runner)["deferred"][0]["reason"]
    assert len(calls) == 1
    assert store.retry_followup(job["id"])
    result = invoke(store, now, execute=True, runner=runner)
    row = store.followups()[0]
    assert len(calls) == 2 and len(result["actions"]) == 1
    assert row["attempts"] == 1 and row["status"] == "running"
    assert result["completed"] == [] and row["fields"]["prompt"] != old_prompt
    assert not store.note_followup_delivery(job["id"], SID, "old-turn", hashlib.sha256(old_prompt.encode()).hexdigest())


def test_complete_job_is_never_dispatched_despite_old_unconsumed_ack(state):
    store, job, now, _ = state
    invoke(store, now, execute=True, runner=lambda *a, **kw: {"returncode": 0})
    put_read(store, job, now[0])
    assert invoke(store, now)["completed"] == [job["id"]]
    now[0] += sup.LEASE_SECONDS + 1
    result = invoke(store, now, execute=True, runner=lambda *a, **kw: pytest.fail("completed job must never dispatch"))
    assert result["actions"] == [] and result["errors"] == []


def test_consumption_and_reread_do_not_waive_unresolved_gates(state):
    store, job, now, _ = state
    invoke(store, now, execute=True, runner=lambda *a, **kw: {"returncode": 0})
    consume_prompt(store, job)
    put_read(store, job, now[0])
    result = invoke(store, now, gates=lambda *a: pass_gates() + [{"gate": "REVIEW_CONCLUDED", "status": "FAIL"}])
    assert result["completed"] == []
    assert store.followups()[0]["status"] == "running"
    assert "gates remain unfinished" in result["deferred"][0]["verification"]


def test_real_reread_and_passing_gates_complete_after_restart(state):
    store, job, now, path = state
    put_read(store, job, now[0])
    restarted = Store(path / "state")
    result = invoke(restarted, now)
    assert result["completed"] == [job["id"]]
    assert restarted.followups()[0]["status"] == "complete"


@pytest.mark.parametrize("receipt_change", [
    {"read_at": 1700}, {"read_at": 2100}, {"head_sha": "previous"},
    {"scope_digest": "wrong"}, {"comment_ids": []},
])
def test_stale_or_incomplete_reread_cannot_complete(state, receipt_change):
    store, job, now, _ = state
    put_read(store, job, now[0], **receipt_change)
    comments = [{"id": "c1", "updated_at": 1900}]
    result = invoke(store, now, snap=snapshot(now[0], comments))
    assert result["completed"] == []
    assert store.followups()[0]["status"] != "complete"


def test_edited_comment_invalidates_reread_even_same_id(state):
    store, job, now, _ = state
    put_read(store, job, now[0], comment_ids=["c1"])
    snap = snapshot(now[0], [{"id": "c1", "updated_at": 1999.5}])
    assert invoke(store, now, snap=snap)["completed"] == []


def test_failed_individual_gate_blocks_completion(state):
    store, job, now, _ = state
    put_read(store, job, now[0])
    def failed(*args):
        return pass_gates() + [{"gate": "CI_PENDING", "status": "WAIT"}]
    assert invoke(store, now, gates=failed)["completed"] == []


def test_expired_lease_is_reclaimed_before_receipt_completion(state):
    store, job, now, _ = state
    claimed = store.claim_followup(job["id"], 1)
    put_read(store, claimed, now[0])
    now[0] += 2
    assert invoke(store, now)["completed"] == [job["id"]]


def test_runner_failure_persists_retry_and_bounded_attempts(state):
    store, job, now, _ = state
    def runner(*args, **kwargs):
        raise TimeoutError("timeout")
    for attempt in range(3):
        result = invoke(store, now, execute=True, runner=runner)
        assert result["errors"][0]["error"] == "timeout"
        now[0] += sup.RETRY_SECONDS + 1
    row = store.followups()[0]
    assert row["status"] == "failed" and row["attempts"] == 3
    assert invoke(store, now, execute=True, runner=runner)["actions"] == []


def test_missing_session_and_checkout_become_durable_failures(state):
    store, job, now, path = state
    store.record_session(SID, "codex", str(path / "missing"))
    result = invoke(store, now)
    assert "cwd" in result["errors"][0]["error"]
    assert store.followups()[0]["status"] == "failed"


def test_busy_claude_is_not_resumed(state):
    store, job, now, path = state
    # Existing client identity is intentionally immutable in Store.
    other = str(uuid.uuid4())
    store.record_session(other, "claude", str(path))
    store.record_scope(other, SCOPE)
    cjob = store.register_followup(other, "Owner/Repo", 14, 0, SCOPE)
    calls = []
    result = invoke(store, now, execute=True, availability=lambda *a: "busy", runner=lambda *a, **kw: calls.append(a) or {"returncode": 0})
    crow = next(j for j in store.followups() if j["id"] == cjob["id"])
    assert crow["status"] == "pending" and crow["attempts"] == 0
    assert any("busy" in d["reason"] for d in result["deferred"])
    assert len(calls) == 1  # Only the independent Codex job was queued.


def test_idle_claude_uses_own_id_and_stdin_then_stays_unfinished(state):
    store, job, now, path = state
    other = str(uuid.uuid4())
    store.record_session(other, "claude", str(path))
    store.record_scope(other, SCOPE)
    cjob = store.register_followup(other, "Owner/Repo", 14, 0, SCOPE)
    calls = []
    result = invoke(store, now, execute=True, availability=lambda *a: "idle", runner=lambda *a, **kw: calls.append((a, kw)) or {"returncode": 0})
    command, opts = next(item for item in calls if item[0][0][0] == "claude")
    assert command[0] == ["claude", "--print", "--resume", other, "--output-format", "json"]
    assert "Owner/Repo/pull/14" in opts["input_text"]
    assert opts["timeout"] == sup.WORKER_TIMEOUT
    assert result["completed"] == []


def test_issue_followup_uses_issue_collector_and_review_only(state):
    store, job, now, path = state
    ijob = store.register_followup(SID, "Owner/Repo", 17, 0, SCOPE, issue_number=17)
    calls = []
    async def issue(repo, number):
        calls.append(number)
        return snapshot(now[0])
    result = asyncio.run(sup.tick(store, collect_pr=lambda *a: snapshot(now[0]), collect_issue=issue,
                                 evaluate_pr=pass_gates, evaluate_review=lambda *a: pass_gates()[3:], clock=lambda: now[0]))
    assert calls == [17]
    action = next(a for a in result["actions"] if a["id"] == ijob["id"])
    assert "Owner/Repo/issues/17" in action["prompt"]


def test_pr_refresh_failure_is_visible_and_does_not_drop_registry(state):
    store, job, now, path = state
    store.register(SID, "codex", "Owner/Repo", 12, str(path))
    def collector(*a):
        raise RuntimeError("GitHub unavailable")
    result = asyncio.run(sup.tick(store, collect_pr=collector, collect_issue=collector,
                                 evaluate_pr=pass_gates, evaluate_review=pass_gates, clock=lambda: now[0]))
    assert len(result["errors"]) == 2
    assert store.prs()[0]["number"] == 12
    assert store.followups()[0]["status"] == "pending"


def test_process_timeout_terminates_worker_group(tmp_path):
    with pytest.raises(TimeoutError):
        sup.run_process([sys.executable, "-c", "import time;time.sleep(10)"], cwd=str(tmp_path), timeout=0.02)


def test_claude_inventory_unknown_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(sup, "run_process", lambda *a, **kw: {"returncode": 0, "stdout": "not-json"})
    assert sup.claude_availability(SID, str(tmp_path)) == "unknown"


def test_scope_digest_ignores_only_bookkeeping_not_slice_count():
    assert sup.scope_digest(SCOPE) == sup.scope_digest(dict(SCOPE, updated_at=2000))
    assert sup.scope_digest(SCOPE) != sup.scope_digest(dict(SCOPE, slice_count=2))


@pytest.fixture
def pr_state(tmp_path, monkeypatch):
    now = [2000.0]
    monkeypatch.setattr("caller_pr_gates.store.time.time", lambda: now[0])
    store = Store(tmp_path / "registry")
    store.register(SID, "codex", "Owner/Repo", 12, str(tmp_path))
    now[0] = 2400.0
    return store, now, tmp_path


def failed_ci(*a):
    return [dict(g, status="FAIL", details=["linux unit failed"]) if g["gate"] == "CI_FAILED" else g for g in pass_gates()]


def test_ordinary_pr_recovery_queues_existing_task_after_inactivity(pr_state):
    store, now, path = pr_state
    calls = []
    result = invoke(store, now, gates=failed_ci, execute=True, runner=lambda *a, **kw: calls.append((a, kw)) or {"returncode": 0})
    assert len(calls) == 1
    assert calls[0][0][0][:4] == ["codex", "queue", "--thread", SID]
    assert "original human-approved task scope" in result["actions"][0]["prompt"]
    row = store.recovery("Owner/Repo", 12, SID)
    assert row["attempts"] == 1 and row["state"] == "awaiting_verification"
    assert result["completed"] == []


def test_ordinary_pr_ci_pending_only_polls(pr_state):
    store, now, _ = pr_state
    def pending(*a):
        return [dict(g, status="WAIT", details=["running"]) if g["gate"] == "CI_PENDING" else g for g in pass_gates()]
    assert invoke(store, now, gates=pending, execute=True, runner=lambda *a, **kw: pytest.fail("must not launch a model for elapsed CI time"))["actions"] == []
    assert store.recovery("Owner/Repo", 12, SID) is None


def test_reported_same_failure_does_not_redispatch_after_cooldown(pr_state):
    from caller_pr_gates.handoff import hold_unresolved
    store, now, _ = pr_state
    hold_unresolved(store, SID, "Owner/Repo", 12, snapshot(now[0]), failed_ci())
    now[0] += sup.LEASE_SECONDS * 4
    result = invoke(store, now, gates=failed_ci, execute=True,
                    runner=lambda *a, **kw: pytest.fail("reported unchanged blocker must remain quiet"))
    assert result["actions"] == [] and result["completed"] == []
    assert "no redispatch" in result["deferred"][0]["reason"]
    assert store.recovery("Owner/Repo", 12, SID)["state"] == "held_unresolved"
    assert store.snapshot("Owner/Repo", 12)["gates"][1]["status"] == "FAIL"


def test_new_failed_attempt_releases_reported_hold(pr_state):
    from caller_pr_gates.handoff import hold_unresolved
    store, now, _ = pr_state
    first = dict(snapshot(now[0]), checks=[{"kind": "job", "key": "job:unit", "name": "unit",
        "id": "5", "run_attempt": 1, "status": "completed", "conclusion": "failure"}])
    hold_unresolved(store, SID, "Owner/Repo", 12, first, failed_ci())
    changed = dict(first, checks=[dict(first["checks"][0], run_attempt=2)])
    calls = []
    result = invoke(store, now, snap=changed, gates=failed_ci, execute=True,
                    runner=lambda *a, **kw: calls.append(a) or {"returncode": 0})
    assert len(calls) == 1 and result["actions"][0]["dispatched"]
    assert store.recovery("Owner/Repo", 12, SID)["state"] == "awaiting_verification"


@pytest.mark.parametrize("lifecycle", [{"state": "closed", "merged": True},
                                       {"state": "closed", "merged": False}])
def test_closed_pr_does_not_launch_source_repairs_or_waive_failures(pr_state, lifecycle):
    store, now, _ = pr_state
    result = invoke(store, now, snap=dict(snapshot(now[0]), **lifecycle), gates=failed_ci, execute=True,
                    runner=lambda *a, **kw: pytest.fail("must not reopen completed lifecycle for repairs"))
    assert result["actions"] == [] and result["completed"] == []
    assert "no source-repair" in result["deferred"][0]["reason"]
    assert store.prs()[0]["status"] == "active"
    assert store.snapshot("Owner/Repo", 12)["gates"][1]["status"] == "FAIL"


def test_worker_acknowledgement_preserves_reported_hold(pr_state):
    from caller_pr_gates.handoff import hold_unresolved
    store, now, _ = pr_state
    def runner(*a, **kw):
        hold_unresolved(store, SID, "Owner/Repo", 12, snapshot(now[0]), failed_ci())
        return {"returncode": 0}
    invoke(store, now, gates=failed_ci, execute=True, runner=runner)
    row = store.recovery("Owner/Repo", 12, SID)
    assert row["state"] == "held_unresolved" and row["runner_exit"] == 0
    assert row["lease_until"] > now[0]


@pytest.mark.parametrize("read_due", [False, True])
def test_due_pr_handoff_preserves_required_reread(state, read_due):
    from caller_pr_gates.handoff import hold_unresolved
    store, job, now, path = state
    store.register(SID, "codex", "Owner/Repo", 12, str(path))
    if read_due:
        put_read(store, job, now[0])
    hold_unresolved(store, SID, "Owner/Repo", 12, snapshot(now[0]), failed_ci())
    calls = []
    result = invoke(store, now, gates=failed_ci, execute=True,
                    runner=lambda *a, **kw: calls.append(a) or {"returncode": 0})
    assert len(calls) == (0 if read_due else 1)
    assert result["completed"] == []
    assert store.followups()[0]["status"] != "complete"
    assert store.get_scope(SID)["slice_count"] == SCOPE["slice_count"]


def test_recent_session_activity_defers_pr_recovery(pr_state):
    store, now, path = pr_state
    store.record_session(SID, "codex", str(path))
    result = invoke(store, now, gates=failed_ci, execute=True, runner=lambda *a, **kw: pytest.fail("busy owner"))
    assert "recent activity" in result["deferred"][0]["reason"]


def test_pr_recovery_lease_and_cooldown_survive_restart(pr_state):
    store, now, path = pr_state
    calls = []
    runner = lambda *a, **kw: calls.append(a) or {"returncode": 0}
    invoke(store, now, gates=failed_ci, execute=True, runner=runner)
    restarted = Store(path / "registry")
    assert invoke(restarted, now, gates=failed_ci, execute=True, runner=runner)["actions"] == []
    assert len(calls) == 1
    now[0] += sup.LEASE_SECONDS + 1
    invoke(restarted, now, gates=failed_ci, execute=True, runner=runner)
    assert len(calls) == 2


def test_pr_recovery_bounded_attempts_and_failure_evidence(pr_state):
    store, now, _ = pr_state
    def runner(*a, **kw):
        return {"returncode": 1, "stderr": "resume session absent"}
    for attempt in range(3):
        result = invoke(store, now, gates=failed_ci, execute=True, runner=runner)
        assert "resume session absent" in result["errors"][0]["error"]
        now[0] += sup.RECOVERY_COOLDOWN + 1
    result = invoke(store, now, gates=failed_ci, execute=True, runner=lambda *a, **kw: pytest.fail("attempt budget exhausted"))
    assert result["errors"][0]["required_action"]
    assert store.recovery("Owner/Repo", 12, SID)["attempts"] == 3


def test_current_all_gates_verify_pr_recovery_without_agent_exit(pr_state):
    store, now, _ = pr_state
    invoke(store, now, gates=failed_ci, execute=True, runner=lambda *a, **kw: {"returncode": 0})
    result = invoke(store, now, gates=pass_gates)
    assert result["actions"] == []
    assert store.recovery("Owner/Repo", 12, SID)["state"] == "verified"


def test_subagent_pr_recovery_resumes_logical_parent_owner(pr_state):
    store, now, path = pr_state
    child = str(uuid.uuid4())
    now[0] = 2000
    store.register(child, "codex", "Owner/Repo", 13, str(path), parent_session=SID)
    now[0] = 2400
    result = invoke(store, now, gates=failed_ci)
    action = next(a for a in result["actions"] if a["number"] == 13)
    assert action["session_id"] == SID


def test_parallel_ticks_cannot_reserve_two_recovery_workers(pr_state):
    store, now, _ = pr_state
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        async def collector(*a):
            entered.set()
            await release.wait()
            return snapshot(now[0])
        kwargs = dict(collect_pr=collector, collect_issue=collector, evaluate_pr=failed_ci, evaluate_review=failed_ci, clock=lambda: now[0])
        first = asyncio.create_task(sup.tick(store, **kwargs))
        await entered.wait()
        second = await sup.tick(store, **kwargs)
        assert second["actions"] == []
        assert "dispatch lock" in second["deferred"][0]["reason"]
        release.set()
        await first
    asyncio.run(run())


def test_issue_receipt_uses_current_issue_revision_with_real_review_policy(state):
    from caller_pr_gates.policy import evaluate_review
    store, job, now, _ = state
    issue = store.register_followup(SID, "Owner/Repo", 17, 0, SCOPE, issue_number=17)
    revision = "1970-01-01T00:31:40Z"
    put_read(store, issue, now[0], head_sha=revision)
    snap = {"collected_at": now[0], "comments": [], "updated_at": revision, "author_login": "owner", "actor_login": "owner"}
    result = asyncio.run(sup.tick(store, collect_pr=lambda *a: snapshot(now[0]), collect_issue=lambda *a: snap,
                                 evaluate_pr=pass_gates, evaluate_review=evaluate_review, clock=lambda: now[0]))
    assert issue["id"] in result["completed"]


def test_last_followup_attempt_receives_full_lease_before_exhaustion(state):
    store, job, now, _ = state
    for i in range(3):
        invoke(store, now, execute=True, runner=lambda *a, **kw: {"returncode": 0})
        consume_prompt(store, job)
        if i < 2:
            now[0] += sup.LEASE_SECONDS + 1
    now[0] += sup.SESSION_INACTIVITY + 1
    result = invoke(store, now)
    assert result["errors"] == []
    assert "lease remains active" in result["deferred"][0]["reason"]


def test_missing_owning_session_metadata_is_persisted(state):
    store, job, now, _ = state
    with store._connect() as db:
        db.execute("DELETE FROM sessions WHERE session_id=?", (SID,))
    result = invoke(store, now)
    assert "metadata is missing" in result["errors"][0]["error"]
    assert store.followups()[0]["status"] == "failed"


def test_process_timeout_kills_grandchild_not_only_parent(tmp_path):
    marker = tmp_path / "orphan-marker"
    child = "import time,pathlib;time.sleep(.2);pathlib.Path(" + repr(str(marker)) + ").write_text('orphan')"
    parent = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);time.sleep(10)"
    with pytest.raises(TimeoutError):
        sup.run_process([sys.executable, "-c", parent], cwd=str(tmp_path), timeout=0.08)
    time.sleep(.3)
    assert not marker.exists()


def test_stop_can_complete_due_followup_from_its_fresh_snapshot(state):
    store, job, now, _ = state
    put_read(store, job, now[0])
    current = store.followups()[0]
    completed, reason = sup.complete_verified_followup(store, current, snapshot(now[0]), pass_gates(), now=now[0])
    assert completed and "live reread" in reason
    assert store.followups()[0]["status"] == "complete"


def test_stop_completion_helper_rejects_unhandled_comment(state):
    store, job, now, _ = state
    put_read(store, job, now[0])
    gates = pass_gates() + [{"gate": "REVIEW_CONCLUDED", "status": "FAIL"}]
    completed, reason = sup.complete_verified_followup(store, store.followups()[0], snapshot(now[0]), gates, now=now[0])
    assert not completed and "gates remain unfinished" in reason
