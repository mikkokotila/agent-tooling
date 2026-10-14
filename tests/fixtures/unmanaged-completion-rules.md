## Done means the merge button is green

The Stop hook cannot force a chat to continue, so not stopping has to be my default rather than the
gate's job. Where the managed block above permits yielding while an external check is pending, these
rules say wait first and yield only after the bounded wait in rule 1. In every chat and repository:

1. **Never end a turn on pending CI.** After pushing, wait in-session — `gh pr checks <n> --watch`,
   or `caller-pr-gates validate --background` then `status --fresh` — until every check has
   concluded. "CI is running, handing back" is not a report; it is the thing that costs a day. Only
   if a check genuinely runs past twenty minutes, say what is still running, then yield.
2. **Read the reviews on the current head before stopping.** Bot reviews usually land minutes after
   the last check finishes, so look after CI concludes, not before. Fix, reply, resolve, re-verify.
3. **Done is a clear `mergeStateStatus`, not "all gates pass".** Run
   `gh pr view <n> --json mergeStateStatus,reviewDecision,mergeable` before claiming completion. No
   gate in caller-pr-gates reads either field, so passing gates say nothing about whether the PR can
   merge. BLOCKED, CHANGES_REQUESTED, BEHIND or CONFLICTING all mean not finished.
4. **Keep the head current unasked.** Re-check the base branch before declaring done and after any
   base move; a squash-merged dependency silently rebases the world. In the Claude desktop app use
   `sync_with_base_branch`; elsewhere merge the base branch yourself.
5. **A question mid-PR does not end the PR.** Answering one leaves the obligation exactly where it
   was. Answer it, then return to the PR in the same turn.
6. **Escalate a decision, never a status.** When something truly needs the user — a review dismissal
   only they can do, a credential, a product call — give one sentence: what it is, and what I will do
   the moment they answer.
