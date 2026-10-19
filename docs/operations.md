# Operational inventory

The v0.1.0 baseline was captured from the installed 1.7.0 package, not from the older scratch source named by its former release manifest.

| Component | Versioned source | Live location |
|---|---|---|
| Runtime Python modules and rescue prompt defaults | `caller_pr_gates/` | `~/.local/share/caller-pr-gates/caller_pr_gates/` |
| PR checklist, one-time PRD publication checklist, 58 retained CLI templates | `prompts.toml` | `~/.local/share/caller-pr-gates/prompts.toml` |
| Codex callback definitions | `config/codex-hooks.json` | `~/.codex/hooks.json` |
| Claude callback definitions | `config/claude-hooks.json` | `~/.claude/settings.json`, hooks only |
| Captured Codex hook trust | `capture/codex-trust.json` | `~/.codex/config.toml`, managed entries only |
| CLI wrapper | `config/launcher.sh` | `~/.local/bin/caller-pr-gates` |
| launchd monitor | `config/monitor.plist` | `~/Library/LaunchAgents/local.caller-pr-monitor.plist` |
| Exact dependency payloads | `wheels/runtime/`, `requirements-runtime.lock` | `~/.local/share/caller-pr-stop-gate/` |
| Managed global instructions | One PR checklist block | Marked portions of `~/.codex/AGENTS.md` and `~/.claude/CLAUDE.md` |
| Deployment provenance/backups | Installer | `~/.local/share/agent-tooling/` |
| Existing obligations/evidence/cache/logs | Remain local, never imported into Git | `~/.local/state/caller-pr-gates/` |

The wrapper/hook commands retain their existing absolute paths, client identifiers, environment, event coverage and timeouts. Codex and Claude use the same primary hook entrypoint across their lifecycle events. The two advisory callbacks are removed and their old entrypoint is silent even for cached client settings. Primary callback commands remain unchanged; future installation must not manufacture trust for a changed command.

The launchd service is `local.caller-pr-monitor`, with a 60-second supervisor loop and 300/600-second single/multiple-PR polling. Ownership remains with the exact recorded chat. Closed/merged PR watches retire and queued messages are rejected. Legacy automatic issue-closeout and document delivery loops remain retired; existing evidence remains local. No new chat, recurring automation or ownership transfer is created by installation.

The `pr-gate-supervisor` heartbeat remains paused. ACP remote audit and the old stop gate remain disabled/no-op. No legacy hook is enabled by this repository.

Gate semantics remain PR_NOT_DRAFT, CI_FAILED, CI_PENDING, REVIEW_HANDLED, REVIEW_CONCLUDED, MERGE_CONFLICT, BRANCH_UP_TO_DATE and ISSUE_CLOSURE. Remote-first publication, background local validation, actual current target ancestry, closing actual linked issues, review receipts/thread closure, immutable scope and stale-delivery checks remain in the CLI. PR monitoring uses one fixed checklist; Stop never polls or blocks. Confirmed new PRD publication additionally supplies one review checklist as PostToolUse context to the exact owning root chat and client. Existing followup records do not acquire this receipt. Document polling runs in the agent's active turn; no legacy reminder job is reactivated.

Dependencies include locally modified `github-interface` 0.2.0 and `agent-interface` 0.3.0. The offline wheels reconstruct their actual installed source and package metadata; credentials, bytecode, absolute CLI wrappers and `direct_url.json` are excluded. Upstream package metadata/license files are retained. Runtime file hashes and wheel hashes are in `capture/operational-baseline.json`.

The installer does not install, update or copy the Codex desktop application, Claude CLI, GitHub CLI, macOS, system Python, account credentials or personal plugins. These existing host applications remain prerequisites. The callback wrapper retains the currently installed application paths. All gate-owned Python dependencies are captured.

Official host references: [Codex hooks and trust](https://learn.chatgpt.com/docs/hooks), [GitHub macOS runner architecture](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

Release 0.1.9 accepts an idle Claude desktop session whose launch directory and registered PR worktree share the same Git common directory. Resume still uses the exact session ID and PR checkout. Native consumption requires that checkout; queued body hashes survive live wording edits. Rejected delivery becomes a recorded terminal failure rather than blocking every PR in that chat. Rulesets absent on an account plan do not suppress independently verified repair work. Availability checks precede remote delivery reads and unavailable checkouts back off to the polling cadence.

Release 0.1.10 records worker execution failures even after native delivery was consumed. A structured Claude `is_error` result also fails the delivery attempt. The error remains local, the real worker lease ends on exit, and unchanged failed work is not automatically replayed.

Release 0.1.27 adds a five-minute receipt deadline to Codex checklist deliveries. Queue acceptance does not clear that deadline. An idle owner that has not consumed the event becomes a persistent local monitor-health error; the existing queued event remains authentic and consumable. Legacy queued records receive one fixed grace period. The guard never enqueues another copy, changes polling cadence, interrupts an active owner, or alters Claude execution. A late native receipt clears the delivery error. Automatic desktop recovery requires an authorized execution interface; direct external app-tool clients can be rejected by the desktop's code-signing boundary.

Release 0.1.29 adds `caller-pr-gates link-issues PARENT_URL CHILD_URL --session SESSION` for native issue hierarchy. The child must be the chat's recorded PRD/slice; both URLs must identify distinct issues in the same repository. The operation verifies remote identities and the current parent, never replaces a parent, skips an existing correct edge, and verifies readback after one POST. No arbitrary API-write permission or PR ownership is added. The shared checklist tells both clients to repair routine tooling gaps without another permission request and continue the next authorized task while retaining incomplete blocked work. Repeated actionable hook delivery and merge/validation gates are unchanged.

## Retained post-merge qualification

External merge can retire a PR watch while an already declared local check is still running. Let that managed process finish naturally. Retirement preserves its real exit, before/after input digests, validation plan and review evidence; it does not complete the original task.

For a remaining declared check, use the existing runner from the exact original owner session and recorded checkout:

```sh
caller-pr-gates validate --session OWNER_SESSION \
  --pr https://github.com/OWNER/REPO/pull/NUMBER \
  --cwd /absolute/recorded/checkout --check REMAINING_CHECK \
  --merged-head FULL_ORIGINAL_PR_HEAD_SHA --background
```

`--merged-head` names the original PR head GitHub records as merged, not the squash/merge commit or today's default-branch tip. The checkout must still be clean at that exact commit, with its recorded origin and identical source, declared inputs and environment fingerprint. Live preflight checks the stable merged lifecycle, source/target repository identity and merge commit; the background worker repeats admission. A different owner, checkout, missing retained plan, changed input, unmerged closed PR or unavailable proof refuses admission. This continuation is limited to same-repository PRs.

The retained plan stays necessary and frozen. `validation-plan` accepts the same `--merged-head` option to retain it or, after an actual failure, add a distinct diagnostic check under the existing rules. It cannot remove required checks, rewrite completed evidence or authorize replay of an unchanged failure. Inspect the actual failed result and diagnose it. A source change beyond the pinned merged product requires its own genuine scoped PR.

No registration, monitor restart, database repair or placeholder PR is needed. Closed plans remain visible as incomplete task progress, including when their checkout is missing. Once all retained local checks pass, progress reports `local_checks_passed: true` with `completion_verified: false`; acceptance still requires the original task's evidence. Closed PRs report `MERGE_READY: WAIT` with `applicable: false`, never a synthetic passing readiness receipt. Existing open-PR CI/review/ancestry gates and reminder retirement are unchanged.
