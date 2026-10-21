# Agent tooling

Caller PR orchestration for Codex and Claude on macOS arm64 / Python 3.13. Release 0.1.60 installs runtime 1.7.60.

PRs above250 commits use explicitly paginated comparisons pinned to their full PR base/head SHAs. Complete counts, commit dates, unique identities, head inclusion and stable final PR identity remain required. Ordinary PR batching and all review/CI gates remain unchanged.

Retained documentation checks can explicitly select a committed sibling checker/provenance pair from fresh live-target history. Script/module arguments and the existing composite reader remain frozen; its exact generated launcher changes `LIMEN_PYTHON` only for that corrected execution. Original failures remain recorded until actual corrected checks pass.

An explicit successor qualification reconciles recorded running checks only after validating both retained merged identities. Verified exited owners become audited UNKNOWN results; live or unobservable owners still block. Ordinary status reads do not alter execution history.

After an external merge, the recorded owner can finish its retained qualification with `validation-plan` / `validate --merged-head ORIGINAL_PR_HEAD`. A new private control can declare per-check `input_paths` and receive its own frozen input pin without resetting retained results. The exact checkout, unchanged producer and retained inputs, and stable live merged-source proof remain required. Retired reminders stay retired; closed PRs do not establish task acceptance or merge readiness. See [operations](docs/operations.md#retained-post-merge-qualification).

A failed private Python checker can be corrected through an explicit `correction` link on a fresh pinned check. Its diagnosis, evidence, original obligation and failed run remain recorded. Only a real passing corrected execution can satisfy that obligation; historical failures are never relabeled as passes. See [checker corrections](docs/operations.md#correcting-a-failed-private-checker).

Narrow validation preflights pin actual lifecycle, head and target ancestry while allowing unrelated comment timestamps to change. Full review and completion freshness remain unchanged.

Dependency-directory validation fingerprints exclude Vitest's regenerated timing records. Dependency code, tracked files and explicitly declared cache files remain inputs.

The GitHub gateway can create the exact private personal repository authorized in the recorded scope (`repository_creation`: `full_name`, `visibility=private`, `approval_evidence`). It admits only `repo create OWNER/NAME --private [--description TEXT]`, verifies the authenticated owner and reads back identity and visibility. Clone, push, public creation and other repository mutations remain outside this operation. The immutable v0.1.0 tag preserves the original operational 1.7.0 capture.

```sh
git pull --ff-only
make install
```

Installation verifies the committed candidate offline and activates it transactionally. Updating Git alone never changes live hooks. `make verify`, `make build`, `make status` and `make rollback` are also available. Rollback keeps current PR state.

PR monitoring uses one **Automated PR hook** checklist from `[prompts.pr_checklist]`. It is bound to the first recorded owner chat and PR checkout; another chat in the same repository cannot steal either. Scratch-file redirects follow preceding literal shell directory changes; writes in a shared checkout cannot adopt another chat's monitored PR. Session status excludes legacy foreign registration receipts while retaining their audit history. Ambiguous shell command lists do not establish a relative write checkout; use explicit PR registration for those cases. Absolute write targets and existing recorded ownership remain available. Completion also requires fresh GitHub merge readiness. CI waits stay in-session for up to 20 minutes; reviews are re-read after CI. Stop is silent. Unresolved actionable work returns to an idle owner every five minutes, or ten minutes with multiple PRs, even if unchanged. A delivery receipt proves receipt, never completion. Busy chats receive new work through native context; idle chats receive an explicit automated chat message. Pending checks and API outages alone do not wake chats. Merged/closed PRs cancel delivery, including queued messages. Legacy issue/document reminder loops and formatting messages remain retired. Local evidence and CLI diagnostics remain available.

PRD publication adds one separate `[prompts.prd_review_checklist]` instruction. Record agreed scope with `scope --file FILE`, then publish through `github --kind prd -- issue create ...`; slices use `--kind slice --slice-id ID`. A successful creation and GitHub readback record a receipt. The next PostToolUse callback in the exact root chat and client consumes it once. Reads, edits, linked slices and old records cannot trigger it. The agent keeps the turn active, polls every five minutes, handles findings and waits for every scheduled review before implementation. This adds no background document scheduler or polling API calls. Review completion must be evidenced; elapsed time or the first review is insufficient.

The checker retains separate CI, review, conflict, current target ancestry and linked-issue gates. It publishes before local validation, tracks exact owner chats and polls one open PR every five minutes or several every ten minutes. API errors alone never restart chats.

Under concurrent chat load, registry and request-coordinator migrations run once rather than on every native callback. Collection waiters observe a running generation without taking the writer lock; strict freshness and lease fencing still apply. Owned inbox delivery survives independent discovery failures. Native lifecycle/base proof allows five seconds per parallel read, and temporary failures retain pending work with a local deferred-revalidation receipt. Overlapping callbacks consume an event atomically once. Authenticated queued work with unavailable proof is deferred to the normal cadence with a new event token; the unverified turn is canceled. Failed native proof also backs off so another ready PR can proceed without repeated reads. Consumed receipts record client and hook event, distinguishing hidden context from queued resumes. No polling cadence or completion gate changes.

Issue #3 adds stable quota blocker identity, durable shared request cooldown and freshness-bound collection sharing. Validation launch and execution each read live ancestry without reloading the whole CI/review inventory. Completion and merge still require full current evidence. Source, the editable PR checklist and 58 retained CLI templates, captured dependency wheels and client wiring are versioned here; tokens, personal settings and mutable task state remain local.

Copilot service availability never blocks completion or merge. Every actual Copilot change request still requires the same evidenced disposition, reply and thread resolution as other reviews. See [the service/review policy](docs/copilot-policy.md).

Edit the annotated live `~/.local/share/caller-pr-gates/prompts.toml` to influence both clients immediately, or version repository edits and install them. Valid local edits are preserved; conflicting edits require reconciliation.

See [installation and rollback](docs/installation.md), [request coordination and budgets](docs/github-coordination.md), [operational inventory](docs/operations.md) and [defect history](docs/known-defects.md).

The active checklist and completion rules live together in `[prompts.pr_checklist]` in the installed `~/.local/share/caller-pr-gates/prompts.toml`. Both clients mirror this text inside the managed markers. Installation retires only the exact known separately appended completion block, preserves its original bytes in the rollback journal, and rejects edited variants rather than deleting human text. Merge readiness adds scalar fields to the existing batched GraphQL read, without extra requests or automatic retries.

Issue-review rereads use the current issue revision when no PR head is present, including an explicit null SHA. Changed issue revisions and mismatched real PR heads still fail verification.
