# GitHub request coordination

Each gate-owned Python HTTP attempt and each HTTP request inside a gateway gh child is admitted through a durable SQLite coordinator. Primary cooldown keys are GitHub API host + hashed credential context + quota resource; secondary throttles cover that credential context across resources. Token values, authorization headers and request bodies never enter attribution or cooldown records. Credential resolution is local gh auth token; it performs no /user request. Mutating gateway calls additionally resolve the token's login through one cached /user lookup per token and refuse unless it is the owner identity; reads never look it up.

Retry-After and reset deadlines are combined conservatively. With no usable deadline, retry starts at 60 seconds with bounded exponential backoff. Wall and boot-bound monotonic deadlines prevent a clock jump from shortening the wait; reboot uncertainty is reanchored once. Deadline extensions never shorten an existing cooldown. After expiry, one expiring, atomic lease admits a recovery request. Epoch and token fencing reject crashed/late workers and preserve newer throttles. A successful request only clears its matching throttle; it never supplies passing PR evidence.

The coordinator lives in ~/.local/state/caller-pr-gates/github-coordination.sqlite3, separate from the PR registry. Rolling older processes and requests admitted before a throttle may finish; subsequent requests in the affected scope are denied. Existing policy snapshots and receipts are never restored or deleted.

Schema markers make reopening either database read-only after its additive migration. Active collection waiters do not acquire the writer lock while another generation reads. They still wait for a generation that meets their own freshness boundary; cancellation and expired workers cannot release or overwrite a different generation. This avoids writer contention across concurrent native clients without adding HTTP requests or caching old passing evidence.

A gate-owned gh subprocess receives a temporary GH_CONFIG_DIR with the supported http_unix_socket setting. It retains the exact gh arguments and receives the current credential in its child environment only. A private Unix socket relays each HTTP request to the authenticated GitHub API over verified TLS, coordinating before forwarding. No certificate or global configuration changes are used. Temporary files contain settings only, not credentials; cleanup runs when the child returns. The relay never retries a request, including mutations with ambiguous outcomes. Such failures require reconciliation.

If completion accounting fails after an upstream response, the relay sends HTTP 502 with an explicit accounting/possible-mutation uncertainty message. After attempting either existing settlement operation (`finish()` or `throttle()`), it does not attempt `finish()` during cleanup: a persistent writer lock cannot add a second busy wait before that response. A mutation may already have applied: read back its exact intended result before considering any retry. This response does not establish successful accounting or a failed mutation. A transport failure that has not attempted settlement still gets one best-effort unsuccessful-request cleanup, using the existing backoff. If settlement cannot complete, the existing lease and epoch fencing remain authoritative. The relay does not replay the request, erase reservations or invent a successful attribution receipt. Existing upstream, database and caller time limits are unchanged; a caller that has already timed out still requires reconciliation.

Attribution is bounded to the latest 20,000 events: caller operation, session/process, target path, hashed account context, quota resource, result and actual request count. Cooldown denials record zero requests. No instrumentation request or chat wakeup is generated. PR monitor cadence and native callbacks remain unchanged.

## Collection contract

Profiles are `full-pr`, `issue`, `review` and `validation`, keyed by authenticated credential context and exact target. A 30 ms admission window permits overlapping callers to register before the leader starts any read. The recorded boundary is the latest participating invocation. Joined callers share only that generation; callers arriving after reads start wait for their own generation. Strict fresh invocations never reuse an earlier result. Wall and boot-bound monotonic start times protect boundaries; an expiring lease/token fences crashed or late collection workers. Errors are shared as errors, never as old passing evidence. This admission window is not an evidence TTL.

Full collection reads mutable lifecycle, CI, comments/replies, branch rules, live ancestry and closing issue links again. The first GraphQL pages for actor, protection, threads, reviews, commits and closing links share one response; every connection still enumerates and verifies all additional pages. Complete diff inventories are separately cached only against exact repository/PR, head, live target tip and file count. CI, review and issue state are never cached solely by head.

REST GETs use authenticated `If-None-Match` when a complete representation exists for the exact account/path/parameters/API version. A 304 is a real revalidation, not a TTL hit. A failed response raises; it never returns the previous representation. Not every endpoint supplies an ETag or excludes 304 from quota, so accounting records actual HTTP requests rather than claiming quota-point savings.

Validation preflight reads initial PR, live target branch, exact SHA comparison, final PR and final target branch: five REST requests. Both launcher and worker invoke it independently. An initial full baseline is collected when missing or on a changed head; subsequent preflights never replace the complete snapshot or advance its timestamp. Passing local checks wait for fresh full evidence before completion. A newer complete collection supersedes an older preflight; newer failed preflights block older proof. Legacy undated failures are conservatively stamped once during additive migration. Local ownership/plan/check/input eligibility is checked before avoidable reads. Known unresolved review conclusions retain their existing priority.

`address`, `resolve` and PR `review-read` use a review profile: authenticated actor, complete threads/replies/review/issue comments, commit chronology and double head/lifecycle reads. This profile cannot replace a full policy snapshot. Replies and thread resolution still require the original evidenced disposition. Every pagination cursor and comment revision remains checked.

`supervise --verbose` retains global read/refresh semantics even without `--execute`; output now states that network scope. `status --fresh`, completion and merge retain fresh full reads. Monitor delivery still revalidates lifecycle and exact ownership. Attribution reports operation, profile/generation, session/process, target, hashed account/resource, freshness/reuse, actual HTTP count and outcome. It costs no additional GitHub requests.

## Offline request budgets

Single-page fixture budgets below exclude workflow-job pages; each selected workflow adds its actual job pages. Pagination and retries can increase every budget. GraphQL HTTP requests are not GraphQL quota points.

| Operation | Before | After |
| --- | --- | --- |
| Full PR, cold inventory | 12 REST + 5 GraphQL | 11 REST + 1 GraphQL |
| Full PR, same head/live base and cached diff | 12 REST + 5 GraphQL | 10 REST + 1 GraphQL |
| Plan + N background checks | `1+2*N` full collections | at most one initial full plus `1+2*N` narrow preflights |
| Three review actions | 36 REST + 15 GraphQL | 12 REST + 9 GraphQL |
| Eligible Stop/status or monitor/supervisor overlap | two full collections | one shared full generation |
| Strict later fresh read | full reload | new full generation; conditional REST/diff reuse only |
| Collection requiring a resource under cooldown | repeated failed attempts | zero HTTP starts |

A first plan with no full baseline costs an additional five-request preflight before that baseline. For N=3 and one workflow-job page, the original validation path is 91 REST + 35 GraphQL; the new path is at most 47 REST + 1 GraphQL. With an existing baseline, validation uses only the narrow preflights. Request-count tests verify both profile routing and actual injected-client counts; they do not turn historical account quota deltas into exact attribution.

## Publication reconciliation

Registration or a terminal branch name alone cannot erase unpublished source records. Reconciliation requires verified publication/lifecycle, matching head/commit inventory, exact repository/branch and clean source, the same Git common directory as an explicitly owned checkout, and unchanged owner/record/source checks before deletion. Real edits, different worktrees/repos, missing proof and races retain blockers. Retirement never manufactures passing gate evidence.

References: [GitHub rate-limit handling](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#handle-rate-limit-errors-appropriately), [GitHub CLI socket configuration](https://cli.github.com/manual/gh_config), [CLI Unix transport implementation](https://github.com/cli/go-gh/blob/trunk/pkg/api/http_client.go).

## Owned Limen slice-check recovery

The gateway permits only `POST repos/Vaquum/Limen/actions/workflows/pr_checks_slice_sweep.yml/dispatches` with the exact body `{"ref":"main"}` for an owned open PR containing the fresh live target. The active monitor owner and exact checkout must match both before and after the remote read; a historical registration receipt alone is insufficient. This invokes Limen's existing trusted-main sweep when an API-posted stale slice verdict outranks a newer canonical run. No other repository, workflow, ref or input is admitted; the validated request body is passed as immutable stdin. It does not post check verdicts, waive gates or execute a candidate workflow.
