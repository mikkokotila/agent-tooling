import copy
import json

import pytest

from caller_pr_gates.github import _component_error
from caller_pr_gates.handoff import current_hold, evidence_fingerprint, hold_unresolved
from caller_pr_gates.store import Store


SID = "6a5c5b59-4537-4c0c-9f67-d6e342e903dd"
REPO = "Owner/Repo"
NUMBER = 12


def snapshot():
    return {"repository": REPO, "number": NUMBER, "head_sha": "a" * 40,
            "state": "open", "merged": False, "collected_at": 2000,
            "comments": [{"id": "inline:1", "updated_at": "2026-10-06T08:00:00Z",
                          "is_resolved": False, "body": "Fix the timeout"}],
            "checks": [{"kind": "job", "key": "job:unit", "name": "unit", "id": "1",
                        "run_attempt": 1, "status": "completed", "conclusion": "failure"}]}


def gates():
    return [{"gate": name, "status": "FAIL" if name == "CI_FAILED" else "PASS", "details": []}
            for name in ("PR_NOT_DRAFT", "CI_FAILED", "CI_PENDING", "REVIEW_HANDLED", "REVIEW_CONCLUDED")]


@pytest.fixture
def state(tmp_path, monkeypatch):
    now = [2000.0]
    monkeypatch.setattr("caller_pr_gates.store.time.time", lambda: now[0])
    monkeypatch.setattr("caller_pr_gates.handoff.time.time", lambda: now[0])
    store = Store(tmp_path / "state")
    store.register(SID, "codex", REPO, NUMBER, str(tmp_path))
    store.set_snapshot(REPO, NUMBER, snapshot(), gates())
    return store, now


def test_poll_telemetry_does_not_change_identity():
    first = snapshot()
    first["component_errors"] = {"rules": {"kind": "feature_unavailable", "http_status": 403,
        "path": "/repos/Owner/Repo/rules/branches/main", "message": "Upgrade to GitHub Pro",
        "rate_limit_headers": {"x-ratelimit-remaining": "4500", "x-ratelimit-reset": "2000"}}}
    later = copy.deepcopy(first)
    later["collected_at"] += 600
    later["checks"][0]["updated_at"] = "2026-10-06T09:00:00Z"
    later["component_errors"]["rules"]["rate_limit_headers"] = {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "3000"}
    assert evidence_fingerprint(first, gates()) == evidence_fingerprint(later, gates())


@pytest.mark.parametrize("change", [
    lambda value: value.update(head_sha="b" * 40),
    lambda value: value["checks"][0].update(id="2"),
    lambda value: value["checks"][0].update(run_attempt=2),
    lambda value: value["comments"][0].update(updated_at="2026-10-06T08:01:00Z"),
    lambda value: value["comments"][0].update(body="Fix the connection timeout"),
    lambda value: value["comments"][0].update(is_resolved=True),
    lambda value: value.update(state="closed", merged=True),
])
def test_actual_evidence_changes_identity(change):
    first, later = snapshot(), snapshot()
    change(later)
    assert evidence_fingerprint(first, gates()) != evidence_fingerprint(later, gates())


@pytest.mark.parametrize("key,value", [("kind", "permission_denied"), ("http_status", 429),
                                       ("path", "/repos/Owner/Repo/rules/branches/dev")])
def test_component_cause_changes_identity(key, value):
    first = snapshot()
    first["component_errors"] = {"rules": {"kind": "feature_unavailable", "http_status": 403,
        "path": "/repos/Owner/Repo/rules/branches/main", "message": "Unavailable"}}
    later = copy.deepcopy(first)
    later["component_errors"]["rules"][key] = value
    assert evidence_fingerprint(first, gates()) != evidence_fingerprint(later, gates())


def test_missing_snapshot_unknown_ignores_quota_headers():
    def unknown(remaining):
        return [{"gate": "GITHUB_DATA", "status": "UNKNOWN", "details": [
            'GitHub 403 rate-limit: {\'x-ratelimit-remaining\': \'' + remaining +
            '\'} body={"message":"Upgrade to GitHub Pro","status":"403"}']}]
    assert evidence_fingerprint({}, unknown("4500")) == evidence_fingerprint({}, unknown("4999"))


def quota_exhausted(request_id, at, used="5000", status=403):
    """GitHub's REST quota rejection, raised the way github_interface raises it."""
    headers = {"x-ratelimit-resource": "core", "x-ratelimit-limit": "5000", "x-ratelimit-remaining": "0",
               "x-ratelimit-used": used, "x-ratelimit-reset": "1791575898"}
    body = ('{\n\t"message": "API rate limit exceeded for user ID 1234567. If you reach out to GitHub Support '
            f'for help, please include the request ID {request_id} and timestamp {at} UTC. For more on scraping '
            'GitHub and how it may affect your rights, please review our Terms of Service '
            '(https:\\/\\/docs.github.com\\/en\\/site-policy\\/github-terms\\/github-terms-of-service)",\n'
            '\t"documentation_url": "https:\\/\\/docs.github.com\\/rest\\/overview\\/rate-limits-for-the-rest-api",\n'
            f'\t"status": "{status}"\n}}')
    return RuntimeError(f"GitHub {status} rate-limit: {headers} body={body}")


def collector_failure(error):
    # hook.evaluate keeps the first 600 characters of a collector exception.
    return [{"gate": "GITHUB_DATA", "status": "UNKNOWN", "details": [str(error)[:600]]}]


def test_quota_rejection_ignores_request_id_timestamp_and_cut():
    first = collector_failure(quota_exhausted("32CC:2453F2:2CE3791:42ECEE3:6AC9436A", "2026-10-09 19:41:30", used="5004"))
    later = collector_failure(quota_exhausted("3320:9F412:2F4352D:46AAF19:6AC943D0", "2026-10-09 19:43:12"))
    # The request IDs differ in length, so the cut also ends the JSON differently.
    assert first[0]["details"][0][-3:] != later[0]["details"][0][-3:]
    assert evidence_fingerprint({}, first) == evidence_fingerprint({}, later)


def test_quota_component_error_ignores_request_id_and_timestamp():
    def rules(request_id, at):
        data = snapshot()
        data["component_errors"] = {"rules": _component_error(quota_exhausted(request_id, at),
                                                               "/repos/Owner/Repo/rules/branches/main")}
        return data
    first = rules("32CC:2453F2:2CE3791:42ECEE3:6AC9436A", "2026-10-09 19:41:30")
    later = rules("3320:9F412:2F4352D:46AAF19:6AC943D0", "2026-10-09 19:43:12")
    assert first["component_errors"]["rules"]["message"] != later["component_errors"]["rules"]["message"]
    assert evidence_fingerprint(first, gates()) == evidence_fingerprint(later, gates())


@pytest.mark.parametrize("other", [
    RuntimeError("GitHub 403 rate-limit: {'x-ratelimit-remaining': '4999'} "
                 'body={"message":"Resource not accessible by integration","status":"403"}'),
    quota_exhausted("32CC:2453F2:2CE3791:42ECEE3:6AC9436A", "2026-10-09 19:41:30", status=429),
    RuntimeError("network unavailable"),
])
def test_quota_rejection_stays_distinct_from_other_causes(other):
    quota = collector_failure(quota_exhausted("32CC:2453F2:2CE3791:42ECEE3:6AC9436A", "2026-10-09 19:41:30"))
    assert evidence_fingerprint({}, quota) != evidence_fingerprint({}, collector_failure(other))


def test_other_403_messages_keep_distinct_identities():
    def denied(message):
        return collector_failure(RuntimeError("GitHub 403 rate-limit: {'x-ratelimit-remaining': '4999'} body="
                                              + json.dumps({"message": message, "status": "403"})))
    assert (evidence_fingerprint({}, denied("Resource not accessible by integration"))
            != evidence_fingerprint({}, denied("Must have admin rights to Repository.")))


def test_same_report_holds_once_and_retains_gate_truth(state):
    store, now = state
    first = hold_unresolved(store, SID, REPO, NUMBER, snapshot(), gates(), turn_id="turn-1")
    now[0] += 600
    again = hold_unresolved(store, SID, REPO, NUMBER, snapshot(), gates(), turn_id="turn-2")
    assert again["reported_at"] == first["reported_at"]
    assert current_hold(store, SID, snapshot(), gates())["state"] == "held_unresolved"
    assert store.prs(SID)[0]["status"] == "active"
    assert store.snapshot(REPO, NUMBER)["gates"] == gates()
    assert again["obligation_complete"] is False


def test_hold_and_changed_evidence_never_steal_live_lease(state):
    store, _ = state
    store.record_recovery(REPO, NUMBER, SID, {"state": "dispatching", "lease_until": 3000, "attempts": 2})
    held = hold_unresolved(store, SID, REPO, NUMBER, snapshot(), gates())
    assert held["lease_until"] == 3000 and held["attempts"] == 2
    changed = snapshot()
    changed["checks"][0]["run_attempt"] = 2
    assert current_hold(store, SID, changed, gates()) is None
    row = store.recovery(REPO, NUMBER, SID)
    assert row["state"] == "pending" and row["attempts"] == 0
    assert row["lease_until"] == 3000 and row["obligation_complete"] is False


def test_missing_snapshot_can_hold_by_explicit_owned_identity(state):
    store, _ = state
    unknown = [{"gate": "GITHUB_DATA", "status": "UNKNOWN", "details": ["network unavailable"]}]
    hold_unresolved(store, SID, REPO, NUMBER, {}, unknown)
    assert current_hold(store, SID, {}, unknown, repository=REPO, number=NUMBER)
    with pytest.raises(ValueError, match="not registered"):
        hold_unresolved(store, SID, REPO, 999, {}, unknown)


def test_collector_outage_cannot_release_or_replace_known_head_hold(state):
    store, now = state
    first = hold_unresolved(store, SID, REPO, NUMBER, snapshot(), gates())
    now[0] += 600
    unknown = [{"gate": "GITHUB_DATA", "status": "UNKNOWN", "details": ["network unavailable"]}]
    held = current_hold(store, SID, {}, unknown, repository=REPO, number=NUMBER)
    assert held["fingerprint"] == first["fingerprint"]
    again = hold_unresolved(store, SID, REPO, NUMBER, {}, unknown)
    assert again["reported_at"] == first["reported_at"]
    assert again["handoff_head_sha"] == snapshot()["head_sha"]
    assert current_hold(store, SID, snapshot(), gates())
