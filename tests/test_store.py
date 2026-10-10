from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from caller_pr_gates.store import Store


def scope(count=2):
    return {"agreed_scope": "Implement existing authentication proposal", "acceptance_criteria": ["Existing sessions continue"], "slice_count": count, "slice_ids": [f"AUTH-{i + 1}" for i in range(count)], "approved_at": 100.0}


def worker_register(args):
    state_dir, index = args
    store = Store(state_dir)
    store.register(f"child-{index}", "claude", "org/repo", index + 1, "/project", parent_session="root")
    return index


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.record_session("root", "codex", "/project")

    def test_exact_parent_ownership_covers_descendants_both_clients_and_restart(self):
        self.store.register("leaf", "claude", "org/repo", 3, "/project", parent_session="child")
        self.store.register("root", "codex", "org/repo", 1, "/project")
        self.store.register("child", "codex", "org/repo", 2, "/different", parent_session="root")
        self.store.register("unrelated", "claude", "org/repo", 4, "/project")
        restarted = Store(self.tmp.name)
        for session_id in ("root", "child", "leaf"):
            self.assertEqual({row["number"] for row in restarted.prs(session_id)}, {1, 2, 3})
        self.assertEqual({row["number"] for row in restarted.prs("unrelated")}, {4})
        self.assertEqual(restarted.prs("nonexistent"), [])
        self.assertEqual(len(restarted.prs()), 4)
        self.assertEqual(restarted.session("leaf")["root_session"], "root")

    def test_registered_pr_not_duplicated_and_never_erased_by_receipts(self):
        first = self.store.register("root", "codex", "org/repo", 8, "/project")
        self.store.register("root", "codex", "org/repo", 8, "/other")
        self.store.register("child", "claude", "org/repo", 8, "/other", "root")
        self.assertEqual(len(self.store.prs("root")), 1)
        self.assertEqual(first["status"], "active")
        self.assertEqual(len(Store(self.tmp.name).prs()), 1)

    def test_cycles_and_reparenting_rejected_transactionally(self):
        self.store.record_session("child", "claude", "/project", "root")
        with self.assertRaises(ValueError):
            self.store.record_session("root", "codex", "/project", "child")
        self.assertIsNone(self.store.session("root")["parent_session"])
        with self.assertRaises(ValueError):
            self.store.record_session("child", "claude", "/project", "unrelated")
        self.assertEqual(self.store.session("child")["parent_session"], "root")

    def test_scope_immutable_task_baseline_survives_restart(self):
        self.store.record_session("child", "claude", "/project", "root")
        self.store.record_scope("child", scope())
        self.store.record_scope("root", scope())
        self.assertEqual(Store(self.tmp.name).get_scope("child"), scope())
        changed = scope(3)
        with self.assertRaises(ValueError):
            self.store.record_scope("child", changed)
        self.assertEqual(self.store.get_scope("root"), scope())
        with self.assertRaises(ValueError):
            self.store.record_session("root", "codex", "/project", "another-root")

    def test_unregistered_scope_cannot_later_be_attached_to_another_task(self):
        self.store.record_scope("not-recorded-session", scope())
        with self.assertRaises(ValueError):
            self.store.record_session("not-recorded-session", "codex", "/project", "root")
        self.assertEqual(self.store.get_scope("not-recorded-session"), scope())

    def test_scope_rejects_malformed_and_duplicate_slice_ids(self):
        bad = scope()
        bad["slice_ids"] = ["AUTH-1", "AUTH-1"]
        with self.assertRaises(ValueError):
            self.store.record_scope("root", bad)
        with self.assertRaises(ValueError):
            self.store.record_scope("root", {"agreed_scope": "Anything"})
        self.assertIsNone(self.store.get_scope("root"))

    def test_dispositions_revision_specific_durable_and_task_owned(self):
        self.store.register("root", "codex", "org/repo", 5, "/project")
        self.store.record_session("child", "claude", "/project", "root")
        receipt = {"comment_id": "42", "comment_updated_at": "2026-10-05T12:00:00Z", "disposition": "fixed", "commit_sha": "abc123", "reason": "Corrected token validation", "reply_url": "https://example/reply"}
        self.store.disposition("child", "org/repo", 5, receipt)
        revision = dict(receipt, comment_updated_at="2026-10-05T12:10:00Z", disposition="nonfix", reason="Requested redesign exceeds agreed scope")
        self.store.disposition("root", "org/repo", 5, revision)
        self.assertEqual(Store(self.tmp.name).dispositions("org/repo", 5), [receipt, revision])
        self.store.record_session("unrelated", "codex", "/project")
        with self.assertRaises(ValueError):
            self.store.disposition("unrelated", "org/repo", 5, receipt)
        with self.assertRaises(ValueError):
            self.store.disposition("root", "org/repo", 5, dict(receipt, commit_sha=None))

    def test_snapshot_and_audit_persist_with_gate_evidence(self):
        snapshot = {"head_sha": "abc", "number": 6}
        gates = [{"gate": "CI_PENDING", "status": "WAIT", "details": ["build running"], "evidence": {"check_id": 9}}]
        self.store.set_snapshot("org/repo", 6, snapshot, gates)
        self.store.audit("supervisor_tick", {"owner": "test"})
        loaded = Store(self.tmp.name).snapshot("org/repo", 6)
        self.assertEqual(loaded["snapshot"], snapshot)
        self.assertEqual(loaded["gates"], gates)
        with self.store._connect() as db:
            events = [row[0] for row in db.execute("SELECT event FROM audit ORDER BY id")]
        self.assertEqual(events, ["pr_gates_evaluated", "supervisor_tick"])
        self.assertIsNone(self.store.snapshot("org/repo", 999))

    def job(self, posted_at=100):
        self.store.record_scope("root", scope())
        return self.store.register_followup("root", "org/repo", 7, posted_at, scope(), "prd")

    def test_followup_due_after_thirty_minutes_and_idempotent_post_registration(self):
        job = self.job()
        duplicate = self.job()
        self.assertEqual(job["id"], duplicate["id"])
        self.assertEqual(job["due_at"], 1900)
        with patch("caller_pr_gates.store.time.time", return_value=1899):
            self.assertEqual(self.store.followups("root", due_only=True), [])
            self.assertIsNone(self.store.claim_followup(job["id"]))
        with patch("caller_pr_gates.store.time.time", return_value=1900):
            self.assertEqual(len(self.store.followups("root", due_only=True)), 1)
            claimed = self.store.claim_followup(job["id"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["attempts"], 1)
        self.assertEqual(self.store.followups()[0]["scope"], scope())

    def test_followup_atomic_claim_concurrent_workers(self):
        job = self.job()
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: self.store.claim_followup(job["id"]), range(8)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        self.assertEqual(self.store.followups()[0]["attempts"], 1)

    def test_followup_restart_crash_recovery_rejects_stale_completion(self):
        job = self.job()
        with patch("caller_pr_gates.store.time.time", return_value=2000):
            old = self.store.claim_followup(job["id"], lease_seconds=100)
            self.assertFalse(self.store.complete_followup(job["id"], "wrong"))
            self.assertFalse(self.store.update_followup(job["id"], "complete"))
        restarted = Store(self.tmp.name)
        with patch("caller_pr_gates.store.time.time", return_value=2099):
            self.assertIsNone(restarted.claim_followup(job["id"]))
        with patch("caller_pr_gates.store.time.time", return_value=2101):
            new = restarted.claim_followup(job["id"], lease_seconds=100)
            self.assertNotEqual(new["lease_token"], old["lease_token"])
            self.assertFalse(restarted.complete_followup(job["id"], old["lease_token"]))
            self.assertFalse(restarted.renew_followup(job["id"], old["lease_token"]))
            self.assertTrue(restarted.renew_followup(job["id"], new["lease_token"], 100))
            evidence = {"review_read": {"read_at": 2101, "head_sha": "abc", "comment_ids": ["42"], "scope_digest": "digest"}}
            self.assertTrue(restarted.complete_followup(job["id"], new["lease_token"], evidence))
            self.assertTrue(restarted.complete_followup(job["id"], new["lease_token"]))
        completed = Store(self.tmp.name).followups()[0]
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["attempts"], 2)
        self.assertEqual(completed["fields"], evidence)
        self.assertEqual(self.job()["status"], "complete")
        self.assertIsNone(self.store.claim_followup(job["id"]))

    def test_running_receipt_update_requires_owner_token_and_preserves_fields(self):
        job = self.job()
        claimed = self.store.claim_followup(job["id"])
        self.assertFalse(self.store.update_followup(job["id"], "failed", {"error": "network"}))
        self.assertFalse(self.store.update_followup(job["id"], "running", {"dispatch": "sent"}))
        self.assertTrue(self.store.update_followup(job["id"], "running", {"lease_token": claimed["lease_token"], "dispatch": "sent"}))
        self.assertEqual(self.store.followups()[0]["lease_until"], claimed["lease_until"])
        self.assertTrue(self.store.update_followup(job["id"], "failed", {"lease_token": claimed["lease_token"], "error": "network"}))
        self.assertEqual(self.store.followups()[0]["fields"], {"error": "network", "dispatch": "sent"})
        claimed_again = self.store.claim_followup(job["id"])
        self.assertEqual(claimed_again["attempts"], 2)
        self.assertTrue(self.store.complete_followup(job["id"], claimed_again["lease_token"], {"review_read": {"comment_ids": []}}))
        self.assertEqual(self.store.followups()[0]["fields"]["error"], "network")

    def test_queue_consumption_survives_restart_without_changing_expired_lease(self):
        job = self.job()
        prompt = "exact machine followup"
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        with patch("caller_pr_gates.store.time.time", return_value=2000):
            claimed = self.store.claim_followup(job["id"], lease_seconds=1)
            self.assertTrue(self.store.update_followup(job["id"], "running", {
                "lease_token": claimed["lease_token"], "delivery": "queue_existing_thread",
                "prompt": prompt, "queued_prompt_hash": digest, "dispatch_at": 2000, "acknowledged_at": 2000,
            }))
        with patch("caller_pr_gates.store.time.time", return_value=2100):
            self.assertTrue(self.store.note_followup_delivery(job["id"], "root", "turn-1", digest))
        restarted = Store(self.tmp.name)
        row = restarted.followups()[0]
        self.assertEqual((row["status"], row["attempts"], row["lease_token"], row["lease_until"]),
                         ("running", 1, claimed["lease_token"], 2001))
        self.assertEqual(row["fields"]["delivery_receipt"], {
            "session_id": "root", "turn_id": "turn-1", "prompt_hash": digest,
            "consumed_at": 2100, "dispatch_at": 2000,
        })
        with patch("caller_pr_gates.store.time.time", return_value=2200):
            self.assertTrue(restarted.note_followup_delivery(job["id"], "root", "turn-2", digest))
            self.assertFalse(restarted.complete_followup(job["id"], claimed["lease_token"]))
        self.assertEqual(restarted.followups()[0]["fields"]["delivery_receipt"]["consumed_at"], 2100)
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE event='followup_delivery_consumed'").fetchone()[0], 1)

    def test_queue_consumption_requires_exact_owned_current_prompt(self):
        job = self.job()
        prompt = "legacy exact queued prompt"
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        with patch("caller_pr_gates.store.time.time", return_value=2000):
            claimed = self.store.claim_followup(job["id"])
            fields = {"lease_token": claimed["lease_token"], "delivery": "queue_existing_thread", "prompt": prompt, "dispatch_at": 2000}
            self.assertTrue(self.store.update_followup(job["id"], "running", fields))
            self.store.record_session("child", "codex", "/project", "root")
            for owner, supplied in [("child", digest), ("unrelated", digest), ("root", "a" * 64)]:
                self.assertFalse(self.store.note_followup_delivery(job["id"], owner, "turn", supplied))
            self.assertFalse(self.store.note_followup_delivery("missing", "root", "turn", digest))
            self.assertTrue(self.store.note_followup_delivery(job["id"], "root", None, digest))
            self.assertEqual(self.store.followups()[0]["fields"]["delivery_receipt"]["turn_id"], "")
            self.assertTrue(self.store.update_followup(job["id"], "running", {
                "lease_token": claimed["lease_token"], "prompt": "new dispatch", "delivery_receipt": None,
            }))
            self.assertFalse(self.store.note_followup_delivery(job["id"], "root", "turn", digest))
            self.assertTrue(self.store.complete_followup(job["id"], claimed["lease_token"]))
            self.assertFalse(self.store.note_followup_delivery(job["id"], "root", "turn", hashlib.sha256(b"new dispatch").hexdigest()))

    def test_followup_rejects_changed_baseline_and_invalid_status(self):
        job = self.job()
        with self.assertRaises(ValueError):
            self.store.register_followup("root", "org/repo", 8, 100, scope(3))
        self.assertFalse(self.store.update_followup(job["id"], "running"))
        with self.assertRaises(ValueError):
            self.store.update_followup(job["id"], "discarded")
        with self.assertRaises(ValueError):
            self.store.claim_followup(job["id"], 0)

    def test_concurrent_process_writes_persist_without_lost_registrations(self):
        args = [(self.tmp.name, i) for i in range(12)]
        with get_context("spawn").Pool(4) as pool:
            self.assertEqual(pool.map(worker_register, args), list(range(12)))
        self.assertEqual(len(Store(self.tmp.name).prs("root")), 12)

    def test_conflicting_scopes_written_concurrently_do_not_overwrite(self):
        def write(count):
            try:
                self.store.record_scope("root", scope(count))
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            successes = list(pool.map(write, [2, 3]))
        self.assertEqual(sum(successes), 1)
        self.assertIn(self.store.get_scope("root")["slice_count"], {2, 3})

    def test_issue_dispositions_accept_task_followups_reject_unrelated_issues(self):
        self.store.record_scope("root", scope())
        self.store.register_followup("root", "org/repo", 17, 100, scope(), issue_number=17)
        self.store.record_session("child", "claude", "/project", "root")
        receipt = {"comment_id": "issue-comment-1", "comment_updated_at": "2026-10-05T12:00:00Z", "disposition": "nonfix", "reason": "Additional slice is outside approved scope"}
        self.store.disposition("child", "org/repo", 17, receipt)
        self.assertEqual(Store(self.tmp.name).dispositions("org/repo", 17), [receipt])
        self.store.record_session("unrelated", "claude", "/project")
        with self.assertRaises(ValueError):
            self.store.disposition("unrelated", "org/repo", 17, receipt)
        with self.assertRaises(ValueError):
            self.store.disposition("root", "org/repo", 18, receipt)

    def test_recovery_merge_durable_parent_owned_and_unrelated_rejected(self):
        self.store.register("child", "claude", "org/repo", 9, "/project", "root")
        self.assertIsNone(self.store.recovery("org/repo", 9, "root"))
        self.store.record_recovery("org/repo", 9, "child", {"fingerprint": "sha1", "attempts": 1, "state": "dispatching", "lease_until": 5000, "next_attempt_at": 6000, "dispatch_at": 4000})
        merged = self.store.record_recovery("org/repo", 9, "root", {"state": "failed", "error": "Runner unavailable"})
        self.assertEqual(merged["session_id"], "root")
        self.assertEqual(merged["attempts"], 1)
        self.assertEqual(merged["lease_until"], 5000)
        self.assertEqual(merged["error"], "Runner unavailable")
        self.assertEqual(Store(self.tmp.name).recovery("org/repo", 9, "child"), merged)
        self.store.record_session("unrelated", "codex", "/project")
        self.assertIsNone(self.store.recovery("org/repo", 9, "unrelated"))
        with self.assertRaises(ValueError):
            self.store.record_recovery("org/repo", 9, "unrelated", {"attempts": 0})

    def test_recovery_concurrent_merges_preserve_disjoint_evidence(self):
        self.store.register("root", "codex", "org/repo", 9, "/project")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: self.store.record_recovery("org/repo", 9, "root", {f"evidence_{i}": i}), range(8)))
        recovered = Store(self.tmp.name).recovery("org/repo", 9, "root")
        self.assertEqual({key: recovered[key] for key in recovered if key.startswith("evidence_")}, {f"evidence_{i}": i for i in range(8)})
        with self.assertRaises(ValueError):
            self.store.record_recovery("org/repo", 9, "root", {"session_id": "unrelated"})

    def test_retry_followup_resets_failed_job_without_stealing_live_lease(self):
        job = self.job()
        with patch("caller_pr_gates.store.time.time", return_value=2000):
            claimed = self.store.claim_followup(job["id"], lease_seconds=100)
            self.assertFalse(self.store.retry_followup(job["id"]))
            self.assertTrue(self.store.update_followup(job["id"], "failed", {"lease_token": claimed["lease_token"], "error": "offline"}))
            self.assertTrue(self.store.retry_followup(job["id"]))
            reset = self.store.followups()[0]
            self.assertEqual(reset["status"], "pending")
            self.assertEqual(reset["attempts"], 0)
            self.assertIsNone(reset["lease_token"])
            new = self.store.claim_followup(job["id"], lease_seconds=100)
            self.assertTrue(self.store.complete_followup(job["id"], new["lease_token"], {"read_at": 2000}))
            self.assertFalse(self.store.retry_followup(job["id"]))
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE event='followup_explicit_retry'").fetchone()[0], 1)

    def test_explicit_retry_clears_old_delivery_generation_but_preserves_review_evidence(self):
        job = self.job()
        prompt = "accepted old queue prompt"
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        delivery = {"delivery": "queue_existing_thread", "prompt": prompt, "queued_prompt_hash": digest,
                    "dispatch_at": 2000, "acknowledged_at": 2001, "runner_exit": 0,
                    "next_attempt_at": 9000, "error": "old failure"}
        evidence = {"review_read": {"read_at": 2000, "head_sha": "abc", "comment_ids": []}}
        with patch("caller_pr_gates.store.time.time", return_value=2000):
            claimed = self.store.claim_followup(job["id"], lease_seconds=100)
            self.assertTrue(self.store.update_followup(job["id"], "running", {
                "lease_token": claimed["lease_token"], **delivery, **evidence,
            }))
            self.assertTrue(self.store.note_followup_delivery(job["id"], "root", "turn", digest))
            before = self.store.followups()[0]
            self.assertFalse(self.store.retry_followup(job["id"]))
            self.assertEqual(self.store.followups()[0], before)
        with patch("caller_pr_gates.store.time.time", return_value=2101):
            self.assertTrue(self.store.retry_followup(job["id"]))
            row = self.store.followups()[0]
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["attempts"], 0)
            self.assertIsNone(row["lease_token"])
            self.assertIsNone(row["lease_until"])
            self.assertEqual(row["fields"], evidence)
            self.assertFalse(self.store.note_followup_delivery(job["id"], "root", "old-turn", digest))

    def test_followup_normalizes_semantic_epoch_and_slice_count_must_match(self):
        baseline = scope()
        baseline["approved_at"] = 100
        self.store.record_scope("root", baseline)
        job = self.store.register_followup("root", "org/repo", 17, 0, baseline)
        self.assertEqual(job["scope"]["approved_at"], 100.0)
        with self.assertRaises(ValueError):
            self.store.record_scope("other", dict(scope(), slice_count=3))


class ValidationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.register("root", "codex", "org/repo", 8, "/project")
        self.store.record_session("child", "claude", "/project", "root")

    def plan(self, checks=None):
        return {"checks": checks or [{"id": "unit", "argv": ["python", "-m", "pytest", "tests/unit"],
                                      "reason": "Verify the changed boundary behavior"}]}

    def save(self, plan=None, input_digest="source-1", evidence_digest="remote-1", sid="root", cwd="/project"):
        return self.store.save_validation_plan(sid, "org/repo", 8, cwd, plan or self.plan(), input_digest, evidence_digest)

    def test_owned_plan_parent_subagent_share_and_survive_restart(self):
        saved = self.save(sid="child")
        self.assertEqual(saved["session_id"], "root")
        restarted = Store(self.tmp.name)
        self.assertEqual(restarted.validation_plan("root", "org/repo", 8, "/project/../project"), saved)
        self.assertEqual(restarted.validation_plan("child", "org/repo", 8, "/project"), saved)
        self.assertEqual(restarted.validation_plans("child"), [saved])
        self.assertEqual(restarted.validation_plans("unrelated"), [])
        self.assertIsNone(restarted.validation_plan("unrelated", "org/repo", 8, "/project"))
        with self.assertRaises(ValueError): self.save(sid="unrelated")
        with self.assertRaises(ValueError):
            self.store.save_validation_plan("root", "org/repo", 9, "/project", self.plan(), "source-1", "remote-1")
        with self.assertRaises(ValueError):
            self.store.record_session("root", "codex", "/project", "different-root")

    def test_active_plans_enumerate_all_worktree_contexts(self):
        first = self.save()
        second = self.save(cwd="/other-worktree")
        self.assertEqual({p["plan_id"] for p in self.store.validation_plans("root")}, {first["plan_id"], second["plan_id"]})

    def test_identical_plan_save_retains_run_and_cannot_reset_pass(self):
        saved = self.save()
        run = self.store.begin_validation(saved["plan_id"], "unit", "source-1")
        finished = self.store.finish_validation(run["run_id"], 0, "source-1")
        self.assertEqual(finished["status"], "pass")
        self.assertEqual(self.save(), saved)
        self.assertEqual(self.store.validation_runs(saved["plan_id"]), [finished])
        with self.assertRaises(ValueError): self.store.begin_validation(saved["plan_id"], "unit", "source-1")
        extended = self.plan(); extended["checks"].append({"id": "again", "argv": ["python", "-m", "pytest", "tests/integration"], "reason": "Reassurance"})
        with self.assertRaises(ValueError): self.save(extended)
        reason_change = self.plan(); reason_change["checks"][0]["reason"] = "Different reassurance reason"
        with self.assertRaises(ValueError): self.save(reason_change)
        with self.assertRaises(ValueError): self.save(evidence_digest="")

    def test_failed_command_cannot_repeat_but_failure_can_add_distinct_investigation(self):
        saved = self.save()
        run = self.store.begin_validation(saved["plan_id"], "unit", "source-1")
        failed = self.store.finish_validation(run["run_id"], 1, "source-1")
        self.assertEqual(failed["status"], "fail")
        self.assertEqual(self.save(), saved)
        with self.assertRaises(ValueError): self.store.begin_validation(saved["plan_id"], "unit", "source-1")
        extended = self.plan(); extended["checks"].append({"id": "regression", "argv": ["python", "-m", "pytest", "tests/regression"], "reason": "Locate the observed failure"})
        updated = self.save(extended)
        self.assertEqual(updated["plan_id"], saved["plan_id"])
        self.assertEqual(self.store.validation_runs(updated["plan_id"]), [failed])
        regression = self.store.begin_validation(updated["plan_id"], "regression", "source-1")
        self.assertEqual(regression["status"], "running")

    def test_failure_does_not_allow_command_rename_as_duplicate_retry(self):
        saved = self.save()
        run = self.store.begin_validation(saved["plan_id"], "unit", "source-1")
        self.store.finish_validation(run["run_id"], 1, "source-1")
        duplicate = self.plan(); duplicate["checks"].append({"id": "renamed", "argv": duplicate["checks"][0]["argv"], "reason": "Retry same command"})
        with self.assertRaises(ValueError): self.save(duplicate)
        changed = self.plan(); changed["checks"][0]["argv"] = ["python", "-m", "pytest", "-q"]
        with self.assertRaises(ValueError): self.save(changed)

    def test_source_change_creates_generation_and_retains_required_ids(self):
        first = self.save()
        run = self.store.begin_validation(first["plan_id"], "unit", "source-1")
        self.store.finish_validation(run["run_id"], 0, "source-1")
        changed = self.plan(); changed["checks"][0]["argv"] = ["python", "-m", "pytest", "tests/new-unit"]
        second = self.save(changed, input_digest="source-2")
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(self.store.validation_plans("root"), [second])
        self.assertEqual(len(self.store.validation_runs(first["plan_id"])), 1)
        with self.assertRaises(ValueError): self.store.begin_validation(first["plan_id"], "unit", "source-1")
        with self.assertRaises(ValueError): self.store.begin_validation(second["plan_id"], "unit", "source-1")
        replacement = {"checks": [{"id": "new-only", "argv": ["pytest", "tests/new"], "reason": "Discard old obligation"}]}
        with self.assertRaises(ValueError): self.save(replacement, input_digest="source-3")
        self.assertEqual(self.store.validation_plan("root", "org/repo", 8, "/project"), second)

    def test_new_nonempty_remote_evidence_allows_generation_but_no_command_rewrite(self):
        first = self.save()
        run = self.store.begin_validation(first["plan_id"], "unit", "source-1")
        self.store.finish_validation(run["run_id"], 0, "source-1")
        second = self.save(evidence_digest="remote-new-failure")
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(self.store.begin_validation(second["plan_id"], "unit", "source-1")["status"], "running")

    def test_declared_input_and_environment_requirements_survive_generations(self):
        original = self.plan()
        original.update(input_paths=["ignored/config.json", "ignored/data.csv"], env_names=["MODE", "TARGET"])
        first = self.save(original)
        for name in ("input_paths", "env_names"):
            for replacement in ([], original[name][:1]):
                with self.subTest(name=name, replacement=replacement):
                    changed = {**original, name: replacement}
                    with self.assertRaises(ValueError):
                        self.save(changed, input_digest="different-digest", evidence_digest="new-failure")
                    self.assertEqual(self.store.validation_plan("root", "org/repo", 8, "/project"), first)
        expanded = {**original, "input_paths": original["input_paths"] + ["ignored/new.json"],
                    "env_names": original["env_names"] + ["REGION"]}
        second = self.save(expanded, input_digest="expanded-input")
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(second["plan"], expanded)

    def test_per_check_inputs_cannot_be_dropped_with_source_change(self):
        original = self.plan()
        original["checks"][0].update(input_paths=["ignored/fixture.json"], env_names=["MODE"])
        first = self.save(original)
        for name in ("input_paths", "env_names"):
            changed = self.plan()
            changed["checks"][0].update(input_paths=["ignored/fixture.json"], env_names=["MODE"])
            changed["checks"][0][name] = []
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.save(changed, input_digest="changed-source")
        self.assertEqual(self.store.validation_plan("root", "org/repo", 8, "/project"), first)

    def test_same_argv_in_distinct_declared_subdirectories_is_distinct_check(self):
        checks = [{"id": "a", "argv": ["pytest"], "cwd": "package-a", "reason": "Test package a"},
                  {"id": "b", "argv": ["pytest"], "cwd": "package-b", "reason": "Test package b"}]
        saved = self.save(self.plan(checks))
        first = self.store.begin_validation(saved["plan_id"], "a", "source-1")
        self.store.finish_validation(first["run_id"], 0, "source-1")
        second = self.store.begin_validation(saved["plan_id"], "b", "source-1")
        self.assertEqual(second["status"], "running")
        duplicate = [checks[0], {**checks[0], "id": "renamed"}]
        with self.assertRaises(ValueError): self.save(self.plan(duplicate), input_digest="new-source")

    def test_running_never_passes_or_allows_plan_replacement(self):
        first = self.save()
        run = self.store.begin_validation(first["plan_id"], "unit", "source-1")
        self.assertEqual(self.store.validation_runs(first["plan_id"])[0]["status"], "running")
        self.assertIsNone(run["exit_code"])
        self.assertIsNone(run["finished_at"])
        self.assertEqual(self.save(), first)
        with self.assertRaises(ValueError): self.save(input_digest="source-2")
        with self.assertRaises(ValueError): self.save(evidence_digest="remote-new")
        with self.assertRaises(ValueError): self.store.begin_validation(first["plan_id"], "unit", "source-1")

    def test_atomic_duplicate_claim_one_winner(self):
        first = self.save()
        def claim(_):
            try: return self.store.begin_validation(first["plan_id"], "unit", "source-1")
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(claim, range(8)))
        self.assertEqual(sum(row is not None for row in rows), 1)
        self.assertEqual(len(self.store.validation_runs(first["plan_id"])), 1)

    def test_pass_requires_actual_zero_exit_and_unchanged_inputs(self):
        for exit_code, after, expected in ((0, "source-1", "pass"), (2, "source-1", "fail"),
            (0, "source-2", "unknown"), (2, "source-2", "unknown"), (None, "source-1", "unknown"),
            (-2, "source-1", "unknown"), (0, None, "unknown")):
            with self.subTest(exit_code=exit_code, after=after):
                saved = self.save(cwd=f"/case-{exit_code}-{after}")
                run = self.store.begin_validation(saved["plan_id"], "unit", "source-1")
                result = self.store.finish_validation(run["run_id"], exit_code, after)
                self.assertEqual(result["status"], expected)
                self.assertEqual(self.store.finish_validation(run["run_id"], exit_code, after), result)
                with self.assertRaises(ValueError): self.store.finish_validation(run["run_id"], 17, "rewritten")

    def test_unknown_interruption_permits_required_check_retry_without_claiming_success(self):
        saved = self.save()
        run = self.store.begin_validation(saved["plan_id"], "unit", "source-1")
        self.store.finish_validation(run["run_id"], None, "source-1")
        retry = self.store.begin_validation(saved["plan_id"], "unit", "source-1")
        self.assertNotEqual(run["run_id"], retry["run_id"])
        self.assertEqual([r["status"] for r in self.store.validation_runs(saved["plan_id"])], ["unknown", "running"])

    def test_optional_owner_checks_reject_other_task(self):
        saved = self.save()
        with self.assertRaises(ValueError): self.store.begin_validation(saved["plan_id"], "unit", "source-1", session_id="unrelated")
        run = self.store.begin_validation(saved["plan_id"], "unit", "source-1", session_id="child")
        with self.assertRaises(ValueError): self.store.finish_validation(run["run_id"], 0, "source-1", session_id="unrelated")
        self.assertEqual(self.store.finish_validation(run["run_id"], 0, "source-1", session_id="root")["status"], "pass")

    def test_crash_recovery_requires_owner_pid_proven_exited(self):
        saved = self.save()
        run = self.store.begin_validation(saved["plan_id"], "unit", "source-1", owner_pid=54321)
        for error in (None, PermissionError(), OSError("Unknown process state")):
            with patch("caller_pr_gates.store.os.kill", side_effect=error) as probe:
                self.assertFalse(self.store.recover_validation_run(run["run_id"]))
                probe.assert_called_once_with(54321, 0)
                self.assertEqual(self.store.validation_runs(saved["plan_id"])[0]["status"], "running")
        with patch("caller_pr_gates.store.os.kill", side_effect=ProcessLookupError()):
            self.assertTrue(self.store.recover_validation_run(run["run_id"]))
        interrupted = self.store.validation_runs(saved["plan_id"])[0]
        self.assertEqual(interrupted["status"], "unknown")
        self.assertIsNone(interrupted["exit_code"])
        self.assertFalse(self.store.recover_validation_run(run["run_id"]))
        self.assertEqual(self.store.begin_validation(saved["plan_id"], "unit", "source-1")["status"], "running")

    def test_crash_recovery_requires_entire_recorded_process_group_exited(self):
        saved = self.save()
        run = self.store.begin_validation(saved["plan_id"], "unit", "source-1", owner_pid=54321, owner_group=54321)
        self.assertEqual(run["process_group"], 54321)
        # The wrapper is gone but its child keeps the managed group alive.
        with patch("caller_pr_gates.store.os.kill", side_effect=ProcessLookupError()) as pid_probe:
            for error in (None, PermissionError(), OSError("Unverifiable group")):
                with patch("caller_pr_gates.store.os.killpg", side_effect=error) as group_probe:
                    self.assertFalse(self.store.recover_validation_run(run["run_id"]))
                    group_probe.assert_called_once_with(54321, 0)
            pid_probe.assert_not_called()
        with patch("caller_pr_gates.store.os.killpg", side_effect=ProcessLookupError()):
            self.assertTrue(self.store.recover_validation_run(run["run_id"]))
        self.assertEqual(self.store.validation_runs(saved["plan_id"])[0]["status"], "unknown")
        self.assertEqual(self.store.begin_validation(saved["plan_id"], "unit", "source-1")["status"], "running")

    def test_existing_validation_table_migrates_process_group_without_losing_runs(self):
        path = Path(self.tmp.name) / "legacy"
        path.mkdir()
        with sqlite3.connect(path / "registry.sqlite3") as db:
            db.execute("""CREATE TABLE validation_runs (
                run_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, check_id TEXT NOT NULL,
                input_digest TEXT NOT NULL, status TEXT NOT NULL, exit_code INTEGER,
                before_digest TEXT NOT NULL, after_digest TEXT, started_at REAL NOT NULL,
                finished_at REAL, pid INTEGER)""")
            db.execute("INSERT INTO validation_runs VALUES ('old','old-plan','unit','source','running',NULL,'source',NULL,100,NULL,54321)")
        migrated = Store(path)
        row = migrated.validation_runs("old-plan")[0]
        self.assertIsNone(row["process_group"])
        self.assertEqual(row["pid"], 54321)
        self.assertEqual(row["status"], "running")

    def test_malformed_plan_or_process_metadata_rejected(self):
        bad_plans = ({}, {"checks": []}, {"checks": [{"id": "unit", "argv": "pytest", "reason": "Check"}]},
                     {"checks": [{"id": "unit", "argv": ["pytest"], "reason": ""}]})
        for bad in bad_plans:
            with self.subTest(plan=bad), self.assertRaises(ValueError):
                self.store.save_validation_plan("root", "org/repo", 8, "/project", bad, "source-1", "remote-1")
        saved = self.save()
        with self.assertRaises(ValueError): self.store.begin_validation(saved["plan_id"], "missing", "source-1")
        with self.assertRaises(ValueError): self.store.begin_validation(saved["plan_id"], "unit", "source-1", owner_pid=True)
        with self.assertRaises(ValueError): self.store.begin_validation(saved["plan_id"], "unit", "source-1", owner_group=True)
        run = self.store.begin_validation(saved["plan_id"], "unit", "source-1")
        with self.assertRaises(ValueError): self.store.finish_validation(run["run_id"], True, "source-1")


class ExternalBlockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.register("root", "codex", "org/repo", 8, "/project")
        self.store.record_session("child", "claude", "/project", "root")
        self.url = "https://github.com/org/repo/actions/runs/90/job/91"

    def hold(self, sid="root", head="head-1", fingerprint="gate-1", reason="Copilot quota HTTP 402"):
        return self.store.record_external_blocker(sid, "org/repo", 8, head, fingerprint, reason, self.url)

    def test_hold_is_root_owned_durable_and_never_complete(self):
        original = self.hold(sid="child")
        self.assertEqual(original["session_id"], "root")
        self.assertEqual(original["status"], "held")
        self.assertIsNone(original["released_at"])
        restarted = Store(self.tmp.name)
        self.assertEqual(restarted.external_blocker("root", "org/repo", 8), original)
        self.assertEqual(restarted.external_blocker("child", "org/repo", 8), original)
        self.store.record_session("root", "codex", "/new-turn-workdir")
        self.assertEqual(self.store.external_blocker("root", "org/repo", 8), original)
        self.assertEqual(self.store.prs("root")[0]["status"], "active")

    def test_repeated_same_head_and_gates_hold_is_audited_once(self):
        original = self.hold()
        self.assertEqual(self.hold(reason="Arbitrary repeated description"), original)
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE event='external_blocker_held'").fetchone()[0], 1)
        with self.assertRaises(ValueError): self.hold(head="new-head")
        self.assertEqual(self.store.external_blocker("root", "org/repo", 8), original)

    def test_unrelated_task_cannot_record_release_or_claim_another_hold(self):
        self.hold()
        self.store.record_session("unrelated", "claude", "/project")
        self.assertIsNone(self.store.external_blocker("unrelated", "org/repo", 8))
        with self.assertRaises(ValueError): self.hold(sid="unrelated")
        with self.assertRaises(ValueError):
            self.store.release_external_blocker("unrelated", "org/repo", 8, explicit_retry=True)
        self.assertIsNotNone(self.store.external_blocker("root", "org/repo", 8))

    def test_unchanged_evidence_and_arbitrary_reason_never_release(self):
        original = self.hold()
        for options in ({}, {"reason": "looks complete", "head_sha": "head-2", "fingerprint": "gate-2"},
            {"reason": "new_authoritative_evidence", "head_sha": "head-1", "fingerprint": "gate-1"},
            {"reason": "new_authoritative_evidence", "head_sha": "head-2"}):
            with self.subTest(options=options):
                self.assertFalse(self.store.release_external_blocker("root", "org/repo", 8, **options))
                self.assertEqual(self.store.external_blocker("root", "org/repo", 8), original)

    def test_authoritative_new_head_or_gate_fingerprint_releases_without_marking_green(self):
        for head, fingerprint in (("head-2", "gate-1"), ("head-1", "gate-2")):
            with self.subTest(head=head, fingerprint=fingerprint):
                self.hold()
                snapshot = {"head_sha": "head-1"}
                gates = [{"gate": "CI_FAILED", "status": "FAIL"}]
                self.store.set_snapshot("org/repo", 8, snapshot, gates)
                self.assertTrue(self.store.release_external_blocker("child", "org/repo", 8,
                    reason="new_authoritative_evidence", head_sha=head, fingerprint=fingerprint))
                self.assertIsNone(self.store.external_blocker("root", "org/repo", 8))
                self.assertEqual(self.store.snapshot("org/repo", 8)["gates"], gates)
                self.assertEqual(self.store.prs("root")[0]["status"], "active")

    def test_new_snapshot_alone_does_not_erase_hold_or_claim_success(self):
        original = self.hold()
        self.store.set_snapshot("org/repo", 8, {"head_sha": "head-2"}, [{"gate": "GITHUB_DATA", "status": "UNKNOWN"}])
        self.assertEqual(self.store.external_blocker("root", "org/repo", 8), original)

    def test_explicit_retry_preserves_live_followup_lease_and_obligation(self):
        self.store.record_scope("root", scope())
        job = self.store.register_followup("root", "org/repo", 8, 100, scope(), "prd")
        claimed = self.store.claim_followup(job["id"])
        before = self.store.followups("root")[0]
        self.hold()
        self.assertTrue(self.store.release_external_blocker("root", "org/repo", 8, explicit_retry=True))
        self.assertEqual(self.store.followups("root")[0], before)
        self.assertEqual(self.store.followups("root")[0]["lease_token"], claimed["lease_token"])
        self.assertEqual(self.store.followups("root")[0]["status"], "running")
        self.assertFalse(self.store.release_external_blocker("root", "org/repo", 8, explicit_retry=True))

    def test_hold_can_recur_after_explicit_retry_with_a_new_audit_cycle(self):
        self.hold()
        self.store.release_external_blocker("root", "org/repo", 8, explicit_retry=True)
        reopened = self.hold()
        self.assertEqual(reopened["status"], "held")
        self.assertIsNone(reopened["released_at"])
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE event='external_blocker_held'").fetchone()[0], 2)
            raw = db.execute("SELECT fields FROM audit WHERE event='external_blocker_released'").fetchone()[0]
        self.assertIn('"obligation_complete":false', raw)

    def test_malformed_hold_evidence_is_rejected(self):
        for fields in (("", "gate", "reason", self.url), ("head", "", "reason", self.url),
                       ("head", "gate", "", self.url), ("head", "gate", "reason", "http://example.com"),
                       ("head", "gate", "reason", "https://user:secret@example.com")):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.store.record_external_blocker("root", "org/repo", 8, *fields)
        self.assertIsNone(self.store.external_blocker("root", "org/repo", 8))


class FalseOwnershipCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.register("reader", "codex", "org/repo", 8, "/shared-checkout")
        self.store.register("creator", "claude", "org/repo", 8, "/creator-checkout")
        self.proof_path = Path(self.tmp.name) / "read-only-proof.json"

    def evidence(self, extra_ids=None, hook_induced=False):
        with self.store._connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM audit WHERE event='pr_registered' AND json_extract(fields,'$.session_id')='reader'")]
        ids.extend(extra_ids or [])
        data = {"session_id": "reader", "repository": "org/repo", "number": 8,
                "audit_ids": ids, "preceding_tool": "cat AGENTS.md; no write"}
        if hook_induced: data["hook_induced_registration"] = True
        self.proof_path.write_text(json.dumps(data))
        return {"reason": "Read-only discovery attached the current branch PR to the wrong task",
                "audit_ids": ids, "evidence_file": str(self.proof_path), "confirmed_read_only": True}

    def test_inactive_reader_preserves_real_creator_and_durable_audit_row(self):
        proof = self.evidence()
        self.assertTrue(self.store.correct_false_ownership("reader", "org/repo", 8, proof))
        self.assertEqual(self.store.prs("reader"), [])
        self.assertEqual(len(self.store.prs("creator")), 1)
        self.assertEqual(self.store.prs()[0]["session_id"], "creator")
        restarted = Store(self.tmp.name)
        self.assertEqual(restarted.prs("reader"), [])
        with restarted._connect() as db:
            row = db.execute("SELECT status FROM prs WHERE session_id='reader'").fetchone()
            events = db.execute("SELECT fields FROM audit WHERE event='false_ownership_corrected'").fetchall()
        self.assertEqual(row[0], "misattributed_read_only")
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0][0])["evidence"], proof)
        self.assertFalse(self.store.correct_false_ownership("reader", "org/repo", 8, proof))

    def test_future_explicit_registration_reactivates_the_retained_row(self):
        self.store.correct_false_ownership("reader", "org/repo", 8, self.evidence())
        reactivated = self.store.register("reader", "codex", "org/repo", 8, "/legitimate-fix-worktree")
        self.assertEqual(reactivated["status"], "active")
        self.assertEqual(len(self.store.prs("reader")), 1)
        self.assertEqual(len(self.store.prs("creator")), 1)

    def test_target_writes_or_dispositions_or_validation_prevent_false_correction(self):
        for kind in ("github_mutation", "local_mutation", "disposition", "validation"):
            with self.subTest(kind=kind):
                # Each trial has a distinct genuinely owned PR.
                number = 20 + ("github_mutation", "local_mutation", "disposition", "validation").index(kind)
                self.store.register("reader", "codex", "org/repo", number, "/project")
                with self.store._connect() as db:
                    ids = [row[0] for row in db.execute("SELECT id FROM audit WHERE event='pr_registered' AND json_extract(fields,'$.session_id')='reader' AND json_extract(fields,'$.number')=?", (number,))]
                self.proof_path.write_text(json.dumps({"session_id": "reader", "repository": "org/repo", "number": number, "audit_ids": ids}))
                proof = {"reason": "Claimed read-only", "audit_ids": ids, "evidence_file": str(self.proof_path), "confirmed_read_only": True}
                if kind == "github_mutation":
                    self.store.audit("github_mutation", {"session_id": "reader", "repository": "org/repo", "number": number})
                elif kind == "local_mutation":
                    self.store.audit("ownership_evidence", {"session_id": "reader", "target": f"https://github.com/org/repo/pull/{number}", "origin": "local_mutation", "operation": "git_commit"})
                elif kind == "disposition":
                    self.store.disposition("reader", "org/repo", number, {"comment_id": "c", "comment_updated_at": "revision", "disposition": "nonfix", "reason": "Explained scope"})
                else:
                    plan = {"checks": [{"id": "unit", "argv": ["pytest"], "reason": "Verify fix"}]}
                    self.store.save_validation_plan("reader", "org/repo", number, "/project", plan, "input", "evidence")
                with self.assertRaises(ValueError): self.store.correct_false_ownership("reader", "org/repo", number, proof)
                self.assertIn(number, [row["number"] for row in self.store.prs("reader")])

    def test_real_creator_writes_do_not_prevent_correcting_independent_reader(self):
        self.store.audit("github_mutation", {"session_id": "creator", "repository": "org/repo", "number": 8})
        self.assertTrue(self.store.correct_false_ownership("reader", "org/repo", 8, self.evidence()))
        self.assertEqual(len(self.store.prs("creator")), 1)

    def test_explicit_register_proof_must_document_hook_induced_registration(self):
        self.store.audit("ownership_evidence", {"session_id": "reader", "target": "https://github.com/org/repo/pull/8",
                                                "origin": "explicit_register", "operation": "register"})
        with self.store._connect() as db:
            identifier = db.execute("SELECT MAX(id) FROM audit").fetchone()[0]
        with self.assertRaises(ValueError):
            self.store.correct_false_ownership("reader", "org/repo", 8, self.evidence(extra_ids=[identifier]))
        self.assertTrue(self.store.correct_false_ownership("reader", "org/repo", 8,
            self.evidence(extra_ids=[identifier], hook_induced=True)))

    def test_missing_wrong_target_or_unconfirmed_evidence_cannot_release_ownership(self):
        proof = self.evidence()
        for changed in ({**proof, "confirmed_read_only": False}, {**proof, "audit_ids": [999999]},
                        {**proof, "evidence_file": "/does/not/exist"}, {**proof, "reason": ""}):
            with self.subTest(proof=changed), self.assertRaises(ValueError):
                self.store.correct_false_ownership("reader", "org/repo", 8, changed)
        with self.store._connect() as db:
            creator_id = db.execute("SELECT id FROM audit WHERE event='pr_registered' AND json_extract(fields,'$.session_id')='creator'").fetchone()[0]
        self.proof_path.write_text(json.dumps({"session_id": "reader", "repository": "org/repo", "audit_ids": [creator_id]}))
        proof["audit_ids"] = [creator_id]
        with self.assertRaises(ValueError): self.store.correct_false_ownership("reader", "org/repo", 8, proof)
        self.assertEqual(len(self.store.prs("reader")), 1)


if __name__ == "__main__":
    unittest.main()
