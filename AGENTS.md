# Agent tooling

Preserve original capture under capture/ and tag v0.1.0. Authorized repairs to issue #3 evolve the committed release/runtime-manifest.json; never hide new failures or weaken gates. Publish fixes before local verification. Required verification is tools/manage.py verify through the managed runner. Keep state, tokens, personal settings and chats out of Git. Independently review and verify every repair. Compatible separately reviewed repairs may share one normal transactional installation of the final verified tree when no consumer needs an intermediate release; verify actual installation and consumer use before declaring delivery.

Declare verification with argv ["python3.13", "tools/manage.py", "verify"] and installation with ["python3.13", "tools/manage.py", "install"]. Run through caller-pr-gates validate --background for owned PRs. make install uses the same implementation.

## Explicit manager appointment

When the human says **“you are manager”** (or equivalent) in this repository, read [manager/MANAGER.md](manager/MANAGER.md) and follow its activation and succession procedure. That phrase is sufficient appointment; use a session-owned ledger and the permitted manager coverage: Codex manages all GPT browser, Claude/Codex desktop, Muse and Poise agents; Claude manages Claude desktop, GPT browser through the built-in pane, and Poise; Muse manages only Muse agents. Start fresh in a new manager chat and stop/delete its manager ledger when that session ends. Reuse one heartbeat without inheriting old task history. Never create a second manager or wake an archived chat without exact human permission. Other work in this repository does not activate this role. Every manager message follows the verified-token footer rule in that file.

## Explicit Switchboard appointment

When the human says **“you are switchboard”** (or equivalent), read [switchboard/SWITCHBOARD.md](switchboard/SWITCHBOARD.md). All three host variants cover all accessible local Claude, Codex and Muse sessions. Claude/Codex also check GPT browser chats in a verified new separate browser window; Muse need not. Claude browser chats are always excluded. Observe/report and relay actual human answers once; do not adopt Manager recovery. Other repository work or installation does not activate this role or restart stopped polling.
