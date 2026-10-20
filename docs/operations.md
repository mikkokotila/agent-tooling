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

## Whole-suite verification wait

`tools/manage.py verify`, including the verification required before `install`, gives its entire pytest subprocess a finite **900-second** parent wait. This is cumulative suite execution time, not an individual test or product performance allowance. Every test/assertion and its own deadline remains unchanged; the generic command default remains 180 seconds. The CI job retains its separate 10-minute deadline.

The increase from 600 seconds addresses a retained local installation attempt that reached the parent limit while still making late-suite progress. It does not establish which test was slow, attribute a host cause, or credit that interrupted suite as a pass. A timeout or nonzero pytest exit still propagates before worker admission or deployment and cannot produce a new successful verification receipt. The separate `install --wait-for-workers` option (default 600 seconds) applies only after successful verification and never bypasses an active worker lease.

## Existing release body updates

Release 0.1.45 permits an authorized task to update only the body of an existing release through the existing GitHub API gateway. Use the numeric release ID and its exact API URL as `--target`; a related PR is not a surrogate release owner. The exact session must already be recorded with the same client and working directory, whose GitHub `origin` must match the target repository. No PR registration is needed or performed.

Save the complete approved body in a UTF-8 JSON file containing only `{"body": "..."}`. The body must be a string. If only a prefix is authorized to change, retain the required suffix byte-for-byte in that string and verify it before invoking the gateway. The gateway does not interpret project-specific headings or artifact digests.

```sh
caller-pr-gates github --session "$OWNER_SESSION" --client codex \
  --cwd "$REPOSITORY_CHECKOUT" \
  --target https://api.github.com/repos/OWNER/REPO/releases/RELEASE_ID \
  -- api repos/OWNER/REPO/releases/RELEASE_ID \
  --method PATCH --input "$BODY_ONLY_JSON"
```

Use that exact argument form; relative input files resolve under `--cwd`. Stdin, field/query flags, headers, host overrides, extra fields and other write endpoints are not admitted by this route. The validated JSON is frozen and passed to the existing authenticated relay via stdin. The PATCH response must confirm the release ID, API URL and exact body before a local success audit with its body digest is recorded. No approval, PR ownership, review disposition or completion receipt is created.

The caller remains responsible for authorization and a final readback of the release body and unchanged metadata/assets. Existing read syntax is:

```sh
caller-pr-gates github --session "$OWNER_SESSION" --client codex \
  --cwd "$REPOSITORY_CHECKOUT" -- api repos/OWNER/REPO/releases/RELEASE_ID
```

Read back before retrying any failed or ambiguous mutation; the gateway never retries it automatically. Release creation/deletion, uploads/assets, tag/title/target/draft/prerelease edits and unrelated API writes remain outside this capability. Existing PR gates, ownership and closed-reminder retirement are unchanged.

## Authorized documentation publisher configuration

The existing `github` gateway admits the two approved Limen provisioning writes and the exact retired-name DELETE below. Use the actual recorded root owner, client and original checkout of an owned **open, non-draft, same-repository PR targeting main**. The checkout origin must identify `Vaquum/Limen`. This capability does not adopt a PR, create an approval, modify gate evidence or change reminder retirement.

Use the exact argument forms shown, with saved JSON files. Relative input paths resolve under `--cwd`. Extra flags, fields, endpoints, other repositories/environments/secrets, plaintext and other protection writes are refused. The input is read and serialized once, and those admitted bytes go to the existing authenticated relay through stdin. No caller file is reopened by `gh`.

If the same owner session currently records its primary launch directory, refresh its **already-owned** PR context with the supported registration command in the same execution before the configuration command. Use the original PR worktree for both. Registration preserves the existing owner, scope and validation records; the configuration guard still requires the exact recorded context.

```sh
caller-pr-gates register "$OWNED_OPEN_PR" --session "$OWNER_SESSION" \
  --client codex --cwd "$REPOSITORY_CHECKOUT"
```

Provision the environment before the encrypted secret:

1. Prepare a private JSON file containing exactly `{"deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}}`, then create/update the environment:

   ```sh
   caller-pr-gates github --session "$OWNER_SESSION" --client codex \
     --cwd "$REPOSITORY_CHECKOUT" --target "$OWNED_OPEN_PR" \
     -- api repos/Vaquum/Limen/environments/documentation \
     --method PUT --input "$ENVIRONMENT_JSON"
   ```

2. Get the environment public key through the existing read route. The authorized owner uses its existing private credential handling to prepare a file containing only `encrypted_value` (base64 LibSodium sealed-box ciphertext) and `key_id`. Do not put a plaintext token in command arguments, source, logs or public discussion.

   ```sh
   caller-pr-gates github --session "$OWNER_SESSION" --client codex \
     --cwd "$REPOSITORY_CHECKOUT" \
     -- api repos/Vaquum/Limen/environments/documentation/secrets/public-key

   caller-pr-gates github --session "$OWNER_SESSION" --client codex \
     --cwd "$REPOSITORY_CHECKOUT" --target "$OWNED_OPEN_PR" \
     -- api repos/Vaquum/Limen/environments/documentation/secrets/CLOUDFLARE_DOCS_API_TOKEN \
     --method PUT --input "$ENCRYPTED_SECRET_JSON"
   ```

   The gateway requires the protected-branches-only policy and the current environment key ID. It checks ciphertext format and requires a successful GitHub response plus a fresh secret-metadata readback. It cannot verify the token's Cloudflare permissions or deployment behavior. Its receipt contains metadata and a payload digest, never the ciphertext or token.

Each operation checks the live PR identity before and after its write, rereads the environment policy, and rechecks the recorded ownership and checkout. The secret operation also reads back secret metadata. These are bounded operation-specific reads, without full PR/thread collection or routine polling. A transport error, unknown write outcome, missing readback or identity drift records an incomplete attempt and raises an error, without automatically retrying or rolling back. Reconcile actual fresh readbacks before any retry. Successful receipts establish only these writes/readbacks, not deployment success, product verification or task acceptance. The existing product owner retains those obligations and leaves the predecessor deployment and disconnected independent Git build unchanged.

### Retire the authorized check name after provisioning

The human subsequently approved retiring the **name** `Workers Builds: limen`, including every app binding at that same name, even a concurrent binding. This replaces the former app-85455-only requirement. GitHub's official [Remove status check contexts](https://docs.github.com/en/rest/branches/branch-protection#remove-status-check-contexts) endpoint accepts names, not app IDs. No other required-check name is authorized for removal.

After the environment and secret are provisioned, save a JSON file containing exactly `{"contexts":["Workers Builds: limen"]}`. The original owner uses:

```sh
caller-pr-gates github --session "$OWNER_SESSION" --client codex \
  --cwd "$REPOSITORY_CHECKOUT" --target "$OWNED_OPEN_PR" \
  -- api repos/Vaquum/Limen/branches/main/protection/required_status_checks/contexts \
  --method DELETE --input "$RETIRED_CONTEXT_JSON"
```

Admission requires fresh protected-branches-only environment policy, secret **metadata** existence, full classic protection containing the retired name, and independent `Protect-Main` ruleset 5406995. The gateway never reads a plaintext secret. The only write body contains the retired name; it never submits, unsets or replaces the unrelated check list and never writes the ruleset. The full-list `PATCH .../required_status_checks`, other context methods/names and generic protection/ruleset writes remain denied.

The receipt retains full actual classic/ruleset snapshots before and after the bounded DELETE, including readbacks after a nonzero or unknown write outcome. A verified receipt requires exit 0, exactly the name removal in both classic check representations, unchanged order/app bindings for every other name, unchanged strict/admin/review/thread controls and unchanged independent ruleset. It also rechecks provisioning metadata, live PR identity and recorded ownership. Multiple bindings at the retired name are intentionally removed. Already-absent names refuse admission instead of creating a new mutation receipt.

Concurrent changes to **other** names are not replayed or overwritten by this targeted request. Any observed difference beyond the authorized removal, unavailable authority or unknown write outcome remains incomplete with the actual available snapshots. There is no conditional-write claim, exclusive-writer assumption, automatic mutation retry or full-list rollback. Reconcile fresh readbacks before any new operation. Isolated interleaving tests establish what the gateway sends and how it treats responses; they do not prove undocumented GitHub server semantics.

[PR #55's P1 and failed-head evidence](https://github.com/mikkokotila/agent-tooling/pull/55#discussion_r4323240998) and the full-list lost-update countertests remain intact. [Issue #54](https://github.com/mikkokotila/agent-tooling/issues/54) records the changed human authorization and retained history. Source release and installation do not establish a live cutover or complete the linked Limen #1073, #1080 and #1085 obligations; the original product owner performs the authorized operation and assesses its actual readbacks.

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

### Additional private qualification controls

If remaining acceptance needs a new local driver for the unchanged merged producer, append a new check to the existing plan file with its own `input_paths` (and, when needed, `env_names`). Keep every existing check and the top-level input/environment declarations unchanged. For example, append this check to the existing `checks` list:

```json
{
  "id": "retry-cancel-range-v1",
  "argv": ["python3.13", "storage/qualification-controls/retry-cancel-range-v1.py"],
  "reason": "Verify cancellation during the already-supported retry delay for the pinned range",
  "input_paths": [
    "storage/qualification-controls/retry-cancel-range-v1.py",
    "storage/qualification-controls/retry-cancel-range-v1.json"
  ]
}
```

Use the real interpreter, driver and dependencies for that check; the paths above illustrate the declaration only. Paths are relative to the recorded repository root. New files must be private ignored inputs outside previously frozen input directories, so creating them does not change the retained fingerprint. Do not edit tracked `.gitignore` or producer files to make this work. Per-check input paths and local drivers must use ordinary paths without symlink traversal; replacing a pinned path with a symlink also refuses execution. Explicitly include every additional dependency; keep outputs outside declared inputs. The runner computes the control's `input_digest` from the producer, retained inputs and its additional inputs/environment. Do not supply an `input_digest` or copy a receipt into the plan file.

```sh
caller-pr-gates validation-plan --session OWNER_SESSION \
  --pr https://github.com/OWNER/REPO/pull/NUMBER \
  --cwd /absolute/recorded/checkout --file /absolute/private/plan.json \
  --merged-head FULL_ORIGINAL_PR_HEAD_SHA

caller-pr-gates validate --session OWNER_SESSION \
  --pr https://github.com/OWNER/REPO/pull/NUMBER \
  --cwd /absolute/recorded/checkout --check retry-cancel-range-v1 \
  --merged-head FULL_ORIGINAL_PR_HEAD_SHA --background
```

The appended control gets no historical pass. Its actual exit and before/after digests are recorded separately in the existing run history. Original checks retain their actual results and unchanged-failure retry prohibition; adding a passing control cannot waive a failure. All pins and existing check declarations remain immutable. A revised control needs a distinct frozen file/check identity. An evidenced private checker correction must use the explicit relation below to satisfy the original obligation. Let an active managed process finish naturally before extending its plan. This addition uses the existing preflight and background worker, with no extra GitHub reads.

### Correcting a failed private checker

Release 0.1.43 adds an audited correction for an actual failed private Python checker on the same retained merged producer. Keep the entire existing plan, original input files and all previous check declarations unchanged. Create a corrected private driver and a private evidence file outside the frozen input directories. The evidence file must document the actual failure, why it is a checker defect, the correction and how the original acceptance remains required. Keep private values and logs local.

Append a fresh check copied from the failed check. Keep its `reason`, Python interpreter, trailing arguments and `cwd` identical; change only the script argument to the corrected driver. Retain every per-check input and environment name from the failed check, add the new driver and evidence file to its `input_paths`, and attach:

```json
"correction": {
  "check_id": "original-required-check",
  "run_id": "ACTUAL_FAILED_RUN_UUID",
  "diagnosis": "Explain the evidenced private checker defect and its concrete correction",
  "evidence_path": "storage/qualification-corrections/checker-evidence.txt"
}
```

The example path is illustrative: use ordinary ignored files inside the recorded checkout, outside all previously frozen input directories. The new plan file must also remain outside those pins. Do not supply an `input_digest`. Admission verifies the current actual finished failed run, the original private driver and a different Python program; copied, renamed, whitespace-only or comment-only programs cannot authorize a retry. The old driver, new driver and evidence file must be private ordinary files without symlink traversal. A tracked checker or producer correction still needs a genuine product repair PR. Python programs must use a literal script argument immediately after the unchanged interpreter; this does not admit arbitrary command replacement.

Release 0.1.51 also accepts an existing literal `env NAME=VALUE ...` prefix before that interpreter. Copy the complete prefix byte-for-byte, including assignment names, values and order; change only the Python driver argument. For example, an argv shaped as `["env", "CHECK_MODE=full", "WORKER_COUNT=2", "/absolute/python", "private/original.py", "--mode", "compare"]` keeps its first four and final two elements unchanged. This supports bare `env` with one or more literal assignments, without flags, shell expansion or another wrapper. Adding, removing or changing an assignment, interpreter, trailing argument, `cwd` or acceptance reason is not a checker correction. Keep actual values in the private plan; do not copy them into public reports. The existing failure/run, input-pinning and fresh-execution requirements apply unchanged.

For this retained `env` form, trailing script options such as `-c config.json` stay in their original argv positions after the script path. They remain driver arguments. Python interpreter options before the script are outside this correction route; the existing direct-runner guards also remain unchanged.

Bind and execute through the existing commands:

```sh
caller-pr-gates validation-plan --session ORIGINAL_SESSION \
  --pr https://github.com/OWNER/REPO/pull/NUMBER \
  --cwd /absolute/recorded/checkout --file "$CORRECTION_PLAN" \
  --merged-head FULL_ORIGINAL_PR_HEAD_SHA

caller-pr-gates validate --session ORIGINAL_SESSION \
  --pr https://github.com/OWNER/REPO/pull/NUMBER \
  --cwd /absolute/recorded/checkout --check FRESH_CORRECTED_CHECK_ID \
  --merged-head FULL_ORIGINAL_PR_HEAD_SHA --background
```

An appended, running, failed or unverifiable correction does not discharge the original failure. Once the corrected process really exits zero with unchanged fingerprints, progress reports `corrected_failures` with the historical failed run and the actual passing check/run that satisfies its obligation. `passed` contains only checks that actually passed. The original run, exit, input records and declaration remain unchanged. The correction relation and driver/evidence hashes are recorded in the existing audit log, with no database migration or additional GitHub reads.

A passing control cannot acquire this relation retroactively. Each failed check has at most one correction; if that successor genuinely fails because of another evidenced checker defect, correct that failed successor with another fresh pinned identity. An unrelated passing control, a changed diagnosis alone or an unchanged failed retry cannot satisfy the original work. Let all active managed execution finish before changing the plan. Real checker quality and unchanged acceptance still require diagnosis and review; a changed Python syntax tree alone does not prove a valid fix. Closed-PR local success remains incomplete task acceptance and does not create `MERGE_READY`.

### Correct an unexecuted private checker without destroying evidence

Release 0.1.46 extends the same correction route to a known-invalid private Python checker that has never executed. For example, a retained verifier might open an existing measurement trace for writing. Do not execute that defective checker or manufacture a failed run to gain admission. Preserve its entire declaration, original files, shared input/environment pins and acceptance obligation.

Prepare a materially corrected ordinary ignored driver and a private evidence file outside every frozen input directory. The evidence must explain the concrete defect, why the original cannot safely establish its intended result, the corrected behavior and how all original acceptance remains required. Copy the original check to a fresh ID, retaining its exact interpreter, trailing arguments, `cwd`, `reason`, per-check dependencies and environment names. Change only the Python script argument, append the new driver and evidence file to `input_paths`, omit `input_digest`, and use this metadata instead of a failed `run_id`:

```json
"correction": {
  "check_id": "original-required-check",
  "unexecuted": true,
  "diagnosis": "Identify the evidence-overwriting checker defect and the concrete preservation fix",
  "evidence_path": "storage/qualification-corrections/checker-evidence.txt"
}
```

Use the same `validation-plan ... --merged-head ...` and `validate ... --check FRESH_CORRECTED_CHECK_ID --merged-head ... --background` commands above. The plan file must remain outside the frozen pins. Admission checks all run history for the original check under the exact recorded owner, repository, PR and checkout, including previous plan generations. A running, passed, failed, interrupted or unverifiable original is not unexecuted; a real finished failure must use the existing `run_id` route. Both metadata forms together, false/nonboolean `unexecuted`, reused check identities, changed shared inputs and active execution are refused.

The original remains in the plan with no invented run. Once its correction is admitted, attempts to execute the known-invalid original are refused, including a background worker that reaches admission later. Only the fresh corrected process's actual exit zero on unchanged fingerprints can satisfy the obligation. Progress then reports `corrected_unexecuted` with the original ID and actual passing check/run; `passed` still contains only executed checks. Admission alone, an independent historical pass, a running correction, a failed correction or changed inputs provides no credit. The existing audit event records the original and corrected input/driver hashes, diagnosis and evidence hash. A genuinely failed corrected successor retains that failure and may use the failed-check correction route.

Each correction names exactly one original check. Distinct required IDs keep distinct obligations and fresh execution requirements; the tool does not infer equivalence or assign one receipt to several checks. This path does not change Python command-wrapper support, product-source requirements, review gates, closed-PR retirement or task-acceptance rules.

No registration, monitor restart, database repair or placeholder PR is needed. Closed plans remain visible as incomplete task progress, including when their checkout is missing. Once all retained local checks pass, progress reports `local_checks_passed: true` with `completion_verified: false`; acceptance still requires the original task's evidence. Closed PRs report `MERGE_READY: WAIT` with `applicable: false`, never a synthetic passing readiness receipt. Existing open-PR CI/review/ancestry gates and reminder retirement are unchanged.
