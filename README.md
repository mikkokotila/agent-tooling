# Agent tooling

The immutable v0.1.0 tag captures the operational Caller PR gate 1.7.0 for Codex and Claude. Runtime Python files and all 58 prompt sections are preserved byte-for-byte. Issue #3 is repairing quota identity, shared cooldown and duplicate collection in verified stages. Stage 1 (0.1.1) fixes the three captured identity regressions.

This repository owns source, prompts, dependency snapshots, client wiring and reproducible installation. Mutable PR state, tokens, personal settings, chats and logs remain outside Git.

From the local checkout:

```sh
git pull --ff-only
make install
```

`make install` verifies the candidate offline, preserves local edits/settings/state, activates changed components transactionally, verifies actual native callbacks, and records the exact Git commit. Repeating it on the same checkout reuses content-bound verification and makes no deployment changes. Updating Git alone never changes the live hooks.

Other commands: `make verify` (isolated regression/install tests), `make build` (wheel), `make status` (installed provenance), and `make rollback` (previous code/config deployment while keeping current PR state).

This release supports the captured macOS arm64 / Python 3.13 environment. On the current machine, all operational assets already match: the first installation only changes release provenance. It preserves the existing runtime, callback definitions/trust, monitor process, prompt text and global instruction contents.

The source code and prompt catalogue are byte-identical to the captured gate 1.7.0. Repository release version 0.1.0 and the captured gate's historical version 1.7.0 are recorded separately. See [the complete operational inventory](docs/operations.md), [installation behavior](docs/installation.md), and [known defects](docs/known-defects.md).
