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

## Review receipts after PR closure

Release 0.1.53 lets the exact retained owner record an existing public fix/nonfix conclusion after normal PR retirement. Use the original recorded checkout, clean at the full PR head GitHub currently records for that closed PR:

```sh
caller-pr-gates address --session OWNER_SESSION \
  --pr https://github.com/OWNER/REPO/pull/NUMBER \
  --cwd /absolute/recorded/checkout --closed-head FULL_CLOSED_PR_HEAD_SHA \
  --comment review:REVIEW_DATABASE_ID --disposition nonfix \
  --reason 'Exact substantive explanation present in the public reply' \
  --reply-url https://github.com/OWNER/REPO/pull/NUMBER#issuecomment-REPLY_DATABASE_ID
```

Use the actual normalized comment ID (`review:`, `inline:` or `issue:`). For a real fix, use `--disposition fixed --commit FULL_FIX_COMMIT_SHA`; the existing policy still requires that commit in this PR's history with valid chronology and an actual public conclusion. No new code may be attributed to the closed PR. The command uses the existing bounded review collection, including its current comment revisions, authenticated reply author, content and timing. Missing or stale proof, another owner/checkout/origin, altered source, an open target or an unresolved selected thread refuse this continuation. It is limited to same-repository PRs and does not add a closed-thread mutation route.

The command returns `receipt`, current `review_gates` and `completion_verified: false`. Other findings remain required even if the selected receipt verifies. Record the parent review dispositions with their actual reply URL; the existing policy recognizes a verified owner conclusion reply, so that reply does not need a new self-referential disposition. A truthful acknowledgement already posted for a proven service-only Copilot notice can likewise receive its own verified parent receipt. This adds no obligation to acknowledge a quota notice and exempts no mixed or substantive feedback. Do not use a broad "no findings" claim for actual unresolved feedback.

No re-registration, reopening, adoption or monitor restart is required or performed. The retirement history must retain the same head and lifecycle. Release 0.1.54 also recognizes completed gateway comment/edit acknowledgement cycles: every registration after the first retirement must belong to the original root owner and have one matching `gateway_write` / `pr comment` or `pr edit` ownership record before the next registration. This includes existing body-only edits. The legacy audit records the operation verb, not the edit's fields; the receipt does not certify historical mutation options. The latest registration must already be retired again; ordinary retirement may occur before the separate ownership-evidence write. The canonical owner's client and each registering session's own client remain distinct. Arbitrary registration, another actor/client, a changed observed closed head or lifecycle, missing/ambiguous acknowledgement evidence, and a still-open registration refuse this route.

The current requested checkout must resolve to both retained registration/watch roots and the recorded checkout anchor. New registration audits retain their checkout and client, so their first recorded checkout cannot be replaced by a plan elsewhere. For a legacy repeated history lacking that field, the first validation plan recorded for this exact owner/PR before its first retirement anchors the eligible qualification checkout. The returned `receipt.checkout_anchor` identifies the actual registration or plan and path used; it does not claim that a plan proves where the original registration or every later command ran. Missing pre-retirement proof remains incomplete. Canonical aliases of the same checkout are accepted; a different current checkout, origin, dirty source or changed head is refused.

This is a present-time review receipt, not a certificate of uninterrupted historical checkout identity. An earlier same-owner acknowledgement from another directory does not permanently revoke the owner's receipt access after the current retained checkout/source is restored. A legacy plan declared in another directory identifies that recorded qualification checkout, without reconstructing the unrecorded first registration. Neither case transfers an execution result or changes a failed/pending obligation. No native transcript or operator attestation is imported. The complete admitted registration/retirement history and checkout anchor, plus the clean pinned source, are checked again in the write transaction immediately before insertion. Another observed cycle during collection or insertion requires fresh evidence even when it uses the same owner and head. All original audit and validation records remain unchanged.

The source helper compares a trailing porcelain-v2 status report's HEAD and cleanliness with its first observations and refuses either observed change. This bounded check does not lock unrelated filesystem writers or grant execution credit. Source plans, failures, passes, closed reminders and task acceptance remain unchanged. This command does not relate successor PR qualification to an earlier PR or transfer any execution evidence. Final coverage has the separate explicit boundary below.

## Final successor qualification coverage

Release 0.1.55 adds an explicit conclusion for an original task whose delivered repairs were qualified in a later, separately recorded PR. It supports the same full `npm run check` and `npm run test:e2e` gates expressly required by the immutable original acceptance. It does not copy execution records or assert that different scopes, commands, private inputs or checkouts were identical.

```sh
caller-pr-gates qualify-successor --session ORIGINAL_ROOT_SESSION \
  --pr https://github.com/OWNER/REPO/pull/ORIGINAL \
  --successor https://github.com/OWNER/REPO/pull/FINAL \
  --cwd /absolute/recorded/final-checkout \
  --file /absolute/private/coverage.json
```

Both PRs must already belong to the exact same canonical root owner and repository, be retired as merged, and retain their own unambiguous plan, checkout and immutable scope. No registration, adoption, reopening or monitor restart occurs. The original checkout may be absent: its recorded scope and declarations establish the obligation, not current evidence that its historical private bytes or execution passed. The final checkout must exist, match its own recorded origin and exact merged PR head, and reproduce its actual final-plan fingerprint. Every necessary final check must have its own real exit-zero run in that exact final plan, with equal input/before/after digests. Missing, failed, unknown, running, duplicate or other-plan results cannot supply this proof.

Write the coverage file outside the frozen inputs. It identifies both existing plan IDs, explains each original check and acceptance criterion, and acknowledges **every actual declaration difference**. `criteria` explanations follow the original recorded acceptance-criteria order. `checks` must contain exactly the required original check IDs, also present as identities in the final plan. Missing, extra or empty explanations are refused. For a two-gate case with these actual changes:

```json
{
  "original_plan_id": "EXISTING_ORIGINAL_PLAN_ID",
  "final_plan_id": "EXISTING_FINAL_PLAN_ID",
  "scope_reason": "Explain how the delivered final repair and full gates cover the original scope while retaining the separate final scope.",
  "checks": {
    "check": "Explain the actual full application gate on final source.",
    "browser": "Explain the actual full official browser gate on final source."
  },
  "criteria": ["Explain the first original acceptance criterion; add one entry for every remaining criterion."],
  "changes": {
    "checks/check/argv": "Explain the native worker control with the unchanged existing literal environment.",
    "checks/check/reason": "Explain the same full application obligation.",
    "checks/browser/argv": "Explain the full official gate, native isolated port and worker count.",
    "checks/browser/reason": "Explain the same full browser obligation.",
    "input_paths": "Explain replacement of the original private configuration by the current official full gate; do not claim unknown historical bytes were equal.",
    "env_names": "Explain the final native worker environment pin."
  }
}
```

`changes` keys describe actual differences: `input_paths`, `env_names`, or `checks/ID/argv`, `checks/ID/reason`, `checks/ID/cwd`. The route requires identical published `package.json` script definitions, an identical inherited literal `PATH` assignment (or no inherited assignment) and root-relative check cwd. Other inherited environment assignments are refused even if unchanged; unchanged does not prove that a variable cannot skip tests. It permits the evidenced native `VITEST_MAX_WORKERS` positive integer, `POISE_E2E_PORT` integer 1–65535, and a reduced positive Playwright `--workers` value. It can replace a previously declared private `--config=...` input with the final official full gate only when the original published Git tree proves the path was untracked and did not traverse a tracked link or submodule. This does not claim historical ignored-file contents were verified. The original shared private inputs must consist only of those replaced browser configurations, and it must have no shared environment-name pins; the final plan has no shared private inputs and may pin only the admitted native controls. Extra historical private inputs or environment pins require unsupported continuity proof and are refused, not silently mapped. Arbitrary commands, wrappers, environment changes, test selection/filter arguments, new private drivers or per-check correction chains are outside this route. An explanation cannot override these boundaries. The owner still owes a truthful substantive assessment of the original criteria; matching check IDs or explanatory text alone does not establish feature correctness.

The command performs one bounded fresh original review read, one full final CI/review/closure collection, original issue-closure proof, and authoritative delivered-merge ancestry/tree reads. The original **merge commit** must be an ancestor of the final published head; the original branch head need not be. The actual final merge tree must equal the qualified final-head tree. Actual review findings and public conclusions on both PRs, original issue closure, final CI/issue closure and an independent exact-head approval remain required. At least one latest non-service CI result must have actually succeeded on that head; service-only, skipped or neutral results cannot establish it. Current working bytes must also reproduce the published final tree, independently of clean-status index hints. Closure never fabricates `MERGE_READY`.

An admitted refresh first appends a pending audit record after rechecking both contexts and final source, so any earlier successful receipt stops applying before remote collection. Successful current evidence appends its conclusion only if that attempt remains the latest; a failed, interrupted or superseded refresh cannot replay an earlier success. It changes no plan, scope, run, registration or monitor row. The receipt reports actual final run IDs and `historical_input_continuity: not_established`; old failures, passes and pending declarations retain their exact outcomes. `PR_PROGRESS` can report `QUALIFIED_SUCCESSOR`/`PASS` only for the explicitly covered original plan, with original history and final proof attached. The successor plan continues through its own unchanged progress evaluation; this receipt cannot discharge its different scope. `completion_verified: false` remains explicit: this is not blanket completion of the owner's other acceptance, PRs or work items. The final PR's closed `MERGE_READY` remains `WAIT`/not applicable.

Ordinary status reads check the recorded context, current final inputs and existing remote-evidence freshness without adding polling. Changed or stale proof becomes `SUCCESSOR_INCOMPLETE`; reconcile the specific gap and explicitly repeat `qualify-successor` to collect fresh evidence. It never retries a test, reopens a monitor or changes an old failure. Use a separate file/command for each original plan; the same actual final executions can prove the same full-gate obligations without duplicate computation, while each original scope and explanation stays separately recorded.

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

### Final published head after an external merge

If the recorded checkout was synchronized to the final published PR head before external merge but its necessary plan still binds an earlier source, explicitly name that earlier commit when binding the **same original plan**:

Pass the original necessary-plan JSON containing `checks` and its input/environment declarations. A CLI result envelope containing `plan` and `progress`, or an empty output left by a failed attempt, is not a plan input. Do not copy runner-owned digests or epoch provenance from a receipt into that original file.

```sh
caller-pr-gates validation-plan --session OWNER_SESSION \
  --pr https://github.com/OWNER/REPO/pull/NUMBER \
  --cwd /absolute/recorded/checkout --file /absolute/private/original-plan.json \
  --merged-head FULL_FINAL_PUBLISHED_PR_HEAD_SHA \
  --from-head FULL_PREVIOUS_PLAN_SOURCE_SHA

caller-pr-gates validate --session OWNER_SESSION \
  --pr https://github.com/OWNER/REPO/pull/NUMBER \
  --cwd /absolute/recorded/checkout --check ORIGINAL_NECESSARY_CHECK_ID \
  --merged-head FULL_FINAL_PUBLISHED_PR_HEAD_SHA --background
```

The existing bounded preflight must prove the stable merged PR head, repositories and merge time. The plan must predate that merge, and the exact recorded owner/checkout must be clean at the final head with its matching origin. The earlier full SHA must be an ancestor of the final head. Git objects reconstruct the old tracked-source fingerprint while current private files and declared environment values must reproduce the retained pins; no checkout is rewound and no old code is run. A separate fingerprint reconstructed from the published final tree must match the actual checkout, independently of index hints such as `assume-unchanged` or `skip-worktree`. Historical directory/file/missing types preserve the original declared input meaning across source renames and deletions; current declared directories still include unchanged private inputs after their last tracked child is removed. Source blobs are processed in bounded batches, retaining the existing Git-command timeout. Unavailable history, unreconstructable source (including changed symlinks/submodules), changed private inputs or any altered check/argument/cwd/reason/dependency declaration refuse admission. Source-bound checker-correction receipts cannot transfer through this operation.

Reconstruction is deliberately limited to ordinary Git bytes that reproduce the existing fingerprint. Release 0.1.57 preserves actual filesystem permissions when comparing those bytes with a matching Git executable class; it does not canonicalize the ordinary fingerprint or change a frozen pin. For a historical file without a directly matching current mode, a uniform observed mode of the same Git class can supply a candidate; otherwise the canonical Git mode remains the candidate. If the first complete historical digest does not match, the existing canonical candidate is tried once for only those unresolved paths; the chosen projection must also reproduce every old per-check pin. Neither candidate replaces a surviving matching file's current permissions. A candidate is evidence only when the complete reconstructed digest equals the already-recorded historical pin. Ambiguous or changed permissions can still prevent reconstruction. If files were checked out through CRLF/smudge/encoding transformations, raw blobs may likewise not reproduce those recorded working-tree bytes; admission then remains incomplete. The command does not execute historical checkout filters or replace the old pin with a new canonicalization. Likewise, it does not invent the former membership of undeclared untracked files hidden by later ignore rules. An already explicitly pinned private file remains included despite an ignore-rule change, but an unprovable implicit input set is refused with all history retained. These limits require real evidence for any later extension; they are not permission to edit the plan, recreate old source or bypass the fingerprint comparison.

Unchanged symlinks retain their actual target-content pins. This bounded reconstruction does not resolve a historical symlink target graph: if a link is unchanged but its tracked referent changed, the old target-content pin is not reconstructed and admission refuses. It does not substitute the final target's content for a verified historical result or change that old pin. A producer needing that additional reconstruction remains incomplete under this path.

Git-tree absence is not evidence that a declared private directory was absent. Current declared-directory private files always participate in the retained-pin proof, including when the path gains its first tracked child. Unchanged private contents can reproduce the old pin; added or changed private contents cannot be admitted as part of the published source transition.

Admission appends a new plan generation and records its old/new source and input relationship. Previous plans, failures, passes and pending declarations remain in history. The new generation imports **no old execution credit**, including an older pass with matching final-source bytes; every necessary check needs a fresh actual execution. Its before/after pins and actual process exit remain mandatory. A failure in the new generation stays a failure and cannot be reset by repeating the binding command. Repeating the identical command after successful binding is idempotent. Active runs block a transition; let them finish naturally. A commit with unchanged source content cannot create a new epoch. This option does not authorize post-merge tracked edits, a different head, another owner/checkout or new controls in the transition. Subsequent independently pinned controls retain their existing separate admission rules. Retired reminders stay retired, and passing local checks still do not establish task completion.

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

The example path is illustrative: use ordinary ignored files inside the recorded checkout, outside all previously frozen input directories. The new plan file must also remain outside those pins. Do not supply an `input_digest`. Admission verifies the current actual finished failed run, the original private driver and a different Python program; copied, renamed, whitespace-only or comment-only programs cannot authorize a retry. The old driver, new driver and evidence file must be private ordinary files without symlink traversal. A tracked checker or producer correction still needs a genuine product repair PR. Python programs must use one of the literal script forms below; this does not admit arbitrary command replacement.

Release 0.1.51 also accepts an existing literal `env NAME=VALUE ...` prefix before that interpreter. Copy the complete prefix byte-for-byte, including assignment names, values and order; change only the Python driver argument. For example, an argv shaped as `["env", "CHECK_MODE=full", "WORKER_COUNT=2", "/absolute/python", "private/original.py", "--mode", "compare"]` keeps its first four and final two elements unchanged. This supports bare `env` with one or more literal assignments, without flags, shell expansion or another wrapper. Adding, removing or changing an assignment, interpreter, trailing argument, `cwd` or acceptance reason is not a checker correction. Keep actual values in the private plan; do not copy them into public reports. The existing failure/run, input-pinning and fresh-execution requirements apply unchanged.

Release 0.1.56 also recognizes an already declared `python -m coverage run --data-file=OUTPUT SCRIPT` checker, optionally after that same supported literal `env` prefix. Keep the interpreter, `-m coverage run`, the single `--data-file=...` argument and every trailing argument byte-for-byte identical; change only `SCRIPT` to the separately pinned corrected driver. Other coverage option spellings, additional options, module targets and runner wrappers are not supported by this correction route. Existing shared driver pins remain shared; retain them and every original check dependency. This changes script recognition only, not coverage behavior or the required failed-run, diagnosis, source, ownership and fresh-execution evidence.

For this retained `env` form, trailing script options such as `-c config.json` stay in their original argv positions after the script path. They remain driver arguments. Other Python interpreter options before the script are outside this correction route; the existing direct-runner guards also remain unchanged.

An original `env` driver already pinned in the plan's shared `input_paths` stays there; retain the check's separate dependency declarations exactly. Do not add a redundant driver entry to an immutable original check. The corrected driver and diagnosis evidence still need their own new per-check input pins.

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



### Existing Poise Google deployment keys

Record the human-approved configuration as a separate immutable work-item with the ordinary `scope --file SAVED_SCOPE --work-item CONFIGURATION_ID` command. Include `google_actions_configuration` with exactly `repository` (the operator fork), `authorizing_pr` (the exact owned source PR URL) and nonempty `approval_evidence` quoting the human authorization. No deployment identity or credential value is installed as a source constant. The grant allows only the integration's client-ID variable and sealed client-secret, in that one repository, for that recorded PR's exact client/session/checkout. Its retained merged owner may finish deployment preparation without reopening or rearming the PR; closed unmerged work stays denied. Other work-items and PR scope baselines remain immutable.

Use `github --session SESSION --client CLIENT --cwd CHECKOUT --target AUTHORIZING_PR -- api ENDPOINT --method METHOD --input SAVED_JSON`. For `POISE_GOOGLE_CLIENT_ID`, POST `repos/APPROVED_REPOSITORY/actions/variables` to create, or PATCH the same path plus `/POISE_GOOGLE_CLIENT_ID` to update, with exactly `name` and `value`. For `POISE_GOOGLE_CLIENT_SECRET`, PUT `repos/APPROVED_REPOSITORY/actions/secrets/POISE_GOOGLE_CLIENT_SECRET` with exactly GitHub's `key_id` and base64 `encrypted_value`. Seal with that fork's current public key first; never put plaintext secrets in the JSON, arguments or logs. Only a saved JSON file is accepted and the admitted bytes are forwarded through stdin. Other repositories, keys, settings, fields and method overrides stay denied. The write receipt contains only identity and a payload hash. Read back variable/secret metadata before releasing the existing deployment hold; a merge or successful API exit alone is not persistence proof. This capability authorizes no new grant, workflow dispatch or deployment.

### Retire Origo's deployed CryptoHFTData Actions secret

Release0.1.63/runtime1.7.63 adds only the bodyless DELETE of
`repos/Vaquum/Origo/actions/secrets/CRYPTOHFTDATA_API_KEY`. Record the existing human
provider-removal authorization in immutable work-item
`origo-608-retire-cryptohftdata-actions-secret`, with member
`origo_cryptohftdata_secret_retirement` containing exactly `repository: Vaquum/Origo`,
`authorizing_pr: https://github.com/Vaquum/Origo/pull/608` and nonempty
`approval_evidence`. The original root owner/client/checkout must be retained with
PR608 merged. This route never merges, adopts or rearms that PR.

Use the existing gateway with `--target https://github.com/Vaquum/Origo/pull/608`
and exactly `-- api repos/Vaquum/Origo/actions/secrets/CRYPTOHFTDATA_API_KEY --method DELETE`.
No body, headers, other key or configuration operation is admitted. Admission
requires the exact merge SHA's successful `Deploy On Merge` run, successful image
deployment job/step, current main retaining that merge, and credential absence in
bounded immutable Git blobs for both revisions. Exact commits must already exist
locally; admission does not fetch or change checkout files. Source/deployment
identity is rechecked before and after the operation.

An atomic fixed-operation receipt permits only one attempted DELETE, including
concurrent callers. The relay admits only that one exact HTTP write, without
redirects or retries. Verification requires metadata200, DELETE204 and metadata404.
Preflight404 is a refusal because it is not authorization proof. Receipts contain
only whitelisted metadata, identities and source digests. Failed, interrupted,
drifted or unknown outcomes remain incomplete and prevent another DELETE; use
existing read-only metadata requests for investigation. No automatic retry,
override, account revocation or other credential operation is provided.

Retained live collectors/workers are never restarted by this route. Their old
environment copies can remain inactive for the retired integration until normal
rollout; deleting an Actions secret does not erase existing container or host
configuration. Successful deployment evidence records the exact merged release's
deployment and does not claim inspection of every retained runtime environment.

### Owned Origo hosted source-suite fallback

Release0.1.62 permits the current owner of an open `Vaquum/Origo` PR to dispatch only
`.github/workflows/pr_checks_tests.yml` with `runner=github`. This uses the existing
full source-native suite when its self-hosted runner is unavailable; no check,
workflow, runner resource or repository setting is changed. The exact recorded
client and checkout must be clean at the same-repository PR head and current main
ancestry. The workflow file at both immutable base and head must match reviewed
Git blob `6bf211fb8bbc65040bd5848040e36f2c2d463134`.

Save JSON containing exactly `{"ref":"CURRENT_PR_HEAD_BRANCH","inputs":{"runner":"github"}}`.
Then use the ordinary owned target and exact API form:

```sh
caller-pr-gates github --session SESSION --client CLIENT --cwd OWNED_CHECKOUT \
  --target https://github.com/Vaquum/Origo/pull/NUMBER -- \
  api repos/Vaquum/Origo/actions/workflows/pr_checks_tests.yml/dispatches \
  --method POST --input SAVED_JSON
```

The admitted bytes are frozen before the write. Other inputs, refs, workflows,
repositories, methods, headers, stdin bodies and foreign owners remain denied.
The command fixes the internal GitHub API version to `2026-03-10` and requires the
[dispatch response's exact workflow-run ID and URLs](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event). Success requires that returned
run's exact head, branch, repository, workflow and dispatch event, followed by
unchanged PR/checkout/owner readback. Missing or raced outcomes record incomplete
evidence and refuse automatic retry; inspect the actual dispatch outcome before
another invocation. A concurrent dispatch cannot substitute its run identity.
A verified dispatch receipt establishes the run identity, never CI success,
review completion or merge readiness. All existing PR gates remain required.


## Limen 5.46.2 release dispatch

The gateway admits only the existing `Vaquum/Limen` manual release workflow for version 5.46.2. From the original candidate checkout, after its reviewed PR merges, check out its exact merge commit and save `{"ref":"main"}` in a private JSON file. Use the retained candidate PR as `--target`:

```sh
caller-pr-gates github --session SESSION --client CLIENT --cwd CHECKOUT --target https://github.com/Vaquum/Limen/pull/N -- api repos/Vaquum/Limen/actions/workflows/pr_post_release.yml/dispatches --method POST --input PRIVATE_JSON
```

The gate requires retained merged ownership, independent current-head approval, all current checks and review dispositions, a clean matching checkout at live main, coherent release versions and the pinned reviewed workflow/release script. An existing tag, prior dispatch or different predecessor blocks it. It freezes the saved payload and verifies the returned workflow run's source identity. No other version, workflow, ref, input or API-version override is admitted.

An ambiguous response records an incomplete attempt. Inspect the actual workflow runs, tags and releases before any retry; never infer that a failed response means no release started. This command does not prove PyPI or documentation publication. The existing release, package publisher and documentation activation guards still own those outcomes. No branch protection or credential is changed.
