# Publication reconciliation

Updating a clean checkout from the target branch is not new PR work. A recorded
publication request may also outlive a branch switch after its work has shipped.

Full PR snapshots reconcile exact recorded branch refs, rather than requiring the
checkout to remain on that branch. An independently read live default-branch tip
also proves publication when it contains that exact local branch tip. This retires
only the matching clean source record; it does not mark CI, reviews or other work
complete, adopt a referenced PR, or create another commit/PR.

Stop and `status --fresh` reconcile remaining records, including when no active PR
remains. To inspect and reconcile one exact checkout explicitly:

```sh
caller-pr-gates publication-reconcile --session SESSION --cwd /absolute/repository
```

The command reads repository identity and the live default tip twice, verifies
ancestry against the recorded local branch, then compares source and ownership
again before retiring that exact durable record. Dirty source, unpublished
commits, unrelated origins/checkouts, changing target refs, missing evidence,
changed source/ownership and concurrent record updates remain protected. Errors
remain local and records stay incomplete. No direct database repair is needed.
