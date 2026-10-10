# Installation and rollback

Run `make install` from this checkout after updating it. It builds a standard wheel, runs the isolated regression and deployment suite, and activates only after verification. Dependencies and test tools come from captured local wheels; no package-index access is needed. GitHub CI runs this same verification on macOS arm64 with Python 3.13.2.

Verification is bound to the SHA-256 inventory of the entire source checkout. A source/dependency/test/config change invalidates the cached result. An unchanged successful verification is reused. Any failing required check blocks installation. The three captured quota regressions now pass; current releases have no expected failures. The immutable capture records their historical failure.

Installation requires a committed, clean checkout, so recorded Git provenance identifies the deployed source. Commit/publish repository edits before installing; local live prompt edits remain separately protected.

The installer checks for concurrent configuration/source edits and worker leases before changing operational components. A lease can defer activation without clearing state or forcing another chat turn. Source directories/runtimes use atomic filesystem exchange; individual settings use atomic replacement. Candidate callbacks are exercised with a temporary state database, never production ownership or real GitHub mutations.

Each deployment stores a local journal and the previous affected code/config files under `~/.local/share/agent-tooling/deployments/`. A failed activation restores the prior files and monitor service. The next installation recovers an interrupted journal before doing new work. Backups remain local and are not added to Git.

`make rollback` restores the prior deployment. It never restores an old PR database, recreates obligations, changes issue/review evidence or clears leases. Local edits to affected deployed files cause rollback to refuse before writing. Personal settings and instruction text outside the managed sections remain intact during installation.

The first v0.1.0 install on the captured machine is operationally a no-op: it adds repository commit/version provenance to `release.json` and records a deployment journal. It does not replace byte-identical code, dependency files, wiring or prompts, and does not restart an already-running unchanged monitor. Reinstalling the same commit is fully idempotent.

## Prompts

All prompt text remains in one annotated catalogue. Edit the live `~/.local/share/caller-pr-gates/prompts.toml` for immediate local changes, or the repository `prompts.toml` for a reviewed/versioned change followed by `make install`. The running hooks retain the existing valid-edit live reload and invalid-edit last-good fallback.

Installation preserves valid local prompt edits when repository text has not changed. If both have changed since the previous deployment, installation stops before activation and asks the operator to reconcile those actual file differences. It never silently discards local text. Invalid current text also stops installation; the existing running loader remains responsible for its last-good fallback.

Managed global instruction sections mirror this same selected catalogue. Already-loaded conversation context still needs a reread/new run; the installer cannot rewrite an agent's existing context.

## Boundaries

The baseline is specific to macOS arm64 and Python 3.13. The captured Codex trust records apply only to the exact captured home and unchanged callback definitions. A different home/new command requires normal Codex hook review; copying a historical hash cannot grant trust to a new command. The installer does not bypass that mechanism.

Application authentication, GitHub permissions and external runner/account availability remain host/account responsibilities. They are not packaged into the release. Release 0.1.3 installs runtime 1.7.3; these versions and the exact source commit are recorded separately. Native callback commands and trust records stay unchanged; the CLI launcher and managed background worker use Python safe-path mode to prevent a repository cwd from shadowing the installed module.


## Reviewed concurrent maintenance

Unrecorded live source edits normally abort installation before service changes. One complete observed 1.7.1 hash inventory is recorded in `release/upgrade-baselines.json`, with its exact prior installed version/commit. The retained validation-evidence and cancelled-readiness changes were reviewed into source before this baseline was accepted. Any additional difference still fails. Activation compares the exact files again; rollback restores the actual prior files, including that maintenance. It never repairs provenance by editing the current pointer or live database.

Issue #3 was installed in order: stable identity (0.1.1 / 1.7.1), coordinated cooldown (0.1.2 / 1.7.2), then collection profiles (0.1.3 / 1.7.3). Coordination and narrow preflight tables are additive; older compatible processes can retain existing PR state. Installation preserves that database and every current receipt.
