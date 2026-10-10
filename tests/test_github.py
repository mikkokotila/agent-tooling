"""Collector regressions use API fixtures and never open a state database."""
import json
import unittest
from types import SimpleNamespace

from caller_pr_gates.github import EvidenceError, _component_error, collect_with_client
from caller_pr_gates.policy import evaluate_pr


SHA = "a" * 40
OTHER = "b" * 40
STAMP = "2026-10-06T08:00:00Z"
RULES_PATH = "/repos/mikkokotila/caller/rules/branches/main"
FEATURE_MESSAGE = "Upgrade to GitHub Pro or make this repository public to enable this feature."
FEATURE_BODY = json.dumps({"message": FEATURE_MESSAGE,
    "documentation_url": "https://docs.github.com/rest/repos/rules#get-rules-for-a-branch", "status": "403"})
HIGH_QUOTA = {"x-ratelimit-resource": "core", "x-ratelimit-limit": "5000",
    "x-ratelimit-remaining": "4725", "x-ratelimit-used": "275", "x-ratelimit-reset": "1791273615"}


def response_error(status=403, message=FEATURE_MESSAGE, headers=None):
    body = FEATURE_BODY if message == FEATURE_MESSAGE else json.dumps({"message": message})
    if headers is None:
        return RuntimeError(f"GitHub {status}: {body}")
    return RuntimeError(f"GitHub {status} rate-limit: {headers!r} body={body}")


class Client:
    def __init__(self, rule_error=None, rules=None, head_race=False, lifecycle_race=False):
        self.rule_error, self.rules = rule_error, rules or []
        self.head_race, self.lifecycle_race = head_race, lifecycle_race
        self.reads = 0
        self.paths = []

    async def get(self, path, params=None):
        self.paths.append(path)
        if path == "/user":
            return {"login": "agent"}
        if path == "/repos/mikkokotila/caller/pulls/50":
            self.reads += 1
            return {"head": {"sha": OTHER if self.head_race and self.reads > 1 else SHA},
                "base": {"ref": "main"},
                "state": "closed", "merged": not (self.lifecycle_race and self.reads > 1), "draft": False,
                "user": {"login": "agent"}, "changed_files": 1, "commits": 1,
                "updated_at": STAMP, "html_url": "https://github.com/mikkokotila/caller/pull/50",
                "title": "Collector fix", "body": "Original scope", "created_at": STAMP}
        if path == RULES_PATH:
            if self.rule_error:
                raise self.rule_error
            return self.rules
        if path == '/repos/mikkokotila/caller/branches/main':
            return {'commit': {'sha': 'c' * 40}}
        if '/compare/' in path:
            return dict(base_commit={'sha': 'c' * 40}, merge_base_commit={'sha': 'c' * 40},
                        ahead_by=1, behind_by=0, status='ahead')
        if path.endswith("/files"):
            return [{"filename": "src/collector.py"}]
        if path.endswith("/check-runs"):
            return {"total_count": 1, "check_runs": [{"id": 10, "name": "test", "app": {"id": 1},
                "head_sha": SHA, "status": "completed", "conclusion": "success", "created_at": STAMP}]}
        if path.endswith("/statuses"):
            return []
        if path.endswith("/actions/runs"):
            return {"total_count": 1, "workflow_runs": [{"id": 100, "name": "CI", "workflow_id": 5,
                "event": "pull_request", "head_sha": SHA, "run_attempt": 2,
                "status": "completed", "conclusion": "success", "created_at": STAMP}]}
        if path.endswith("/attempts/2/jobs"):
            return {"total_count": 1, "jobs": [{"id": 200, "name": "test", "head_sha": SHA,
                "status": "completed", "conclusion": "success", "started_at": STAMP,
                "check_run_url": "https://api.github.com/repos/mikkokotila/caller/check-runs/10"}]}
        if path.endswith("/comments"):
            return [{"id": 20, "user": {"login": "reviewer"}, "body": "Please fix this edge case",
                "created_at": STAMP, "updated_at": STAMP, "html_url": "https://github.com/mikkokotila/caller/pull/50#issuecomment-20"}]
        raise AssertionError(path)

    async def graphql(self, query, variables):
        if 'closingIssuesReferences' in query:
            from test_policy import closing_graphql
            return closing_graphql(variables, merged=True)
        if "branchProtectionRule" in query:
            pull = {"headRefOid": SHA, "baseRefName": "main", "baseRef": {
                "branchProtectionRule": {"requiredStatusCheckContexts": ["test"]}}}
        elif "commits(first" in query:
            pull = {"commits": {"nodes": [{"commit": {"oid": SHA, "committedDate": STAMP}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        else:
            field = "reviews" if "reviews(first" in query else "reviewThreads"
            pull = {field: {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        return {"repository": {"pullRequest": pull}}


class RulesCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_paid_feature_403_with_high_quota_preserves_merged_pr_successful_components(self):
        client = Client(rule_error=response_error(headers=HIGH_QUOTA))
        data = await collect_with_client(client, "mikkokotila/caller", 50)
        self.assertTrue(data["merged"])
        self.assertEqual("closed", data["state"])
        self.assertFalse(data["draft"])
        self.assertEqual(SHA, data["head_sha_after"])
        self.assertEqual(2, client.reads)
        self.assertEqual({"check", "job", "workflow"}, {check["kind"] for check in data["checks"]})
        self.assertEqual(2, next(check for check in data["checks"] if check["kind"] == "check")["run_attempt"])
        self.assertEqual(["issue:20"], [comment["id"] for comment in data["comments"]])
        self.assertEqual([SHA], data["commit_shas"])
        self.assertEqual(["src/collector.py"], data["changed_files"])
        # The plan cannot have rulesets, so branch protection is the whole required inventory.
        self.assertEqual(["test"], data["expected_checks"])
        self.assertEqual(["test"], data["known_expected_checks"])
        self.assertEqual({"rules": "not_available_on_plan"}, data["component_status"])
        error = data["component_errors"]["rules"]
        self.assertEqual("feature_unavailable", error["kind"])
        self.assertEqual(403, error["http_status"])
        self.assertEqual(FEATURE_MESSAGE, error["message"])
        self.assertEqual(FEATURE_BODY, error["response_body"])
        self.assertEqual(HIGH_QUOTA, error["rate_limit_headers"])
        gates = {gate["gate"]: gate["status"] for gate in evaluate_pr(data, [])}
        self.assertEqual("PASS", gates["PR_NOT_DRAFT"])
        self.assertEqual("PASS", gates["CI_FAILED"])
        self.assertEqual("PASS", gates["CI_PENDING"])
        self.assertEqual("FAIL", gates["REVIEW_HANDLED"])

    async def test_plan_without_rulesets_still_judges_every_actual_check(self):
        class FailingCheck(Client):
            async def get(self, path, params=None):
                result = await super().get(path, params)
                if path.endswith("/check-runs"):
                    result = {**result, "check_runs": [{**result["check_runs"][0], "conclusion": "failure"}]}
                return result
        data = await collect_with_client(FailingCheck(rule_error=response_error(headers=HIGH_QUOTA)), "mikkokotila/caller", 50)
        self.assertEqual({"rules": "not_available_on_plan"}, data["component_status"])
        gates = {gate["gate"]: gate["status"] for gate in evaluate_pr(data, [])}
        self.assertEqual("FAIL", gates["CI_FAILED"])

    async def test_permission_and_transient_rules_errors_keep_required_inventory_unknown(self):
        for error, kind in [(response_error(message="Resource not accessible by integration", headers=HIGH_QUOTA), "permission_denied"),
                            (response_error(status=503, message="Service unavailable"), "transient"),
                            (TimeoutError("request timed out"), "transient")]:
            with self.subTest(kind=kind):
                data = await collect_with_client(Client(rule_error=error), "mikkokotila/caller", 50)
                self.assertEqual(kind, data["component_errors"]["rules"]["kind"])
                self.assertIsNone(data["expected_checks"])
                self.assertEqual(SHA, data["head_sha_after"])
                self.assertEqual(3, len(data["checks"]))
                self.assertEqual(1, len(data["comments"]))

    async def test_successful_rules_and_branch_protection_combine_without_errors(self):
        rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "security"}]}}]
        data = await collect_with_client(Client(rules=rules), "mikkokotila/caller", 50)
        self.assertEqual(["security", "test"], data["expected_checks"])
        self.assertEqual({}, data["component_errors"])
        self.assertEqual({"rules": "complete"}, data["component_status"])

    async def test_malformed_or_unsupported_rules_cannot_become_empty_required_inventory(self):
        for rules in [[{"type": "required_status_checks", "parameters": {}}], [{"type": "workflows"}]]:
            with self.subTest(rules=rules):
                data = await collect_with_client(Client(rules=rules), "mikkokotila/caller", 50)
                self.assertIsNone(data["expected_checks"])
                self.assertEqual("invalid_evidence", data["component_errors"]["rules"]["kind"])
                self.assertEqual(3, len(data["checks"]))

    async def test_rules_gap_never_masks_changed_head_or_lifecycle(self):
        data = await collect_with_client(Client(rule_error=response_error(headers=HIGH_QUOTA), head_race=True), "mikkokotila/caller", 50)
        self.assertEqual(OTHER, data["head_sha_after"])
        self.assertEqual({"UNKNOWN"}, {gate["status"] for gate in evaluate_pr(data, [])})
        with self.assertRaisesRegex(EvidenceError, "merged changed"):
            await collect_with_client(Client(rule_error=response_error(headers=HIGH_QUOTA), lifecycle_race=True), "mikkokotila/caller", 50)


class ResponseClassificationTests(unittest.TestCase):
    def test_real_quota_signals_beat_feature_prose_and_client_rate_limit_label(self):
        for status, headers, message in [(403, {**HIGH_QUOTA, "x-ratelimit-remaining": "0"}, FEATURE_MESSAGE),
                                         (403, {**HIGH_QUOTA, "retry-after": "60"}, FEATURE_MESSAGE),
                                         (403, HIGH_QUOTA, "You have exceeded a secondary rate limit.")]:
            with self.subTest(status=status, headers=headers, message=message):
                self.assertEqual("rate_limited", _component_error(response_error(status, message, headers), RULES_PATH)["kind"])
        self.assertEqual("feature_unavailable", _component_error(response_error(headers=HIGH_QUOTA), RULES_PATH)["kind"])

    def test_structured_chained_response_is_authoritative(self):
        cause = RuntimeError("HTTP403")
        cause.response = SimpleNamespace(status_code=403, text=FEATURE_BODY, headers=HIGH_QUOTA)
        error = RuntimeError("generic client failure")
        error.__cause__ = cause
        result = _component_error(error, RULES_PATH)
        self.assertEqual("feature_unavailable", result["kind"])
        self.assertEqual(FEATURE_MESSAGE, result["message"])
        self.assertEqual(HIGH_QUOTA, result["rate_limit_headers"])

    def test_generic_403_or_feature_prose_on_other_endpoint_is_not_feature_unavailable(self):
        self.assertEqual("permission_denied", _component_error(RuntimeError("GitHub 403: rules inaccessible"), RULES_PATH)["kind"])
        self.assertEqual("permission_denied", _component_error(response_error(headers=HIGH_QUOTA), "/repos/mikkokotila/caller/check-runs")["kind"])


if __name__ == "__main__":
    unittest.main()
