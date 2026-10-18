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
