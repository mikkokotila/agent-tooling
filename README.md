# Agent tooling

Caller PR orchestration for Codex and Claude on macOS arm64 / Python 3.13. Release 0.1.4 installs runtime 1.7.4. The immutable v0.1.0 tag preserves the original operational 1.7.0 capture.

```sh
git pull --ff-only
make install
```

Installation verifies the committed candidate offline and activates it transactionally. Updating Git alone never changes live hooks. `make verify`, `make build`, `make status` and `make rollback` are also available. Rollback keeps current PR state.

The checker retains separate CI, review, conflict, current target ancestry and linked-issue gates. It publishes before local validation, tracks exact owner chats and polls one open PR every five minutes or several every ten minutes. Unchanged errors never restart chats.

Issue #3 adds stable quota blocker identity, durable shared request cooldown and freshness-bound collection sharing. Validation launch and execution each read live ancestry without reloading the whole CI/review inventory. Completion and merge still require full current evidence. Source, all 58 prompts, captured dependency wheels and client wiring are versioned here; tokens, personal settings and mutable task state remain local.

Copilot service availability never blocks completion or merge. Every actual Copilot change request still requires the same evidenced disposition, reply and thread resolution as other reviews. See [the service/review policy](docs/copilot-policy.md).

Edit the annotated live `~/.local/share/caller-pr-gates/prompts.toml` to influence both clients immediately, or version repository edits and install them. Valid local edits are preserved; conflicting edits require reconciliation.

See [installation and rollback](docs/installation.md), [request coordination and budgets](docs/github-coordination.md), [operational inventory](docs/operations.md) and [defect history](docs/known-defects.md).
