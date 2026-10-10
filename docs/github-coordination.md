# GitHub request coordination

Each gate-owned Python HTTP attempt and each HTTP request inside a gateway gh child is admitted through a durable SQLite coordinator. Primary cooldown keys are GitHub API host + hashed credential context + quota resource; secondary throttles cover that credential context across resources. Token values, authorization headers and request bodies never enter attribution or cooldown records. Credential resolution is local gh auth token; it performs no /user request.

Retry-After and reset deadlines are combined conservatively. With no usable deadline, retry starts at 60 seconds with bounded exponential backoff. Wall and boot-bound monotonic deadlines prevent a clock jump from shortening the wait; reboot uncertainty is reanchored once. Deadline extensions never shorten an existing cooldown. After expiry, one expiring, atomic lease admits a recovery request. Epoch and token fencing reject crashed/late workers and preserve newer throttles. A successful request only clears its matching throttle; it never supplies passing PR evidence.

The coordinator lives in ~/.local/state/caller-pr-gates/github-coordination.sqlite3, separate from the PR registry. Rolling older processes and requests admitted before a throttle may finish; subsequent requests in the affected scope are denied. Existing policy snapshots and receipts are never restored or deleted.

A gate-owned gh subprocess receives a temporary GH_CONFIG_DIR with the supported http_unix_socket setting. It retains the exact gh arguments and receives the current credential in its child environment only. A private Unix socket relays each HTTP request to the authenticated GitHub API over verified TLS, coordinating before forwarding. No certificate or global configuration changes are used. Temporary files contain settings only, not credentials; cleanup runs when the child returns. The relay never retries a request, including mutations with ambiguous outcomes. Such failures require reconciliation.

Attribution is bounded to the latest 20,000 events: caller operation, session/process, target path, hashed account context, quota resource, result and actual request count. Cooldown denials record zero requests. No instrumentation request or chat wakeup is generated. PR monitor cadence and native callbacks remain unchanged.

Collection reuse and narrower validation preflights are the next stage of issue #3; this stage implements cooldown only.

References: [GitHub rate-limit handling](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#handle-rate-limit-errors-appropriately), [GitHub CLI socket configuration](https://cli.github.com/manual/gh_config), [CLI Unix transport implementation](https://github.com/cli/go-gh/blob/trunk/pkg/api/http_client.go).
