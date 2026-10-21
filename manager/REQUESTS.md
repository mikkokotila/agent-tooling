# Request separate work

This inbox is for the existing human-appointed local work manager recorded in `${HOME}/.codex/work-manager/state.json → manager_session`. Its role and succession procedure are in `${HOME}/.codex/work-manager/MANAGER.md`. Its existing heartbeat reads requests every 30 minutes. Writing a request does not start another polling service or worker, and an old request does not reactivate a retired or human-archived chat.

Reusable invitation: "Automated local-manager update. If you need work done in a separate repository, submit a request through `${HOME}/.codex/work-manager/requests/README.md`. It can be an existing ticket or work in any form within your authorized scope. The manager checks existing owners, arranges and supervises the work, and returns verified results to this exact chat. Continue feasible work and retain current obligations until acceptance is confirmed." Keep this invitation repository-neutral; only individual assignments name their actual target.

Any already-authorized bounded work may be requested: an existing issue or PR, code, investigation, research, design, documentation or another result, in the same repository, a different repository or without a repository. Do not create a new issue merely to submit a request.

Write a JSON object named `<UUID>.json` in this directory. Write a temporary file in this directory, set mode `0600`, then atomically rename it to the final name. Use a single stable UUID for the same request. Do not edit an accepted request into a different task; send a linked follow-up request if the scope genuinely changes.

```json
{
  "request_id": "<UUID matching the filename>",
  "requester": {
    "client": "codex",
    "session_id": "<your exact current session ID>",
    "cwd": "<your actual checkout or workspace>"
  },
  "task": "<the work to do, or an existing ticket URL with a clear request>",
  "repository": "<owner/repository, when relevant>",
  "references": ["<existing issue, PR, artifact or evidence link, when available>"],
  "acceptance": ["<the result your original task needs>"]
}
```

`request_id`, `requester` and nonempty `task` are required. `client` is `codex` or `claude`. Get your own session ID from current session/hook/transcript evidence; do not borrow a repository's other owner. `repository`, `references` and `acceptance` are optional when the task or linked ticket already establishes them. Never include credentials or private connection settings. Link evidence rather than copying a conversation.

The manager validates the requester and scope, checks existing owners and duplicate work, and records acceptance and progress in its private ledger. It reuses an appropriate owner or creates a regular ChatGPT browser chat using GPT-6, Pro, effort 5/5. It supervises the assigned chat, handles routine GitHub-plugin invocation approvals under existing access, verifies the requested result, and sends it back to the exact requesting chat.

Until the manager confirms acceptance, retain your existing PR and task obligations. After acceptance, do not duplicate the assigned work; continue feasible work on your original task. A file in this inbox is neither a completed task nor permission to drop the original task. An opened PR is not automatically a completed result. Explicit human stops and scope limits remain in force.

The manager tracks every unfinished assignment on each existing wakeup, regardless of chat activity or state. It checks browser chats it assigned; it does not adopt unrelated browser chats. If a reply is inaccessible or delivery is unverified, the work remains tracked. Results are complete only when their acceptance and delivery are verified.
