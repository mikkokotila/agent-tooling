# Explicitly retained defects in v0.1.0

v0.1.0 captures current operational behavior. It does not repair the GitHub quota Stop loop described in the source task.

The original `_error_identity` strips quota header telemetry but retains per-request request IDs/timestamps inside error messages, including truncated JSON. This changes the evidence fingerprint for an unchanged outage. With outstanding publication/correction work, a truthful second Stop can therefore be blocked again in both clients.

On the immutable v0.1.0 tag, these original regression tests are marked `xfail(strict=True)`:

- `tests/test_handoff.py::test_quota_rejection_ignores_request_id_timestamp_and_cut`
- `tests/test_handoff.py::test_quota_component_error_ignores_request_id_and_timestamp`
- `tests/test_stop_handoff.py::StopHandoffTests::test_unchanged_quota_outage_is_reported_once_then_yields`

Strict expected failure means they are visibly failing as expected; an unexpected pass fails this capture's verification too. A later fix must deliberately update the known-defect inventory/marks and verify the corrected behavior. All other regressions and new installation tests must pass.

The separately discussed shared cooldown is also outside this release. There is no new primary-quota cooldown/backoff added to the captured gate. No PR gates are waived and no state database is edited to silence this defect.

## Issue #3 repair

Version 0.1.1 removes the three expected-failure marks after correcting stable quota identity and retaining full redacted diagnostics. Version 0.1.2 adds shared cooldown; 0.1.3 adds collection profiles and measured request reductions. The v0.1.0 tag remains the immutable capture.

Issue #3 repairs these captured failures in 0.1.1 and adds coordinated recovery and narrower collections through 0.1.3. Current required tests have no expected failures. The original capture remains immutable.

### Owned Limen slice sweep dispatch (0.1.36)

The gateway previously denied the repository-prescribed trusted-main sweep needed to replace a stale API-posted slice failure. Admit only that named Limen workflow with ref main and no inputs, behind owned open-PR and fresh ancestry checks. Negative-route tests retain rejection of unrelated actions and immutable-input tests prevent a caller-file race.
