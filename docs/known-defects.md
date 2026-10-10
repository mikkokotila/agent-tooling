# Explicitly retained defects in v0.1.0

v0.1.0 captures current operational behavior. It does not repair the GitHub quota Stop loop described in the source task.

The original `_error_identity` strips quota header telemetry but retains per-request request IDs/timestamps inside error messages, including truncated JSON. This changes the evidence fingerprint for an unchanged outage. With outstanding publication/correction work, a truthful second Stop can therefore be blocked again in both clients.

These original regression tests remain in the suite and are marked `xfail(strict=True)` with this reason:

- `tests/test_handoff.py::test_quota_rejection_ignores_request_id_timestamp_and_cut`
- `tests/test_handoff.py::test_quota_component_error_ignores_request_id_and_timestamp`
- `tests/test_stop_handoff.py::StopHandoffTests::test_unchanged_quota_outage_is_reported_once_then_yields`

Strict expected failure means they are visibly failing as expected; an unexpected pass fails this capture's verification too. A later fix must deliberately update the known-defect inventory/marks and verify the corrected behavior. All other regressions and new installation tests must pass.

The separately discussed shared cooldown is also outside this release. There is no new primary-quota cooldown/backoff added to the captured gate. No PR gates are waived and no state database is edited to silence this defect.

## Issue #3 repair

Version 0.1.1 removes the three expected-failure marks after correcting stable quota identity and retaining full redacted diagnostics. Shared cooldown and collection reuse remain pending later stages of this same issue until their tests and installation are verified. The v0.1.0 tag remains the immutable capture.
