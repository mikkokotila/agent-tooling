import asyncio
import hashlib
import unittest

from caller_pr_gates.github import (EvidenceError, _rest_pages, _threads,
    collect_issue_with_client, collect_with_client)
from caller_pr_gates.policy import evaluate_pr, evaluate_review

SHA = "a" * 40
OTHER = "b" * 40
BEFORE = "2026-10-05T09:00:00Z"
COMMENTED = "2026-10-05T10:00:00Z"
FIXED = "2026-10-05T11:00:00Z"
REPLIED = "2026-10-05T12:00:00Z"


def fresh_branch(head=SHA, base='c' * 40, ref='main'):
    return dict(base_ref=ref, base_ref_after=ref, base_sha=base, base_sha_after=base,
                branch_comparison=dict(head_sha=head, base_sha=base, merge_base_sha=base,
                                       ahead_by=int(head != base), behind_by=0,
                                       status='ahead' if head != base else 'identical'))


def closing_link(head=SHA, repo='owner/repo', number=2, state='OPEN'):
    return dict(default_branch='main', closure_head_sha=head, closure_base_ref='main',
                closing_issues=[dict(repository=repo, number=number, url=f'https://github.com/{repo}/issues/{number}', state=state)])


def closing_graphql(variables, head=SHA, merged=False):
    repo = variables['owner'] + '/' + variables['repo']
    data = closing_link(head, repo, variables['number'] + 1, 'CLOSED' if merged else 'OPEN')
    return {'repository': {'defaultBranchRef': {'name': 'main'}, 'pullRequest': {
        'headRefOid': head, 'baseRefName': 'main', 'state': 'MERGED' if merged else 'OPEN', 'merged': merged,
        'closingIssuesReferences': {'nodes': [dict(number=i['number'], url=i['url'], state=i['state'], repository={'nameWithOwner': i['repository']}) for i in data['closing_issues']], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}


def snapshot():
    return {"repository": "owner/repo", "number": 1, "state": "open", "merged": False,
        "draft": False, "mergeable": True, "head_sha": SHA, "head_sha_after": SHA, "actor_login": "agent",
        "author_login": "agent", "checks": [check()], "expected_checks": ["test"],
        "comments": [], "commit_shas": [SHA], "commit_dates": {SHA: FIXED}, **fresh_branch(), **closing_link()}


def check(**kwargs):
    return {"id": "10", "name": "test", "kind": "check", "status": "completed",
        "conclusion": "success", "head_sha": SHA, "updated_at": FIXED,
        "created_at": FIXED, "run_attempt": 1, **kwargs}


def comment(**kwargs):
    return {"id": "inline:20", "thread_id": "thread1", "author_login": "reviewer",
        "body": "Guard the boundary case", "created_at": COMMENTED, "updated_at": COMMENTED,
        "url": "https://github.com/owner/repo/pull/1#discussion_r20", "kind": "inline",
        "is_resolved": False, "is_outdated": False, "replies": [], **kwargs}


def receipt(**kwargs):
    return {"comment_id": "inline:20", "comment_updated_at": COMMENTED,
        "disposition": "fixed", "reason": "Added the boundary guard", "commit_sha": SHA, **kwargs}


def statuses(data, receipts=None):
    return {gate["gate"]: gate["status"] for gate in evaluate_pr(data, receipts or [])}


class PolicyTests(unittest.TestCase):
    def test_clean_individual_checks_pass(self):
        self.assertTrue(all(status == "PASS" for status in statuses(snapshot()).values()))

    def test_draft_is_separate_from_successful_ci(self):
        data = snapshot(); data["draft"] = True
        result = statuses(data)
        self.assertEqual("FAIL", result["PR_NOT_DRAFT"])
        self.assertEqual("PASS", result["CI_FAILED"])

    def test_one_optional_failed_check_blocks_even_with_required_success(self):
        data = snapshot(); data["checks"].append(check(id="11", name="optional", conclusion="failure"))
        result = statuses(data)
        self.assertEqual("FAIL", result["CI_FAILED"])
        self.assertEqual("PASS", result["CI_PENDING"])

    def test_one_running_workflow_job_blocks(self):
        data = snapshot(); data["checks"].append(check(id="11", name="deploy", kind="job", status="in_progress", conclusion=None))
        result = statuses(data)
        self.assertEqual("PASS", result["CI_FAILED"])
        self.assertEqual("WAIT", result["CI_PENDING"])

    def test_cancelled_timed_out_and_action_required_block(self):
        for outcome in ("cancelled", "timed_out", "action_required", "stale", "startup_failure"):
            with self.subTest(outcome=outcome):
                data = snapshot(); data["checks"] = [check(conclusion=outcome)]
                self.assertEqual("FAIL", statuses(data)["CI_FAILED"])

    def test_later_successful_rerun_supersedes_failure(self):
        data = snapshot(); data["checks"] = [check(id="9", conclusion="failure", created_at=BEFORE, updated_at=BEFORE),
            check(id="10", run_attempt=2)]
        self.assertEqual("PASS", statuses(data)["CI_FAILED"])

    def test_newer_running_attempt_supersedes_old_success(self):
        data = snapshot(); data["checks"] = [check(id="9", created_at=BEFORE), check(status="queued", conclusion=None)]
        self.assertEqual("WAIT", statuses(data)["CI_PENDING"])

    def test_different_check_apps_are_not_collapsed(self):
        data = snapshot(); data["checks"] = [check(key="check:1:test", conclusion="failure"), check(id="11", key="check:2:test")]
        self.assertEqual("FAIL", statuses(data)["CI_FAILED"])

    def test_missing_required_check_waits(self):
        data = snapshot(); data["expected_checks"].append("security")
        self.assertEqual("WAIT", statuses(data)["CI_PENDING"])

    def test_no_known_ci_is_valid_but_unknown_requirements_never_pass(self):
        data = snapshot(); data["checks"] = []; data["expected_checks"] = []
        self.assertEqual("PASS", statuses(data)["CI_PENDING"])
        del data["expected_checks"]
        self.assertEqual("UNKNOWN", statuses(data)["CI_PENDING"])

    def test_unknown_ci_outcome_fails_closed(self):
        data = snapshot(); data["checks"] = [check(conclusion="new_github_enum")]
        self.assertEqual("UNKNOWN", statuses(data)["CI_FAILED"])
        self.assertEqual("UNKNOWN", statuses(data)["CI_PENDING"])

    def test_missing_rules_preserves_known_failures_and_pending_jobs(self):
        data = snapshot()
        data.update(expected_checks=None, known_expected_checks=['test'],
                    component_errors={'rules': {'kind': 'feature_unavailable', 'message': 'Rules API unavailable'}})
        data['checks'] += [check(id='11', name='browser', conclusion='failure'),
                           check(id='12', name='deploy', status='in_progress', conclusion=None)]
        self.assertEqual('FAIL', statuses(data)['CI_FAILED'])
        self.assertEqual('WAIT', statuses(data)['CI_PENDING'])
        self.assertEqual('PASS', statuses(data)['PR_NOT_DRAFT'])
        self.assertEqual('PASS', statuses(data)['REVIEW_HANDLED'])

    def test_missing_rules_keeps_verified_checks_without_passing_completion(self):
        data = snapshot()
        data.update(expected_checks=None, known_expected_checks=['test'])
        gates = evaluate_pr(data, [])
        ci = [g for g in gates if g['gate'].startswith('CI_')]
        self.assertEqual({'UNKNOWN'}, {g['status'] for g in ci})
        self.assertEqual([check()], ci[0]['evidence']['checks'])

    def test_head_race_invalidates_all_gates(self):
        data = snapshot(); data["head_sha_after"] = OTHER
        self.assertEqual({"UNKNOWN"}, set(statuses(data).values()))

    def test_ci_for_wrong_head_invalidates_ci(self):
        data = snapshot(); data["checks"] = [check(head_sha=OTHER)]
        self.assertEqual("UNKNOWN", statuses(data)["CI_FAILED"])

    def test_outdated_resolved_thread_does_not_manufacture_fix(self):
        data = snapshot(); data["comments"] = [comment(is_outdated=True, is_resolved=True)]
        result = statuses(data)
        self.assertEqual("FAIL", result["REVIEW_HANDLED"])
        self.assertEqual("PASS", result["REVIEW_CONCLUDED"])

    def test_fix_without_conclusion_remains_blocked_separately(self):
        data = snapshot(); data["comments"] = [comment()]
        result = statuses(data, [receipt()])
        self.assertEqual("PASS", result["REVIEW_HANDLED"])
        self.assertEqual("FAIL", result["REVIEW_CONCLUDED"])

    def test_verified_actor_explanation_concludes_fix(self):
        reply_url = "https://github.com/owner/repo/pull/1#discussion_r21"
        reply = {"id": "inline:21", "author_login": "agent", "body": "Added the boundary guard.", "url": reply_url, "created_at": REPLIED}
        data = snapshot(); data["comments"] = [comment(is_resolved=True, replies=[reply])]
        result = statuses(data, [receipt(reply_url=reply_url)])
        self.assertEqual("PASS", result["REVIEW_HANDLED"])
        self.assertEqual("PASS", result["REVIEW_CONCLUDED"])

    def test_unrelated_or_spoofed_reply_does_not_conclude(self):
        for author, body in (("other", "Added the boundary guard"), ("agent", "Looks good")):
            with self.subTest(author=author, body=body):
                reply = {"author_login": author, "body": body, "url": "reply", "created_at": REPLIED}
                data = snapshot(); data["comments"] = [comment(replies=[reply])]
                self.assertEqual("FAIL", statuses(data, [receipt(reply_url="reply")])["REVIEW_CONCLUDED"])

    def test_edited_comment_invalidates_previous_disposition_and_reply(self):
        data = snapshot(); data["comments"] = [comment(updated_at=REPLIED, is_resolved=True)]
        self.assertEqual("FAIL", statuses(data, [receipt()])["REVIEW_HANDLED"])

    def test_force_push_removed_fix_commit_invalidates_handling(self):
        data = snapshot(); data["comments"] = [comment(is_resolved=True)]; data["commit_shas"] = [OTHER]
        self.assertEqual("FAIL", statuses(data, [receipt()])["REVIEW_HANDLED"])

    def test_commit_before_review_cannot_be_fix_evidence(self):
        data = snapshot(); data["comments"] = [comment(is_resolved=True)]; data["commit_dates"] = {SHA: BEFORE}
        self.assertEqual("FAIL", statuses(data, [receipt()])["REVIEW_HANDLED"])

    def test_nonfix_reason_and_verified_reply_handle_issue_comment(self):
        data = snapshot()
        c = comment(id="issue:30", kind="issue", thread_id=None, replies=[{"id": "issue:31", "author_login": "agent",
            "body": "Outside the approved scope; tracked for later.", "url": "reply", "created_at": REPLIED}])
        data["comments"] = [c]
        record = receipt(comment_id="issue:30", disposition="nonfix", reason="Outside the approved scope", reply_url="reply")
        self.assertEqual("PASS", statuses(data, [record])["REVIEW_CONCLUDED"])
        self.assertEqual("PASS", statuses(data, [record])["REVIEW_HANDLED"])

    def test_all_external_followup_comments_are_independent_obligations(self):
        data = snapshot(); data["comments"] = [comment(is_resolved=True), comment(id="inline:22", body="Also cover null input", is_resolved=True)]
        self.assertEqual("FAIL", statuses(data, [receipt()])["REVIEW_HANDLED"])

    def test_same_account_original_human_comment_is_an_obligation(self):
        data = snapshot(); data["comments"] = [comment(author_login="agent")]
        self.assertEqual("FAIL", statuses(data)["REVIEW_HANDLED"])

    def test_verified_conclusion_reply_is_not_a_recursive_obligation(self):
        reply = comment(id="inline:21", author_login="agent", body="Added the boundary guard",
                        created_at=REPLIED, updated_at=REPLIED, url="reply")
        parent = comment(is_resolved=True, replies=[reply])
        data = snapshot(); data["comments"] = [parent, reply]
        result = statuses(data, [receipt(reply_url="reply")])
        self.assertEqual("PASS", result["REVIEW_HANDLED"])
        self.assertEqual("PASS", result["REVIEW_CONCLUDED"])

    def test_stale_or_malformed_reply_remains_an_obligation(self):
        scenarios = (
            {"body": "Unrelated comment"},
            {"created_at": BEFORE},
            {"author_login": "other"},
            {"updated_at": None},
        )
        for changes in scenarios:
            with self.subTest(changes=changes):
                reply = comment(id="inline:21", author_login="agent", body="Added the boundary guard",
                                created_at=REPLIED, updated_at=REPLIED, url="reply")
                reply.update(changes)
                parent = comment(is_resolved=True, replies=[reply])
                data = snapshot(); data["comments"] = [parent, reply]
                gates = evaluate_review(data, [receipt(reply_url="reply")])
                self.assertNotEqual("PASS", gates[0]["status"])
                self.assertNotIn("reply", gates[0]["evidence"]["verified_conclusion_replies"])

    def test_parent_edit_or_removed_fix_evidence_prevents_reply_exemption(self):
        for invalidate in ("comment_edit", "removed_fix"):
            with self.subTest(invalidate=invalidate):
                reply = comment(id="inline:21", author_login="agent", body="Added the boundary guard",
                                created_at=REPLIED, updated_at=REPLIED, url="reply")
                parent = comment(is_resolved=True, replies=[reply])
                data = snapshot(); data["comments"] = [parent, reply]
                if invalidate == "comment_edit": parent["updated_at"] = FIXED
                else: data["commit_shas"] = []
                gates = evaluate_review(data, [receipt(reply_url="reply")])
                self.assertEqual("FAIL", gates[0]["status"])
                self.assertNotIn("reply", gates[0]["evidence"]["verified_conclusion_replies"])

    def test_external_reviewer_followup_is_never_a_conclusion_reply(self):
        reply = comment(id="inline:21", author_login="reviewer", body="Added the boundary guard",
                        created_at=REPLIED, updated_at=REPLIED, url="reply")
        parent = comment(is_resolved=True, replies=[reply])
        data = snapshot(); data["comments"] = [parent, reply]
        self.assertEqual("FAIL", statuses(data, [receipt(reply_url="reply")])["REVIEW_HANDLED"])

    def test_external_pr_author_comment_remains_an_obligation(self):
        data = snapshot(); data["author_login"] = "external_author"
        data["comments"] = [comment(author_login="external_author")]
        self.assertEqual("FAIL", statuses(data)["REVIEW_HANDLED"])

    def test_issue_fixed_receipt_matches_actual_post_comment_document_edit(self):
        data = {"kind": "issue", "actor_login": "agent", "author_login": "agent", "body": "Adjusted PRD scope wording",
                "updated_at": REPLIED, "document_edited_at": FIXED, "comments": [comment(kind="issue", thread_id=None)]}
        record = receipt(document_revision=data["document_edited_at"], content_sha256=hashlib.sha256(data["body"].encode()).hexdigest())
        self.assertEqual("PASS", evaluate_review(data, [record])[0]["status"])
        data["updated_at"] = "2026-10-05T13:00:00Z"
        self.assertEqual("PASS", evaluate_review(data, [record])[0]["status"])
        data["body"] += " expanded scope"
        self.assertEqual("FAIL", evaluate_review(data, [record])[0]["status"])

    def test_issue_reply_activity_does_not_manufacture_document_edit(self):
        data = {"kind": "issue", "actor_login": "agent", "author_login": "agent", "body": "Unedited PRD",
                "updated_at": REPLIED, "document_edited_at": BEFORE, "comments": [comment(kind="issue", thread_id=None)]}
        record = receipt(document_revision=data["document_edited_at"], content_sha256=hashlib.sha256(data["body"].encode()).hexdigest())
        self.assertEqual("FAIL", evaluate_review(data, [record])[0]["status"])

    def test_closed_unmerged_and_merged_keep_obligations(self):
        for merged in (False, True):
            with self.subTest(merged=merged):
                data = snapshot(); data.update(state="closed", merged=merged, comments=[comment()])
                self.assertEqual("FAIL", statuses(data)["REVIEW_HANDLED"])

    def test_issue_review_has_only_review_gates(self):
        result = evaluate_review({"comments": [], "actor_login": "agent", "author_login": "agent"}, [])
        self.assertEqual(["REVIEW_HANDLED", "REVIEW_CONCLUDED"], [gate["gate"] for gate in result])


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapped_rest_reads_every_page(self):
        class Client:
            async def get(self, path, params):
                page = params["page"]
                return {"total_count": 101, "check_runs": [{"id": n} for n in range(100)] if page == 1 else [{"id": 100}]}
        self.assertEqual(101, len(await _rest_pages(Client(), "/check-runs", field="check_runs")))

    async def test_incomplete_wrapped_rest_response_fails_closed(self):
        class Client:
            async def get(self, path, params):
                return {"total_count": 101, "check_runs": [{"id": 1}]}
        with self.assertRaises(EvidenceError):
            await _rest_pages(Client(), "/check-runs", field="check_runs")

    async def test_nested_review_comment_pagination_is_not_truncated(self):
        class Client:
            async def graphql(self, query, variables):
                if 'closingIssuesReferences' in query: return closing_graphql(variables)
                if "PullRequestReviewThread" in query:
                    return {"node": {"comments": {"nodes": [{"id": "reply"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
                return {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "thread", "isResolved": False, "isOutdated": True,
                    "comments": {"nodes": [{"id": "root"}], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        threads = await _threads(Client(), {})
        self.assertEqual(["root", "reply"], [item["id"] for item in threads[0]["comments"]])

    async def test_permissions_error_propagates_instead_of_empty_pass(self):
        class Client:
            async def get(self, path, params):
                raise RuntimeError("GitHub 403: resource not accessible")
        with self.assertRaises(RuntimeError):
            await _rest_pages(Client(), "/actions/runs", field="workflow_runs")

    async def test_issue_revision_race_fails_closed(self):
        class Client:
            reads = 0
            async def graphql(self, query, variables):
                if 'closingIssuesReferences' in query: return closing_graphql(variables)
                return {"repository": {"issue": {"lastEditedAt": FIXED, "updatedAt": COMMENTED, "body": "Scope"}}}
            async def get(self, path, params=None):
                if path == "/user": return {"login": "agent"}
                if path.endswith("/comments"): return []
                self.reads += 1
                return {"user": {"login": "agent"}, "updated_at": COMMENTED if self.reads == 1 else REPLIED,
                    "title": "PRD", "body": "Scope", "state": "open"}
        with self.assertRaises(EvidenceError):
            await collect_issue_with_client(Client(), "owner/repo", 1)


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    class Client:
        def __init__(self, change_head=False, rules_fail=False):
            self.reads = 0; self.change_head = change_head; self.rules_fail = rules_fail; self.paths = []
        async def get(self, path, params=None):
            self.paths.append(path)
            if path == "/user": return {"login": "agent"}
            if path == "/repos/owner/repo/pulls/1":
                self.reads += 1
                return {"head": {"sha": OTHER if self.change_head and self.reads > 1 else SHA}, "base": {"ref": "main"}, "draft": False,
                    "merged": False, "state": "open", "user": {"login": "agent"}, "changed_files": 1,
                    "commits": 1, "updated_at": FIXED, "html_url": "pr", "title": "Fix", "body": "", "created_at": BEFORE}
            if path.endswith("/files"): return [{"filename": "src/main.py"}]
            if path == '/repos/owner/repo/branches/main': return {'commit': {'sha': 'c' * 40}}
            if '/compare/' in path:
                return dict(base_commit={'sha': 'c' * 40}, merge_base_commit={'sha': 'c' * 40},
                            ahead_by=1, behind_by=0, status='ahead')
            if path.endswith("/check-runs"):
                return {"total_count": 1, "check_runs": [{"id": 10, "name": "test", "status": "completed", "conclusion": "success",
                    "head_sha": SHA, "app": {"id": 1}, "created_at": FIXED}]}
            if path.endswith("/actions/runs"):
                return {"total_count": 1, "workflow_runs": [{"id": 100, "name": "CI", "workflow_id": 5, "event": "pull_request", "head_sha": SHA,
                    "run_attempt": 2, "status": "completed", "conclusion": "success", "created_at": FIXED}]}
            if path.endswith("/attempts/2/jobs"):
                return {"total_count": 1, "jobs": [{"id": 200, "name": "test", "head_sha": SHA, "status": "completed", "conclusion": "success",
                    "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/10", "started_at": FIXED}]}
            if "/rules/branches/" in path:
                if self.rules_fail: raise RuntimeError("GitHub 403: rules inaccessible")
                return [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "security"}]}}]
            if path.endswith("/reviews"):
                return [{"id": 20, "user": {"login": "reviewer"}, "state": "COMMENTED", "body": "Please handle this", "submitted_at": COMMENTED, "html_url": "review"}]
            if path.endswith("/comments"):
                return [{"id": 20, "user": {"login": "reviewer"}, "body": "Another issue", "created_at": COMMENTED, "updated_at": COMMENTED, "html_url": "comment"}]
            if path.endswith("/statuses"): return []
            raise AssertionError(path)
        async def graphql(self, query, variables):
            if 'closingIssuesReferences' in query: return closing_graphql(variables)
            if "branchProtectionRule" in query:
                pull = {"headRefOid": SHA, "baseRefName": "main", "baseRef": {"branchProtectionRule": {"requiredStatusCheckContexts": ["test"]}}}
            elif "reviews(first" in query:
                pull = {"reviews": {"nodes": [{"id": "R_20", "fullDatabaseId": "20", "author": {"login": "reviewer"}, "state": "COMMENTED", "body": "Please handle this", "submittedAt": COMMENTED, "createdAt": COMMENTED, "updatedAt": FIXED, "url": "review"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
            elif "commits(first" in query:
                pull = {"commits": {"nodes": [{"commit": {"oid": SHA, "committedDate": FIXED}}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
            else:
                pull = {"reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
            return {"repository": {"pullRequest": pull}}

    async def test_complete_collection_combines_rules_and_reads_current_attempt_jobs(self):
        client = self.Client()
        data = await collect_with_client(client, "owner/repo", 1)
        self.assertEqual(["security", "test"], data["expected_checks"])
        self.assertEqual({"workflow", "job", "check"}, {item["kind"] for item in data["checks"]})
        self.assertIn("/repos/owner/repo/actions/runs/100/attempts/2/jobs", client.paths)
        self.assertEqual(2, next(item for item in data["checks"] if item["kind"] == "check")["run_attempt"])
        self.assertEqual({"review:20", "issue:20"}, {item["id"] for item in data["comments"]})
        self.assertEqual(FIXED, next(item for item in data["comments"] if item["kind"] == "review")["updated_at"])

    async def test_collection_exposes_head_race_to_all_gates(self):
        data = await collect_with_client(self.Client(change_head=True), "owner/repo", 1)
        self.assertEqual({"UNKNOWN"}, set(statuses(data).values()))

    async def test_rules_permission_error_does_not_silently_remove_required_checks(self):
        data = await collect_with_client(self.Client(rules_fail=True), "owner/repo", 1)
        self.assertIsNone(data['expected_checks'])
        self.assertEqual('UNKNOWN', statuses(data)['CI_FAILED'])
        self.assertEqual('PASS', statuses(data)['PR_NOT_DRAFT'])
        self.assertEqual('FAIL', statuses(data)['REVIEW_HANDLED'])


if __name__ == "__main__": unittest.main()
